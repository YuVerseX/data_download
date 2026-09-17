#!/usr/bin/env python3
"""Download monthly or yearly Northwest Pacific subsets of OSTIA REP L4 SST."""
from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


PRODUCT_ID = "SST_GLO_SST_L4_REP_OBSERVATIONS_010_011"
DATASET_ID = "METOFFICE-GLO-SST-L4-REP-OBS-SST"
PRODUCT_START = date(1981, 10, 1)
DEFAULT_BBOX = (100.0, 180.0, 0.0, 60.0)
DEFAULT_VARIABLES = ("analysed_sst", "analysis_error")
VALID_VARIABLES = frozenset(
    ("analysed_sst", "analysis_error", "sea_ice_fraction", "mask")
)
REQUEST_ATTR = "ostia_rep_download_request"


class DownloadError(ValueError):
    """Locally generated error whose text is safe to display."""


@dataclass(frozen=True)
class Job:
    start: date
    end: date


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def format_bytes(value: float | None) -> str:
    if value is None:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def build_jobs(start: date, end: date, chunk: str) -> list[Job]:
    jobs: list[Job] = []
    cursor = start
    while cursor <= end:
        if chunk == "monthly":
            boundary = next_month(date(cursor.year, cursor.month, 1)) - timedelta(days=1)
        else:
            boundary = date(cursor.year, 12, 31)
        boundary = min(boundary, end)
        jobs.append(Job(cursor, boundary))
        cursor = boundary + timedelta(days=1)
    return jobs


def expected_days(job: Job):
    import numpy as np

    return np.arange(job.start.isoformat(), (job.end + timedelta(days=1)).isoformat(),
                     dtype="datetime64[D]")


def request(args, job: Job) -> dict:
    west, east, south, north = args.bbox
    return {
        "dataset_id": DATASET_ID,
        "dataset_version": args.dataset_version,
        "variables": args.variables,
        "minimum_longitude": west,
        "maximum_longitude": east,
        "minimum_latitude": south,
        "maximum_latitude": north,
        "start_datetime": f"{job.start.isoformat()}T00:00:00",
        "end_datetime": f"{job.end.isoformat()}T23:59:59",
        "coordinates_selection_method": "inside",
    }


