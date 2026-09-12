#!/usr/bin/env python3
"""Download ESA CCI v5.5 regional SSS using CEDA DAP2 hyperslabs."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET

import numpy as np
import requests
import xarray as xr

VERSION = "5.5"
ROOT = "https://data.cci.ceda.ac.uk/thredds/"
PRODUCT = "esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days"
VARIABLES = ("sss", "sss_random_error", "noutliers", "total_nobs", "pct_var",
             "sss_qc", "lsc_qc", "isc_qc")
NS = {"c": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"}
ATTR = "cci_sss_download_request"


class DownloadError(ValueError):
    """Safe user-facing failure."""


def parse_years(text):
    years = set()
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d{4})(?:-(\d{4}))?\s*", part)
        if not m or not 1900 <= int(m[1]) <= int(m[2] or m[1]) <= 9998:
            raise argparse.ArgumentTypeError("Use 2010-2023 or 2010,2012")
        years.update(range(int(m[1]), int(m[2] or m[1]) + 1))
    return sorted(years)


def catalog(session, path, retries, timeout):
    for attempt in range(retries):
        try:
            response = session.get(ROOT + "catalog/" + path + "/catalog.xml", timeout=timeout)
            response.raise_for_status()
            return ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError) as exc:
            if attempt == retries - 1:
                raise DownloadError(f"CEDA catalog failed ({type(exc).__name__})") from None
            time.sleep(min(2 ** attempt, 30))


def entries(tree):
    result = {}
    for node in tree.findall(".//c:dataset[@urlPath]", NS):
        path = node.attrib["urlPath"]
        match = re.search(r"-(\d{8})-fv5\.5\.nc$", path)
        if match:
            stamp = datetime.strptime(match[1], "%Y%m%d").date()
            size = node.find("c:dataSize", NS)
            factors = {"bytes": 1, "Kbytes": 1000, "Mbytes": 1000000, "Gbytes": 1000000000}
            nbytes = float(size.text) * factors[size.attrib["units"]] if size is not None else 0
            result[stamp] = (path, nbytes)
    return result


def days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def grid(bbox):
    w, e, s, n = bbox
    lat = np.arange(-89.875, 90, .25)
    lon = np.arange(-179.875, 180, .25)
    return {"lat": lat[(lat >= s) & (lat <= n)], "lon": lon[(lon >= w) & (lon <= e)]}


def complete_default_tasks(inventory, today):
    """Keep the actual product start, but never silently skip interior gaps."""
    nonempty = {year: items for year, items in inventory.items() if items and year >= 2001}
    if not nonempty:
        raise DownloadError("No product dates available from 2001")
    first = max(date(2001, 1, 1), min(d for items in nonempty.values() for d in items))
    complete = [year for year, items in nonempty.items()
                if year < today.year and set(days(date(year, 1, 1), date(year, 12, 31))) <= items.keys()]
    if not complete:
        raise DownloadError("No complete calendar year available")
    latest = max(complete)
    all_items = {d: item for items in inventory.values() for d, item in items.items()}
    wanted = days(first, date(latest, 12, 31))
    missing = set(wanted) - all_items.keys()
    if missing:
        raise DownloadError(f"Interior coverage gap: {len(missing)} days, first {min(missing)}; no files downloaded")
    for year in sorted(y for y in inventory if y > latest):
        logging.warning("Exclude incomplete/current year %s", year)
    logging.info("Product request starts %s; latest complete year %s", first, latest)
    return {d: all_items[d] for d in wanted}


def validate(ds, stamp, expected_grid, signature=None):
    if str(ds.attrs.get("product_version")) != VERSION:
        raise DownloadError("Unexpected product_version")
    if signature is not None and ds.attrs.get(ATTR) != signature:
        raise DownloadError("Existing request/version/provenance differs; choose another output directory")
    if ("time" not in ds.coords or ds.time.dims != ("time",)
            or ds.sizes.get("time") != 1 or ds.time.values[0] != np.datetime64(stamp)):
        raise DownloadError("Unexpected time coordinate")
    for name, values in expected_grid.items():
        if (name not in ds.coords or ds[name].dims != (name,) or not len(values)
                or not np.array_equal(ds[name].values, values)):
            raise DownloadError(f"Unexpected or incomplete {name} grid")
    if ds.lat.attrs.get("units") != "degrees_north" or ds.lon.attrs.get("units") != "degrees_east":
        raise DownloadError("Unexpected coordinate units")
    for name in VARIABLES:
        if name not in ds or ds[name].dims != ("time", "lat", "lon"):
            raise DownloadError(f"Missing variable or unexpected dimensions: {name}")
        ds[name].load()  # Decode the complete payload; land/ice NaNs remain valid.
        if np.isinf(ds[name].values).any():
            raise DownloadError(f"Infinite values: {name}")
    for name in ("sss", "sss_random_error"):
        if ds[name].attrs.get("units") != "0.001":
            raise DownloadError(f"Unexpected units: {name}")
    if ds.pct_var.attrs.get("units") != "%":
        raise DownloadError("Unexpected pct_var units")
    for name in ("noutliers", "total_nobs", "sss_qc", "lsc_qc", "isc_qc"):
        values = ds[name].values
        finite = values[np.isfinite(values)]
        upper = 1 if name.endswith("_qc") else 10000
        if (ds[name].attrs.get("units", "1") not in ("1", "")
                or np.any(finite < 0) or np.any(finite > upper)
                or np.any(finite != np.floor(finite))):
            raise DownloadError(f"Invalid count/quality flag: {name}")
    if ds.attrs.get("time_coverage_duration") != "P7D" or ds.attrs.get("time_coverage_resolution") != "P1D":
        raise DownloadError("Unexpected temporal resolution/window")


def download(session, stamp, path, args):
    expected = grid(args.bbox)
    source = ROOT + "dodsC/" + path
    signature = json.dumps(dict(version=VERSION, source=source, date=stamp.isoformat(),
                                bbox=args.bbox, variables=VARIABLES, method="DAP2 spatial subset; no temporal averaging"),
                           sort_keys=True, separators=(",", ":"))
    target = args.out / str(stamp.year) / Path(path).name
    if target.exists():
        with xr.open_dataset(target, decode_timedelta=False) as ds:
            validate(ds, stamp, expected, signature)
        logging.info("SKIP %s", target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".nc.part")
    for attempt in range(args.retries):
        try:
            # pydap uses requests (including configured proxy/CA settings), unlike libcurl.
            with xr.open_dataset(source, engine="pydap", decode_timedelta=False,
                                 backend_kwargs={"session": session, "timeout": args.timeout}) as remote:
                w, e, s, n = args.bbox
                selected = remote[list(VARIABLES)].sel(lat=slice(s, n), lon=slice(w, e)).load()
                validate(selected, stamp, expected)
                selected.attrs.update({ATTR: signature,
                    "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
                    "download_source_url": source,
                    "processing_method": "CEDA OPeNDAP spatial hyperslab; original 7-day running mean retained; no interpolation or daily aggregation",
                    "source_geospatial_extent": json.dumps({k: remote.attrs.get(k) for k in
                        ("geospatial_lat_min", "geospatial_lat_max", "geospatial_lon_min", "geospatial_lon_max")}),
                    "geospatial_lat_min": float(expected["lat"][0]), "geospatial_lat_max": float(expected["lat"][-1]),
                    "geospatial_lon_min": float(expected["lon"][0]), "geospatial_lon_max": float(expected["lon"][-1])})
                # Remove upstream global chunk sizes; retain scientific attrs and fill values.
                for variable in selected.variables.values():
                    variable.encoding = {k: v for k, v in variable.encoding.items()
                                         if k in ("_FillValue", "dtype", "units", "calendar")}
                    variable.attrs.pop("_ChunkSizes", None)
                selected.to_netcdf(partial, engine="netcdf4")
            with xr.open_dataset(partial, decode_timedelta=False) as check:
                validate(check, stamp, expected, signature)
            if target.exists():
                raise DownloadError("Output appeared during download; refusing to overwrite")
            partial.rename(target)
            logging.info("SAVED %s (%d bytes)", target, target.stat().st_size)
            return
        except Exception as exc:
            if partial.exists():
                partial.unlink()
            if attempt == args.retries - 1:
                raise DownloadError(f"Download failed for {stamp}: {type(exc).__name__}; rerun resumes at unfinished files") from None
            logging.warning("Retry %d/%d for %s (%s)", attempt + 1, args.retries, stamp, type(exc).__name__)
            time.sleep(min(2 ** attempt, 30))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("year_range", nargs="?", type=parse_years, help="Years, e.g. 2011-2022")
    p.add_argument("--years", type=parse_years)
    p.add_argument("--start-date", type=date.fromisoformat)
    p.add_argument("--end-date", type=date.fromisoformat)
    p.add_argument("--all-available", action="store_true", help="Discover coverage from first available date through latest complete calendar year")
    p.add_argument("--bbox", nargs=4, type=float, default=[105., 125., 0., 25.], metavar=("WEST", "EAST", "SOUTH", "NORTH"))
    p.add_argument("-o", "--output-dir", "--out", dest="out", type=Path, default=Path("data/cci_sss"))
    p.add_argument("--retries", type=int, default=3, help="Maximum attempts including the initial attempt")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("-n", "--dry-run", action="store_true", help="Query only small catalogs; no data payload")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.year_range and args.years:
        p.error("Use positional years or --years, not both")
    args.years = args.years or args.year_range
    if not any([args.years, args.start_date, args.end_date, args.all_available]):
        args.all_available = True
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    w, e, s, n = args.bbox
    if not all(np.isfinite(args.bbox)) or not -180 <= w < e <= 180 or not -90 <= s < n <= 90:
        p.error("bbox must be WEST EAST SOUTH NORTH, within [-180,180] / [-90,90]; no dateline crossing")
    if not all(len(v) for v in grid(args.bbox).values()):
        p.error("bbox contains no 0.25-degree grid centers")
    if args.retries < 1 or args.timeout < 1:
        p.error("retries and timeout must be >0")
    if sum([bool(args.years), bool(args.start_date or args.end_date), args.all_available]) != 1:
        p.error("Choose --years, --start-date with --end-date, or --all-available")
    if bool(args.start_date) != bool(args.end_date) or (args.start_date and args.start_date > args.end_date):
        p.error("Both ordered start/end dates are required")
    try:
        with requests.Session() as session:
            root = catalog(session, PRODUCT, args.retries, args.timeout)
            available = sorted(int(x.attrib["{http://www.w3.org/1999/xlink}title"])
                               for x in root.findall(".//c:catalogRef", NS))
            logging.info("CEDA v%s catalog years %s; 0.25 degree grid, 50 km resolution, 7-day running mean sampled daily", VERSION, available)
            if not available:
                raise DownloadError("No product year catalogs found")
            years = args.years or (list(range(args.start_date.year, args.end_date.year + 1)) if args.start_date else available)
            if any(y not in available for y in years):
                raise DownloadError(f"Requested years unavailable; server lists {available}")
            tasks = {}
            inventory = {}
            for year in years:
                listed = entries(catalog(session, PRODUCT + f"/{year}", args.retries, args.timeout))
                if any(d.year != year for d in listed):
                    raise DownloadError("Catalog contains dates outside its year")
                inventory[year] = listed
                if args.all_available:
                    continue
                wanted = days(date(year, 1, 1), date(year, 12, 31))
                if args.start_date:
                    wanted = [d for d in wanted if args.start_date <= d <= args.end_date]
                missing = set(wanted) - listed.keys()
                if missing:
                    raise DownloadError(f"Missing {len(missing)} requested dates in {year}, first {min(missing)}; not fabricated")
                tasks.update({d: listed[d] for d in wanted})
            if args.all_available:
                tasks = complete_default_tasks(inventory, date.today())
            if not tasks:
                raise DownloadError("No complete requested coverage")
            cells = len(grid(args.bbox)["lat"]) * len(grid(args.bbox)["lon"])
            logging.info("PLAN %d files %s..%s; bbox W E S N=%s; regional numeric payload ~%.2f MiB; global originals total %.2f GiB (not downloaded)",
                         len(tasks), min(tasks), max(tasks), args.bbox, len(tasks) * cells * 28 / 2**20,
                         sum(v[1] for v in tasks.values()) / 2**30)
            logging.info("DAP transfers regional arrays plus headers/coordinates; no byte resume, retry one day; one .part file at a time")
            if not args.dry_run:
                for stamp, (path, _) in sorted(tasks.items()):
                    download(session, stamp, path, args)
        return 0
    except KeyboardInterrupt:
        logging.error("Interrupted; rerun resumes unfinished files")
        return 130
    except Exception as exc:
        logging.error("%s", str(exc) if isinstance(exc, DownloadError) else f"{type(exc).__name__}; inspect configuration/connectivity (sensitive details suppressed)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
