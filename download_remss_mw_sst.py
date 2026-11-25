#!/usr/bin/env python3
"""Download & crop PO.DAAC GHRSST REMSS microwave L3U SST (AMSR2 v8.2 & GMI v8.2a).

Each daily global file is only ~4-5 MB, so the script downloads via NASA Earthdata HTTPS
(reusing ~/.edl_token), crops immediately to --bbox with xarray, and saves compact regional NetCDF4.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import datetime as dt
import json
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path

import requests
from tqdm import tqdm

SENSORS = {
    "amsr2": ("C2600786104-POCLOUD", "AMSR2-REMSS-L3U-v8.2"),
    "gmi": ("C2036877762-POCLOUD", "GMI-REMSS-L3U-v8.2a"),
}
CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.json"
DEFAULT_TOKEN_FILE = Path.home() / ".edl_token"
DEFAULT_BBOX = (136.0, 161.0, 22.0, 42.0)  # Kuroshio + 2 deg margin for MW footprint convolution


class EarthdataSession(requests.Session):
    """Preserve Authorization Bearer header across NASA Earthdata -> S3/Cloudfront redirects."""

    def rebuild_auth(self, prepared_request, response):  # type: ignore[override]
        headers = prepared_request.headers
        url = prepared_request.url or ""
        if "earthdata.nasa.gov" not in url:
            headers.pop("Authorization", None)


def parse_years(text: str) -> list[int]:
    years: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{4}))?\s*", part)
        if not m:
            raise argparse.ArgumentTypeError("Use YYYY, YYYY-YYYY or YYYY,YYYY")
        first, last = int(m[1]), int(m[2] or m[1])
        if not 2012 <= first <= last <= 2100:
            raise argparse.ArgumentTypeError("Years must be ordered and >= 2012")
        years.update(range(first, last + 1))
    return sorted(years)


def load_token(explicit: Path | None) -> str:
    if explicit is None:
        if env := os.environ.get("EARTHDATA_TOKEN", "").strip():
            token = env
        else:
            if not DEFAULT_TOKEN_FILE.is_file():
                raise RuntimeError(f"Earthdata token not found at {DEFAULT_TOKEN_FILE}")
            token = DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    else:
        token = explicit.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("Earthdata token is empty")

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = dt.datetime.fromtimestamp(
            json.loads(base64.urlsafe_b64decode(payload))["exp"], dt.timezone.utc
        )
        if exp <= dt.datetime.now(dt.timezone.utc):
            raise RuntimeError(
                f"Earthdata token expired at {exp:%Y-%m-%d %H:%M} UTC. "
                "Regenerate at https://urs.earthdata.nasa.gov and save to ~/.edl_token"
            )
    except RuntimeError:
        raise
    except Exception:
        pass
    return token


def search_granules(collection_id: str, year: int, timeout: int = 30) -> list[tuple[str, str]]:
    """Return sorted (YYYYMMDD, https_nc_url) list from public CMR."""
    found: dict[str, str] = {}
    page = 1
    while True:
        r = requests.get(
            CMR_GRANULES,
            params={
                "collection_concept_id": collection_id,
                "temporal": f"{year}-01-01T00:00:00Z,{year}-12-31T23:59:59Z",
                "page_size": 500,
                "page_num": page,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        entries = r.json().get("feed", {}).get("entry", [])
        if not entries:
            break
        for e in entries:
            title = str(e.get("title", ""))
            m = re.match(r"(\d{8})", title)
            if not m or not m.group(1).startswith(str(year)):
                continue
            nc_url = next(
                (
                    l.get("href") for l in e.get("links", [])
                    if str(l.get("href", "")).startswith("https://")
                    and str(l.get("href", "")).endswith(".nc")
                ),
                None,
            )
            if nc_url:
                found[m.group(1)] = nc_url
        if len(entries) < 500:
            break
        page += 1
    return sorted(found.items())


def verify_nc(path: Path) -> bool:
    import xarray as xr

    try:
        with xr.open_dataset(path, decode_timedelta=False) as ds:
            return "sea_surface_temperature" in ds.data_vars
    except Exception:
        return False


def download_and_crop_one(
    day_str: str,
    url: str,
    target: Path,
    bbox: tuple[float, float, float, float],
    token: str,
    retries: int,
    timeout: int,
) -> tuple[str, int]:
    import xarray as xr

    if target.exists() and verify_nc(target):
        return "skipped", 0

    target.parent.mkdir(parents=True, exist_ok=True)
    west, east, south, north = bbox
    session = EarthdataSession()
    session.headers.update({"Authorization": f"Bearer {token}"})

    last_err = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            with tempfile.TemporaryDirectory(prefix=".mw-", dir=target.parent) as tmp:
                raw_nc = Path(tmp) / "global.nc"
                with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
                    if r.status_code in (401, 403):
                        raise RuntimeError(f"Fatal HTTP {r.status_code} (check token / PO.DAAC EULA)")
                    r.raise_for_status()
                    with raw_nc.open("wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            if chunk:
                                f.write(chunk)

                with xr.open_dataset(raw_nc, decode_timedelta=False) as ds:
                    lat_name = "lat" if "lat" in ds.coords else "latitude"
                    lon_name = "lon" if "lon" in ds.coords else "longitude"
                    lat_vals = ds[lat_name].values
                    lon_vals = ds[lon_name].values
                    if lon_vals.min() < 0 and west >= 0 and east > 180:
                        ds = ds.assign_coords({lon_name: (ds[lon_name] % 360)}).sortby(lon_name)
                    lat_slice = slice(south, north) if lat_vals[0] < lat_vals[-1] else slice(north, south)
                    sub = ds.sel({lat_name: lat_slice, lon_name: slice(west, east)}).load()
                    enc = {v: {"zlib": True, "complevel": 1} for v in sub.data_vars}
                    part = target.with_name(f"{target.name}.{os.getpid()}.part")
                    sub.to_netcdf(part, engine="netcdf4", encoding=enc)
                    part.replace(target)
                return "done", target.stat().st_size
        except RuntimeError:
            raise
        except Exception as exc:
            last_err = str(exc)
        if attempt < retries:
            time.sleep(min(30, 2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(last_err)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download & crop PO.DAAC REMSS AMSR2/GMI L3U SST.")
    p.add_argument("years", type=parse_years, help="Years, e.g. 2016-2025 or 2024")
    p.add_argument("--sensors", nargs="+", choices=list(SENSORS), default=list(SENSORS))
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "E", "S", "N"), default=list(DEFAULT_BBOX))
    p.add_argument("-o", "--output-dir", type=Path, default=Path("data/remss_mw_sst"))
    p.add_argument("--token-file", type=Path, default=None, help="Earthdata token file (default ~/.edl_token)")
    p.add_argument("-j", "--jobs", type=int, default=4, help="Concurrent daily downloads (1-8)")
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("-n", "--dry-run", action="store_true", help="Query CMR granule list only (no token needed)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    jobs = max(1, min(8, args.jobs))

    tasks: list[tuple[str, int, str, str]] = []
    for sensor in args.sensors:
        cid, short = SENSORS[sensor]
        for y in args.years:
            items = search_granules(cid, y)
            print(f"  CMR {short:22s} {y}: {len(items)} daily granules")
            tasks.extend((sensor, y, d, url) for d, url in items)

    if not tasks:
        print("No granules found.")
        return 1

    if args.dry_run:
        est_remote_gb = len(tasks) * 4.5 / 1024
        est_local_mb = len(tasks) * 0.15
        print(
            f"DRY-RUN total: {len(tasks)} granules | bbox={args.bbox} | "
            f"remote ~{est_remote_gb:.2f} GB -> cropped local ~{est_local_mb:.0f} MB"
        )
        return 0

    token = load_token(args.token_file)
    counts = {"done": 0, "skipped": 0, "failed": 0}
    total_bytes = 0
    with futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        fut_map = {
            ex.submit(
                download_and_crop_one, d, url,
                args.output_dir / sensor / f"{y}" / f"{sensor.upper()}_L3U_{d}.nc",
                tuple(args.bbox), token, args.retries, args.timeout,
            ): f"{sensor}:{d}"
            for sensor, y, d, url in tasks
        }
        for fut in tqdm(futures.as_completed(fut_map), total=len(fut_map), unit="day"):
            key = fut_map[fut]
            try:
                st, nb = fut.result()
                counts[st] += 1
                total_bytes += nb
            except Exception as exc:
                counts["failed"] += 1
                print(f"[ERR] {key}: {exc}", file=sys.stderr)

    print(f"Done: {counts}, saved {total_bytes/1e6:.1f} MB cropped NetCDF")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
