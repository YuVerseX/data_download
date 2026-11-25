#!/usr/bin/env python3
"""Download NOAA GDP Hourly v2.01 15 m surface drifter observations from public AOML ERDDAP.

No credentials required. Queries ERDDAP by month to avoid timeouts, computes exact boolean
`drogue_status` from `drogue_lost_date`, and saves one clean Parquet file per calendar year.
"""
from __future__ import annotations

import argparse
import calendar
import io
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from tqdm import tqdm

ERDDAP_BASE = "https://erddap.aoml.noaa.gov/gdp/erddap/tabledap/drifter_hourly_qc.csvp"
DEFAULT_BBOX = (137.0, 160.0, 23.0, 41.0)  # W E S N
ERDDAP_VARS = (
    "ID", "WMO", "time", "latitude", "longitude",
    "ve", "vn", "err_lat", "err_lon", "err_ve", "err_vn",
    "sst", "err_sst", "flg_sst", "gap", "drogue_lost_date", "typebuoy",
)


def parse_years(text: str) -> list[int]:
    years: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{4}))?\s*", part)
        if not m:
            raise argparse.ArgumentTypeError("Use YYYY, YYYY-YYYY or YYYY,YYYY")
        first, last = int(m[1]), int(m[2] or m[1])
        if not 1979 <= first <= last <= 2100:
            raise argparse.ArgumentTypeError("Years must be ordered and >= 1979")
        years.update(range(first, last + 1))
    return sorted(years)


def build_month_url(year: int, month: int, bbox: tuple[float, float, float, float]) -> str:
    west, east, south, north = bbox
    last_day = calendar.monthrange(year, month)[1]
    t0 = f"{year}-{month:02d}-01T00:00:00Z"
    t1 = f"{year}-{month:02d}-{last_day:02d}T23:59:59Z"
    query = (
        ",".join(ERDDAP_VARS)
        + f"&time>={t0}&time<={t1}"
        + f"&latitude>={south}&latitude<={north}"
        + f"&longitude>={west}&longitude<={east}"
    )
    return f"{ERDDAP_BASE}?{urllib.parse.quote(query, safe=',&=<>:-TZ')}"


def fetch_month(year: int, month: int, bbox: tuple[float, float, float, float], retries: int, timeout: int):
    import pandas as pd

    url = build_month_url(year, month, bbox)
    last_err = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 404:
                # ERDDAP returns HTTP 404 when no rows match the time/space constraint
                return None
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            # Strip unit suffixes like "latitude (degrees_north)" -> "latitude"
            df.columns = [c.split(" (")[0].strip() for c in df.columns]
            return df
        except Exception as exc:
            last_err = str(exc)
        if attempt < retries:
            time.sleep(min(20, 2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"{year}-{month:02d} failed: {last_err}")


def download_year(
    year: int,
    bbox: tuple[float, float, float, float],
    output_dir: Path,
    drogued_only: bool,
    retries: int,
    timeout: int,
    overwrite: bool,
) -> tuple[str, int]:
    import pandas as pd

    target = output_dir / f"GDP_hourly_{year}.parquet"
    if target.exists() and not overwrite:
        try:
            n = len(pd.read_parquet(target, columns=["ID"]))
            print(f"  [SKIP] {target.name} ({n:,} rows)")
            return "skipped", n
        except Exception:
            pass

    output_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for month in tqdm(range(1, 13), desc=f"GDP {year}", unit="mo", leave=False):
        df = fetch_month(year, month, bbox, retries, timeout)
        if df is not None and not df.empty:
            frames.append(df)

    if not frames:
        print(f"  [EMPTY] No GDP rows found for {year} in bbox={bbox}")
        return "empty", 0

    merged = pd.concat(frames, ignore_index=True)
    merged["time"] = pd.to_datetime(merged["time"], utc=True, errors="coerce")
    merged["drogue_lost_date"] = pd.to_datetime(merged["drogue_lost_date"], utc=True, errors="coerce")
    merged["drogue_status"] = merged["drogue_lost_date"].isna() | (merged["time"] < merged["drogue_lost_date"])
    if drogued_only:
        merged = merged[merged["drogue_status"]].reset_index(drop=True)

    part = target.with_suffix(".parquet.part")
    merged.to_parquet(part, index=False, compression="zstd")
    part.replace(target)
    n_drifters = merged["ID"].nunique() if "ID" in merged.columns else 0
    print(
        f"  [DONE] {target.name}: {len(merged):,} hourly rows, {n_drifters} unique drifters "
        f"({target.stat().st_size/1e6:.2f} MB)"
    )
    return "done", len(merged)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download NOAA GDP Hourly v2.01 drifters from AOML ERDDAP.")
    p.add_argument("years", type=parse_years, help="Years, e.g. 2022,2024-2025")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "E", "S", "N"), default=list(DEFAULT_BBOX))
    p.add_argument("-o", "--output-dir", type=Path, default=Path("data/gdp_drifters"))
    p.add_argument("--all-drifters", action="store_true", help="Keep both drogued and undrogued rows (default: drogued only)")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("-n", "--dry-run", action="store_true", help="Check ERDDAP endpoint and print plan")
    p.add_argument("--overwrite", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bbox = tuple(args.bbox)

    if args.dry_run:
        info_url = "https://erddap.aoml.noaa.gov/gdp/erddap/info/drifter_hourly_qc/index.json"
        r = requests.get(info_url, timeout=20)
        r.raise_for_status()
        print(f"DRY-RUN OK: ERDDAP reachable (HTTP {r.status_code})")
        print(f"  Years={args.years} | bbox={bbox} | drogued_only={not args.all_drifters}")
        print(f"  Sample URL: {build_month_url(args.years[0], 1, bbox)}")
        return 0

    for y in args.years:
        download_year(y, bbox, args.output_dir, not args.all_drifters, args.retries, args.timeout, args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
