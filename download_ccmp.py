#!/usr/bin/env python3
"""RSS CCMP V3.1: subset daily global files, optionally save strict UTC daily means."""
from __future__ import annotations
import argparse
import calendar
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
import numpy as np
import xarray as xr

BASE = "https://data.remss.com/ccmp/v03.1/"
VARIABLES = ("uwnd", "vwnd", "nobs")
PATTERN = r'CCMP_Wind_Analysis_(\d{8})_V03\.1_L4\.nc(?=["<])'


def validate_bbox(bbox):
    w, e, s, n = bbox
    if not all(np.isfinite(bbox)) or not (0 <= w < e <= 360 and -90 <= s < n <= 90):
        raise ValueError("bbox order W E S N; longitude 0..360, no crossing zero")
    for low, high, origin in ((w, e, .125), (s, n, -89.875)):
        first = origin + np.ceil((low - origin) / .25) * .25
        if first > high:
            raise ValueError("bbox contains no CCMP 0.25 degree grid centres")


def retry(operation, attempts=3):
    for attempt in range(attempts):
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError("RSS access denied; follow official registration instructions.") from exc
            if exc.code in (400, 404) or attempt + 1 == attempts:
                raise
        except (OSError, ValueError):
            if attempt + 1 == attempts:
                raise
        print(f"Retry {attempt + 2}/{attempts}", flush=True)
        time.sleep(min(2 ** attempt, 20))


def listing(url, attempts):
    def fetch():
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read(2 * 1024 * 1024).decode("utf-8")
    return retry(fetch, attempts)


def month_files(year, month, attempts):
    url = f"{BASE}Y{year}/M{month:02d}/"
    try:
        text = listing(url, attempts)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    return {dt.datetime.strptime(day, "%Y%m%d").date(): url + f"CCMP_Wind_Analysis_{day}_V03.1_L4.nc"
            for day in re.findall(PATTERN, text)}


def latest_complete_year(attempts):
    years = sorted({int(y) for y in re.findall(r"Y(\d{4})/", listing(BASE, attempts))}, reverse=True)
    for year in years:
        if year >= dt.date.today().year:
            continue
        available = set()
        for month in range(1, 13):
            available.update(month_files(year, month, attempts))
        expected = {dt.date(year, 1, 1) + dt.timedelta(days=i) for i in range(365 + calendar.isleap(year))}
        if available == expected:
            print(f"Latest complete year verified against all daily filenames: {year}")
            return year
    raise ValueError("No complete calendar year found")


def requested_days(args):
    if args.start_date or args.end_date:
        if args.years or not (args.start_date and args.end_date):
            raise ValueError("Provide both --start-date and --end-date, without years")
        ranges = [(dt.date.fromisoformat(args.start_date), dt.date.fromisoformat(args.end_date))]
    else:
        text = args.years or "2001-latest"
        if "latest" in text:
            text = text.replace("latest", str(latest_complete_year(args.retries)))
        ranges = []
        for part in text.split(","):
            match = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", part)
            if not match:
                raise ValueError("Expected years: 2001, 2001-2005, 2001,2003 or 2001-latest")
            ranges.append((dt.date(int(match[1]), 1, 1), dt.date(int(match[2] or match[1]), 12, 31)))
    days = set()
    for start, end in ranges:
        if start > end or start < dt.date(1993, 1, 1) or end >= dt.date.today():
            raise ValueError("Invalid/reversed range, before 1993, or including today/future")
        days.update(start + dt.timedelta(days=i) for i in range((end - start).days + 1))
    return sorted(days)


