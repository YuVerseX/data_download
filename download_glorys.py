#!/usr/bin/env python3
"""调用官方 copernicusmarine.subset 裁剪 GLORYS12V1，逐年直接写出。

每年独立请求，由 Toolbox 自动选择 ARCO 服务；不合并多年文件或本地切分。
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


DATASET_ID = "cmems_mod_glo_phy_my_0.083deg_P1D-m"
FILENAME = "GLORYS12V1_SCS_daily_{year}.nc"
MIN_YEAR, MAX_YEAR = 1993, 2025   # 数据到 2026-06-23，2026 不是完整年，不收

DEFAULT_BBOX = (105.0, 125.0, 0.0, 25.0)          # 南海
DEFAULT_VARIABLES = ("thetao", "so", "uo", "vo", "zos")  # 海冰 4 个变量南海全缺测，不要
TMP_DIRNAME = ".glorys_tmp"


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


def days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


@dataclass
class Options:
    """一次运行里所有年份共用的请求参数。"""
    bbox: tuple[float, float, float, float]
    variables: list[str]
    min_depth: float | None
    max_depth: float | None
    compression: int


def subset_kwargs(options: Options, year: int) -> dict:
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
        "start_datetime": f"{year}-01-01T00:00:00",
        "end_datetime": f"{year}-12-31T23:59:59",
        "netcdf_compression_level": options.compression,
    }


def verify(path: Path, year: int, variables: list[str]) -> str | None:
    """检查变量和完整逐日时间轴；不代表全部数据块的完整性校验。"""
    import numpy as np
    import xarray

    try:
        with xarray.open_dataset(path, decode_timedelta=False) as dataset:
            missing = [name for name in variables if name not in dataset.variables]
            if missing:
                return f"缺变量 {', '.join(missing)}"

            if "time" not in dataset.dims:
                return "没有 time 维"
            actual = dataset.sizes["time"]
            expected = days_in_year(year)
            if actual != expected:
                return f"time 长度 {actual}，该年应为 {expected} 天"

            stamps = dataset["time"].values.astype("datetime64[D]")
            expected_dates = np.arange(np.datetime64(f"{year}-01-01"),
                                       np.datetime64(f"{year + 1}-01-01"))
            if not np.array_equal(stamps, expected_dates):
                return f"时间轴不是 {year} 年完整、递增且无重复的逐日序列"
    except (OSError, ValueError, TypeError) as exc:
        return f"无法打开：{exc}"

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
        response = copernicusmarine.subset(
            **subset_kwargs(options, year), dry_run=True)
        if getattr(response.status, "value", response.status) not in ("000", "001"):
            return Estimate(error=str(response.message))
        return Estimate(response.file_size, response.data_transfer_size)
    except Exception as exc:
        return Estimate(error=str(exc) or exc.__class__.__name__)


def pending_years(years: list[int], output_dir: Path, options: Options,
                  overwrite: bool) -> tuple[list[int], list[Outcome]]:
    """挑出还要下载的年份；已存在且校验通过的直接跳过。"""
    todo: list[int] = []
    skipped: list[Outcome] = []
    for year in years:
        target = output_dir / FILENAME.format(year=year)
        if overwrite or not target.exists():
            todo.append(year)
            continue
        problem = verify(target, year, options.variables)
        if problem is None:
            skipped.append(Outcome(year, "exists"))
        else:
            todo.append(year)
            log(f"{target.name} 已存在但校验不过（{problem}），重下")
    return todo, skipped


def download_year(year: int, output_dir: Path, tmp_dir: Path,
                  options: Options, label: str) -> Outcome:
    """官方接口直接写单年临时文件，校验后同盘改名，不复制或重新编码。"""
    import copernicusmarine

    target = output_dir / FILENAME.format(year=year)
    temp = tmp_dir / target.name
    print(f"{label} 下载 {year}，磁盘可用 {format_bytes(shutil.disk_usage(tmp_dir).free)}",
          flush=True)
    try:
        response = copernicusmarine.subset(
            **subset_kwargs(options, year),
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
        return Outcome(year, "failed", str(exc) or exc.__class__.__name__)


def show_plan(years: list[int], output_dir: Path, options: Options,
              overwrite: bool) -> int:
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
    print(f"逐年独立裁剪，共 {len(years)} 个年份；服务由官方 Toolbox 自动选择")
    print("以下为官方粗略估算，单位沿用 API 的 MB：")
    print("文件估算未考虑实际压缩效果；传输量为上限粗估，不是实测流量。")
    print("估算仅供参考，不据此拒绝下载，也不要求双倍空间。\n")
    print("年份       文件大小估算(MB)     传输量上限粗估(MB)")
    failed = False
    for year in years:
        todo, _ = pending_years([year], output_dir, options, overwrite)
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
        description="官方裁剪 GLORYS12V1 区域逐日再分析，逐年直接保存。",
        epilog=f"年份写法：2001 / 2001-2005 / 2001,2003-2005；"
               f"可用范围 {MIN_YEAR}-{MAX_YEAR}。"
               f"每年独立请求，直接保存，不进行本地切分。")
    parser.add_argument("years", type=parse_years, help="单个年份、区间或它们的组合")
    parser.add_argument("--strategy", choices=("yearly",), default="yearly",
                        help="仅支持 yearly；默认逐年直接保存，无需指定")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                        help="输出目录，默认当前工作目录")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="只列出逐年计划和官方粗略估算，不实际下载")
    parser.add_argument("--overwrite", action="store_true",
                        help="已存在的年文件也重新下载")
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
    args = parser.parse_args()

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
            return show_plan(args.years, output_dir, options, args.overwrite)
        except KeyboardInterrupt:
            return 130

    tmp_dir = output_dir / TMP_DIRNAME
    try:
        tmp_dir.mkdir(exist_ok=True)
    except OSError as exc:
        log(f"错误：无法创建临时目录 {tmp_dir}：{exc}")
        return 1

    results: list[Outcome] = []
    try:
        for index, year in enumerate(args.years, start=1):
            label = f"[{index}/{len(args.years)}]"
            todo, skipped = pending_years([year], output_dir, options, args.overwrite)
            results.extend(skipped)
            for outcome in skipped:
                print(f"{label} {FILENAME.format(year=outcome.year)} 已完整，跳过")
            if not todo:
                continue

            outcome = download_year(year, output_dir, tmp_dir, options, label)
            results.append(outcome)
            if outcome.status == "failed":
                log(f"{label} {year} 失败：{outcome.detail}")
                if args.stop_on_error:
                    break
    except KeyboardInterrupt:
        log("\n已中断。脚本不支持字节续传，未完成的年份下次重新请求；"
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
