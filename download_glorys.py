#!/usr/bin/env python3
"""下载 GLORYS12V1 南海区域逐日再分析，默认逐年直接落盘。

数据走 copernicusmarine toolbox 从 ARCO(zarr) 裁剪，没有可直接 GET 的整年文件，
因此不存在 SCSORA 那种 Range 续传。可选 grouped 策略合并请求以省流量。
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
from datetime import date
from pathlib import Path


DATASET_ID = "cmems_mod_glo_phy_my_0.083deg_P1D-m"
FILENAME = "GLORYS12V1_SCS_daily_{year}.nc"
MIN_YEAR, MAX_YEAR = 1993, 2025   # 数据到 2026-06-23，2026 不是完整年，不收

# ARCO 在时间维上分块存储，一块约 5.75 年，边界落在年中而不是年初。
# 下面是实测出来的边界年：这些年份的数据横跨两个块，单独请求也要付两块的流量。
# 探测办法：对单变量逐年 dry-run，流量是基准值 2 倍的就是边界年。
# 数据集时间范围往后延伸时会出现新边界（下一个约在 2027），届时要重测。
CHUNK_BOUNDARY_YEARS = (1998, 2004, 2010, 2015, 2021)

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


def format_span(years: list[int]) -> str:
    return f"{years[0]}" if len(years) == 1 else f"{years[0]}-{years[-1]}"


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


def chunk_index(year: int) -> int:
    """年份落在第几个 ARCO 块组。边界年跨两块，归入它开启的那一组。"""
    return sum(1 for boundary in CHUNK_BOUNDARY_YEARS if year >= boundary)


def group_years(years: list[int]) -> list[list[int]]:
    """把年份按 ARCO 时间块归并，同块的年份合成一次请求。

    合并是这个脚本最主要的优化：同一块内逐年请求会把整块反复拉一遍，
    实测 2016-2020 合并后 247 GiB，拆成 5 次要 1237 GiB。
    磁盘装不下整块时，分几次调用脚本、每次给一个年份即可。
    """
    groups: dict[int, list[int]] = {}
    for year in years:
        groups.setdefault(chunk_index(year), []).append(year)
    return [sorted(groups[key]) for key in sorted(groups)]


@dataclass
class Options:
    """一次运行里所有年份共用的请求参数。"""
    bbox: tuple[float, float, float, float]
    variables: list[str]
    min_depth: float | None
    max_depth: float | None
    compression: int


def subset_kwargs(options: Options, years: list[int]) -> dict:
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
        "start_datetime": f"{years[0]}-01-01",
        "end_datetime": f"{years[-1]}-12-31",
        "netcdf_compression_level": options.compression,
    }


def verify(path: Path, year: int, variables: list[str]) -> str | None:
    """打开文件核对 time 维，通过返回 None，否则返回失败原因。

    toolbox 既不给校验和也不支持续传，压缩后字节数又不可预测，
    所以只能靠打开文件验时间轴——这比比字节数更能查出截断和缺天。
    """
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

            stamps = dataset["time"].values
            first = stamps[0].astype("datetime64[D]").item()
            last = stamps[-1].astype("datetime64[D]").item()
            if first != date(year, 1, 1) or last != date(year, 12, 31):
                return f"时间范围 {first}~{last} 与 {year} 年不符"
    except OSError as exc:
        return f"无法打开：{exc}"

    return None


@dataclass
class Outcome:
    year: int
    status: str  # done | exists | failed
    detail: str = ""


@dataclass
class Estimate:
    file_bytes: float = 0.0
    transfer_bytes: float = 0.0
    error: str = ""


def ensure_space(directory: Path, needed: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < needed:
        raise RuntimeError(
            f"磁盘空间不足：还需 {format_bytes(needed)}，可用 {format_bytes(free)}")


def probe(options: Options, years: list[int]) -> Estimate:
    """dry-run 问一次体积和流量，不下载。"""
    import copernicusmarine

    try:
        response = copernicusmarine.subset(
            **subset_kwargs(options, years), dry_run=True)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # toolbox 的异常类型很杂，一律转成可读消息
        return Estimate(error=str(exc) or exc.__class__.__name__)
    # toolbox 报的 "MB" 实为 MiB，这里一次性换成字节，后面统一走 format_bytes
    unit = 1024 * 1024
    return Estimate(float(response.file_size) * unit,
                    float(response.data_transfer_size) * unit)


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


def transfer_encoding(source, compression: int) -> dict:
    """沿用源文件的打包方式。

    toolbox 下来的变量是 int16 + scale_factor 打包的，xarray 读进来会解码成 float64，
    不把原始 dtype 和标度抄回去的话，切分出来的文件要大三四倍。
    """
    keys = ("dtype", "scale_factor", "add_offset", "_FillValue", "units", "calendar")
    encoding = {key: source.encoding[key] for key in keys if key in source.encoding}
    if compression:
        encoding["zlib"] = True
        encoding["complevel"] = compression
    return encoding


def split_by_year(source: Path, years: list[int], options: Options,
                  label: str) -> list[Path]:
    """把一个多年的中间文件切成逐年文件，返回写出的临时文件路径。"""
    import xarray

    written: list[Path] = []
    # 块文件可能有几十 GB，必须惰性读，不能整个载进内存
    with xarray.open_dataset(source, chunks={},
                             decode_timedelta=False) as dataset:
        encoding = {name: transfer_encoding(dataset[name], options.compression)
                    for name in dataset.data_vars}
        encoding.update({name: transfer_encoding(dataset[name], 0)
                         for name in dataset.coords})
        for year in years:
            piece = dataset.sel(time=slice(f"{year}-01-01", f"{year}-12-31"))
            temp = source.with_name(f"_year_{year}.nc")
            print(f"{label} 切分 {year}", flush=True)
            piece.to_netcdf(temp, encoding={name: spec for name, spec in encoding.items()
                                            if name in piece.variables})
            written.append(temp)
    return written


def download_group(years: list[int], output_dir: Path, tmp_dir: Path,
                   options: Options, label: str) -> list[Outcome]:
    """下载一个时间块，落盘成逐年文件。"""
    import copernicusmarine

    span = format_span(years)
    estimate = probe(options, years)
    if estimate.error:
        return [Outcome(year, "failed", f"查询失败：{estimate.error}") for year in years]

    print(f"{label} 请求 {span}（{len(years)} 年）"
          f"落盘约 {format_bytes(estimate.file_bytes)}，"
          f"网络流量约 {format_bytes(estimate.transfer_bytes)}")

    # 多年块要先落一个完整的中间文件再切分，峰值是中间文件加切出来的年文件
    needed = estimate.file_bytes * (2 if len(years) > 1 else 1)
    try:
        ensure_space(tmp_dir, int(needed))
    except RuntimeError as exc:
        return [Outcome(year, "failed", str(exc)) for year in years]

    chunk_name = f"_chunk_{span}.nc"
    chunk_path = tmp_dir / chunk_name
    chunk_path.unlink(missing_ok=True)

    try:
        response = copernicusmarine.subset(
            **subset_kwargs(options, years),
            output_directory=str(tmp_dir),
            output_filename=chunk_name,
            overwrite=True,
            disable_progress_bar=not sys.stderr.isatty(),
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        chunk_path.unlink(missing_ok=True)
        return [Outcome(year, "failed", str(exc) or exc.__class__.__name__)
                for year in years]

    if getattr(response.status, "value", response.status) != "000":
        chunk_path.unlink(missing_ok=True)
        return [Outcome(year, "failed", f"toolbox 返回 {response.message}")
                for year in years]
    if not chunk_path.exists():
        return [Outcome(year, "failed", f"未找到预期的输出文件 {chunk_name}")
                for year in years]

    # 单年块下下来就是最终内容，改名即可；多年块要切
    if len(years) == 1:
        pieces = [chunk_path]
    else:
        try:
            pieces = split_by_year(chunk_path, years, options, label)
        except KeyboardInterrupt:
            raise
        except (OSError, ValueError) as exc:
            chunk_path.unlink(missing_ok=True)
            return [Outcome(year, "failed", f"切分失败：{exc}") for year in years]
        chunk_path.unlink(missing_ok=True)

    results: list[Outcome] = []
    for year, piece in zip(years, pieces):
        problem = verify(piece, year, options.variables)
        target = output_dir / FILENAME.format(year=year)
        if problem is not None:
            bad = target.with_suffix(".nc.bad")
            piece.replace(bad)
            results.append(Outcome(year, "failed", f"校验失败：{problem}，已留作 {bad.name}"))
            continue
        piece.replace(target)
        print(f"{label} 完成 {target.name}（{format_bytes(target.stat().st_size)}）")
        results.append(Outcome(year, "done"))
    return results


def show_plan(groups: list[list[int]], output_dir: Path, options: Options,
              overwrite: bool) -> int:
    west, east, south, north = options.bbox
    if options.min_depth is None and options.max_depth is None:
        depth = "全部"
    else:
        depth = f"{options.min_depth or 0}-{options.max_depth or '底'} m"
    total_years = sum(len(group) for group in groups)

    print(f"目标目录：{output_dir}")
    print(f"数据集：{DATASET_ID}")
    print(f"区域：{west}-{east}°E, {south}-{north}°N   深度：{depth}")
    print(f"变量：{', '.join(options.variables)}")
    print(f"计划处理 {total_years} 个年份，共 {len(groups)} 次请求\n")
    # 中文按两列显示，f-string 的宽度是按字符数算的，表头只能手工对齐
    print("请求时段      年份数        落盘      网络流量  待下年份")

    file_total = transfer_total = 0.0
    for group in groups:
        todo, skipped = pending_years(group, output_dir, options, overwrite)
        if not todo:
            print(f"{format_span(group):<14}{len(group):>6}{'-':>12}{'-':>14}"
                  f"  全部已完整，跳过")
            continue
        # 已完整的年份会从请求里剔掉，所以时段要按实际要下的年份算
        span = format_span(todo)
        estimate = probe(options, todo)
        if estimate.error:
            print(f"{span:<14}{len(todo):>6}{'-':>12}{'-':>14}  查询失败：{estimate.error}")
            continue
        file_total += estimate.file_bytes
        transfer_total += estimate.transfer_bytes
        note = ",".join(str(year) for year in todo)
        if skipped:
            note += f"（另 {len(skipped)} 年已完整）"
        print(f"{span:<14}{len(todo):>6}{format_bytes(estimate.file_bytes):>12}"
              f"{format_bytes(estimate.transfer_bytes):>14}  {note}")

    free = shutil.disk_usage(output_dir).free
    print(f"\n合计落盘 {format_bytes(file_total)}，"
          f"需要拉取 {format_bytes(transfer_total)} 网络流量")
    print(f"目标盘可用 {format_bytes(free)}")
    if file_total > free:
        log("注意：可用空间不足以一次下完，需分批或先腾空间")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载 GLORYS12V1 南海区域逐日再分析。",
        epilog=f"年份写法：2001 / 2001-2005 / 2001,2003-2005；"
               f"可用范围 {MIN_YEAR}-{MAX_YEAR}。"
               f"默认逐年下载；--strategy grouped 合并请求以省流量，但需要本地切分。")
    parser.add_argument("years", type=parse_years, help="单个年份、区间或它们的组合")
    parser.add_argument("--strategy", choices=("yearly", "grouped"), default="yearly",
                        help="yearly 逐年直下（默认）；grouped 合并请求后切分，节省网络流量")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                        help="输出目录，默认当前工作目录")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="只列出请求计划、体积和流量，不实际下载")
    parser.add_argument("--overwrite", action="store_true",
                        help="已存在的年文件也重新下载")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="某一块失败就停止，默认继续下一块")
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
    groups = ([[year] for year in args.years] if args.strategy == "yearly"
              else group_years(args.years))

    # toolbox 每次请求都要打一大段 INFO，交互式下载时留着看进度，dry-run 时纯噪音
    if args.dry_run:
        quiet_toolbox()
        try:
            return show_plan(groups, output_dir, options, args.overwrite)
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
        for index, group in enumerate(groups, start=1):
            label = f"[{index}/{len(groups)}]"
            todo, skipped = pending_years(group, output_dir, options, args.overwrite)
            results.extend(skipped)
            for outcome in skipped:
                print(f"{label} {FILENAME.format(year=outcome.year)} 已完整，跳过")
            if not todo:
                continue

            outcomes = download_group(todo, output_dir, tmp_dir, options, label)
            results.extend(outcomes)
            failed = [o for o in outcomes if o.status == "failed"]
            for outcome in failed:
                log(f"{label} {outcome.year} 失败：{outcome.detail}")
            if failed and args.stop_on_error:
                break
    except KeyboardInterrupt:
        log("\n已中断。toolbox 不支持续传，未完成的块下次要整块重来；"
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