def validate(ds, day, bbox=None, daily=False):
    if str(ds.attrs.get("product_version")) != "3.1":
        raise ValueError("Expected CCMP version 3.1")
    names = ("uwnd", "vwnd", "nobs_sum", "valid_sample_count") if daily else VARIABLES
    for name in names:
        if name not in ds or ds[name].dims != ("time", "latitude", "longitude"):
            raise ValueError(f"Missing variable or wrong dimensions: {name}")
    for name in ("uwnd", "vwnd"):
        if ds[name].attrs.get("units") not in ("m s-1", "m/s"):
            raise ValueError(f"Unexpected units: {name}")
    expected_time = np.datetime64(day, "ns") + np.array([12] if daily else [0, 6, 12, 18], dtype="timedelta64[h]")
    if not np.array_equal(ds.time.values, expected_time):
        raise ValueError("Expected exact UTC 00/06/12/18 analyses or daily noon timestamp")
    for coord, unit, low, high in (("latitude", "degrees_north", -89.875, 89.875), ("longitude", "degrees_east", .125, 359.875)):
        actual = ds[coord].values
        if ds[coord].attrs.get("units") != unit or actual.ndim != 1 or not len(actual) or not np.all(np.isfinite(actual)):
            raise ValueError(f"Invalid coordinate: {coord}")
        expected = np.arange(low, high + .125, .25)
        if bbox:
            lower, upper = (bbox[2], bbox[3]) if coord == "latitude" else (bbox[0], bbox[1])
            expected = expected[(expected >= lower) & (expected <= upper)]
        if not np.array_equal(actual, expected):
            raise ValueError(f"Incomplete or unexpected 0.25 degree grid: {coord}")
    ds[list(names)].load()
    for name in names:
        if np.any(np.isinf(ds[name].values)):
            raise ValueError(f"Unexpected infinite values: {name}")
    if daily:
        expected_bounds = np.array([[np.datetime64(day), np.datetime64(day) + np.timedelta64(1, "D")]], dtype="datetime64[ns]")
        if "time_bounds" not in ds or not np.array_equal(ds.time_bounds.values, expected_bounds):
            raise ValueError("Daily bounds must span exactly the requested UTC day")
        count = ds.valid_sample_count.values
        if np.any(~np.isfinite(count)) or np.any((count < 0) | (count > 4) | (count != np.floor(count))):
            raise ValueError("Invalid daily valid_sample_count")
        both_finite = np.isfinite(ds.uwnd.values) & np.isfinite(ds.vwnd.values)
        if not np.array_equal(both_finite, count == 4):
            raise ValueError("Daily winds disagree with valid_sample_count")
        for name in ("nobs_sum", "valid_sample_count"):
            if ds[name].attrs.get("units") != "1":
                raise ValueError(f"Expected dimensionless daily variable: {name}")
        finite = ds.nobs_sum.values[np.isfinite(ds.nobs_sum.values)]
        if np.any((finite < 0) | (finite > 400) | (finite != np.floor(finite))):
            raise ValueError("nobs_sum outside valid integer range 0..400")
    if not daily:
        if ds.nobs.attrs.get("units", "1") not in ("1", "", "unitless", "dimensionless"):
            raise ValueError("nobs must be dimensionless (RSS source omits units)")
        nobs = ds.nobs.values
        finite = nobs[np.isfinite(nobs)]
        if np.any(finite < 0) or np.any(finite > 100) or np.any(finite != np.floor(finite)):
            raise ValueError("nobs outside valid integer range 0..100")