def request_signature(args, job: Job, source: dict) -> str:
    payload = {
        "product_id": PRODUCT_ID,
        "request": request(args, job),
        "source": source,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def output_name(job: Job) -> str:
    return f"OSTIA_REP_daily_{job.start}_{job.end}.nc"


def inspect_remote(args) -> dict:
    import copernicusmarine as cm
    import numpy as np

    west, east, south, north = args.bbox
    with cm.open_dataset(
            dataset_id=DATASET_ID,
            dataset_version=args.dataset_version,
            variables=args.variables,
            minimum_longitude=west,
            maximum_longitude=east,
            minimum_latitude=south,
            maximum_latitude=north,
            coordinates_selection_method="inside") as remote:
        times = remote.time.values.astype("datetime64[D]")
        longitudes = remote.longitude.values.copy()
        latitudes = remote.latitude.values.copy()
        if not times.size or np.isnat(times).any() or not np.array_equal(times, np.unique(times)):
            raise DownloadError("remote time coordinate is empty, invalid, duplicated, or unordered")
        if not longitudes.size or not latitudes.size:
            raise DownloadError("the selected bbox contains no grid centers")
        missing_variables = [name for name in args.variables if name not in remote]
        if missing_variables:
            raise DownloadError(f"remote variables are missing: {', '.join(missing_variables)}")
        return {
            "time": times,
            "longitude": longitudes,
            "latitude": latitudes,
            "source": {
                "dataset_id": DATASET_ID,
                "dataset_version": args.dataset_version or "latest",
                "product_version": str(remote.attrs.get("product_version", "")),
            },
        }


def validate_coverage(jobs: list[Job], available) -> None:
    import numpy as np

    for job in jobs:
        wanted = expected_days(job)
        missing = wanted[~np.isin(wanted, available)]
        if missing.size:
            raise DownloadError(
                f"requested dates are unavailable ({missing[0]} through {missing[-1]}); "
                f"remote coverage is {available[0]} through {available[-1]}")


def verify(path: Path, args, job: Job, metadata: dict,
           identity: str | None = None) -> str | None:
    import numpy as np
    import xarray as xr

    try:
        with xr.open_dataset(path, decode_timedelta=False) as data:
            if identity is not None and data.attrs.get(REQUEST_ATTR) != identity:
                return "request parameters or source version differ (or provenance is missing)"
            expected = expected_days(job)
            if "time" not in data.coords or not np.array_equal(
                    data.time.values.astype("datetime64[D]"), expected):
                return "time coordinate is incomplete, duplicated, or unordered"
            for name in ("longitude", "latitude"):
                if name not in data.coords or not np.array_equal(
                        data[name].values, metadata[name]):
                    return f"{name} grid differs from the requested remote grid"
            for name in args.variables:
                if name not in data:
                    return f"variable is missing: {name}"
                if set(data[name].dims) != {"time", "latitude", "longitude"}:
                    return f"unexpected dimensions for {name}: {data[name].dims}"
                for offset in range(0, data.sizes["time"], 7):
                    data[name].isel(time=slice(offset, offset + 7)).load()
    except Exception as exc:
        return f"unreadable NetCDF ({type(exc).__name__})"
    return None


def response_size(response, name: str) -> float | None:
    value = getattr(response, name, None)
    if value is None:
        return None
    value = float(value) * 1024 ** 2
    return value if math.isfinite(value) and value >= 0 else None


def check_status(response, dry_run: bool = False) -> None:
    wanted = "001" if dry_run else "000"
    if getattr(response.status, "value", response.status) != wanted:
        raise RuntimeError("Copernicus request did not succeed")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start-date", required=True, type=parse_date, metavar="YYYY-MM-DD")
    p.add_argument("--end-date", required=True, type=parse_date, metavar="YYYY-MM-DD")
    p.add_argument("-o", "--output-dir", type=Path, default=Path.cwd())
    p.add_argument("--bbox", type=float, nargs=4, default=DEFAULT_BBOX,
                   metavar=("W", "E", "S", "N"), help="default: 100 180 0 60")
    p.add_argument("-v", "--variables", nargs="+", default=list(DEFAULT_VARIABLES),
                   help="analysed_sst analysis_error sea_ice_fraction mask")
    p.add_argument("--chunk", choices=("monthly", "yearly"), default="monthly",
                   help="output time chunk (default: monthly)")
    p.add_argument("--dataset-version", help="pin a Copernicus dataset version")
    p.add_argument("--compression", type=int, choices=range(10), default=1)
    p.add_argument("--retries", type=positive_int, default=3)
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--stop-on-error", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    return p


def validate_args(p: argparse.ArgumentParser, args) -> None:
    if args.start_date > args.end_date:
        p.error("--start-date must not be after --end-date")
    if args.start_date < PRODUCT_START:
        p.error(f"this product starts on {PRODUCT_START}")
    west, east, south, north = args.bbox
    if (not all(math.isfinite(value) for value in args.bbox)
            or not (-180 <= west < east <= 180 and -90 <= south < north <= 90)):
        p.error("bbox requires -180 <= W < E <= 180 and -90 <= S < N <= 90")
    args.variables = sorted(set(args.variables))
    unknown = sorted(set(args.variables) - VALID_VARIABLES)
    if unknown:
        p.error(f"unknown variables: {', '.join(unknown)}")


def run(args) -> int:
    import copernicusmarine as cm
    import netCDF4
    from tqdm import tqdm

    logging.getLogger("copernicusmarine").setLevel(logging.ERROR)
    jobs = build_jobs(args.start_date, args.end_date, args.chunk)
    print("Inspecting official remote coordinates, variables, and time coverage...", flush=True)
    metadata = inspect_remote(args)
    validate_coverage(jobs, metadata["time"])
    output = args.output_dir.expanduser().resolve()
    lon, lat = metadata["longitude"], metadata["latitude"]
    print(f"Product: {PRODUCT_ID}")
    print(f"Dataset: {DATASET_ID}; source: {metadata['source']}")
    print(f"Remote time coverage: {metadata['time'][0]} through {metadata['time'][-1]}")
    print(f"Dates: {args.start_date} through {args.end_date}; chunk: {args.chunk}")
    print(f"Requested bbox (W E S N): {' '.join(map(str, args.bbox))}")
    print(f"Selected grid centers: lon {lon[0]}..{lon[-1]} ({lon.size}), "
          f"lat {lat[0]}..{lat[-1]} ({lat.size})")
    print(f"Variables: {', '.join(args.variables)}")
    print(f"Output: {output}; files: {len(jobs)}", flush=True)
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)

    visible = sys.stderr.isatty() and not args.no_progress
    bar = tqdm(total=len(jobs), desc="Files", unit="file", disable=not visible)
    done = skipped = failures = 0
    total_file = total_transfer = 0.0
    estimates_complete = True

    def report(message: str) -> None:
        tqdm.write(message) if visible else print(message, flush=True)

    try:
        for index, job in enumerate(jobs, 1):
            target = output / output_name(job)
            identity = request_signature(args, job, metadata["source"])
            label = f"[{index}/{len(jobs)}] {job.start}..{job.end}"
            if target.exists() and not args.overwrite:
                problem = verify(target, args, job, metadata, identity)
                if problem is None:
                    report(f"{label}: verified, skip")
                    skipped += 1
                else:
                    report(f"{label}: existing file retained: {problem}; use --overwrite")
                    failures += 1
                bar.update()
                if failures and args.stop_on_error:
                    break
                continue

            temp = output / f".{target.stem}.partial.nc"
            succeeded = False
            for attempt in range(1, args.retries + 1):
                try:
                    estimate = cm.subset(
                        **request(args, job), dry_run=True,
                        netcdf_compression_level=args.compression,
                        disable_progress_bar=True)
                    check_status(estimate, dry_run=True)
                    file_size = response_size(estimate, "file_size")
                    transfer_size = response_size(estimate, "data_transfer_size")
                    report(f"{label}: attempt {attempt}/{args.retries}; "
                           f"file ~{format_bytes(file_size)}, "
                           f"transfer ~{format_bytes(transfer_size)}")
                    if args.dry_run:
                        estimates_complete &= file_size is not None and transfer_size is not None
                        total_file += file_size or 0
                        total_transfer += transfer_size or 0
                        succeeded = True
                        break
                    if file_size is not None and shutil.disk_usage(output).free < file_size * 1.2:
                        raise DownloadError("insufficient disk space (20% reserve required)")
                    response = cm.subset(
                        **request(args, job), output_directory=str(output),
                        output_filename=temp.name, overwrite=True,
                        netcdf_compression_level=args.compression,
                        disable_progress_bar=not visible)
                    check_status(response)
                    problem = verify(temp, args, job, metadata)
                    if problem:
                        raise DownloadError(problem)
                    with netCDF4.Dataset(temp, "a") as data:
                        data.setncattr(REQUEST_ATTR, identity)
                    temp.replace(target)
                    report(f"{label}: saved {target.name} ({format_bytes(target.stat().st_size)})")
                    succeeded = True
                    break
                except Exception as exc:
                    detail = str(exc) if isinstance(exc, DownloadError) else (
                        f"{type(exc).__name__}; check login, network, and disk")
                    report(f"{label}: attempt failed: {detail}")
                    if attempt < args.retries:
                        time.sleep(min(2 ** attempt, 30))
            done += int(succeeded)
            failures += int(not succeeded)
            bar.update()
            if not succeeded and args.stop_on_error:
                break
    finally:
        bar.close()

    if args.dry_run:
        if estimates_complete:
            print(f"Estimated totals: file {format_bytes(total_file)}, "
                  f"transfer {format_bytes(total_transfer)}")
        else:
            print("Estimated totals unavailable in this Toolbox version")
    print(f"{'Planned' if args.dry_run else 'Done'}: {done}; "
          f"skipped: {skipped}; failed: {failures}")
    return 1 if failures else 0


def main() -> int:
    p = parser()
    args = p.parse_args()
    validate_args(p, args)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Completed files are kept; rerun to continue.", file=sys.stderr)
        return 130
    except DownloadError as exc:
        print(f"Invalid request: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed ({type(exc).__name__}). Check dependencies, network, and "
              "`copernicusmarine login`.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
