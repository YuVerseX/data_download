#!/usr/bin/env python3
"""Download regional ERA5 single-level data by calendar month."""
from __future__ import annotations

import argparse
import calendar
import contextlib
import json
import logging
import math
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HOURLY = "reanalysis-era5-single-levels"
CDS_DAILY = "derived-era5-single-levels-daily-statistics"
CATALOGUE = "https://cds.climate.copernicus.eu/api/catalogue/v1/collections/"
LEGACY_FLUX_REFERENCE = "https://confluence.ecmwf.int/plugins/viewsource/viewpagesrc.action?pageId=462888259"
NWP = (100., 180., 0., 60.)
SCS = (105., 125., 0., 25.)
HEAT = {
    "sshf": "surface_sensible_heat_flux", "slhf": "surface_latent_heat_flux",
    "ssr": "surface_net_solar_radiation", "str": "surface_net_thermal_radiation",
}
STRESS = {"ewss": "eastward_turbulent_surface_stress", "nsss": "northward_turbulent_surface_stress"}
HOUR_VARS = {
    "u10": "10m_u_component_of_wind", "v10": "10m_v_component_of_wind",
    "msl": "mean_sea_level_pressure", **HEAT, **STRESS,
}
DAILY_VARS = {
    HOUR_VARS["u10"]: ("u10",),
    HOUR_VARS["v10"]: ("v10",),
    "2m_temperature": ("t2m",),
    "2m_dewpoint_temperature": ("d2m",),
    HOUR_VARS["msl"]: ("msl",),
    "mean_total_precipitation_rate": ("avg_tprate", "mtpr"),
    "mean_evaporation_rate": ("avg_ie", "mer"),
    "mean_surface_sensible_heat_flux": ("avg_ishf", "msshf"),
    "mean_surface_latent_heat_flux": ("avg_slhtf", "mslhf"),
    "mean_surface_net_short_wave_radiation_flux": ("avg_snswrf", "msnswrf"),
    "mean_surface_net_long_wave_radiation_flux": ("avg_snlwrf", "msnlwrf"),
    "sea_ice_cover": ("siconc",),
}
DAILY_PROCESSING = "sum 24 hourly accumulations D01..D+1 00 UTC / 86400 s; NaN propagates; no differencing"
FLUX_ATTR = "era5_flux_request"
DAILY_ATTR = "era5_daily_forcing_request"
LOG = logging.getLogger("era5")


class DownloadError(ValueError):
    pass


def parse_years(text):
    years = set()
    for part in text.split(","):
        match = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{4}))?\s*", part)
        if not match:
            raise argparse.ArgumentTypeError("Use YYYY, YYYY-YYYY or YYYY,YYYY")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1940 <= first <= last <= 9998:
            raise argparse.ArgumentTypeError("Years must be ordered and >= 1940")
        years.update(range(first, last + 1))
    return sorted(years)


def month_chunks(first, last):
    while first <= last:
        end = min(last, date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]))
        yield first, end
        first = end + timedelta(days=1)


def failure_details(exc):
    if isinstance(exc, DownloadError):
        return str(exc), False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return f"HTTP {status}: check credentials and dataset terms", False
    if isinstance(status, int):
        return f"HTTP {status}", status in (408, 425, 429, 500, 502, 503, 504)
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS certificate failure", False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError, TimeoutError, ConnectionError)):
        return f"{type(exc).__name__}: network request failed", True
    if isinstance(exc, OSError):
        return f"{type(exc).__name__} (errno={exc.errno}): check disk space and output path", False
    return f"{type(exc).__name__}: check CDS request status or local file format", False


def retry(operation, attempts):
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            detail, transient = failure_details(exc)
            if not transient or attempt == attempts:
                raise DownloadError(detail) from None
            delay = min(30, 2 ** attempt)
            LOG.warning("%s. Retry %d/%d in %ds", detail, attempt + 1, attempts, delay)
            time.sleep(delay)


