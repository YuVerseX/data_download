#!/usr/bin/env python3
"""按经纬度范围从 Argo GDAC 下载剖面 NetCDF，默认南海；附覆盖统计。"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import http.client
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm


BASE_URL = "https://data-argo.ifremer.fr"   # GDAC 的 Coriolis 节点
INDEX_NAME = "ar_index_global_prof.txt.gz"
INDEX_MAX_AGE = 7 * 86400          # 索引每天更新，一周内的缓存直接用
SELECTED_NAME = "index_selected.csv"
FAILED_NAME = "failed.txt"

# 南海：东起吕宋海峡西侧，西到中南半岛，南到纳土纳群岛外海
DEFAULT_BBOX = (105.0, 121.0, 2.0, 25.0)
MIN_YEAR = 1997                    # Argo 最早的剖面在 1997 年
AVG_PROFILE_BYTES = 20 * 1024      # 单个剖面文件实测 18–22 KiB，用来估体积

CHUNK_SIZE = 64 * 1024
USER_AGENT = "argo-downloader/1.0"
RETRIES = 4                        # 剖面文件小，失败重下比断点续传划算
TIMEOUT = 60
MAX_BACKOFF = 30
MAX_WORKERS = 16                   # 对方是公共服务器，别开太猛
FATAL_HTTP = frozenset({400, 401, 403, 404, 410, 451})


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def parse_bbox(spec: str) -> tuple[float, float, float, float]:
    """解析 "lon0,lon1,lat0,lat1"。"""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"需要 4 个数字 lon0,lon1,lat0,lat1，收到 {spec!r}")
    try:
        lon0, lon1, lat0, lat1 = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{spec!r} 里有非数字") from exc
    if lon0 >= lon1 or lat0 >= lat1:
        raise argparse.ArgumentTypeError(f"范围反了：{spec!r}，要求 lon0<lon1 且 lat0<lat1")
    if not (-180 <= lon0 <= 180 and -180 <= lon1 <= 180):
        raise argparse.ArgumentTypeError("经度要在 -180..180，索引里用的是这个约定")
    if not (-90 <= lat0 <= 90 and -90 <= lat1 <= 90):
        raise argparse.ArgumentTypeError("纬度要在 -90..90")
    return lon0, lon1, lat0, lat1


def parse_years(spec: str) -> set[int]:
    """解析 "2010" / "2010-2020" / "2010,2013-2015"。"""
    max_year = time.gmtime().tm_year
    years: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d{4})(?:\s*-\s*(\d{4}))?", part)
        if not match:
            raise argparse.ArgumentTypeError(
                f"无法解析 {part!r}，可用写法：2010 / 2010-2020 / 2010,2013-2015")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            raise argparse.ArgumentTypeError(f"年份区间反了：{part!r}")
        if start < MIN_YEAR or end > max_year:
            raise argparse.ArgumentTypeError(
                f"{part!r} 超出可用范围 {MIN_YEAR}-{max_year}")
        years.update(range(start, end + 1))
    if not years:
        raise argparse.ArgumentTypeError("没有指定任何年份")
    return years


def backoff(consecutive: int) -> float:
    return min(2 ** consecutive, MAX_BACKOFF) + random.uniform(0, 1)


def http_get(url: str, target: Path) -> int:
    """下到 .part 再改名；返回字节数。永久性错误不重试。"""
    partial = target.with_name(target.name + ".part")
    last = "未知错误"
    for attempt in range(1, RETRIES + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                expected = response.headers.get("Content-Length")
                size = 0
                with partial.open("wb") as output:
                    while block := response.read(CHUNK_SIZE):
                        output.write(block)
                        size += len(block)
            if expected is not None and size != int(expected):
                raise RuntimeError(f"大小不符：期望 {expected}，实际 {size}")
            partial.replace(target)
            return size
        except urllib.error.HTTPError as exc:
            if exc.code in FATAL_HTTP:
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from exc
            last = f"HTTP {exc.code} {exc.reason}"
        except (OSError, http.client.HTTPException, RuntimeError) as exc:
            last = str(exc) or exc.__class__.__name__
        if attempt < RETRIES:
            time.sleep(backoff(attempt))
    partial.unlink(missing_ok=True)
    raise RuntimeError(last)


@dataclass(frozen=True)
class Profile:
    path: str          # 相对 dac/ 的路径，如 csio/2902711/profiles/D2902711_133.nc
    date: str          # YYYYMMDDHHMMSS，索引里偶尔为空
    lat: float
    lon: float
    institution: str

    @property
    def float_id(self) -> str:
        return self.path.split("/")[1]

    @property
    def mode(self) -> str:
        """D=延时模式（过了二级质控），R=实时模式（只有自动质控）。"""
        return "D" if self.path.rsplit("/", 1)[-1].startswith("D") else "R"

    @property
    def year(self) -> int | None:
        return int(self.date[:4]) if len(self.date) >= 4 else None


def fetch_index(cache: Path, refresh: bool) -> Path:
    """取全局剖面索引，一周内的缓存复用。"""
    if cache.exists() and not refresh:
        age = time.time() - cache.stat().st_mtime
        if age < INDEX_MAX_AGE:
            print(f"复用索引 {cache.name}（{format_bytes(cache.stat().st_size)}，"
                  f"{age / 86400:.1f} 天前下载），加 --refresh-index 可强制更新")
            return cache
        print(f"索引已过期 {age / 86400:.1f} 天，重新下载")

    url = f"{BASE_URL}/{INDEX_NAME}"
    print(f"下载索引 {url}")
    size = http_get(url, cache)
    print(f"索引就绪（{format_bytes(size)}）")
    return cache


def read_index(cache: Path, bbox: tuple[float, float, float, float],
               years: set[int] | None, mode: str) -> list[Profile]:
    """流式读索引并按条件筛，整个文件解压后有 1 GiB 级，别一次读进内存。"""
    lon0, lon1, lat0, lat1 = bbox
    selected: list[Profile] = []
    with gzip.open(cache, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("#") or line.startswith("file,"):
                continue
            fields = line.rstrip("\n").split(",")
            if len(fields) < 7:
                continue
            path, date, lat_text, lon_text = fields[0], fields[1], fields[2], fields[3]
            if not lat_text or not lon_text:
                continue          # 定位失败的剖面，索引里留空
            try:
                lat, lon = float(lat_text), float(lon_text)
            except ValueError:
                continue
            if not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1):
                continue
            if years is not None and (len(date) < 4 or int(date[:4]) not in years):
                continue
            if mode != "all" and not path.rsplit("/", 1)[-1].startswith(mode):
                continue
            selected.append(Profile(path, date, lat, lon, fields[6]))
    selected.sort(key=lambda p: (p.date, p.path))
    return selected


def write_selection(profiles: list[Profile], target: Path) -> None:
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("file,date,latitude,longitude,institution,mode,float_id\n")
        for p in profiles:
            handle.write(f"{p.path},{p.date},{p.lat},{p.lon},"
                         f"{p.institution},{p.mode},{p.float_id}\n")
    print(f"筛选结果写入 {target}")


def summarize(profiles: list[Profile], bbox: tuple[float, float, float, float]) -> None:
    lon0, lon1, lat0, lat1 = bbox
    print(f"\n范围 {lon0}–{lon1}°E, {lat0}–{lat1}°N")
    if not profiles:
        print("该范围内没有剖面")
        return

    floats = {p.float_id for p in profiles}
    dated = [p for p in profiles if p.year is not None]
    modes = Counter(p.mode for p in profiles)
    print(f"剖面 {len(profiles)} 条，浮标 {len(floats)} 个，"
          f"预计体积 {format_bytes(len(profiles) * AVG_PROFILE_BYTES)}（按 20 KiB/条估）")
    print(f"延时模式 D {modes['D']} 条（{modes['D'] * 100 / len(profiles):.1f}%），"
          f"实时模式 R {modes['R']} 条")
    if dated:
        print(f"时间跨度 {dated[0].date[:8]} – {dated[-1].date[:8]}")

    print("\n按年份：")
    per_year = Counter(p.year for p in dated)
    floats_per_year: dict[int, set[str]] = {}
    for p in dated:
        floats_per_year.setdefault(p.year, set()).add(p.float_id)
    for year in sorted(per_year):
        bar = "#" * round(per_year[year] * 40 / max(per_year.values()))
        print(f"  {year}  {per_year[year]:>6}  浮标 {len(floats_per_year[year]):>3}  {bar}")

    print("\n按提交机构（是数据处理中心，不等于布放国）：")
    for institution, count in Counter(p.institution for p in profiles).most_common():
        print(f"  {institution or '(空)':<6}{count:>7}")


def download_all(profiles: list[Profile], out_dir: Path,
                 workers: int, overwrite: bool) -> tuple[int, int, list[str]]:
    root = out_dir / "dac"
    done = skipped = 0
    failures: list[str] = []

    def worker(profile: Profile) -> tuple[Profile, str, str]:
        target = root / profile.path
        if target.exists() and target.stat().st_size > 0 and not overwrite:
            return profile, "exists", ""
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            http_get(f"{BASE_URL}/dac/{profile.path}", target)
        except RuntimeError as exc:
            return profile, "failed", str(exc)
        return profile, "done", ""

    # disable=None：输出重定向到文件时自动关掉进度条，免得几万行 \r 刷屏
    with tqdm(total=len(profiles), unit="个", desc="剖面", disable=None) as progress:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            for profile, status, detail in executor.map(worker, profiles):
                if status == "done":
                    done += 1
                elif status == "exists":
                    skipped += 1
                else:
                    failures.append(f"{profile.path}\t{detail}")
                progress.update(1)
        except KeyboardInterrupt:
            # 不等在途请求，Ctrl-C 要立刻有反应
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        executor.shutdown(wait=True)
    return done, skipped, failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按经纬度范围从 Argo GDAC 下载剖面 NetCDF，默认南海。",
        epilog="先下全局索引（约 56 MiB，一周内复用缓存），筛出范围内的剖面再逐个下载。"
               "中断后重跑同样的命令即可，已下好的文件会跳过。")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                        help="输出目录，默认当前工作目录")
    parser.add_argument("--bbox", type=parse_bbox, default=DEFAULT_BBOX,
                        metavar="lon0,lon1,lat0,lat1",
                        help="经纬度范围，默认南海 105,121,2,25")
    parser.add_argument("--years", type=parse_years, default=None,
                        help="限定年份，如 2010-2020；默认全部")
    parser.add_argument("--mode", choices=("all", "D", "R"), default="all",
                        help="数据模式：D 延时（质控更严）、R 实时，默认都要")
    parser.add_argument("-j", "--workers", type=int, default=4,
                        help=f"并发下载数，默认 4，上限 {MAX_WORKERS}")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="只筛选和统计，不下载剖面")
    parser.add_argument("--overwrite", action="store_true",
                        help="已存在的剖面文件也重新下载")
    parser.add_argument("--refresh-index", action="store_true",
                        help="强制重新下载全局索引")
    args = parser.parse_args()

    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers 要在 1..{MAX_WORKERS}")

    out_dir = args.output_dir.expanduser().resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"错误：无法创建输出目录 {out_dir}：{exc}")
        return 1
    print(f"目标目录：{out_dir}")

    try:
        cache = fetch_index(out_dir / INDEX_NAME, args.refresh_index)
    except (RuntimeError, OSError) as exc:
        log(f"错误：索引获取失败：{exc}")
        return 1
    except KeyboardInterrupt:
        log("\n已中断。")
        return 130

    try:
        profiles = read_index(cache, args.bbox, args.years, args.mode)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        log(f"错误：索引解析失败（{exc}），加 --refresh-index 重下试试")
        return 1

    summarize(profiles, args.bbox)
    if not profiles:
        return 0
    write_selection(profiles, out_dir / SELECTED_NAME)

    if args.dry_run:
        print("\n--dry-run，未下载剖面文件")
        return 0

    print(f"\n开始下载 {len(profiles)} 个剖面到 {out_dir / 'dac'}，并发 {args.workers}")
    try:
        done, skipped, failures = download_all(
            profiles, out_dir, args.workers, args.overwrite)
    except KeyboardInterrupt:
        log("\n已中断，重跑同样的命令可继续。")
        return 130

    print(f"\n汇总：新下载 {done}，已存在跳过 {skipped}，失败 {len(failures)}")
    if failures:
        report = out_dir / FAILED_NAME
        report.write_text("\n".join(failures) + "\n", encoding="utf-8")
        log(f"失败清单写入 {report}，重跑同样的命令会只补这些")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
