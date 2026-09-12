#!/usr/bin/env python3
"""CDS ERA5 hourly surface energy, with optional UTC daily mean flux."""
from __future__ import annotations
import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
import importlib
import errno
import json
import logging
import math
from pathlib import Path
import re
import sys
import time
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

DATASET = "reanalysis-era5-single-levels"
CATALOGUE = f"https://cds.climate.copernicus.eu/api/catalogue/v1/collections/{DATASET}"
VARIABLES = dict(sshf="surface_sensible_heat_flux", slhf="surface_latent_heat_flux",
                 ssr="surface_net_solar_radiation", str="surface_net_thermal_radiation")
SEMANTICS = "https://confluence.ecmwf.int/plugins/viewsource/viewpagesrc.action?pageId=462888259"
REQUEST_ATTR = "era5_flux_request"
LOG = logging.getLogger("era5_flux")
DAILY_PROCESSING = "sum 24 hourly accumulations D01..D+1 00 UTC / 86400 s; NaN propagates; no differencing"
DAILY_REFERENCE = "UTC day beginning at coordinate; mean over [D00,D+1 00)"
DAILY_CELL_METHODS = "time: mean (interval: 1 day)"

class DownloadError(ValueError):
    pass

def failure_details(exc):
    """Return an actionable, credential-safe diagnosis and retry decision."""
    import requests
    if isinstance(exc, DownloadError):
        return str(exc), False
    if isinstance(exc, ModuleNotFoundError):
        name = exc.name if exc.name and re.fullmatch(r"[A-Za-z0-9_.]+", exc.name) else "dependency"
        return f"Missing Python module {name}; use this Python's -m pip install -r requirements.txt", False
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return f"HTTP {status}: check CDS credentials and accept this dataset's licence", False
        if status in (408, 425, 429, 500, 502, 503, 504):
            return f"HTTP {status}: temporary CDS service/rate-limit failure", True
        return f"HTTP {status}: CDS rejected the request; check dataset/request availability", False
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS certificate failure; check proxy certificates and system clock", False
    if isinstance(exc, requests.exceptions.ProxyError):
        return "Proxy connection failed; check this terminal's HTTP_PROXY/HTTPS_PROXY and proxy listener", True
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return "Network request timed out (this is not a CDS queue time limit)", True
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, ConnectionError)):
        return "Network connection interrupted; check connectivity/proxy and retry", True
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EACCES, errno.EROFS, errno.ENOENT):
        return f"Local filesystem failure ({errno.errorcode[exc.errno]}); check output path, space and permissions", False
    return f"{type(exc).__name__}: local processing or CDS client failure; check dependencies, credentials and request settings", False

def preflight(dry_run=False):
    modules = ("requests",) if dry_run else ("requests", "numpy", "xarray", "netCDF4", "cdsapi")
    missing = []
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
        except Exception as exc:
            raise DownloadError(f"Cannot load dependency {name} ({type(exc).__name__}); repair the active Python environment") from None
    if missing:
        raise DownloadError(f"Missing/unusable dependencies: {', '.join(missing)}. In this terminal run: python -m pip install -r requirements.txt")

def parse_years(text):
    result = set()
    for part in text.split(","):
        match = re.fullmatch(r"\s*(\d{4})(?:-(\d{4}))?\s*", part)
        if not match:
            raise argparse.ArgumentTypeError("Use 2001 / 2001-2005 / 2001,2003-2005")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1940 <= first <= last <= 9998:
            raise argparse.ArgumentTypeError("Years must be ordered and >=1940")
        result.update(range(first, last + 1))
    return sorted(result)

def discover(retries=3, timeout=60):
    import requests
    for attempt in range(retries):
        try:
            with requests.get(CATALOGUE, timeout=timeout) as response:
                response.raise_for_status()
                metadata = response.json()
            first, last = metadata["extent"]["temporal"]["interval"][0]
            return date.fromisoformat(first[:10]), date.fromisoformat(last[:10]), metadata.get("updated", "unknown")
        except Exception as exc:
            details, retryable = failure_details(exc)
            if not retryable or attempt + 1 == retries:
                raise DownloadError(f"CDS catalogue unavailable: {details}; no guessed latest year") from None
            LOG.warning("Catalogue attempt %d/%d failed: %s; retrying", attempt + 1, retries, details)
            time.sleep(min(2 ** attempt, 10))