def make_client(timeout):
    import cdsapi

    def log_status(message, *args, **kwargs):
        text = str(message)
        if args:
            try:
                text %= args
            except (TypeError, ValueError):
                return
        state = re.search(r"(?:status|state|request is).*?\b(accepted|queued|running|successful|completed|failed)\b", text, re.I)
        if state:
            LOG.info("CDS task: %s", state[1])

    def quiet(*args, **kwargs):
        pass

    try:
        return cdsapi.Client(quiet=True, debug=False, progress=False, timeout=timeout,
                             retry_max=1, info_callback=log_status, warning_callback=quiet,
                             error_callback=quiet, debug_callback=quiet)
    except Exception:
        raise DownloadError("Configure ~/.cdsapirc and accept the CDS dataset terms") from None


def select_periods(args, dataset):
    today = datetime.now(timezone.utc).date()
    month = today.year * 12 + today.month - 4
    final_end = date(month // 12, month % 12 + 1, 1) - timedelta(days=1)

    def discover():
        with requests.get(CATALOGUE + dataset, timeout=args.timeout) as response:
            response.raise_for_status()
            first, last = response.json()["extent"]["temporal"]["interval"][0]
        return date.fromisoformat(first[:10]), date.fromisoformat(last[:10])

    if args.start_date or args.end_date:
        if args.years or not (args.start_date and args.end_date):
            raise DownloadError("Use both --start-date and --end-date, or positional years")
        selected = [(args.start_date, args.end_date)]
    elif args.years:
        selected = [(date(year, 1, 1), date(year, 12, 31)) for year in args.years]
    elif args.mode == "flux":
        selected = []
    else:
        raise DownloadError("Specify years or --start-date and --end-date")

    available_start, available_end = date(1940, 1, 1), final_end
    try:
        available_start, available_end = retry(discover, args.retries)
    except DownloadError as exc:
        if args.mode == "flux":
            raise
        LOG.warning("Catalogue unavailable (%s). Using the conservative date cutoff", exc)
    limit = min(final_end, available_end) - timedelta(days=int(args.daily_mean))
    if not selected:
        latest = limit.year if (limit.month, limit.day) == (12, 31) else limit.year - 1
        selected = [(date(year, 1, 1), date(year, 12, 31))
                    for year in range(max(2001, available_start.year), latest + 1)]
    if not selected or any(not available_start <= start <= end <= limit for start, end in selected):
        raise DownloadError(f"Select dates within {available_start}..{limit}")

    merged = []
    for first, last in selected:
        if merged and first == merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], last)
        else:
            merged.append((first, last))
    return merged


def build_request(args, first, last):
    west, east, south, north = args.bbox
    request = {
        "product_type": "reanalysis", "year": str(first.year), "month": [f"{first.month:02d}"],
        "day": [f"{day:02d}" for day in range(first.day, last.day + 1)],
        "area": [north, west, south, east],
    }
    if args.mode == "cds-daily":
        request.update(variable=args.variables, daily_statistic="daily_mean",
                       time_zone=args.time_zone, frequency=args.frequency)
    else:
        request.update(variable=[HOUR_VARS[name] for name in args.variables],
                       time=[f"{hour:02d}:00" for hour in range(24)],
                       data_format="netcdf", download_format="unarchived")
    return request


def signature(dataset, request):
    return json.dumps({"dataset": dataset, "request": request}, sort_keys=True, separators=(",", ":"))


def flux_signature(first, last, bbox, kind):
    return json.dumps(dict(dataset=HOURLY, product_type="reanalysis", grid=0.25,
                           first=str(first), last=str(last), bbox=list(bbox),
                           variables=list(HEAT), kind=kind,
                           processing_version=2 if kind == "daily" else 1), sort_keys=True)