def daily_mean(ds):
    """Equal weights for four analyses; any missing time makes that cell missing."""
    day = ds.time.values[0].astype("datetime64[D]")
    expected = day + np.array([0, 6, 12, 18], dtype="timedelta64[h]")
    if not np.array_equal(ds.time.values, expected):
        raise ValueError("Reject missing, duplicate or cross-day analysis times")
    stamp = [day.astype("datetime64[ns]") + np.timedelta64(12, "h")]
    result = xr.Dataset(attrs=dict(ds.attrs))
    for name in ("uwnd", "vwnd"):
        result[name] = ds[name].mean("time", skipna=False, keep_attrs=True).expand_dims(time=stamp)
        result[name].attrs["cell_methods"] = "time: mean (four equally weighted analyses at 00, 06, 12, 18 UTC; no interpolation)"
    result["valid_sample_count"] = (ds.uwnd.notnull() & ds.vwnd.notnull()).sum("time").expand_dims(time=stamp).astype("int8")
    result.valid_sample_count.attrs = {"long_name": "number of times with both wind components finite", "units": "1"}
    result["nobs_sum"] = ds.nobs.sum("time", skipna=False, keep_attrs=True).expand_dims(time=stamp)
    result.nobs_sum.attrs = {"long_name": "sum of nobs over four analyses; not unique daily satellite observations", "units": "1"}
    for name in ("uwnd", "vwnd"):
        result[name].attrs["ancillary_variables"] = "nobs_sum valid_sample_count"
    result["time_bounds"] = (("time", "bounds"), np.array([[day, day + np.timedelta64(1, "D")]], dtype="datetime64[ns]"))
    result.time.attrs = {"standard_name": "time", "bounds": "time_bounds"}
    return result


def save_product(ds, path, day, bbox, source, daily):
    fingerprint = json.dumps({"source": source, "bbox_W_E_S_N": list(bbox), "day": str(day), "daily_mean": daily, "schema": 1}, sort_keys=True)
    if path.exists():
        with xr.open_dataset(path) as existing:
            validate(existing, day, bbox, daily)
            if existing.attrs.get("download_request") != fingerprint:
                raise ValueError(f"Existing request mismatch; choose another output directory: {path}")
        print(f"Skip validated file {path}", flush=True)
        return
    if ds is None:
        return False
    out = ds.copy()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    out.attrs.update(download_request=fingerprint, download_time_utc=now, download_source=source,
                     processing_method="UTC four-analysis arithmetic mean, skipna=False" if daily else "native 6-hour analyses, spatial subset only",
                     geospatial_lat_min=float(out.latitude.min()), geospatial_lat_max=float(out.latitude.max()),
                     geospatial_lon_min=float(out.longitude.min()), geospatial_lon_max=float(out.longitude.max()))
    for coord in ("latitude", "longitude"):
        out[coord].attrs["actual_range"] = np.array([float(out[coord].min()), float(out[coord].max())])
    out.attrs["history"] = out.attrs.get("history", "") + f"\n{now}: {out.attrs['processing_method']} by download_ccmp.py"
    for key in ("valid_min", "valid_max", "delta_t"):
        out.time.attrs.pop(key, None)
    encoding = {name: {"zlib": True, "complevel": 4} for name in out.data_vars}
    encoding["time"] = {"units": f"hours since {day} 00:00:00", "calendar": "proleptic_gregorian"}
    if daily:
        encoding["time_bounds"].update(encoding["time"])
    temp = path.with_suffix(".nc.part")
    out.to_netcdf(temp, engine="netcdf4", encoding=encoding)
    with xr.open_dataset(temp) as check:
        validate(check, day, bbox, daily)
    if path.exists():
        raise ValueError(f"Destination appeared while writing; refusing overwrite: {path}")
    temp.rename(path)
    print(f"Saved {path} ({path.stat().st_size:,} bytes)", flush=True)
    return True