def ranges(args, coverage_start, coverage_end):
    today = datetime.now(timezone.utc).date()
    month_index = today.year * 12 + today.month - 1 - 3
    stable_end = date(month_index // 12, month_index % 12 + 1, 1) - timedelta(days=1)
    limit = min(coverage_end, stable_end)
    if args.start_date or args.end_date:
        if args.years or not (args.start_date and args.end_date):
            raise DownloadError("Use both --start-date and --end-date, without positional years")
        selected = [(args.start_date, args.end_date)]
    else:
        usable_end = limit - timedelta(days=int(args.daily_mean))
        latest = usable_end.year if usable_end.month == 12 and usable_end.day == 31 else usable_end.year - 1
        years = args.years or list(range(max(2001, coverage_start.year), latest + 1))
        selected = [(date(y, 1, 1), date(y, 12, 31)) for y in years]
    for first, last in selected:
        if first > last or first < coverage_start or last + timedelta(days=int(args.daily_mean)) > limit:
            raise DownloadError(f"Range exceeds conservative final-ERA5 coverage {coverage_start}..{limit}; daily mean needs next day")
    if not selected:
        raise DownloadError("No complete years available")
    return selected

def month_chunks(first, last):
    while first <= last:
        end = min(last, date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]))
        yield first, end
        first = end + timedelta(days=1)

def cds_request(first, last, bbox):
    west, east, south, north = bbox
    return dict(product_type=["reanalysis"], variable=list(VARIABLES.values()),
                year=[str(first.year)], month=[f"{first.month:02d}"],
                day=[f"{day:02d}" for day in range(first.day, last.day + 1)],
                time=[f"{hour:02d}:00" for hour in range(24)],
                area=[north, west, south, east], grid=[0.25, 0.25],
                data_format="netcdf", download_format="unarchived")

def signature(first, last, bbox, kind):
    return json.dumps(dict(dataset=DATASET, product_type="reanalysis", grid=0.25,
                           first=str(first), last=str(last), bbox=list(bbox),
                           variables=list(VARIABLES), kind=kind, processing_version=2 if kind == "daily" else 1), sort_keys=True)

def normalized(data):
    if "valid_time" in data.dims:
        data = data.rename(valid_time="time")
    if "expver" in data:
        import numpy as np
        try:
            final = np.all(data.expver.values.astype(float) == 1)
        except (TypeError, ValueError):
            final = False
        if not final:
            raise DownloadError("ERA5T/mixed expver detected; only final ERA5 expver=1 allowed")
        if "expver" in data.dims:
            if data.sizes["expver"] != 1:
                raise DownloadError("Unexpected multiple expver coordinates")
            data = data.isel(expver=0, drop=True)
    return data

