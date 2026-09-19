#!/usr/bin/env python3
"""Download daily ERA5 atmospheric forcing for Northwest Pacific models."""
from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

DATASET = "derived-era5-single-levels-daily-statistics"
CATALOGUE_URL = f"https://cds.climate.copernicus.eu/api/catalogue/v1/collections/{DATASET}"
DEFAULT_BBOX = (100.0, 180.0, 0.0, 60.0)
DEFAULT_VARIABLES = (
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "2m_temperature",
    "2m_dewpoint_temperature",
    "mean_sea_level_pressure",
    "mean_total_precipitation_rate",
    "mean_evaporation_rate",
    "mean_surface_sensible_heat_flux",
    "mean_surface_latent_heat_flux",
    "mean_surface_net_short_wave_radiation_flux",
    "mean_surface_net_long_wave_radiation_flux",
    "sea_ice_cover",
)
VALID_VARIABLES = frozenset(DEFAULT_VARIABLES)
EXPECTED_DATA_VARIABLES = {
    "10m_u_component_of_wind": "u10",
    "10m_v_component_of_wind": "v10",
    "2m_temperature": "t2m",
    "2m_dewpoint_temperature": "d2m",
    "mean_sea_level_pressure": "msl",
    "mean_total_precipitation_rate": "avg_tprate",
    "mean_evaporation_rate": "avg_ie",
    "mean_surface_sensible_heat_flux": "avg_ishf",
    "mean_surface_latent_heat_flux": "avg_slhtf",
    "mean_surface_net_short_wave_radiation_flux": "avg_snswrf",
    "mean_surface_net_long_wave_radiation_flux": "avg_snlwrf",
    "sea_ice_cover": "siconc",
}
VALID_TIME_ZONES = tuple(
    [f"utc-{hour:02d}:00" for hour in range(12, 0, -1)]
    + [f"utc+{hour:02d}:00" for hour in range(15)]
)
REQUEST_ATTR = "era5_daily_forcing_request"
LOG = logging.getLogger("era5_forcing")


class DownloadError(ValueError):
    """Locally generated error whose text is safe to display."""


@dataclass(frozen=True)
class Job:
    start: date
    end: date


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parse_year(value: str) -> tuple[int, int]:
    parts = value.split("-")
    if len(parts) not in (1, 2) or any(
        len(part) != 4 or not part.isdigit() for part in parts
    ):
        raise argparse.ArgumentTypeError("year must be YYYY or YYYY-YYYY")
    first = int(parts[0])
    last = int(parts[-1])
    if first < 1940 or last < first:
        raise argparse.ArgumentTypeError("year range must start at 1940 or later")
    return first, last


def next_month(value: date) -> date:
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def build_jobs(start: date, end: date) -> list[Job]:
    jobs: list[Job] = []
    cursor = start
    while cursor <= end:
        month_end = next_month(date(cursor.year, cursor.month, 1)) - timedelta(days=1)
        job_end = min(month_end, end)
        jobs.append(Job(cursor, job_end))
        cursor = job_end + timedelta(days=1)
    return jobs


def expected_days(job: Job) -> int:
    return (job.end - job.start).days + 1