def process(day, url, args):
    paths = [args.output / f"ccmp_v3.1_{day:%Y%m%d}_6hourly.nc"]
    if args.daily_mean:
        paths.append(args.output / f"ccmp_v3.1_{day:%Y%m%d}_daily.nc")
    needed = [save_product(None, path, day, args.bbox, url, i == 1) is False for i, path in enumerate(paths)]
    if not any(needed):
        return
    # A validated regional source is sufficient to recreate only the missing mean.
    if not needed[0]:
        with xr.open_dataset(paths[0]) as subset:
            save_product(daily_mean(subset), paths[1], day, args.bbox, url, True)
        return
    rawdir = args.output / "raw"
    rawdir.mkdir(parents=True, exist_ok=True)
    raw = rawdir / url.rsplit("/", 1)[1]
    if raw.exists():
        with xr.open_dataset(raw) as check:
            validate(check, day)
    else:
        def fetch():
            part = raw.with_suffix(".nc.part")
            with urllib.request.urlopen(url, timeout=90) as response, part.open("wb") as stream:
                expected = response.headers.get("Content-Length")
                total = 0
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
                    total += len(chunk)
            if expected and total != int(expected):
                raise ValueError("Download size differs from Content-Length")
            with xr.open_dataset(part) as check:
                validate(check, day)
            if raw.exists():
                raise ValueError("Global destination appeared while downloading; refusing overwrite")
            part.rename(raw)
        retry(fetch, args.retries)
    with xr.open_dataset(raw) as ds:
        subset = ds[list(VARIABLES)].sel(longitude=slice(*args.bbox[:2]), latitude=slice(*args.bbox[2:])).load()
        validate(subset, day, args.bbox)
        if needed[0]:
            save_product(subset, paths[0], day, args.bbox, url, False)
        if args.daily_mean and needed[1]:
            save_product(daily_mean(subset), paths[1], day, args.bbox, url, True)
    if not args.keep_global:
        raw.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("years", nargs="?", help="2001-2005 / 2001,2003 / 2001-latest (default)")
    parser.add_argument("--start-date", help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end-date", help="YYYY-MM-DD, inclusive")
    parser.add_argument("--bbox", nargs=4, type=float, default=[105., 125., 0., 25.], metavar=("W", "E", "S", "N"))
    parser.add_argument("-o", "--output", "--output-dir", type=Path, default=Path("ccmp"))
    parser.add_argument("-n", "--dry-run", action="store_true", help="Directory metadata and one HEAD only; no NetCDF")
    parser.add_argument("--daily-mean", action="store_true", help="Also save UTC four-analysis daily mean")
    parser.add_argument("--keep-global", action="store_true", help="Keep validated global source (default: remove after crop)")
    parser.add_argument("--retries", type=int, default=3, help="Total attempts (default 3)")
    args = parser.parse_args(argv)
    try:
        validate_bbox(args.bbox)
        if args.retries < 1:
            raise ValueError("--retries must be >= 1")
        days = requested_days(args)
        files = {}
        print("Checking RSS monthly directories (metadata only)...", flush=True)
        for year, month in sorted({(d.year, d.month) for d in days}):
            files.update(month_files(year, month, args.retries))
        missing = [str(d) for d in days if d not in files]
        if missing:
            raise ValueError(f"Missing {len(missing)} daily files; download not started. Examples: {missing[:8]}")
        print(f"CCMP V3.1: {days[0]}..{days[-1]}, {len(days)} days, bbox W E S N={args.bbox}")
        if args.dry_run:
            def head():
                with urllib.request.urlopen(urllib.request.Request(files[days[0]], method="HEAD"), timeout=60) as response:
                    return int(response.headers.get("Content-Length", 0))
            size = retry(head, args.retries)
            print(f"First global day {size:,} bytes; rough network {size * len(days) / 1024**3:.3f} GiB (size varies).")
            print("One global day at a time then crop; reserve >=100MiB temporary disk. Global files removed by default.")
            print("Native 00/06/12/18 UTC preserved; daily mean=" + str(args.daily_mean))
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        for index, day in enumerate(days, 1):
            print(f"[{index}/{len(days)}] {day}", flush=True)
            process(day, files[day], args)
        return 0
    except KeyboardInterrupt:
        print("Interrupted; completed files retained, current partial request will restart.", file=sys.stderr)
        return 130
    except urllib.error.HTTPError as exc:
        print(f"HTTP error {exc.code}; no credentials or request URL printed.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"I/O failure: {type(exc).__name__}; retry the same command.", file=sys.stderr)
        return 1
    except RuntimeError:
        print("Runtime failure; verify NetCDF input and RSS access, then retry.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