def validate(data, first, last, bbox, daily=False):
    import numpy as np
    data = normalized(data)
    step = "D" if daily else "h"
    expected = np.arange(np.datetime64(str(first)), np.datetime64(str(last + timedelta(days=1))), dtype=f"datetime64[{step}]")
    if "time" not in data.coords or not np.array_equal(data.time.values, expected):
        raise DownloadError("Incomplete, duplicated, unordered or unexpected time axis")
    if daily:
        bounds = np.column_stack([expected, expected + np.timedelta64(1, "D")])
        if (data.time.attrs.get("bounds") != "time_bounds" or "time_bounds" not in data
                or data.time_bounds.dims != ("time", "bounds")
                or not np.array_equal(data.time_bounds.values, bounds)):
            raise DownloadError("Missing or incorrect UTC daily time bounds")
        if (data.attrs.get("processing") != DAILY_PROCESSING
                or data.attrs.get("time_reference") != DAILY_REFERENCE
                or data.attrs.get("processing_reference") != SEMANTICS):
            raise DownloadError("Missing or incorrect daily processing provenance")
    west, east, south, north = bbox
    for axis, lo, hi in (("latitude", south, north), ("longitude", west, east)):
        if axis not in data.coords or data[axis].dims != (axis,):
            raise DownloadError(f"Missing/invalid coordinate {axis}")
        coordinate_units = data[axis].attrs.get("units")
        accepted_units = {"degrees_north", "degree_north", "degrees_N", "degree_N"} if axis == "latitude" else {"degrees_east", "degree_east", "degrees_E", "degree_E"}
        if coordinate_units is not None and coordinate_units not in accepted_units:
            raise DownloadError(f"Unexpected coordinate units for {axis}")
        # Accept equivalent western longitudes encoded in the 0..360 convention.
        if axis == "longitude" and lo < 0 and np.any(data[axis].values > hi):
            converted = (data[axis] + 180) % 360 - 180
            if np.all((converted.values >= lo) & (converted.values <= hi)):
                converted.attrs = data[axis].attrs.copy()
                data = data.assign_coords(longitude=converted).sortby("longitude")
        target = np.arange(math.ceil(lo * 4) / 4, math.floor(hi * 4) / 4 + .125, .25)
        if not np.array_equal(np.sort(data[axis].values), target):
            raise DownloadError(f"Unexpected regional 0.25-degree grid: {axis}")
        differences = np.diff(data[axis].values)
        if not (np.all(differences > 0) or np.all(differences < 0)):
            raise DownloadError(f"Unordered regional coordinate: {axis}")
    units = {"W m**-2", "W m-2", "W/m^2"} if daily else {"J m**-2", "J m-2", "J/m^2"}
    for name in VARIABLES:
        if name not in data or set(data[name].dims) != {"time", "latitude", "longitude"}:
            raise DownloadError(f"Missing variable or incorrect dimensions: {name}")
        if data[name].attrs.get("units") not in units:
            raise DownloadError(f"Unexpected units for {name}")
        if daily and (data[name].attrs.get("cell_methods") != DAILY_CELL_METHODS
                      or data[name].attrs.get("positive") != "down"):
            raise DownloadError(f"Unexpected daily aggregation/sign convention for {name}")
        values = data[name].values
        if np.isinf(values).any():
            raise DownloadError(f"Invalid infinite payload for {name}")
    return data

def daily_mean(data, first, last):
    import numpy as np
    import xarray as xr
    data = normalized(data)
    if "time" not in data.coords or data.time.dims != ("time",):
        raise DownloadError("Daily mean requires a one-dimensional time coordinate")
    if not np.all(np.diff(data.time.values) > np.timedelta64(0, "ns")):
        raise DownloadError("Daily mean requires unique, increasing hourly timestamps")
    days = np.arange(str(first), str(last + timedelta(days=1)), dtype="datetime64[D]")
    needed = (days[:, None].astype("datetime64[h]") + np.arange(1, 25).astype("timedelta64[h]")).reshape(-1)
    if not np.isin(needed, data.time.values).all():
        raise DownloadError("Daily mean needs all D01 through D+1 00 UTC timestamps")
    out = xr.Dataset(attrs=data.attrs.copy())
    for name in VARIABLES:
        samples = data[name].sel(time=needed).assign_coords(time=np.repeat(days, 24))
        out[name] = samples.groupby("time").sum(skipna=False) / 86400.0
        source_attrs = dict(data[name].attrs)
        # GRIB accumulation keys and CF energy names no longer describe a mean flux.
        attrs = {key: value for key, value in source_attrs.items()
                 if not key.startswith("GRIB_") and key not in {"standard_name", "long_name", "units", "cell_methods"}}
        attrs.update(units="W m-2", cell_methods=DAILY_CELL_METHODS, positive="down",
                     long_name="UTC daily mean " + VARIABLES[name].replace("_", " "),
                     source_variable_attributes=json.dumps(source_attrs, sort_keys=True, default=str))
        out[name].attrs = attrs
    out.attrs.update(processing=DAILY_PROCESSING, time_reference=DAILY_REFERENCE, processing_reference=SEMANTICS)
    out["time_bounds"] = (("time", "bounds"), np.column_stack([days, days + np.timedelta64(1, "D")]))
    out.time.attrs["bounds"] = "time_bounds"
    out.time.encoding["units"] = "days since 1970-01-01"
    out.time_bounds.encoding["units"] = "days since 1970-01-01"
    return out