def conservative_final_end(today: date | None = None) -> date:
    today = today or datetime.now(timezone.utc).date()
    month_index = today.year * 12 + today.month - 1 - 3
    return date(month_index // 12, month_index % 12 + 1, 1) - timedelta(days=1)


def build_request(args: argparse.Namespace, job: Job) -> dict:
    west, east, south, north = args.bbox
    return {
        "product_type": "reanalysis",
        "variable": args.variables,
        "year": str(job.start.year),
        "month": [f"{job.start.month:02d}"],
        "day": [f"{day:02d}" for day in range(job.start.day, job.end.day + 1)],
        "daily_statistic": "daily_mean",
        "time_zone": args.time_zone,
        "frequency": args.frequency,
        "area": [north, west, south, east],
    }


def request_signature(args: argparse.Namespace, job: Job) -> str:
    return json.dumps(
        {"dataset": DATASET, "request": build_request(args, job)},
        sort_keys=True,
        separators=(",", ":"),
    )


def output_path(output_dir: Path, job: Job, variable_count: int) -> Path:
    suffix = ".nc" if variable_count == 1 else ".zip"
    return output_dir / f"ERA5_daily_forcing_{job.start}_{job.end}{suffix}"


def failure_details(exc: Exception) -> tuple[str, bool]:
    """Return a credential-safe diagnosis and whether retrying may help."""
    if isinstance(exc, DownloadError):
        return str(exc), False
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return f"HTTP {status}: check CDS credentials and dataset terms", False
        if status in (408, 425, 429, 500, 502, 503, 504):
            return f"HTTP {status}: temporary CDS service or rate-limit failure", True
        return f"HTTP {status}: CDS rejected the request", False
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS failure; check proxy certificates and system clock", True
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy connection failed; check HTTP_PROXY/HTTPS_PROXY", True
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return "network request timed out", True
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            ConnectionError,
        ),
    ):
        return "network connection interrupted", True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (32, 33):
        return "local output file is temporarily locked (WinError 32/33)", True
    if isinstance(exc, OSError) and exc.errno in (
        errno.ENOSPC,
        errno.EACCES,
        errno.EROFS,
        errno.ENOENT,
    ):
        code = errno.errorcode.get(exc.errno, "OSERROR")
        return f"local filesystem failure ({code})", False
    return f"{type(exc).__name__}: CDS client or local processing failure", False


def inspect_catalogue(retries: int, timeout: float) -> tuple[date, date, str]:
    for attempt in range(1, retries + 1):
        try:
            with requests.get(CATALOGUE_URL, timeout=timeout) as response:
                response.raise_for_status()
                metadata = response.json()
            interval = metadata["extent"]["temporal"]["interval"][0]
            first = date.fromisoformat(interval[0][:10])
            last = date.fromisoformat(interval[1][:10])
            return first, last, str(metadata.get("updated", "unknown"))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise DownloadError(
                f"invalid CDS catalogue metadata ({type(exc).__name__})"
            ) from None
        except requests.RequestException as exc:
            detail, retryable = failure_details(exc)
            if not retryable or attempt == retries:
                raise DownloadError(
                    f"cannot read CDS catalogue metadata: {detail}"
                ) from None
            delay = min(10, 2 ** (attempt - 1))
            LOG.warning(
                "Catalogue attempt %d/%d failed (%s); retrying in %ds",
                attempt,
                retries,
                detail,
                delay,
            )
            time.sleep(delay)


def _axis_values(dataset, names: tuple[str, ...], path: Path) -> tuple[str, list[float]]:
    for name in names:
        if name in dataset.variables:
            variable = dataset.variables[name]
            if variable.ndim != 1 or variable.dimensions != (name,):
                raise DownloadError(f"{path.name}: invalid {name} coordinate")
            try:
                return name, [float(value) for value in variable[:]]
            except (TypeError, ValueError, OverflowError):
                raise DownloadError(f"{path.name}: unreadable {name} coordinate") from None
    raise DownloadError(f"{path.name}: missing {names[0]} coordinate")


def _validate_axis(
    path: Path, axis: str, values: list[float], lower: float, upper: float
) -> None:
    expected = [
        lower + index * 0.25 for index in range(round((upper - lower) * 4) + 1)
    ]
    if axis == "longitude":
        values = [
            value
            if -180 <= value <= 180
            else ((value + 180) % 360) - 180
            for value in values
        ]
    actual = sorted(values)
    if len(actual) != len(expected) or any(
        not math.isclose(got, wanted, abs_tol=1e-6)
        for got, wanted in zip(actual, expected)
    ):
        raise DownloadError(
            f"{path.name}: unexpected {axis} grid for requested bbox"
        )
    if len(values) > 1 and not (
        all(a < b for a, b in zip(values, values[1:]))
        or all(a > b for a, b in zip(values, values[1:]))
    ):
        raise DownloadError(f"{path.name}: unordered or duplicated {axis} coordinate")