def expected_times(first, last, unit):
    import numpy as np

    return np.arange(first.isoformat(), (last + timedelta(days=1)).isoformat(), dtype=f"datetime64[{unit}]")


def validate(data, first, last, bbox, unit):
    """Check time and grid and normalize coordinate names for local NetCDF output."""
    import numpy as np

    aliases = {old: new for old, new in (("valid_time", "time"), ("lat", "latitude"), ("lon", "longitude"))
               if old in data.dims and new not in data.dims}
    data = data.rename(aliases)
    if unit == "h" and "expver" in data:
        try:
            final = bool(np.all(data.expver.values.astype(float) == 1))
        except (TypeError, ValueError):
            final = False
        if not final:
            raise DownloadError("ERA5T or mixed expver in hourly data")
        if "expver" in data.dims:
            data = data.isel(expver=0, drop=True)
        data = data.drop_vars("expver", errors="ignore")
    if "time" not in data.coords or data.time.dims != ("time",):
        raise DownloadError("Missing time coordinate")
    times = data.time.values
    if times.dtype.kind != "M":
        raise DownloadError("Cannot decode the time coordinate")
    expected = expected_times(first, last, "D" if unit == "day" else unit)
    if unit == "day":
        times = times.astype("datetime64[D]")
    if not np.array_equal(times, expected):
        raise DownloadError(f"Incomplete or unexpected time axis for {first}..{last}")
    west, east, south, north = bbox
    for name, low, high in (("longitude", west, east), ("latitude", south, north)):
        if name not in data.coords or data[name].dims != (name,):
            raise DownloadError(f"Missing {name} coordinate")
        axis = data[name]
        values = axis.values
        if name == "longitude":
            values = (values - low) % 360 + low
            if high - low == 360:
                high -= 0.25  # One point at the seam of a global grid.
        expected = np.arange(low, high + 0.125, 0.25)
        if not np.array_equal(np.sort(values), expected):
            raise DownloadError(f"Unexpected {name} grid for bbox {bbox}")
        if not np.array_equal(values, axis.values):
            data = data.assign_coords({name: (name, values, axis.attrs)}).sortby(name)
        elif not (np.all(np.diff(values) > 0) or np.all(np.diff(values) < 0)):
            raise DownloadError(f"Unordered {name} coordinate")
    return data


def check_hourly_units(data, names):
    units = {
        "u10": {"ms-1", "m/s"}, "v10": {"ms-1", "m/s"}, "msl": {"pa"},
        **{name: {"jm-2", "j/m2"} for name in HEAT},
        **{name: {"nm-2s", "ns/m2"} for name in STRESS},
    }
    for name in names:
        if name not in data or set(data[name].dims) != {"time", "latitude", "longitude"}:
            raise DownloadError(f"Missing variable or unexpected dimensions: {name}")
        source = str(data[name].attrs.get("units", ""))
        if re.sub(r"[\s*^]", "", source).lower() not in units[name]:
            raise DownloadError(f"Unexpected hourly units for {name}: {source}")


@contextlib.contextmanager
def open_payload(payload, first, last, bbox, variables, mode, temp):
    """Read either a bare NetCDF or NetCDF members of a CDS ZIP response."""
    import xarray as xr

    expected = {name: DAILY_VARS[name] if mode == "cds-daily" else (name,) for name in variables}
    with contextlib.ExitStack() as stack:
        members = [payload]
        if zipfile.is_zipfile(payload):
            with zipfile.ZipFile(payload) as archive:
                entries = [item for item in archive.infolist() if item.filename.lower().endswith(".nc")]
                members = []
                for index, entry in enumerate(entries):
                    member = temp / f"member-{index}.nc"
                    with archive.open(entry) as source, member.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    members.append(member)
        found = set()
        parts = []
        for member in members:
            data = stack.enter_context(xr.open_dataset(member, decode_timedelta=False,
                                                       chunks=None if mode == "cds-daily" else "auto"))
            data = validate(data, first, last, bbox, "day" if mode == "cds-daily" else "h")
            names = [name for name in data.data_vars
                     if set(data[name].dims) == {"time", "latitude", "longitude"}]
            matched = {key for key, aliases in expected.items() if any(name in aliases for name in names)}
            if found & matched:
                raise DownloadError("Duplicate variables in CDS response")
            found.update(matched)
            if mode != "cds-daily":
                keep = [name for name in names if name in expected]
                check_hourly_units(data, keep)
                parts.append(data[keep])
        if found != expected.keys():
            raise DownloadError(f"CDS response lacks: {', '.join(sorted(expected.keys() - found))}")
        yield xr.merge(parts, join="exact") if mode != "cds-daily" else None