def status_callback(message, *args, **kwargs):
    # Whitelist task-state words only; upstream messages may include signed URLs.
    text = str(message)
    if args:
        try:
            text = text % args
        except (TypeError, ValueError):
            return
    found = re.search(r"(?:status|state|request is).*?\b(accepted|queued|running|successful|completed|failed)\b", text, re.I)
    if found:
        LOG.info("CDS task state: %s", found[1].lower())

def suppressed_callback(*args, **kwargs):
    pass

def check_existing(path, first, last, bbox, kind):
    import xarray as xr
    if not path.exists():
        return False
    try:
        with xr.open_dataset(path, decode_timedelta=False) as data:
            if data.attrs.get(REQUEST_ATTR) != signature(first, last, bbox, kind):
                raise DownloadError("Existing request/provenance differs; choose another output directory")
            validate(data, first, last, bbox, kind == "daily")
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"Existing file unreadable ({type(exc).__name__}); move aside before retrying") from None
    LOG.info("Verified complete; skip %s", path)
    return True

def commit(data, path, first, last, bbox, kind):
    import xarray as xr
    if path.exists():
        raise DownloadError("Refusing to overwrite existing output")
    temporary = path.with_suffix(".writing.nc")
    if kind == "daily":
        data.attrs.pop("downloaded_utc", None)
    data.attrs.update({REQUEST_ATTR: signature(first, last, bbox, kind), "source": CATALOGUE,
                       "processed_utc" if kind == "daily" else "downloaded_utc": datetime.now(timezone.utc).isoformat(),
                       "processing_reference": SEMANTICS,
                       "era5_release_policy": "final ERA5 only; exclude recent three calendar months",
                       "flux_positive": "downward; upward ocean heat loss is negative"})
    data.to_netcdf(temporary)
    with xr.open_dataset(temporary, decode_timedelta=False) as test:
        validate(test, first, last, bbox, kind == "daily")
    if path.exists():
        raise DownloadError("Output appeared during download; refusing overwrite")
    temporary.rename(path)
    LOG.info("Saved %s", path)

def download_chunk(client, path, first, last, bbox, retries):
    import xarray as xr
    if check_existing(path, first, last, bbox, "hourly"):
        return
    partial = path.with_suffix(".download.nc")
    for attempt in range(retries):
        try:
            LOG.info("CDS submit/wait %s..%s (attempt %d/%d)", first, last, attempt + 1, retries)
            client.retrieve(DATASET, cds_request(first, last, bbox), str(partial))
            with xr.open_dataset(partial, decode_timedelta=False) as source:
                data = validate(source, first, last, bbox).load()
                data.attrs["processing"] = "Original hourly energy J m-2; timestamp ends preceding hour"
                commit(data, path, first, last, bbox, "hourly")
            partial.unlink(missing_ok=True)
            return
        except Exception as exc:
            if path.exists():
                # A concurrent writer or a cleanup failure must never cause overwrite.
                if check_existing(path, first, last, bbox, "hourly"):
                    return
            details, retryable = failure_details(exc)
            if not retryable or attempt + 1 == retries:
                raise DownloadError(f"CDS chunk {first}..{last} failed: {details}; completed chunks retained") from None
            LOG.warning("CDS chunk %s..%s attempt %d/%d failed: %s; retrying", first, last, attempt + 1, retries, details)
            time.sleep(min(2 ** attempt, 20))

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("years", nargs="?", type=parse_years, help="Default 2001..latest complete final-ERA5 year (catalogue query)")
    p.add_argument("--start-date", type=date.fromisoformat)
    p.add_argument("--end-date", type=date.fromisoformat)
    p.add_argument("--bbox", nargs=4, type=float, default=(105., 125., 0., 25.), metavar=("W", "E", "S", "N"))
    p.add_argument("-o", "--output-dir", type=Path, default=Path("era5_flux"))
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("--daily-mean", action="store_true", help="Also generate UTC daily W m-2; needs adjacent day")
    p.add_argument("--retries", type=int, default=3, help="Maximum total attempts per catalogue/chunk request (>=1)")
    p.add_argument("--timeout", type=float, default=60, help="HTTP request timeout in seconds; does not limit CDS queue/job time")
    return p

