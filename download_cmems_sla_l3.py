#!/usr/bin/env python3
"""Download CMEMS along-track 1 Hz L3 SLA (SEALEVEL_GLO_PHY_L3_MY_008_062) by mission and year.

Uses Copernicus Marine Toolbox to fetch daily global along-track files into a temporary directory,
crops each file immediately to the requested bbox, and writes one compact Parquet per (mission, year).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm

PRODUCT_ID = "SEALEVEL_GLO_PHY_L3_MY_008_062"
DATASET_TEMPLATE = "cmems_obs-sl_glo_phy-ssh_my_{mission}-l3-duacs_PT1S"
# Active missions covering 2016-2025 (c2n/alg included so downstream can hold one out for validation)
DEFAULT_MISSIONS = (
    "alg", "c2", "c2n", "h2a", "h2ag", "h2b",
    "j2", "j2g", "j2n", "j3", "j3n", "s3a", "s3b", "s6a-lr", "swon",
)
DEFAULT_BBOX = (137.0, 160.0, 23.0, 41.0)  # Kuroshio Extension + 1 deg buffer (W E S N)
KEEP_VARS = (
    "sla_filtered", "sla_unfiltered", "mdt", "lwe", "dac",
    "ocean_tide", "internal_tide", "track", "cycle",
)
LOG = logging.getLogger("cmems_sla_l3")


def parse_years(text: str) -> list[int]:
    years: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{4}))?\s*", part)
        if not m:
            raise argparse.ArgumentTypeError("Use YYYY, YYYY-YYYY or YYYY,YYYY")
        first, last = int(m[1]), int(m[2] or m[1])
        if not 1993 <= first <= last <= 2100:
            raise argparse.ArgumentTypeError("Years must be ordered and >= 1993")
        years.update(range(first, last + 1))
    return sorted(years)


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def crop_nc_to_df(path: Path, mission: str, bbox: tuple[float, float, float, float]):
    import numpy as np
    import pandas as pd
    import xarray as xr

    west, east, south, north = bbox
    with xr.open_dataset(path) as ds:
        if "latitude" not in ds or "longitude" not in ds or "time" not in ds:
            return None
        lat = ds["latitude"].values
        lon = ds["longitude"].values
        # Normalize longitude into [-180, 180) or [0, 360) to match requested bbox
        if west >= 0:
            lon_cmp = np.where(lon < 0, lon + 360.0, lon)
        else:
            lon_cmp = np.where(lon > 180, lon - 360.0, lon)

        mask = (
            np.isfinite(lat) & np.isfinite(lon_cmp)
            & (lat >= south) & (lat <= north)
            & (lon_cmp >= west) & (lon_cmp <= east)
        )
        if not np.any(mask):
            return None

        idx = np.nonzero(mask)[0]
        cols: dict[str, object] = {
            "time": ds["time"].values[idx],
            "latitude": lat[idx].astype(np.float32),
            "longitude": lon_cmp[idx].astype(np.float32),
            "mission": mission,
        }
        for name in KEEP_VARS:
            if name in ds.variables and ds[name].ndim == 1 and ds[name].size == lat.size:
                vals = ds[name].values[idx]
                if np.issubdtype(vals.dtype, np.floating):
                    vals = vals.astype(np.float32)
                cols[name] = vals
        return pd.DataFrame(cols)


def verify_parquet(path: Path) -> bool:
    import pandas as pd

    try:
        df = pd.read_parquet(path, columns=["time", "latitude", "longitude"])
        return len(df) >= 0 and {"time", "latitude", "longitude"}.issubset(df.columns)
    except Exception:
        return False


def process_mission_year(
    mission: str,
    year: int,
    bbox: tuple[float, float, float, float],
    output_dir: Path,
    dataset_version: str | None,
    dry_run: bool,
    overwrite: bool,
) -> tuple[str, int]:
    import copernicusmarine
    import pandas as pd

    dataset_id = DATASET_TEMPLATE.format(mission=mission)
    target = output_dir / f"SLA_L3_{mission}_{year}.parquet"

    if not dry_run and target.exists() and not overwrite:
        if verify_parquet(target):
            LOG.info("Skip %s (verified)", target.name)
            return "skipped", 0
        LOG.warning("Existing file invalid, re-downloading: %s", target.name)

    kwargs = dict(
        dataset_id=dataset_id,
        filter=f"*/{year}/*",
        disable_progress_bar=True,
    )
    if dataset_version:
        kwargs["dataset_version"] = dataset_version

    if dry_run:
        res = copernicusmarine.get(**kwargs, dry_run=True)
        files = getattr(res, "files", []) or []
        total_mb = float(getattr(res, "total_size", 0.0) or 0.0)
        if files:
            print(f"  {mission:6s} {year}: {len(files):3d} global daily files (~{total_mb:.1f} MB remote)")
        return ("planned" if files else "empty"), len(files)

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".sla-{mission}-{year}-", dir=output_dir) as tmp:
        tmp_path = Path(tmp)
        res = copernicusmarine.get(
            **kwargs,
            output_directory=str(tmp_path),
            no_directories=True,
            overwrite=True,
        )
        files = sorted(tmp_path.glob("*.nc"))
        if not files:
            LOG.info("No data for %s %d", mission, year)
            return "empty", 0

        frames = []
        for nc_file in files:
            df = crop_nc_to_df(nc_file, mission, bbox)
            if df is not None and not df.empty:
                frames.append(df)

        if not frames:
            LOG.info("No points inside bbox for %s %d (%d files scanned)", mission, year, len(files))
            return "empty", 0

        merged = pd.concat(frames, ignore_index=True)
        merged.sort_values("time", inplace=True, kind="mergesort")
        partial = target.with_suffix(".parquet.part")
        merged.to_parquet(partial, index=False, compression="zstd")
        partial.replace(target)
        LOG.info(
            "Saved %s (%d points from %d files, %s)",
            target.name, len(merged), len(files), format_bytes(target.stat().st_size),
        )
        return "done", len(merged)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download & crop CMEMS along-track 1 Hz L3 SLA (008_062) per mission-year.",
    )
    p.add_argument("years", type=parse_years, help="Years, e.g. 2016-2025 or 2024,2025")
    p.add_argument(
        "--missions", nargs="+", default=list(DEFAULT_MISSIONS),
        help=f"Satellite mission codes (default: {' '.join(DEFAULT_MISSIONS)})",
    )
    p.add_argument(
        "--bbox", nargs=4, type=float, metavar=("W", "E", "S", "N"),
        default=list(DEFAULT_BBOX),
        help="Region W E S N (default: 137 160 23 41)",
    )
    p.add_argument("-o", "--output-dir", type=Path, default=Path("data/cmems_sla_l3"))
    p.add_argument("--dataset-version", default=None, help="Optional fixed CMEMS dataset version")
    p.add_argument("-n", "--dry-run", action="store_true", help="Query remote files without downloading")
    p.add_argument("--overwrite", action="store_true", help="Re-download existing Parquet files")
    p.add_argument("--stop-on-error", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    west, east, south, north = args.bbox
    if not (-180 <= west < east <= 360 and -90 <= south < north <= 90):
        print(f"Invalid bbox: {args.bbox}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("copernicusmarine").setLevel(logging.WARNING)

    tasks = [(m, y) for y in args.years for m in args.missions]
    print(f"Product: {PRODUCT_ID} | bbox={args.bbox} | tasks={len(tasks)} ({'DRY-RUN' if args.dry_run else args.output_dir})")

    counts = {"done": 0, "skipped": 0, "empty": 0, "planned": 0, "failed": 0}
    bar = tqdm(tasks, disable=args.no_progress or args.dry_run, unit="task")
    for mission, year in bar:
        bar.set_postfix_str(f"{mission} {year}")
        try:
            status, _ = process_mission_year(
                mission, year, tuple(args.bbox), args.output_dir,
                args.dataset_version, args.dry_run, args.overwrite,
            )
            counts[status] += 1
        except Exception as exc:
            counts["failed"] += 1
            LOG.error("Failed %s %d: %s", mission, year, exc)
            if args.stop_on_error:
                return 1

    print("Summary:", counts)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