def _netcdf_contents(
    path: Path, bbox: tuple[float, float, float, float]
) -> tuple[tuple[date, ...], set[str]]:
    from netCDF4 import Dataset, num2date

    with Dataset(path) as dataset:
        latitude, latitude_values = _axis_values(dataset, ("latitude", "lat"), path)
        longitude, longitude_values = _axis_values(dataset, ("longitude", "lon"), path)
        west, east, south, north = bbox
        _validate_axis(path, "latitude", latitude_values, south, north)
        _validate_axis(path, "longitude", longitude_values, west, east)
        for name in ("valid_time", "time"):
            if name not in dataset.variables:
                continue
            variable = dataset.variables[name]
            if variable.ndim != 1:
                raise DownloadError(f"{path.name}: {name} is not one-dimensional")
            try:
                decoded = num2date(
                    variable[:],
                    units=variable.units,
                    calendar=getattr(variable, "calendar", "standard"),
                )
                dates = tuple(date(item.year, item.month, item.day) for item in decoded)
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise DownloadError(
                    f"{path.name}: cannot decode {name} coordinate"
                ) from None
            data_variables = {
                variable_name
                for variable_name, candidate in dataset.variables.items()
                if variable_name != name
                and name in candidate.dimensions
                and latitude in candidate.dimensions
                and longitude in candidate.dimensions
            }
            if not data_variables:
                raise DownloadError(f"{path.name}: no time-dependent data variables")
            return dates, data_variables
    raise DownloadError(f"{path.name}: no time or valid_time coordinate")


def _validate_netcdf(
    path: Path,
    job: Job,
    bbox: tuple[float, float, float, float],
) -> set[str]:
    wanted = tuple(job.start + timedelta(days=index) for index in range(expected_days(job)))
    actual, data_variables = _netcdf_contents(path, bbox)
    if actual != wanted:
        actual_range = f"{actual[0]}..{actual[-1]}" if actual else "empty"
        raise DownloadError(
            f"{path.name}: expected {wanted[0]}..{wanted[-1]} ({len(wanted)} days), "
            f"found {actual_range} ({len(actual)} records)"
        )
    return data_variables


def validate_download(
    path: Path,
    job: Job,
    requested_variables: list[str],
    bbox: tuple[float, float, float, float],
) -> None:
    expected_variables = {EXPECTED_DATA_VARIABLES[name] for name in requested_variables}
    if not path.is_file() or path.stat().st_size == 0:
        raise DownloadError(f"empty or missing download: {path}")
    if len(requested_variables) == 1:
        if zipfile.is_zipfile(path):
            raise DownloadError("CDS returned an unexpected ZIP for one variable")
        data_variables = _validate_netcdf(path, job, bbox)
        if data_variables != expected_variables:
            raise DownloadError(
                f"CDS NetCDF variables {sorted(data_variables)} do not match "
                f"requested variables {sorted(expected_variables)}"
            )
        return
    if not zipfile.is_zipfile(path):
        raise DownloadError("CDS response is not the expected multi-variable ZIP archive")

    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise DownloadError(f"corrupt ZIP member: {bad_member}")
        members = [name for name in archive.namelist() if name.lower().endswith(".nc")]
        if not members:
            raise DownloadError("CDS archive contains no NetCDF files")
        data_variables: set[str] = set()
        with tempfile.TemporaryDirectory(prefix="era5_validate_") as temp_dir:
            root = Path(temp_dir).resolve()
            for member in members:
                member_path = (root / member).resolve()
                if root not in member_path.parents:
                    raise DownloadError(f"unsafe archive member path: {member}")
                archive.extract(member, root)
                data_variables.update(_validate_netcdf(member_path, job, bbox))
        missing = expected_variables - data_variables
        if missing:
            raise DownloadError(
                "CDS archive is missing requested data variables: "
                f"{', '.join(sorted(missing))}; found: "
                f"{', '.join(sorted(data_variables))}"
            )


def stamp_archive(path: Path, signature: str) -> None:
    sidecar = path.with_suffix(path.suffix + ".json")
    temporary = sidecar.with_suffix(sidecar.suffix + ".part")
    temporary.write_text(
        json.dumps({REQUEST_ATTR: signature}, indent=2) + "\n", encoding="ascii"
    )
    os.replace(temporary, sidecar)