def _main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        w, e, s, n = args.bbox
        if not all(math.isfinite(x) for x in args.bbox) or not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
            raise DownloadError("bbox must be W E S N with -180<=W<E<=180, -90<=S<N<=90")
        if any(abs(x * 4 - round(x * 4)) > 1e-8 for x in args.bbox):
            raise DownloadError("Use bbox boundaries aligned to the 0.25-degree grid")
        if args.retries < 1:
            raise DownloadError("retries must be >=1 (maximum total attempts)")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise DownloadError("timeout must be finite and >0")
        preflight(args.dry_run)
        first, last, updated = discover(args.retries, args.timeout)
        selected = ranges(args, first, last)
        # Merge adjacent years before chunking, avoiding duplicate boundary-day requests.
        merged = []
        for start, end in selected:
            if merged and start <= merged[-1][1] + timedelta(days=1):
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        chunks = [chunk for start, end in merged for chunk in month_chunks(start, end + timedelta(days=int(args.daily_mean)))]
        days = sum((end - start).days + 1 for start, end in chunks)
        cells = (round((e-w)*4)+1) * (round((n-s)*4)+1)
        LOG.info("CDS coverage %s..%s, updated %s", first, last, updated)
        LOG.info("Selected %s; %d chunks; bbox W E S N=%s", merged, len(chunks), args.bbox)
        LOG.info("Float32 hourly payload estimate %.3f GiB; largest chunk %.1f MiB; server-side subset; compression/overhead unknown",
                 days*24*cells*4*4/1024**3, min(31, days)*24*cells*4*4/1024**2)
        if args.dry_run:
            LOG.info("Dry-run: only public catalogue queried; no CDS job submitted or data downloaded")
            LOG.info("First request: %s", json.dumps(cds_request(*chunks[0], args.bbox)))
            return 0
        import cdsapi
        try:
            client = cdsapi.Client(quiet=True, debug=False, retry_max=1, sleep_max=10, timeout=args.timeout,
                                   info_callback=status_callback, warning_callback=suppressed_callback,
                                   error_callback=suppressed_callback, debug_callback=suppressed_callback)
        except Exception:
            raise DownloadError("CDS authentication unavailable; configure ~/.cdsapirc or CDSAPI_URL/CDSAPI_KEY and accept dataset terms") from None
        args.output_dir.mkdir(parents=True, exist_ok=True)
        LOG.info("CDS requests can queue before transfer; timeout applies to HTTP only. Ctrl+C retains completed files.")
        paths = []
        for start, end in tqdm(chunks, desc="ERA5 completed", unit="chunk", dynamic_ncols=True, disable=None):
            path = args.output_dir / f"era5_flux_hourly_{start}_{end}.nc"
            download_chunk(client, path, start, end, args.bbox, args.retries)
            paths.append((start, end, path))
        if args.daily_mean:
            import xarray as xr
            for begin, finish in merged:
                for start, end in tqdm(list(month_chunks(begin, finish)), desc="ERA5 daily mean", unit="month", dynamic_ncols=True, disable=None):
                    path = args.output_dir / f"era5_flux_daily_{start}_{end}.nc"
                    if check_existing(path, start, end, args.bbox, "daily"):
                        continue
                    pieces = []
                    for a, b, raw in paths:
                        if a <= end + timedelta(days=1) and b >= start:
                            with xr.open_dataset(raw, decode_timedelta=False) as data:
                                pieces.append(data[list(VARIABLES)].load())
                    combined = xr.concat(pieces, dim="time").sortby("time")
                    commit(daily_mean(combined, start, end), path, start, end, args.bbox, "daily")
        return 0
    except DownloadError as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.error("Interrupted; verified completed files retained")
        return 130
    except Exception as exc:
        # Do not print raw exception strings: CDS can include keys/signed URLs.
        if isinstance(exc, ImportError):
            LOG.error("Dependency import failed (%s); run python -m pip install -r requirements.txt", type(exc).__name__)
        else:
            details, _ = failure_details(exc)
            LOG.error("Operation failed: %s", details)
        return 1

def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with logging_redirect_tqdm():
        return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