def download_month(client, args, dataset, first, last):
    import xarray as xr

    request = build_request(args, first, last)
    output = args.output_dir
    is_daily = args.mode == "cds-daily"
    attr = FLUX_ATTR if args.mode == "flux" else "era5_request"
    identity = (flux_signature(first, last, args.bbox, "hourly") if args.mode == "flux"
                else signature(dataset, request))
    stem = {"hourly": "era5_hourly", "flux": "era5_flux_hourly", "cds-daily": "ERA5_daily_forcing"}[args.mode]
    target = output / f"{stem}_{first}_{last}.nc"
    if is_daily and len(args.variables) > 1:
        target = target.with_suffix(".zip")
    sidecar = target.with_suffix(target.suffix + ".json")

    with tempfile.TemporaryDirectory(prefix=".era5-", dir=output) as folder:
        temp = Path(folder)
        if (target.exists() or (is_daily and sidecar.exists())) and not args.overwrite:
            if is_daily:
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    matches = meta.get(DAILY_ATTR) == identity
                except (OSError, ValueError, AttributeError):
                    matches = False
                if matches:
                    with open_payload(target, first, last, args.bbox, args.variables, args.mode, temp):
                        pass
            else:
                with xr.open_dataset(target, decode_timedelta=False) as ds:
                    matches = ds.attrs.get(attr) == identity
                    if matches:
                        ds = validate(ds, first, last, args.bbox, "h")
                        check_hourly_units(ds, args.variables)
            if not matches:
                hint = " or pass --overwrite" if args.mode != "flux" else ""
                raise DownloadError(f"Existing file or request metadata differs: {target}. Use another output directory{hint}")
            LOG.info("Skip %s", target.name)
            return target

        payload = temp / "download.part"

        def retrieve():
            payload.unlink(missing_ok=True)
            return client.retrieve(dataset, request, str(payload))

        LOG.info("Request %s..%s", first, last)
        retry(retrieve, args.retries)
        with open_payload(payload, first, last, args.bbox, args.variables, args.mode, temp) as data:
            if not is_daily:
                staged = temp / "output.nc"
                data.attrs.update({attr: identity, "source": CATALOGUE + dataset})
                data.to_netcdf(staged, engine="netcdf4", encoding={
                    name: {"zlib": True, "complevel": 1} for name in args.variables
                })
            elif target.suffix == ".zip" and not zipfile.is_zipfile(payload):
                staged = temp / "output.zip"
                with zipfile.ZipFile(staged, "w") as archive:
                    archive.write(payload, "data.nc")
            elif target.suffix == ".nc" and zipfile.is_zipfile(payload):
                staged = temp / "member-0.nc"
                if (temp / "member-1.nc").exists():
                    raise DownloadError("Single-variable response contains multiple NetCDF files")
            else:
                staged = payload
        if is_daily:
            meta = temp / "request.json"
            meta.write_text(json.dumps({DAILY_ATTR: identity}, indent=2) + "\n", encoding="utf-8")
        staged.replace(target)
        if is_daily:
            meta.replace(sidecar)
        LOG.info("Saved %s", target.name)
        return target


