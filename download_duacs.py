#!/usr/bin/env python3
"""Download DUACS daily L4 regional data, one verified NetCDF per year."""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
import sys
import time
from pathlib import Path

DATASET_ID = "cmems_obs-sl_glo_phy-ssh_my_allsat-l4-duacs-0.125deg_P1D"
DEFAULT_BBOX = (105.0, 125.0, 0.0, 25.0)
DEFAULT_VARIABLES = ("sla", "adt", "ugos", "vgos", "ugosa", "vgosa", "err_sla")
REQUEST_ATTR = "duacs_download_request"


class DownloadError(ValueError):
    """Locally generated errors safe to display without exposing credentials."""


def parse_years(text: str) -> list[int]:
    years: set[int] = set()
    for part in text.split(","):
        match = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{4}))?\s*", part)
        if not match:
            raise argparse.ArgumentTypeError("Use 2001 / 2001-2005 / 2001,2003-2005")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1993 <= first <= last <= 9998:
            raise argparse.ArgumentTypeError("Years must be ordered and >= 1993")
        years.update(range(first, last + 1))
    return sorted(years)


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def expected_days(year: int):
    import numpy as np
    return np.arange(f"{year}-01-01", f"{year + 1}-01-01", dtype="datetime64[D]")


def complete_years(stamps) -> list[int]:
    import numpy as np
    days = np.asarray(stamps).astype("datetime64[D]")
    if not days.size or np.isnat(days).any():
        raise DownloadError("Invalid remote time coordinate")
    first, last = int(str(days.min())[:4]), int(str(days.max())[:4])
    return [year for year in range(first, last + 1)
            if np.array_equal(days[(days >= np.datetime64(f"{year}-01-01"))
                                   & (days < np.datetime64(f"{year + 1}-01-01"))],
                              expected_days(year))]


def request(args, year: int) -> dict:
    west, east, south, north = args.bbox
    return dict(dataset_id=DATASET_ID, dataset_version=args.dataset_version,
                variables=args.variables, minimum_longitude=west,
                maximum_longitude=east, minimum_latitude=south,
                maximum_latitude=north, start_datetime=f"{year}-01-01T00:00:00",
                end_datetime=f"{year}-12-31T23:59:59",
                coordinates_selection_method="inside")


def signature(args, year: int, metadata: dict) -> str:
    return json.dumps(dict(request=request(args, year), source=metadata),
                      sort_keys=True, separators=(",", ":"))


def verify(path: Path, year: int, variables: list[str], grid: dict,
           identity: str | None = None) -> str | None:
    import numpy as np
    import xarray as xr
    try:
        with xr.open_dataset(path, decode_timedelta=False) as data:
            if identity is not None and data.attrs.get(REQUEST_ATTR) != identity:
                return "request or source version differs (or provenance is missing)"
            if "time" not in data.coords or not np.array_equal(
                    data.time.values.astype("datetime64[D]"), expected_days(year)):
                return "time axis is incomplete, duplicated or unordered"
            for name, values in grid.items():
                if name not in data.coords or not np.array_equal(data[name].values, values):
                    return f"{name} grid differs"
            for name in variables:
                if name not in data or set(data[name].dims) != {"time", "latitude", "longitude"}:
                    return f"missing or unexpected dimensions: {name}"
                # Check the entire payload in bounded monthly-sized slices.
                for start in range(0, data.sizes["time"], 31):
                    data[name].isel(time=slice(start, start + 31)).load()
    except Exception as exc:
        return f"unreadable NetCDF ({type(exc).__name__})"
    return None


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be >= 1")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("years", nargs="?", type=parse_years,
                   help="Default: 2001 through the latest complete remote year")
    p.add_argument("-o", "--output-dir", type=Path, default=Path.cwd())
    p.add_argument("--bbox", type=float, nargs=4, default=DEFAULT_BBOX,
                   metavar=("W", "E", "S", "N"), help="Default: 105 125 0 25")
    p.add_argument("-v", "--variables", nargs="+", default=list(DEFAULT_VARIABLES))
    p.add_argument("--dataset-version", help="Pin a Copernicus dataset version")
    p.add_argument("--compression", type=int, choices=range(10), default=1)
    p.add_argument("--retries", type=positive_int, default=3,
                   help="Maximum attempts per year, including first attempt (default: 3)")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--stop-on-error", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    return p


def check_status(response, dry_run: bool = False) -> None:
    if getattr(response.status, "value", response.status) != ("001" if dry_run else "000"):
        raise RuntimeError("Copernicus request did not succeed")


