#!/usr/bin/env python3
"""Download regional NOAA/PO.DAAC ACSPO L3S IR SST (L3S_LEO_DY-STAR-v2.81) via OPeNDAP cropping.

Uses NASA CMR public search to resolve exact daily granule names, dynamically reads the 1-D lat/lon
axes once from OPeNDAP (safe for ascending or descending latitude), and downloads only the requested
regional subset (~1-2 MB/day instead of ~408 MB/day global). Requires Earthdata Bearer token.
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
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

COLLECTION = "C2805339147-POCLOUD"
SHORT_NAME = "L3S_LEO_DY-STAR-v2.81"
CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.json"
OPENDAP_BASE = f"https://opendap.earthdata.nasa.gov/collections/{COLLECTION}/granules"
DEFAULT_TOKEN_FILE = Path.home() / ".edl_token"
DEFAULT_BBOX = (137.0, 160.0, 23.0, 41.0)  # W E S N
DEFAULT_VARS = (
    "sea_surface_temperature",
    "sses_bias",
    "sses_standard_deviation",
    "quality_level",
    "l2p_flags",
)


@dataclass(frozen=True)
class GridWindow:
    lat_i0: int
    lat_i1: int
    lon_j0: int
    lon_j1: int


def parse_years(text: str) -> list[int]:
    years: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{4}))?\s*", part)
        if not m:
            raise argparse.ArgumentTypeError("Use YYYY, YYYY-YYYY or YYYY,YYYY")
        first, last = int(m[1]), int(m[2] or m[1])
        if not 2000 <= first <= last <= 2100:
            raise argparse.ArgumentTypeError("Years must be ordered and >= 2000")
        years.update(range(first, last + 1))
    return sorted(years)


def load_token(explicit: Path | None) -> str:
    if explicit is None:
        if env := os.environ.get("EARTHDATA_TOKEN", "").strip():
            token = env
        else:
            path = DEFAULT_TOKEN_FILE
            if not path.is_file():
                raise RuntimeError(f"Earthdata token not found at {path}")
            token = path.read_text(encoding="utf-8").strip()
    else:
        token = explicit.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("Earthdata token file is empty")

    # Decode JWT exp field for early friendly warning
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


def search_granules_year(year: int, timeout: int = 30) -> list[tuple[str, str]]:
    """Query public CMR for (YYYYMMDD, granule_title) pairs in a calendar year."""
    out: dict[str, str] = {}
    page = 1
    while True:
        r = requests.get(
            CMR_GRANULES,
            params={
                "collection_concept_id": COLLECTION,
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
            if m and m.group(1).startswith(str(year)):
                out[m.group(1)] = title
        if len(entries) < 500:
            break
        page += 1
    return sorted(out.items())


def resolve_grid_window(sample_title: str, bbox: tuple[float, float, float, float], token: str) -> GridWindow:
    """Read 1-D lat and lon coordinates once from OPeNDAP to compute exact index bounds."""
    import numpy as np

    headers = {"Authorization": f"Bearer {token}"}
    axes: dict[str, np.ndarray] = {}
    for axis in ("lat", "lon"):
        q = urllib.parse.urlencode({"dap4.ce": f"/{axis}"})
        url = f"{OPENDAP_BASE}/{sample_title}.dap.csv?{q}"
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code in (401, 403):
            raise RuntimeError(f"Auth failed (HTTP {r.status_code}) on OPeNDAP. Refresh ~/.edl_token.")
        r.raise_for_status()
        vals = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line or not (line[0].isdigit() or line[0] == "-"):
                continue
            vals.append(float(line.split(",")[-1]))
        axes[axis] = np.asarray(vals, dtype=np.float64)

    west, east, south, north = bbox
    lat_idx = np.nonzero((axes["lat"] >= south) & (axes["lat"] <= north))[0]
    lon_idx = np.nonzero((axes["lon"] >= west) & (axes["lon"] <= east))[0]
    if lat_idx.size == 0 or lon_idx.size == 0:
        raise RuntimeError(f"Bbox {bbox} falls outside ACSPO grid")
    return GridWindow(int(lat_idx.min()), int(lat_idx.max()), int(lon_idx.min()), int(lon_idx.max()))


def build_ce(variables: tuple[str, ...], win: GridWindow) -> str:
    box = f"[{win.lat_i0}:1:{win.lat_i1}][{win.lon_j0}:1:{win.lon_j1}]"
    parts = [f"/{v}[0:1:0]{box}" for v in variables]
    parts.extend([f"/lat[{win.lat_i0}:1:{win.lat_i1}]", f"/lon[{win.lon_j0}:1:{win.lon_j1}]", "/time[0:1:0]"])
    return ";".join(parts)


def verify_nc4(path: Path, variables: tuple[str, ...]) -> bool:
    import h5py

    try:
        with h5py.File(path, "r") as f:
            return all(v in f for v in (*variables, "lat", "lon", "time"))
    except OSError:
        return False


def download_one(
    day_str: str,
    title: str,
    target: Path,
    ce: str,
    variables: tuple[str, ...],
    token: str,
    retries: int,
    timeout: int,
) -> tuple[str, int]:
    if target.exists() and verify_nc4(target, variables):
        return "skipped", 0

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.{os.getpid()}.part")
    url = f"{OPENDAP_BASE}/{title}.dap.nc4?{urllib.parse.urlencode({'dap4.ce': ce})}"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}

    last_err = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
                if r.status_code in (401, 403):
                    raise RuntimeError(f"Fatal HTTP {r.status_code} (check token / PO.DAAC EULA)")
                r.raise_for_status()
                with partial.open("wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        if chunk:
                            f.write(chunk)
            if verify_nc4(partial, variables):
                size = partial.stat().st_size
                partial.replace(target)
                return "done", size
            last_err = "HDF5 verification failed"
        except RuntimeError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_err = str(exc)
        partial.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(min(30, 2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(last_err)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=f"Download regional PO.DAAC {SHORT_NAME} via OPeNDAP.")
    p.add_argument("years", type=parse_years, help="Years, e.g. 2016-2025 or 2024")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "E", "S", "N"), default=list(DEFAULT_BBOX))
    p.add_argument("-v", "--variables", nargs="+", default=list(DEFAULT_VARS))
    p.add_argument("-o", "--output-dir", type=Path, default=Path("data/acspo_l3s"))
    p.add_argument("--token-file", type=Path, default=None, help="Earthdata token file (default ~/.edl_token)")
    p.add_argument("-j", "--jobs", type=int, default=4, help="Concurrent daily downloads (1-8)")
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("-n", "--dry-run", action="store_true", help="Query CMR granule list only (no token needed)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    jobs = max(1, min(8, args.jobs))
    variables = tuple(args.variables)

    all_granules: list[tuple[int, str, str]] = []
    for y in args.years:
        items = search_granules_year(y)
        print(f"  CMR {SHORT_NAME} {y}: {len(items)} daily granules")
        all_granules.extend((y, d, t) for d, t in items)

    if not all_granules:
        print("No granules found.")
        return 1

    if args.dry_run:
        est_mb = len(all_granules) * 1.5
        print(f"DRY-RUN total: {len(all_granules)} days | bbox={args.bbox} | est regional output ~{est_mb/1024:.2f} GB")
        return 0

    token = load_token(args.token_file)
    win = resolve_grid_window(all_granules[0][2], tuple(args.bbox), token)
    ce = build_ce(variables, win)
    print(f"Resolved OPeNDAP window: lat[{win.lat_i0}:{win.lat_i1}], lon[{win.lon_j0}:{win.lon_j1}]")

    counts = {"done": 0, "skipped": 0, "failed": 0}
    total_bytes = 0
    with futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        fut_map = {
            ex.submit(
                download_one, d, t,
                args.output_dir / f"{y}" / f"ACSPO_L3S_{d}.nc4",
                ce, variables, token, args.retries, args.timeout,
            ): d
            for y, d, t in all_granules
        }
        for fut in tqdm(futures.as_completed(fut_map), total=len(fut_map), unit="day"):
            d = fut_map[fut]
            try:
                st, nb = fut.result()
                counts[st] += 1
                total_bytes += nb
            except Exception as exc:
                counts["failed"] += 1
                print(f"[ERR] {d}: {exc}", file=sys.stderr)

    print(f"Done: {counts}, downloaded {total_bytes/1e6:.1f} MB")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