def aggregate_flux(paths, output, first, last, bbox):
    """Preserve the existing --daily-mean output of download_era5_flux.py."""
    import numpy as np
    import xarray as xr

    target = output / f"era5_flux_daily_{first}_{last}.nc"
    identity = flux_signature(first, last, bbox, "daily")
    if target.exists():
        with xr.open_dataset(target, decode_timedelta=False) as data:
            if data.attrs.get(FLUX_ATTR) != identity:
                raise DownloadError(f"Existing daily flux differs: {target}")
            validate(data, first, last, bbox, "D")
            days = expected_times(first, last, "D")
            bounds = np.column_stack((days, days + np.timedelta64(1, "D")))
            if ("time_bounds" not in data or not np.array_equal(data.time_bounds.values, bounds)
                    or any(name not in data or data[name].attrs.get("units") not in ("W m-2", "W m**-2", "W/m^2")
                           for name in HEAT)):
                raise DownloadError(f"Existing daily flux has invalid time bounds or units: {target}")
        LOG.info("Skip %s", target.name)
        return

    days = expected_times(first, last, "D")
    hours = days[0] + np.arange(1, len(days) * 24 + 1).astype("timedelta64[h]")
    with contextlib.ExitStack() as stack:
        pieces = [stack.enter_context(xr.open_dataset(path, chunks="auto", decode_timedelta=False))[list(HEAT)]
                  .sel(time=slice(hours[0], hours[-1]))
                  for start, end, path in paths if end >= first and start <= last + timedelta(days=1)]
        source = xr.concat(pieces, dim="time", join="exact")
        if not np.array_equal(source.time.values, hours):
            raise DownloadError(f"Daily heat flux needs all hours from {hours[0]} through {hours[-1]}")
        daily = source.assign_coords(time=np.repeat(days, 24)).groupby("time").sum(skipna=False) / 86400.
        for name, long_name in HEAT.items():
            daily[name].attrs = {"units": "W m-2", "positive": "down", "cell_methods": "time: mean (interval: 1 day)",
                                 "long_name": "UTC daily mean " + long_name.replace("_", " ")}
        daily["time_bounds"] = (("time", "bounds"), np.column_stack((days, days + np.timedelta64(1, "D"))))
        daily.time.attrs["bounds"] = "time_bounds"
        for name in ("time", "time_bounds"):
            daily[name].encoding["units"] = "days since 1970-01-01"
        daily.attrs.update({FLUX_ATTR: identity, "processing": DAILY_PROCESSING,
                            "time_reference": "UTC day beginning at coordinate; mean over [D00,D+1 00)",
                            "processing_reference": LEGACY_FLUX_REFERENCE, "source": CATALOGUE + HOURLY})
        with tempfile.TemporaryDirectory(prefix=".era5-", dir=output) as folder:
            path = Path(folder) / "daily.nc"
            daily.to_netcdf(path, engine="netcdf4")
            path.replace(target)
    LOG.info("Saved %s", target.name)