def run(args) -> int:
    import copernicusmarine as cm
    import netCDF4
    from tqdm import tqdm
    logging.getLogger("copernicusmarine").setLevel(logging.ERROR)
    visible = sys.stderr.isatty() and not args.no_progress
    args.variables = sorted(set(args.variables))
    probe_request = request(args, 2001)
    del probe_request["start_datetime"], probe_request["end_datetime"]
    print("Inspecting remote coordinates and source version...", flush=True)
    with cm.open_dataset(**probe_request) as remote:
        stamps = remote.time.values
        available = complete_years(stamps)
        grid = {name: remote[name].values.copy() for name in ("longitude", "latitude")}
        metadata = {key: str(remote.attrs.get(key, "")) for key in
                    ("product_version", "software_version")}
        if any(not values.size for values in grid.values()):
            raise DownloadError("Selected region contains no grid cells")
    years = args.years if args.years is not None else [y for y in available if y >= 2001]
    if not years:
        raise DownloadError("No complete years available from 2001")
    unavailable = sorted(set(years) - set(available))
    if unavailable:
        raise DownloadError(f"Not complete on server: {unavailable}. Coverage: {stamps[0]} to {stamps[-1]}")
    output = args.output_dir.expanduser().resolve()
    print(f"Dataset: {DATASET_ID}\nSource: {metadata}\nRemote time: {stamps[0]} to {stamps[-1]}")
    print(f"BBox (W E S N): {args.bbox}; grid: {len(grid['longitude'])} x {len(grid['latitude'])}")
    print(f"Variables: {', '.join(args.variables)}\nOutput: {output}", flush=True)
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
    failures = skipped = done = 0
    total_file = total_transfer = 0.0
    bar = tqdm(total=len(years), desc="Years", unit="year", disable=not visible)

    def report(message: str) -> None:
        if visible:
            tqdm.write(message)
        else:
            print(message, flush=True)

    try:
        for index, year in enumerate(years, 1):
            label = f"[{index}/{len(years)}] {year}"
            target = output / f"DUACS_SCS_daily_{year}.nc"
            identity = signature(args, year, metadata)
            if target.exists() and not args.overwrite:
                problem = verify(target, year, args.variables, grid, identity)
                if problem is None:
                    report(f"{label}: verified, skip")
                    skipped += 1
                    bar.update()
                    continue
                report(f"{label}: existing file retained: {problem}; use --overwrite to replace")
                failures += 1
                bar.update()
                if args.stop_on_error:
                    break
                continue
            temp = output / f".DUACS_SCS_daily_{year}.partial.nc"
            succeeded = False
            for attempt in range(1, args.retries + 1):
                try:
                    estimate = cm.subset(**request(args, year), dry_run=True,
                                         netcdf_compression_level=args.compression)
                    check_status(estimate, dry_run=True)
                    file_size = float(estimate.file_size) * 1024**2
                    transfer = float(estimate.data_transfer_size) * 1024**2
                    if not all(math.isfinite(v) and v >= 0 for v in (file_size, transfer)):
                        raise DownloadError("Invalid size estimate")
                    report(f"{label}: attempt {attempt}/{args.retries}; file ~{format_bytes(file_size)}, "
                           f"transfer ~{format_bytes(transfer)}")
                    if args.dry_run:
                        total_file += file_size
                        total_transfer += transfer
                        succeeded = True
                        break
                    if shutil.disk_usage(output).free < file_size * 1.2:
                        raise DownloadError("Insufficient disk space (20% reserve)")
                    response = cm.subset(**request(args, year), output_directory=str(output),
                                         output_filename=temp.name, overwrite=True,
                                         netcdf_compression_level=args.compression,
                                         disable_progress_bar=not visible)
                    check_status(response)
                    report(f"{label}: verifying all variables and daily coordinates...")
                    problem = verify(temp, year, args.variables, grid)
                    if problem:
                        raise DownloadError(problem)
                    with netCDF4.Dataset(temp, "a") as data:
                        data.setncattr(REQUEST_ATTR, identity)
                    temp.replace(target)
                    report(f"{label}: saved {target.name} ({format_bytes(target.stat().st_size)})")
                    succeeded = True
                    break
                except Exception as exc:
                    # Third-party errors may embed signed URLs; do not print their contents.
                    detail = str(exc) if isinstance(exc, DownloadError) else (
                        f"{type(exc).__name__}; check copernicusmarine login, network and disk")
                    report(f"{label}: attempt failed: {detail}")
                    if attempt < args.retries:
                        time.sleep(min(2 ** attempt, 30))
            if succeeded:
                done += 1
            else:
                failures += 1
            bar.update()
            if not succeeded and args.stop_on_error:
                break
    finally:
        bar.close()
    if args.dry_run:
        print(f"Estimated totals: file {format_bytes(total_file)}, transfer {format_bytes(total_transfer)}")
    print(f"{'Planned' if args.dry_run else 'Done'}: {done}; skipped: {skipped}; failed: {failures}")
    return 1 if failures else 0


def main() -> int:
    p = parser()
    args = p.parse_args()
    w, e, s, n = args.bbox
    if not all(math.isfinite(v) for v in args.bbox) or not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        p.error("bbox requires -180 <= W < E <= 180 and -90 <= S < N <= 90")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Completed files are kept; incomplete years restart on rerun.", file=sys.stderr)
        return 130
    except DownloadError as exc:
        print(f"Invalid request: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed ({type(exc).__name__}). Check dependencies, network and copernicusmarine login.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