def existing_is_valid(
    path: Path,
    job: Job,
    signature: str,
    requested_variables: list[str],
    bbox: tuple[float, float, float, float],
) -> bool:
    sidecar = path.with_suffix(path.suffix + ".json")
    try:
        metadata = json.loads(sidecar.read_text(encoding="ascii"))
        if metadata.get(REQUEST_ATTR) != signature:
            return False
        validate_download(path, job, requested_variables, bbox)
        return True
    except Exception:
        return False


def download_job(client, args: argparse.Namespace, job: Job, target: Path) -> None:
    signature = request_signature(args, job)
    sidecar = target.with_suffix(target.suffix + ".json")
    if not args.overwrite:
        if existing_is_valid(
            target, job, signature, args.variables, tuple(args.bbox)
        ):
            LOG.info("Skip verified %s", target.name)
            return
        if target.exists() or sidecar.exists():
            raise DownloadError(
                f"existing output is incomplete or belongs to another request: {target}; "
                "use --overwrite or another output directory"
            )

    partial = target.with_suffix(target.suffix + ".part")
    try:
        partial.unlink(missing_ok=True)
    except OSError as exc:
        detail, _ = failure_details(exc)
        raise DownloadError(f"cannot remove stale partial {partial} ({detail})") from None

    request = build_request(args, job)
    for attempt in range(1, args.retries + 1):
        try:
            LOG.info(
                "Download %s through %s (attempt %d/%d)",
                job.start,
                job.end,
                attempt,
                args.retries,
            )
            client.retrieve(DATASET, request, str(partial))
            validate_download(partial, job, args.variables, tuple(args.bbox))
            os.replace(partial, target)
            stamp_archive(target, signature)
            return
        except Exception as exc:
            cleanup_error = None
            try:
                partial.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_error, _ = failure_details(cleanup_exc)
            detail, retryable = failure_details(exc)
            if cleanup_error:
                raise DownloadError(
                    f"{job.start}..{job.end} failed ({detail}); "
                    f"partial cleanup also failed ({cleanup_error})"
                ) from None
            if not retryable or attempt == args.retries:
                raise DownloadError(
                    f"{job.start}..{job.end} failed ({detail}); "
                    "completed archives were retained"
                ) from None
            delay = min(60, 5 * 2 ** (attempt - 1))
            LOG.warning("Attempt failed (%s); retrying in %ds", detail, delay)
            time.sleep(delay)


def status_callback(message, *args, **kwargs) -> None:
    """Log task states without exposing upstream URLs or credentials."""
    text = str(message)
    if args:
        try:
            text = text % args
        except (TypeError, ValueError):
            return
    found = re.search(
        r"(?:status|state|request is).*?\b(accepted|queued|running|successful|completed|failed)\b",
        text,
        re.IGNORECASE,
    )
    if found:
        LOG.info("CDS task state: %s", found[1].lower())


def suppressed_callback(*args, **kwargs) -> None:
    pass


def selected_period(args: argparse.Namespace) -> tuple[date, date]:
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise DownloadError("--start-date and --end-date must be used together")
        if args.years is not None:
            raise DownloadError("use either a year range or explicit dates, not both")
        start, end = args.start_date, args.end_date
    elif args.years is not None:
        first, last = args.years
        start = date(first, 1, 1)
        end = date(last, 12, 31)
    else:
        raise DownloadError("provide a year/range or --start-date and --end-date")
    if end < start:
        raise DownloadError("end date must not precede start date")
    return start, end


