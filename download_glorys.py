#!/usr/bin/env python3
"""调用官方 copernicusmarine.subset 裁剪 GLORYS12V1。

支持直接按年下载、按月并发下载后事务式合并年度文件，或直接保留月文件。
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import contextlib
import errno
import hashlib
import json
import logging
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path


DATASET_ID = "cmems_mod_glo_phy_my_0.083deg_P1D-m"
FILENAME = "GLORYS12V1_SCS_daily_{year}.nc"
MIN_YEAR, MAX_YEAR = 1993, 2025   # 数据到 2026-06-23，2026 不是完整年，不收

DEFAULT_BBOX = (105.0, 125.0, 0.0, 25.0)          # 南海
DEFAULT_VARIABLES = ("thetao", "so", "uo", "vo", "zos")  # 海冰 4 个变量南海全缺测，不要
TMP_DIRNAME = ".glorys_tmp"
PARTS_DIRNAME = "monthly"
PART_FILENAME = "GLORYS12V1_SCS_daily_{year}{month:02d}.nc"


def parse_years(spec: str) -> list[int]:
    """解析 "2001" / "2001-2005" / "2001,2003-2005"。"""
    years: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d{4})(?:\s*-\s*(\d{4}))?", part)
        if not match:
            raise argparse.ArgumentTypeError(
                f"无法解析 {part!r}，可用写法：2001 / 2001-2005 / 2001,2003-2005")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            raise argparse.ArgumentTypeError(f"年份区间反了：{part!r}")
        if start < MIN_YEAR or end > MAX_YEAR:
            raise argparse.ArgumentTypeError(
                f"{part!r} 超出可用范围 {MIN_YEAR}-{MAX_YEAR}")
        years.update(range(start, end + 1))
    if not years:
        raise argparse.ArgumentTypeError("没有指定任何年份")
    return sorted(years)


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("必须 >= 1")
    return value


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def quiet_toolbox() -> None:
    """压掉 toolbox 的 INFO 输出。

    它在 import 时就用 dictConfig 配好了自己的 logger，所以必须先 import 再降级，
    否则设置会被随后的 import 覆盖掉。
    """
    import copernicusmarine  # noqa: F401  只为触发它的 logging 配置

    logging.getLogger("copernicusmarine").setLevel(logging.ERROR)


@dataclass
class Options:
    """一次运行里所有年份共用的请求参数。"""
    bbox: tuple[float, float, float, float]
    variables: list[str]
    min_depth: float | None
    max_depth: float | None
    compression: int


def request_cache_key(options: Options) -> str:
    """隔离不同区域、深度和变量的月分片缓存。"""
    request = {
        "dataset_id": DATASET_ID,
        "bbox": list(options.bbox),
        "variables": sorted(options.variables),
        "min_depth": options.min_depth,
        "max_depth": options.max_depth,
    }
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def subset_kwargs(options: Options, start: date, end: date) -> dict:
    west, east, south, north = options.bbox
    return {
        "dataset_id": DATASET_ID,
        "variables": list(options.variables),
        "minimum_longitude": west,
        "maximum_longitude": east,
        "minimum_latitude": south,
        "maximum_latitude": north,
        "minimum_depth": options.min_depth,
        "maximum_depth": options.max_depth,
        "start_datetime": f"{start.isoformat()}T00:00:00",
        "end_datetime": f"{end.isoformat()}T23:59:59",
        "netcdf_compression_level": options.compression,
    }


def year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def expected_dates(start: date, end: date):
    import numpy as np

    return np.arange(np.datetime64(start), np.datetime64(end) + np.timedelta64(1, "D"))


def verify_period(path: Path, start: date, end: date,
                  variables: list[str]) -> str | None:
    """检查变量和完整逐日时间轴；不代表全部数据块的逐值校验。"""
    import numpy as np
    import xarray

    try:
        with xarray.open_dataset(path, decode_timedelta=False) as dataset:
            missing = [name for name in variables if name not in dataset.variables]
            if missing:
                return f"缺变量 {', '.join(missing)}"

            if "time" not in dataset.dims:
                return "没有 time 维"
            stamps = dataset["time"].values.astype("datetime64[D]")
            wanted = expected_dates(start, end)
            if not np.array_equal(stamps, wanted):
                return (f"时间轴不是 {start.isoformat()} 至 {end.isoformat()} "
                        "完整、递增且无重复的逐日序列")
    except Exception as exc:
        return f"无法打开：{exception_chain(exc)}"

    return None


def verify(path: Path, year: int, variables: list[str]) -> str | None:
    start, end = year_bounds(year)
    return verify_period(path, start, end, variables)


def verify_data_readable(path: Path, variables: list[str]) -> str | None:
    """逐时间片读取数据，仅在合并读取失败后用于定位损坏分片。"""
    import netCDF4

    try:
        with netCDF4.Dataset(path) as dataset:
            names = list(dict.fromkeys([*variables, *dataset.variables]))
            for name in names:
                variable = dataset.variables[name]
                variable.set_auto_maskandscale(False)
                if "time" not in variable.dimensions:
                    variable[...]
                    continue
                time_axis = variable.dimensions.index("time")
                for index in range(dataset.dimensions["time"].size):
                    selection = [slice(None)] * variable.ndim
                    selection[time_axis] = index
                    variable[tuple(selection)]
    except Exception as exc:
        return exception_chain(exc)
    return None


@dataclass
class Outcome:
    year: int
    status: str  # done | exists | failed
    detail: str = ""


@dataclass
class Estimate:
    file_mb: float | None = None
    transfer_mb: float | None = None
    error: str = ""


def probe(options: Options, year: int) -> Estimate:
    """保留官方 MB 估算值；不当作实际文件大小或网络流量。"""
    import copernicusmarine

    try:
        start, end = year_bounds(year)
        response = copernicusmarine.subset(
            **subset_kwargs(options, start, end), dry_run=True)
        if getattr(response.status, "value", response.status) not in ("000", "001"):
            return Estimate(error=str(response.message))
        return Estimate(response.file_size, response.data_transfer_size)
    except Exception as exc:
        return Estimate(error=str(exc) or exc.__class__.__name__)


def verify_month_files(output_dir: Path, year: int,
                       variables: list[str]) -> str | None:
    parts_dir = output_dir / str(year)
    for month in range(1, 13):
        part = parts_dir / PART_FILENAME.format(year=year, month=month)
        if not part.exists():
            return f"缺少 {part.name}"
        start, end = month_bounds(year, month)
        problem = verify_period(part, start, end, variables)
        if problem is not None:
            return f"{part.name}：{problem}"
    return None


def pending_years(years: list[int], output_dir: Path, options: Options,
                  overwrite: bool, strategy: str = "yearly") -> tuple[list[int], list[Outcome]]:
    """挑出还要下载的年份；已有年度文件或完整月文件直接跳过。"""
    todo: list[int] = []
    skipped: list[Outcome] = []
    for year in years:
        target = output_dir / FILENAME.format(year=year)
        if overwrite:
            todo.append(year)
            continue
        if strategy == "monthly-files":
            problem = verify_month_files(output_dir, year, options.variables)
            if problem is None:
                skipped.append(Outcome(year, "exists"))
                continue
            todo.append(year)
            continue
        if target.exists():
            problem = verify(target, year, options.variables)
            if problem is None:
                skipped.append(Outcome(year, "exists"))
                continue
            log(f"{target.name} 已存在但校验不过（{problem}），重下")
        todo.append(year)
    return todo, skipped


def exception_chain(exc: BaseException) -> str:
    """保留 Toolbox 清理异常背后的原始错误。"""
    details: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current) or current.__class__.__name__
        details.append(f"{current.__class__.__name__}: {text}")
        current = current.__cause__ or current.__context__
    return "；此前异常：".join(details)


def download_year(year: int, output_dir: Path, tmp_dir: Path,
                  options: Options, label: str) -> Outcome:
    """官方接口直接写单年临时文件，校验后同盘改名，不复制或重新编码。"""
    import copernicusmarine

    target = output_dir / FILENAME.format(year=year)
    temp = tmp_dir / target.name
    start, end = year_bounds(year)
    print(f"{label} 下载 {year}，磁盘可用 {format_bytes(shutil.disk_usage(tmp_dir).free)}",
          flush=True)
    try:
        response = copernicusmarine.subset(
            **subset_kwargs(options, start, end),
            output_directory=str(tmp_dir),
            output_filename=temp.name,
            overwrite=True,
            disable_progress_bar=not sys.stderr.isatty(),
        )
        if getattr(response.status, "value", response.status) != "000":
            return Outcome(year, "failed", f"toolbox 返回 {response.message}")
        if not temp.exists():
            return Outcome(year, "failed", f"未找到预期的输出文件 {temp.name}")
        problem = verify(temp, year, options.variables)
        if problem is not None:
            return Outcome(year, "failed", f"校验失败：{problem}；临时文件保留在 {temp}")
        temp.replace(target)
        print(f"{label} 完成 {target.name}（{format_bytes(target.stat().st_size)}）",
              flush=True)
        return Outcome(year, "done")
    except Exception as exc:
        return Outcome(year, "failed", exception_chain(exc))


def remove_toolbox_leftovers(target: Path) -> None:
    """清理已释放句柄的 Toolbox 随机临时文件；仍被锁定的留待下次。"""
    for path in target.parent.glob(f"{target.name}.*"):
        with contextlib.suppress(OSError):
            path.unlink()


class RetryablePublishError(RuntimeError):
    """Windows 目标文件暂时被占用，允许重新尝试发布。"""


def is_nonretryable_local_error(exc: BaseException) -> bool:
    """磁盘满/只读等本地错误立即失败；Windows 共享冲突仍应重试。"""
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) in (32, 33):
        return False
    return exc.errno in (errno.ENOSPC, errno.EACCES, errno.EROFS)


def publish_month(staged: Path, target: Path, retries: int = 1,
                  cancel_event: threading.Event | None = None,
                  label: str = "") -> None:
    for attempt in range(1, retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise RetryablePublishError("已取消")
        try:
            staged.replace(target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) != 5:
                raise
            if attempt == retries:
                raise RetryablePublishError(
                    f"目标文件可能正被占用：{target}") from exc
            delay = min(30, 2 ** attempt)
            prefix = f"[{label}] " if label else ""
            log(f"{prefix}目标文件可能正被占用；{delay}s 后重试发布")
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise RetryablePublishError("已取消") from exc
            else:
                time.sleep(delay)


def download_month(year: int, month: int, parts_dir: Path, options: Options,
                   retries: int, workers: int,
                   cancel_event: threading.Event | None = None,
                   final_dir: Path | None = None,
                   force: bool = False) -> tuple[int, str, str]:
    import copernicusmarine

    start, end = month_bounds(year, month)
    name = PART_FILENAME.format(year=year, month=month)
    target = (final_dir or parts_dir) / name
    staged = parts_dir / name
    if not force and target.exists():
        problem = verify_period(target, start, end, options.variables)
        if problem is None:
            return month, "exists", ""
        log(f"{target.name} 分片校验不过（{problem}），重下")
    if not force and final_dir is not None and staged.exists():
        problem = verify_period(staged, start, end, options.variables)
        if problem is None:
            try:
                publish_month(staged, target, retries, cancel_event,
                              f"{year}-{month:02d}")
            except RetryablePublishError as exc:
                return month, "failed", exception_chain(exc)
            return month, "exists", ""
        log(f"{staged.name} 缓存分片校验不过（{problem}），重下")

    last_error = ""
    for attempt in range(1, retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            return month, "failed", "已取消"
        remove_toolbox_leftovers(staged)
        try:
            response = copernicusmarine.subset(
                **subset_kwargs(options, start, end),
                output_directory=str(parts_dir),
                output_filename=staged.name,
                overwrite=True,
                disable_progress_bar=workers > 1 or not sys.stderr.isatty(),
            )
            if getattr(response.status, "value", response.status) != "000":
                raise RuntimeError(f"toolbox 返回 {response.message}")
            if not staged.exists():
                raise RuntimeError(f"未找到预期的输出文件 {staged.name}")
            problem = verify_period(staged, start, end, options.variables)
            if problem is not None:
                raise RuntimeError(f"校验失败：{problem}")
            if final_dir is not None:
                publish_month(staged, target, retries, cancel_event,
                              f"{year}-{month:02d}")
            remove_toolbox_leftovers(staged)
            return month, "done", ""
        except Exception as exc:
            last_error = exception_chain(exc)
            if (attempt == retries or is_nonretryable_local_error(exc)
                    or isinstance(exc, RetryablePublishError)):
                break
            delay = min(30, 2 ** attempt)
            log(f"[{year}-{month:02d}] 第 {attempt}/{retries} 次失败：{last_error}；"
                f"{delay}s 后重试")
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    return month, "failed", "已取消"
            else:
                time.sleep(delay)
    return month, "failed", last_error


def merge_months(year: int, parts: list[Path], target: Path, tmp_dir: Path,
                 options: Options) -> str | None:
    """惰性读取月分片并生成年度临时文件，校验后原子替换。"""
    import xarray as xr

    merging = tmp_dir / f"{target.name}.merging"
    with contextlib.suppress(OSError):
        merging.unlink()
    try:
        with xr.open_mfdataset(
                [str(path) for path in parts], combine="nested", concat_dim="time",
                data_vars="minimal", coords="minimal", compat="equals", join="exact",
                combine_attrs="override", decode_timedelta=False, chunks={}) as dataset:
            encoding: dict[str, dict] = {}
            keep = {"scale_factor", "add_offset", "dtype", "_FillValue", "units", "calendar"}
            for name, variable in dataset.variables.items():
                selected = {key: value for key, value in variable.encoding.items()
                            if key in keep}
                if name in dataset.data_vars and options.compression > 0:
                    selected.update(zlib=True, complevel=options.compression,
                                    contiguous=False, shuffle=True)
                elif name in dataset.coords:
                    selected["_FillValue"] = None
                encoding[name] = selected
            dataset.to_netcdf(merging, mode="w", engine="netcdf4", encoding=encoding)

        problem = verify(merging, year, options.variables)
        if problem is not None:
            return f"年度合并校验失败：{problem}；文件保留在 {merging}"
        merging.replace(target)
        return None
    except Exception as exc:
        detail = exception_chain(exc)
        if (isinstance(exc, (OSError, RuntimeError))
                and getattr(exc, "errno", None) != errno.ENOSPC):
            damaged: list[str] = []
            undeleted: list[str] = []
            for part in parts:
                problem = verify_data_readable(part, options.variables)
                if problem is None:
                    continue
                try:
                    part.unlink()
                    damaged.append(part.name)
                except OSError as unlink_error:
                    undeleted.append(f"{part.name}（{unlink_error}）")
            if damaged:
                detail += f"；已删除损坏分片：{', '.join(damaged)}"
            if undeleted:
                detail += f"；损坏分片删除失败：{', '.join(undeleted)}"
        return detail


def run_month_downloads(year: int, parts_dir: Path, options: Options,
                        workers: int, retries: int, label: str,
                        final_dir: Path | None = None,
                        force: bool = False) -> list[tuple[int, str, str]]:
    """并发下载一个年份的 12 个分片，并显示单条聚合进度。"""
    from tqdm.auto import tqdm

    results: list[tuple[int, str, str]] = []
    progress = tqdm(
        total=12,
        desc=f"{label} {year}",
        unit="月",
        dynamic_ncols=True,
        disable=not sys.stderr.isatty(),
        file=sys.stderr,
    )
    counts = {"done": 0, "exists": 0, "failed": 0}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    cancel_event = threading.Event()
    futures: dict[concurrent.futures.Future, int] = {}
    try:
        futures = {
            pool.submit(download_month, year, month, parts_dir, options,
                        retries, workers, cancel_event, final_dir, force): month
            for month in range(1, 13)
        }
        for future in concurrent.futures.as_completed(futures):
            scheduled_month = futures[future]
            try:
                month, status, detail = future.result()
            except Exception as exc:
                month, status, detail = scheduled_month, "failed", exception_chain(exc)
            results.append((month, status, detail))
            counts[status] += 1
            progress.set_postfix_str(
                f"下载 {counts['done']} | 缓存 {counts['exists']} | 失败 {counts['failed']}",
                refresh=False,
            )
            progress.update(1)
            if status == "failed":
                log(f"{label} {year}-{month:02d} 失败：{detail}")
            elif progress.disable:
                action = "已完整，跳过" if status == "exists" else "完成"
                print(f"{label} {year}-{month:02d} {action}", flush=True)
    except BaseException:
        cancel_event.set()
        for future in futures:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        pool.shutdown()
    finally:
        progress.close()
    return results


def failed_month_outcome(year: int,
                         results: list[tuple[int, str, str]]) -> Outcome | None:
    failed = sorted(result for result in results if result[1] == "failed")
    if not failed:
        return None
    months = ", ".join(f"{month:02d}" for month, _, _ in failed)
    return Outcome(year, "failed", f"月份 {months} 下载失败；已完成分片保留")


def download_monthly_year(year: int, output_dir: Path, tmp_dir: Path,
                          options: Options, workers: int, retries: int,
                          keep_monthly: bool, label: str) -> Outcome:
    target = output_dir / FILENAME.format(year=year)
    cache_dir = tmp_dir / PARTS_DIRNAME / request_cache_key(options)
    parts_dir = cache_dir / str(year)
    parts_dir.mkdir(parents=True, exist_ok=True)
    print(f"{label} 按月下载 {year}（并发 {workers}，每月最多 {retries} 次），"
          f"磁盘可用 {format_bytes(shutil.disk_usage(tmp_dir).free)}", flush=True)

    results = run_month_downloads(year, parts_dir, options, workers, retries, label)
    failed = failed_month_outcome(year, results)
    if failed is not None:
        return failed

    parts = [parts_dir / PART_FILENAME.format(year=year, month=month)
             for month in range(1, 13)]
    print(f"{label} 合并 {year} 年 12 个月分片", flush=True)
    problem = merge_months(year, parts, target, tmp_dir, options)
    if problem is not None:
        return Outcome(year, "failed", f"合并失败：{problem}；月分片保留")

    if not keep_monthly:
        cleanup_failed: list[str] = []
        for part in parts:
            try:
                part.unlink()
            except OSError as exc:
                cleanup_failed.append(f"{part.name}（{exc}）")
        try:
            parts_dir.rmdir()
            cache_dir.rmdir()
            cache_dir.parent.rmdir()
        except OSError:
            pass
        if cleanup_failed:
            log(f"{label} 年度文件已完成，但部分月分片清理失败："
                f"{', '.join(cleanup_failed)}")
    print(f"{label} 完成 {target.name}（{format_bytes(target.stat().st_size)}）", flush=True)
    return Outcome(year, "done")


def cleanup_empty_cache_dirs(parts_dir: Path, cache_dir: Path) -> None:
    """月分片迁走后，按从内到外的顺序清理空缓存目录。"""
    for directory in (parts_dir, cache_dir, cache_dir.parent):
        with contextlib.suppress(OSError):
            directory.rmdir()


def promote_cached_months(year: int, cache_parts_dir: Path, final_parts_dir: Path,
                          options: Options) -> int:
    """把旧 monthly 合并模式留下的有效分片原子迁移到可见目录。"""
    promoted = 0
    for month in range(1, 13):
        cached = cache_parts_dir / PART_FILENAME.format(year=year, month=month)
        if not cached.exists():
            continue
        final = final_parts_dir / cached.name
        start, end = month_bounds(year, month)
        if final.exists() and verify_period(final, start, end, options.variables) is None:
            with contextlib.suppress(OSError):
                cached.unlink()
            continue
        problem = verify_period(cached, start, end, options.variables)
        if problem is not None:
            log(f"{cached.name} 旧缓存校验不过（{problem}），不复用")
            continue
        try:
            publish_month(cached, final)
        except RetryablePublishError as exc:
            log(f"{cached.name} 旧缓存发布失败（{exc}），稍后重试")
            continue
        promoted += 1
    return promoted


def download_month_files_year(year: int, output_dir: Path, tmp_dir: Path,
                              options: Options, workers: int, retries: int,
                              overwrite: bool, label: str) -> Outcome:
    """下载并保留可直接使用的月文件，不生成年度物理合并文件。"""
    final_parts_dir = output_dir / str(year)
    final_parts_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = tmp_dir / PARTS_DIRNAME / request_cache_key(options)
    cache_parts_dir = cache_dir / str(year)
    promoted = 0
    if not overwrite:
        promoted = promote_cached_months(
            year, cache_parts_dir, final_parts_dir, options)
    cache_parts_dir.mkdir(parents=True, exist_ok=True)

    reused = f"，迁移旧缓存 {promoted} 个月" if promoted else ""
    print(f"{label} 保留月文件 {year}（并发 {workers}，每月最多 {retries} 次{reused}），"
          f"磁盘可用 {format_bytes(shutil.disk_usage(tmp_dir).free)}", flush=True)
    results = run_month_downloads(
        year, cache_parts_dir, options, workers, retries, label,
        final_dir=final_parts_dir, force=overwrite)
    cleanup_empty_cache_dirs(cache_parts_dir, cache_dir)
    failed = failed_month_outcome(year, results)
    if failed is not None:
        return failed

    total_size = sum(
        (final_parts_dir / PART_FILENAME.format(year=year, month=month)).stat().st_size
        for month in range(1, 13)
    )
    print(f"{label} 完成 {year} 年 12 个月文件（{format_bytes(total_size)}），"
          f"目录：{final_parts_dir}", flush=True)
    return Outcome(year, "done")


def show_plan(years: list[int], output_dir: Path, options: Options,
              overwrite: bool, strategy: str, workers: int, retries: int) -> int:
    west, east, south, north = options.bbox
    if options.min_depth is None and options.max_depth is None:
        depth = "全部"
    else:
        lower = 0 if options.min_depth is None else options.min_depth
        upper = "底" if options.max_depth is None else options.max_depth
        depth = f"{lower}-{upper} m"
    print(f"目标目录：{output_dir}")
    print(f"数据集：{DATASET_ID}")
    print(f"区域：经度 {west} 至 {east}，纬度 {south} 至 {north}；深度：{depth}")
    print(f"变量：{', '.join(options.variables)}")
    if strategy == "monthly":
        print(f"逐月下载、每年本地合并，共 {len(years) * 12} 个分片；"
              f"并发 {workers}，每月最多 {retries} 次")
    elif strategy == "monthly-files":
        print(f"逐月下载并直接保留，共 {len(years) * 12} 个文件；"
              f"并发 {workers}，每月最多 {retries} 次，不生成年度合并文件")
    else:
        print(f"逐年独立裁剪，共 {len(years)} 个年份；服务由官方 Toolbox 自动选择")
    print("以下为官方粗略估算，单位沿用 API 的 MB：")
    print("文件估算未考虑实际压缩效果；传输量为上限粗估，不是实测流量。")
    print("估算仅供参考，不据此拒绝下载，也不要求双倍空间。\n")
    print("年份       文件大小估算(MB)     传输量上限粗估(MB)")
    failed = False
    for year in years:
        todo, _ = pending_years(
            [year], output_dir, options, overwrite, strategy)
        if not todo:
            print(f"{year}       已通过校验，跳过")
            continue
        estimate = probe(options, year)
        if estimate.error:
            print(f"{year}       查询失败：{estimate.error}")
            failed = True
            continue
        file_size = "未知" if estimate.file_mb is None else f"{estimate.file_mb:,.2f}"
        transfer = "未知" if estimate.transfer_mb is None else f"{estimate.transfer_mb:,.2f}"
        print(f"{year} {file_size:>22} {transfer:>25}")
    print(f"\n目标盘可用 {format_bytes(shutil.disk_usage(output_dir).free)}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="官方裁剪 GLORYS12V1 区域逐日再分析，保存为年度或月度文件。",
        epilog=f"年份写法：2001 / 2001-2005 / 2001,2003-2005；"
               f"可用范围 {MIN_YEAR}-{MAX_YEAR}。"
               "monthly 下载月分片后合并；monthly-files 直接保留月文件。")
    parser.add_argument("years", type=parse_years, help="单个年份、区间或它们的组合")
    parser.add_argument(
        "--strategy", choices=("yearly", "monthly", "monthly-files"), default="yearly",
        help="yearly 按年下载；monthly 按月下载后合并；"
             "monthly-files 直接保留月文件，默认 yearly")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                        help="输出目录，默认当前工作目录")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="只列出逐年计划和官方粗略估算，不实际下载")
    parser.add_argument("--overwrite", action="store_true",
                        help="重建已有年度文件；monthly-files 重下已有月文件")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="某一年失败就停止，默认继续下一年")
    parser.add_argument("--bbox", type=float, nargs=4,
                        metavar=("W", "E", "S", "N"), default=list(DEFAULT_BBOX),
                        help=f"经纬度范围，默认 {' '.join(map(str, DEFAULT_BBOX))}")
    parser.add_argument("-v", "--variables", nargs="+", default=list(DEFAULT_VARIABLES),
                        help=f"变量列表，默认 {' '.join(DEFAULT_VARIABLES)}")
    parser.add_argument("-z", "--min-depth", type=float, default=None,
                        help="最小深度（m），默认不限")
    parser.add_argument("-Z", "--max-depth", type=float, default=None,
                        help="最大深度（m），默认不限")
    parser.add_argument("--compression", type=int, choices=range(10), default=1,
                        metavar="0-9", help="NetCDF 压缩级别，默认 1；0 为不压缩")
    parser.add_argument("-j", "--workers", type=positive_int, default=2,
                        help="monthly/monthly-files 模式的并发月数，默认 2")
    parser.add_argument("--retries", type=positive_int, default=3,
                        help="月度模式每月最多尝试次数，默认 3（包含首次）")
    parser.add_argument("--keep-monthly", action="store_true",
                        help="年度合并校验通过后仍保留月分片")
    args = parser.parse_args()

    if args.strategy != "monthly" and args.keep_monthly:
        parser.error("--keep-monthly 只能与 --strategy monthly 一起使用")

    west, east, south, north = args.bbox
    if west >= east or south >= north:
        parser.error(f"bbox 反了：{args.bbox}，要求 W<E 且 S<N")
    if (args.min_depth is not None and args.max_depth is not None
            and args.min_depth > args.max_depth):
        parser.error(f"深度范围反了：{args.min_depth} > {args.max_depth}")

    output_dir = args.output_dir.expanduser().resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"错误：无法创建输出目录 {output_dir}：{exc}")
        return 1

    options = Options(bbox=(west, east, south, north), variables=args.variables,
                      min_depth=args.min_depth, max_depth=args.max_depth,
                      compression=args.compression)

    # toolbox 每次请求都要打一大段 INFO，交互式下载时留着看进度，dry-run 时纯噪音
    if args.dry_run:
        quiet_toolbox()
        try:
            return show_plan(args.years, output_dir, options, args.overwrite,
                             args.strategy, args.workers, args.retries)
        except KeyboardInterrupt:
            return 130

    tmp_dir = output_dir / TMP_DIRNAME
    if args.strategy in ("monthly", "monthly-files") and args.workers > 1:
        quiet_toolbox()
    try:
        tmp_dir.mkdir(exist_ok=True)
    except OSError as exc:
        log(f"错误：无法创建临时目录 {tmp_dir}：{exc}")
        return 1

    results: list[Outcome] = []
    try:
        for index, year in enumerate(args.years, start=1):
            label = f"[{index}/{len(args.years)}]"
            todo, skipped = pending_years(
                [year], output_dir, options, args.overwrite, args.strategy)
            results.extend(skipped)
            for outcome in skipped:
                if args.strategy == "monthly-files":
                    print(f"{label} {outcome.year} 年 12 个月文件已完整，跳过")
                else:
                    print(f"{label} {FILENAME.format(year=outcome.year)} 已完整，跳过")
            if not todo:
                continue

            try:
                if args.strategy == "monthly":
                    outcome = download_monthly_year(
                        year, output_dir, tmp_dir, options, args.workers, args.retries,
                        args.keep_monthly, label)
                elif args.strategy == "monthly-files":
                    outcome = download_month_files_year(
                        year, output_dir, tmp_dir, options, args.workers, args.retries,
                        args.overwrite, label)
                else:
                    outcome = download_year(year, output_dir, tmp_dir, options, label)
            except Exception as exc:
                outcome = Outcome(year, "failed", exception_chain(exc))
            results.append(outcome)
            if outcome.status == "failed":
                log(f"{label} {year} 失败：{outcome.detail}")
                if args.stop_on_error:
                    break
    except KeyboardInterrupt:
        if args.strategy in ("monthly", "monthly-files"):
            log("\n已中断。已校验通过的月文件会保留，下次运行将从缺失月份继续；"
                "正在传输的单个月份不支持字节续传。")
        else:
            log("\n已中断。年度模式不支持字节续传，未完成的年份下次重新请求；"
                "已校验通过的年文件不会重下。")
        return 130
    finally:
        with contextlib.suppress(OSError):
            tmp_dir.rmdir()   # 只在空目录时成功，留下的中间文件不动

    failed = [r for r in results if r.status == "failed"]
    if len(args.years) > 1 or failed:
        done = sum(r.status == "done" for r in results)
        exists = sum(r.status == "exists" for r in results)
        print(f"\n汇总：完成 {done}，跳过 {exists}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