def parser(mode=None):
    result = argparse.ArgumentParser(description=__doc__)

    def options(p, name):
        p.set_defaults(mode=name, daily_mean=False, overwrite=False)
        p.add_argument("years", nargs="?", type=parse_years, metavar="YEAR_OR_RANGE")
        p.add_argument("--start-date", type=date.fromisoformat)
        p.add_argument("--end-date", type=date.fromisoformat)
        p.add_argument("--bbox", nargs=4, type=float, default=SCS if name == "flux" else NWP,
                       metavar=("W", "E", "S", "N"))
        default_dir = {"flux": "era5_flux", "hourly": "data/era5_hourly", "cds-daily": "data/era5_daily_forcing"}
        p.add_argument("-o", "--output-dir", type=Path, default=Path(default_dir[name]))
        p.add_argument("-n", "--dry-run", action="store_true", help="Show request plan without submitting a CDS job")
        p.add_argument("--retries", type=int, default=4 if name == "cds-daily" else 3)
        p.add_argument("--timeout", type=float, default=30. if name == "cds-daily" else 60.,
                       help="Catalogue timeout in seconds" if name == "cds-daily" else "HTTP timeout in seconds")
        if name == "cds-daily":
            zones = [f"utc{hour:+03d}:00" for hour in range(-12, 15)]
            p.add_argument("--time-zone", choices=zones, default="utc+00:00")
            p.add_argument("--frequency", choices=("1_hourly", "3_hourly", "6_hourly"), default="1_hourly")
            p.add_argument("-v", "--variables", nargs="+", choices=DAILY_VARS, default=list(DAILY_VARS))
        elif name == "hourly":
            p.add_argument("-v", "--variables", nargs="+", choices=HOUR_VARS,
                           default=["u10", "v10", "msl", *HEAT])
        else:
            p.set_defaults(variables=list(HEAT))
            p.add_argument("--daily-mean", action="store_true", help="Also calculate UTC daily mean heat flux")
        if name != "flux":
            p.add_argument("--overwrite", action="store_true")

    if mode:
        options(result, mode)
    else:
        commands = result.add_subparsers(dest="product", required=True)
        options(commands.add_parser("hourly", help="Download hourly ERA5 (wind, pressure, heat, optional stress)"), "hourly")
        options(commands.add_parser("cds-daily", help="Download official ERA5 daily means"), "cds-daily")
    return result


def main(argv=None, *, mode=None):
    args = parser(mode).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("multiurl").setLevel(logging.WARNING)
    try:
        west, east, south, north = args.bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise DownloadError("bbox needs -180 <= W < E <= 180 and -90 <= S < N <= 90")
        if any(value * 4 != round(value * 4) for value in args.bbox):
            raise DownloadError("bbox boundaries must align to the 0.25-degree grid")
        if args.retries < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
            raise DownloadError("retries and timeout must be positive")
        if len(args.variables) != len(set(args.variables)):
            raise DownloadError("Repeated variable in --variables")
        dataset = CDS_DAILY if args.mode == "cds-daily" else HOURLY
        periods = select_periods(args, dataset)
        chunks = [chunk for first, last in periods
                  for chunk in month_chunks(first, last + timedelta(days=int(args.daily_mean)))]
        LOG.info("%s, %s..%s, %d requests. Bbox %s. Variables %s",
                 dataset, chunks[0][0], chunks[-1][1], len(chunks), list(args.bbox), ", ".join(args.variables))
        if args.dry_run:
            cells = (int((east - west) * 4) + int(east - west < 360)) * (int((north - south) * 4) + 1)
            days = sum((last - first).days + 1 for first, last in chunks)
            samples = days * (1 if dataset == CDS_DAILY else 24)
            size_mb = samples * cells * len(args.variables) * 4 / 1024**2
            if size_mb < 1024:
                LOG.info("Approximate uncompressed float32 payload: %.2f MiB", size_mb)
            else:
                LOG.info("Approximate uncompressed float32 payload: %.2f GiB", size_mb / 1024)
            LOG.info("First request: %s", json.dumps(build_request(args, *chunks[0])))
            return 0
        client = make_client(600 if args.mode == "cds-daily" else args.timeout)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        paths = [(first, last, download_month(client, args, dataset, first, last)) for first, last in chunks]
        if args.daily_mean:
            for first, last in periods:
                for start, end in month_chunks(first, last):
                    aggregate_flux(paths, args.output_dir, start, end, args.bbox)
        return 0
    except KeyboardInterrupt:
        return 130
    except ImportError as exc:
        LOG.error("Missing dependency %s. Run python -m pip install -r requirements.txt", exc.name)
    except Exception as exc:
        LOG.error("%s", failure_details(exc)[0])
    return 2 if args.mode == "cds-daily" else 1


if __name__ == "__main__":
    sys.exit(main())
