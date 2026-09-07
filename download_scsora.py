#!/usr/bin/env python3
"""下载 SCSORA 逐日再分析 NetCDF，支持年份范围、断点续传和完整性校验。"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import os
import random
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


BASE_URL = "https://www.hellosea.org.cn/SCSORA"
FILENAME = "SCSORA_daily_{year}.nc"
MIN_YEAR, MAX_YEAR = 2001, 2024

CHUNK_SIZE = 256 * 1024   # 块小一点，慢链路上进度也能及时刷新
USER_AGENT = "SCSORA-downloader/2.0"
RETRIES = 10              # 连续无进展的重试上限
TIMEOUT = 60
MAX_BACKOFF = 30
LOG_INTERVAL = 60         # 输出被重定向时的打印间隔（秒）
FATAL_HTTP = frozenset({400, 401, 403, 404, 405, 410, 416, 451})


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


class LogProgress:
    """stderr 不是终端时用：定时打一行。

    tqdm 在这种场合只写 \\r 不写换行，几小时的日志会被压成一整行还夹着方块字符。
    """

    def __init__(self, total: int, initial: int, label: str) -> None:
        self.total, self.n, self.label = total, initial, label
        self.started = self.last = time.monotonic()
        self.start_n = initial

    def update(self, delta: int) -> None:
        self.n += delta
        now = time.monotonic()
        if now - self.last < LOG_INTERVAL:
            return
        self.last = now
        speed = (self.n - self.start_n) / (now - self.started)
        eta = (self.total - self.n) / speed / 3600 if speed else 0
        log(f"{self.label} {self.n * 100 / self.total:6.2f}%  "
            f"{format_bytes(self.n)} / {format_bytes(self.total)}  "
            f"{format_bytes(speed)}/s  ETA {eta:.1f} h")

    def close(self) -> None:
        pass


def make_progress(total: int, initial: int, label: str):
    if not sys.stderr.isatty():
        return LogProgress(total, initial, label)
    from tqdm import tqdm
    return tqdm(total=total, initial=initial, unit="B", unit_scale=True,
                unit_divisor=1024, desc=label, dynamic_ncols=True, smoothing=0.05)


@dataclass(frozen=True)
class RemoteFile:
    size: int
    etag: str | None


def backoff(consecutive: int) -> float:
    return min(2 ** consecutive, MAX_BACKOFF) + random.uniform(0, 1)


def head(url: str) -> RemoteFile:
    """取远端大小和 ETag；瞬时故障重试，永久性错误立即放弃。"""
    last = "未知错误"
    for attempt in range(1, RETRIES + 1):
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                length = response.headers.get("Content-Length")
                if length is None:
                    raise RuntimeError("服务器未返回 Content-Length，无法校验完整性")
                return RemoteFile(int(length), response.headers.get("ETag"))
        except urllib.error.HTTPError as exc:
            if exc.code in FATAL_HTTP:
                raise RuntimeError(f"服务器返回 HTTP {exc.code} {exc.reason}") from exc
            last = f"HTTP {exc.code} {exc.reason}"
        except (OSError, http.client.HTTPException) as exc:
            last = str(exc) or exc.__class__.__name__
        if attempt < RETRIES:
            time.sleep(backoff(attempt))
    raise RuntimeError(f"获取文件信息失败：{last}")


def validate_range(response, offset: int, total: int) -> None:
    if offset == 0:
        if response.status not in (200, 206):
            raise RuntimeError(f"意外的 HTTP 状态码 {response.status}")
        return
    if response.status == 200:
        # 请求带了 If-Range，返回 200 说明远端文件在下载期间被换掉了
        raise RuntimeError("远端文件已变更，.part 不能再续写，请删除后重下")
    if response.status != 206:
        raise RuntimeError(f"服务器不支持续传（HTTP {response.status}），.part 已保留")

    content_range = response.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range.strip())
    if not match:
        raise RuntimeError(f"无法解析 Content-Range：{content_range!r}")
    start, _, remote_total = (int(g) for g in match.groups())
    if start != offset or remote_total != total:
        raise RuntimeError(
            f"续传响应不匹配：期望 offset={offset} size={total}，实际 {content_range!r}")


if sys.platform == "win32":
    import msvcrt

    def lock_exclusive(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl

    def lock_exclusive(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def ensure_space(directory: Path, needed: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < needed:
        raise RuntimeError(
            f"磁盘空间不足：还需 {format_bytes(needed)}，可用 {format_bytes(free)}")


def stream_once(output, url: str, remote: RemoteFile, offset: int, progress) -> None:
    headers = {"User-Agent": USER_AGENT, "Range": f"bytes={offset}-"}
    if remote.etag:
        headers["If-Range"] = remote.etag  # 文件变了就让服务器拒绝续传
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        validate_range(response, offset, remote.size)
        while True:
            block = response.read(CHUNK_SIZE)
            if not block:
                break
            output.write(block)
            progress.update(len(block))


def fetch(partial: Path, url: str, remote: RemoteFile, progress) -> None:
    """全程持有 .part 的独占句柄：既防并发写入，又保证 offset 取自真实文件大小。"""
    with partial.open("ab") as output:
        try:
            lock_exclusive(output)
        except OSError as exc:
            raise RuntimeError(f"另一个进程正在下载 {partial.name}，本次跳过") from exc

        consecutive = 0
        while True:
            output.flush()
            offset = os.fstat(output.fileno()).st_size
            if offset > remote.size:
                raise RuntimeError(
                    f"{partial.name} 比远端文件多 {offset - remote.size} 字节"
                    f"（本地 {offset} / 远端 {remote.size}），请手工删除后重下")
            if offset == remote.size:
                break

            ensure_space(partial.parent, remote.size - offset)
            progress.n = offset

            error = ""
            try:
                stream_once(output, url, remote, offset, progress)
            except urllib.error.HTTPError as exc:
                if exc.code in FATAL_HTTP:
                    raise RuntimeError(
                        f"服务器返回 HTTP {exc.code} {exc.reason}") from exc
                error = f"HTTP {exc.code} {exc.reason}"
            except (OSError, http.client.HTTPException) as exc:
                error = str(exc) or exc.__class__.__name__

            output.flush()
            current = os.fstat(output.fileno()).st_size
            if current >= remote.size:
                break

            # 只要这轮写进了新字节就不算失败，否则长下载会被正常的连接中断耗光额度
            consecutive = 0 if current > offset else consecutive + 1
            if consecutive > RETRIES:
                raise RuntimeError(
                    f"连续 {RETRIES} 次没有进展（{error or '连接提前结束'}）；"
                    f"已保留 {format_bytes(current)}，重跑同样的命令可继续")

            delay = backoff(consecutive)
            log(f"{partial.name}: {error or '连接提前结束'}，已就绪 "
                f"{format_bytes(current)}，{delay:.0f}s 后重试"
                f"（连续失败 {consecutive}/{RETRIES}）")
            time.sleep(delay)

        output.flush()
        os.fsync(output.fileno())


@dataclass
class Outcome:
    year: int
    status: str  # done | exists | failed
    detail: str = ""


def download_one(year: int, output_dir: Path, overwrite: bool, label: str) -> Outcome:
    filename = FILENAME.format(year=year)
    url = f"{BASE_URL}/{filename}"
    target = output_dir / filename
    partial = target.with_name(filename + ".part")

    try:
        remote = head(url)
    except RuntimeError as exc:
        return Outcome(year, "failed", str(exc))

    if target.exists():
        size = target.stat().st_size
        if size == remote.size:
            stale = (f"；另有残留 {partial.name}，可删" if partial.exists() else "")
            print(f"{label} {filename} 已完整，跳过（{format_bytes(size)}）{stale}")
            return Outcome(year, "exists")
        if not overwrite:
            return Outcome(year, "failed",
                           f"{filename} 已存在但大小不符（本地 {size} / 远端 "
                           f"{remote.size} 字节），未覆盖；确认无用后加 --overwrite")
        target.unlink()
        print(f"{label} 已删除大小不符的旧文件 {filename}")

    offset = partial.stat().st_size if partial.exists() else 0
    resume = f"，续传自 {format_bytes(offset)}" if offset else ""
    print(f"{label} {filename}  {format_bytes(remote.size)}{resume}")

    try:
        with contextlib.closing(make_progress(remote.size, offset, label)) as progress:
            fetch(partial, url, remote, progress)
        actual = partial.stat().st_size
        if actual != remote.size:
            return Outcome(year, "failed",
                           f"大小校验失败：期望 {remote.size}，实际 {actual}")
        partial.replace(target)
        print(f"{label} 完成 {filename}（{format_bytes(actual)}）")
        return Outcome(year, "done")
    except RuntimeError as exc:
        return Outcome(year, "failed", str(exc))
    except (OSError, http.client.HTTPException) as exc:
        return Outcome(year, "failed", str(exc) or exc.__class__.__name__)


def show_plan(years: list[int], output_dir: Path) -> int:
    print(f"目标目录：{output_dir}")
    print(f"计划处理 {len(years)} 个年份\n")
    print(f"{'年份':<4}{'远端大小':>10}  本地状态")

    pending = 0
    for year in years:
        filename = FILENAME.format(year=year)
        target = output_dir / filename
        partial = target.with_name(filename + ".part")
        try:
            remote = head(f"{BASE_URL}/{filename}")
        except RuntimeError as exc:
            print(f"{year:<6}{'-':>14}  查询失败：{exc}")
            continue

        if target.exists():
            size = target.stat().st_size
            state = "已完整" if size == remote.size else f"大小不符（本地 {size} 字节）"
            pending += 0 if size == remote.size else remote.size
        elif partial.exists():
            done = partial.stat().st_size
            state = f"已下 {format_bytes(done)}，待下 {format_bytes(remote.size - done)}"
            pending += remote.size - done
        else:
            state = "未开始"
            pending += remote.size
        print(f"{year:<6}{format_bytes(remote.size):>14}  {state}")

    free = shutil.disk_usage(output_dir).free
    print(f"\n合计待下载 {format_bytes(pending)}，目标盘可用 {format_bytes(free)}")
    if pending > free:
        log("注意：可用空间不足以一次下完，需分批或先腾空间")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载 SCSORA 逐日再分析 NetCDF 文件。",
        epilog=f"年份写法：2001 / 2001-2005 / 2001,2003-2005；"
               f"可用范围 {MIN_YEAR}-{MAX_YEAR}。中断后重跑同样的命令即可续传。")
    parser.add_argument("years", type=parse_years, help="单个年份、区间或它们的组合")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                        help="输出目录，默认当前工作目录")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="只列出下载计划和本地状态，不实际下载")
    parser.add_argument("--overwrite", action="store_true",
                        help="正式文件已存在但大小不符时删除重下")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="某个年份失败就停止，默认继续下一个")
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"错误：无法创建输出目录 {output_dir}：{exc}")
        return 1

    if args.dry_run:
        try:
            return show_plan(args.years, output_dir)
        except KeyboardInterrupt:
            return 130

    results: list[Outcome] = []
    try:
        for index, year in enumerate(args.years, start=1):
            label = f"[{index}/{len(args.years)}]"
            outcome = download_one(year, output_dir, args.overwrite, label)
            results.append(outcome)
            if outcome.status == "failed":
                log(f"{label} {year} 失败：{outcome.detail}")
                if args.stop_on_error:
                    break
    except KeyboardInterrupt:
        log("\n已中断，重跑同样的命令可继续续传。")
        return 130

    failed = [r for r in results if r.status == "failed"]
    if len(args.years) > 1 or failed:
        done = sum(r.status == "done" for r in results)
        skipped = sum(r.status == "exists" for r in results)
        print(f"\n汇总：完成 {done}，跳过 {skipped}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