def validate_args(args: argparse.Namespace) -> None:
    west, east, south, north = args.bbox
    if not all(math.isfinite(value) for value in args.bbox):
        raise DownloadError("bbox values must be finite")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise DownloadError(
            "bbox must satisfy -180 <= W < E <= 180 and -90 <= S < N <= 90"
        )
    if any(abs(value * 4 - round(value * 4)) > 1e-8 for value in args.bbox):
        raise DownloadError("bbox boundaries must align to the ERA5 0.25-degree grid")
    unknown = sorted(set(args.variables) - VALID_VARIABLES)
    if unknown:
        raise DownloadError(f"unsupported variables: {', '.join(unknown)}")
    if len(set(args.variables)) != len(args.variables):
        raise DownloadError("--variables must not contain duplicates")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Download official daily-mean ERA5 forcing as monthly CDS files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    result.add_argument("years", nargs="?", type=parse_year, metavar="YEAR_OR_RANGE")
    result.add_argument("--start-date", type=parse_date)
    result.add_argument("--end-date", type=parse_date)
    result.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        default=DEFAULT_BBOX,
        metavar=("W", "E", "S", "N"),
    )
    result.add_argument(
        "--variables",
        nargs="+",
        default=list(DEFAULT_VARIABLES),
        choices=sorted(VALID_VARIABLES),
    )
    result.add_argument(
        "--frequency",
        choices=("1_hourly", "3_hourly", "6_hourly"),
        default="1_hourly",
    )
    result.add_argument(
        "--time-zone",
        choices=VALID_TIME_ZONES,
        default="utc+00:00",
        help="CDS daily boundary, for example utc+00:00 or utc+08:00",
    )
    result.add_argument(
        "-o", "--output-dir", type=Path, default=Path("data/era5_daily_forcing")
    )
    result.add_argument("--retries", type=int, default=4)
    result.add_argument(
        "--timeout", type=float, default=30.0, help="catalogue request timeout in seconds"
    )
    result.add_argument("--overwrite", action="store_true")
    result.add_argument("-n", "--dry-run", action="store_true")
    return result


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("multiurl").setLevel(logging.WARNING)
    try:
        validate_args(args)
        if args.retries < 1:
            raise DownloadError("--retries must be >= 1")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise DownloadError("--timeout must be finite and > 0")
        start, end = selected_period(args)
        try:
            available_start, available_end, updated = inspect_catalogue(
                args.retries, args.timeout
            )
        except DownloadError as exc:
            available_start = date(1940, 1, 1)
            available_end = date.today()
            updated = "unavailable"
            LOG.warning("%s; continuing without catalogue preflight", exc)
        available_end = min(available_end, conservative_final_end())
        if start < available_start or end > available_end:
            raise DownloadError(
                f"requested {start}..{end}, but conservative final-ERA5 coverage is "
                f"{available_start}..{available_end} (catalogue updated {updated})"
            )
        jobs = build_jobs(start, end)
        west, east, south, north = args.bbox
        cells = (round((east - west) * 4) + 1) * (round((north - south) * 4) + 1)
        days = (end - start).days + 1
        raw_gib = days * cells * len(args.variables) * 4 / 1024**3
        logging.info(
            "Conservative final-ERA5 coverage %s..%s; catalogue updated %s",
            available_start,
            available_end,
            updated,
        )
        logging.info(
            "Selected %s..%s: %d monthly jobs, %d variables",
            start,
            end,
            len(jobs),
            len(args.variables),
        )
        logging.info(
            "Approximate float32 payload before NetCDF compression: %.1f GiB", raw_gib
        )
        logging.info(
            "BBox W/E/S/N=%s; daily boundary=%s; source sampling=%s",
            args.bbox,
            args.time_zone,
            args.frequency,
        )
        logging.info(
            "First request: %s",
            json.dumps(build_request(args, jobs[0]), ensure_ascii=True),
        )
        if args.dry_run:
            logging.info("Dry-run: no CDS job submitted")
            return 0

        try:
            import cdsapi
            import netCDF4  # noqa: F401
        except ImportError as exc:
            raise DownloadError(
                f"missing dependency {exc.name}; run python -m pip install -r requirements.txt"
            ) from None

        args.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            client = cdsapi.Client(
                timeout=600,
                quiet=True,
                debug=False,
                progress=False,
                retry_max=1,
                sleep_max=10,
                info_callback=status_callback,
                warning_callback=suppressed_callback,
                error_callback=suppressed_callback,
                debug_callback=suppressed_callback,
            )
        except Exception:
            raise DownloadError(
                "CDS authentication unavailable; configure ~/.cdsapirc and "
                "accept the dataset terms"
            ) from None
        for job in jobs:
            download_job(
                client,
                args,
                job,
                output_path(args.output_dir, job, len(args.variables)),
            )
        LOG.info(
            "Completed %d monthly files in %s",
            len(jobs),
            args.output_dir.resolve(),
        )
        return 0
    except (DownloadError, OSError) as exc:
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logging.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
