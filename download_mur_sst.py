#!/usr/bin/env python3
"""下载 GHRSST MUR L4 海表温度（MUR-JPL-L4-GLOB-v4.1）的区域子集。

走 OPeNDAP 服务端裁剪，只取指定 bbox 的格点。需要 Earthdata Login 的 Bearer token。
"""

from __future__ import annotations

import argparse
import base64
import calendar
import concurrent.futures as futures
import datetime as dt
import http.client
import json
import os
import random
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


COLLECTION = "C1996881146-POCLOUD"
GRANULE = "{:%Y%m%d}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1"
OPENDAP = f"https://opendap.earthdata.nasa.gov/collections/{COLLECTION}/granules"

FIRST_DAY = dt.date(2002, 6, 1)     # 数据集首日，实测 2002-06-01 至今逐日连续无缺
GRID_ANCHOR = dt.date(2020, 1, 1)   # 网格校验固定用这天，与用户选的日期无关

# 网格常量，2026-09-07 实测：lat 17999 点 -89.99..89.99，lon 36000 点 -179.99..180.00
LAT0, LON0, STEP = -89.99, -179.99, 0.01
NLAT, NLON = 17999, 36000

DEFAULT_BBOX = (105.0, 0.0, 125.0, 25.0)                    # 南海
DEFAULT_VARS = ("analysed_sst", "analysis_error", "mask")
DEFAULT_TOKEN_FILE = Path.home() / ".edl_token"

EST_PER_DAY = 4.3 * 1024**2         # 单日体积估算，仅用于 --dry-run 的规模预估

USER_AGENT = "MUR-SST-downloader/1.0"
CHUNK_SIZE = 256 * 1024
RETRIES = 5
MAX_BACKOFF = 30
TIMEOUT = 30                        # 正常 6～7 秒就回来，卡住没必要等太久
LOG_INTERVAL = 60
MAX_JOBS = 8
FATAL_HTTP = frozenset({400, 401, 403, 405, 410, 416, 451})

_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, file=sys.stderr, flush=True)


def out(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def cell(text: str, width: int, align: str = ">") -> str:
    """按终端显示宽度补空格。中文占两列，直接用 f-string 的 :<n 会错位。"""
    import unicodedata
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    pad = " " * max(0, width - shown)
    return pad + text if align == ">" else text + pad


# --------------------------------------------------------------------------
# 日期解析
# --------------------------------------------------------------------------

def _expand_point(text: str) -> tuple[dt.date, dt.date]:
    """把 2026 / 202601 / 20260115 展开成 [起, 止] 闭区间。"""
    if not text.isdigit() or len(text) not in (4, 6, 8):
        raise argparse.ArgumentTypeError(
            f"无法解析 {text!r}，粒度只能是年(4位)、年月(6位)或年月日(8位)")
    year = int(text[:4])
    if not 1900 <= year <= 2999:
        raise argparse.ArgumentTypeError(f"年份 {year} 不合理")
    if len(text) == 4:
        return dt.date(year, 1, 1), dt.date(year, 12, 31)

    month = int(text[4:6])
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError(f"{text!r} 里的月份 {month:02d} 不存在")
    last = calendar.monthrange(year, month)[1]
    if len(text) == 6:
        return dt.date(year, month, 1), dt.date(year, month, last)

    day = int(text[6:8])
    if not 1 <= day <= last:
        raise argparse.ArgumentTypeError(f"{text!r} 里的日期不存在")
    return dt.date(year, month, day), dt.date(year, month, day)


def _parse_part(part: str) -> tuple[dt.date, dt.date]:
    """解析单个片段。

    连字符的含义由两侧位数决定，避免 2020-2024（年区间）和 2026-01（年月）混淆：
      1 段            2026 / 202601 / 20260115
      2 段且右边 2 位  2026-01              年月
      2 段且右边 >2 位 2020-2024            区间
      3 段            2026-01-15           年月日
    """
    segments = part.split("-")
    if len(segments) == 1:
        return _expand_point(segments[0])
    if len(segments) == 3:
        return _expand_point("".join(segments))
    if len(segments) == 2:
        left, right = segments
        if len(right) == 2:                       # 2026-01
            return _expand_point(left + right)
        start = _expand_point(left)[0]            # 区间：左端取首日，右端取末日
        end = _expand_point(right)[1]
        if start > end:
            raise argparse.ArgumentTypeError(f"区间反了：{part!r}")
        return start, end
    raise argparse.ArgumentTypeError(f"无法解析 {part!r}")


def parse_dates(spec: str) -> list[dt.date]:
    """解析年份/年月/日期及其区间与逗号组合，返回去重排序后的日期列表。"""
    days: set[dt.date] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        start, end = _parse_part(part)
        days.update(start + dt.timedelta(n) for n in range((end - start).days + 1))
    if not days:
        raise argparse.ArgumentTypeError("没有指定任何日期")
    return sorted(days)


def clip_dates(days: list[dt.date]) -> list[dt.date]:
    """裁掉数据集范围外的日期。

    晚于今天的静默裁掉——写"2026"自然会带上尚未发布的日期，不值得每次都提示；
    只有裁完一天不剩时才说明原因。
    """
    today = dt.date.today()
    early = [d for d in days if d < FIRST_DAY]
    kept = [d for d in days if FIRST_DAY <= d <= today]
    if early:
        log(f"提示：{len(early)} 天早于数据集首日 {FIRST_DAY}，已跳过")
    if not kept and len(early) < len(days):
        log(f"提示：指定的日期都晚于今天（{today}），上游尚未发布")
    return kept


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------

def load_token(explicit: Path | None) -> str:
    if explicit is None:
        if env := os.environ.get("EARTHDATA_TOKEN", "").strip():
            return env
        label, path = "默认路径", DEFAULT_TOKEN_FILE
    else:
        label, path = "--token-file", explicit

    if not path.is_file():
        raise RuntimeError(f"{label} {path} 不存在")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"{label} {path} 是空的")
    return token


def check_token(token: str) -> None:
    """读 JWT 载荷里的 exp，只做 base64 解码、不验签。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        expiry = dt.datetime.fromtimestamp(
            json.loads(base64.urlsafe_b64decode(payload))["exp"], dt.timezone.utc)
    except Exception:
        log("提示：无法解析 token 的有效期，直接尝试使用")
        return

    now = dt.datetime.now(dt.timezone.utc)
    if expiry <= now:
        raise RuntimeError(
            f"token 已于 {expiry:%Y-%m-%d %H:%M} UTC 过期，"
            f"请到 https://urs.earthdata.nasa.gov 重新生成")
    if (left := (expiry - now).days) <= 7:
        log(f"警告：token 还有 {left} 天到期（{expiry:%Y-%m-%d}），记得及时更换")


# --------------------------------------------------------------------------
# 网格索引
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    i0: int
    i1: int
    j0: int
    j1: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.i1 - self.i0 + 1, self.j1 - self.j0 + 1


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = text.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox 格式为 西,南,东,北，例如 105,0,125,25")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bbox 里有非数字：{text!r}") from None
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise argparse.ArgumentTypeError(f"bbox 超出经纬度范围：{text!r}")
    return west, south, east, north


def bbox_to_window(bbox: tuple[float, float, float, float]) -> Window:
    west, south, east, north = bbox
    i0 = max(0, round((south - LAT0) / STEP))
    i1 = min(NLAT - 1, round((north - LAT0) / STEP))
    j0 = max(0, round((west - LON0) / STEP))
    j1 = min(NLON - 1, round((east - LON0) / STEP))
    if i0 > i1 or j0 > j1:
        raise argparse.ArgumentTypeError("bbox 落在网格之外")
    return Window(i0, i1, j0, j1)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Fatal(RuntimeError):
    """不该重试的错误，例如 token 失效。"""


class Missing(RuntimeError):
    """上游没有这一天。"""


def backoff(attempt: int) -> float:
    return min(2 ** attempt, MAX_BACKOFF) + random.uniform(0, 1)


def request(url: str, token: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
        "Authorization": f"Bearer {token}",
    })


def classify(exc: urllib.error.HTTPError) -> str:
    """把 HTTPError 归类成 致命 / 缺失 / 可重试。"""
    if exc.code == 404:
        raise Missing("上游尚未发布") from exc
    if exc.code in (401, 403):
        raise Fatal(f"认证失败（HTTP {exc.code}），请检查 token") from exc
    if exc.code in FATAL_HTTP:
        raise Fatal(f"HTTP {exc.code} {exc.reason}") from exc
    return f"HTTP {exc.code} {exc.reason}"


def read_url(url: str, token: str) -> bytes:
    """带重试地取回整个响应体。"""
    last = "未知错误"
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request(url, token), timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last = classify(exc)
        except (OSError, http.client.HTTPException) as exc:
            last = str(exc) or exc.__class__.__name__
        if attempt < RETRIES:
            time.sleep(backoff(attempt))
    raise RuntimeError(last)


# --------------------------------------------------------------------------
# 完整性校验
# --------------------------------------------------------------------------

def verify_nc4(path: Path, variables: tuple[str, ...]) -> str | None:
    """校验 nc4 是否完整可读。返回 None 表示通过，否则返回原因。

    只看文件头魔数是挡不住截断的（实测截断到 99% 仍能通过魔数检查），
    必须真的打开——HDF5 的 superblock 记录了文件应有长度。
    """
    import h5py
    try:
        with h5py.File(path, "r") as handle:
            for name in (*variables, "lat", "lon", "time"):
                if name not in handle:
                    return f"缺少变量 {name}"
    except OSError as exc:
        return str(exc).split("\n")[0][:120]
    return None


# --------------------------------------------------------------------------
# 进度显示
# --------------------------------------------------------------------------

class LogProgress:
    """stderr 不是终端时用：定时打一行，避免日志被 \\r 压成一整行。"""

    def __init__(self, total: int, label: str) -> None:
        self.total, self.label = total, label
        self.n = 0
        self.postfix = ""
        self.started = self.last = time.monotonic()

    def update(self, delta: int = 1) -> None:
        self.n += delta
        now = time.monotonic()
        if now - self.last < LOG_INTERVAL and self.n < self.total:
            return
        self.last = now
        elapsed = now - self.started
        rate = self.n / elapsed if elapsed else 0
        eta = (self.total - self.n) / rate / 60 if rate else 0
        log(f"{self.label} {self.n}/{self.total} ({self.n * 100 / self.total:5.1f}%)  "
            f"{rate * 60:.1f} 天/分  ETA {eta:.0f} 分  {self.postfix}")

    def set_postfix_str(self, text: str) -> None:
        self.postfix = text

    def close(self) -> None:
        pass


class YearProgress:
    """一年一条进度条，单位是天；累计体积挂在 postfix 上。"""

    def __init__(self, total: int, label: str) -> None:
        self.lock = threading.Lock()
        self.done = self.failed = self.missing = 0
        self.nbytes = 0
        if sys.stderr.isatty():
            from tqdm import tqdm
            self.bar = tqdm(total=total, unit="天", desc=label,
                            dynamic_ncols=True, smoothing=0.1)
        else:
            self.bar = LogProgress(total, label)

    def tick(self, status: str, nbytes: int = 0) -> None:
        with self.lock:
            setattr(self, status, getattr(self, status) + 1)
            self.nbytes += nbytes
            bits = [format_bytes(self.nbytes)]
            if self.missing:
                bits.append(f"未发布{self.missing}")
            if self.failed:
                bits.append(f"失败{self.failed}")
            self.bar.set_postfix_str(" ".join(bits))
            self.bar.update(1)

    def close(self) -> None:
        self.bar.close()


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

def build_ce(variables: tuple[str, ...], window: Window) -> str:
    box = f"[{window.i0}:1:{window.i1}][{window.j0}:1:{window.j1}]"
    parts = [f"/{name}[0:1:0]{box}" for name in variables]
    parts.append(f"/lat[{window.i0}:1:{window.i1}]")
    parts.append(f"/lon[{window.j0}:1:{window.j1}]")
    parts.append("/time[0:1:0]")
    return ";".join(parts)


def verify_window(window: Window, token: str) -> None:
    """抽查四个边界索引的实际坐标，防止上游改网格后静默产出错位数据。

    固定用 GRID_ANCHOR 这天，避免用户请求的首日恰好尚未发布时把整个任务带崩。
    """
    checks = (
        ("lat", window.i0, LAT0 + window.i0 * STEP),
        ("lat", window.i1, LAT0 + window.i1 * STEP),
        ("lon", window.j0, LON0 + window.j0 * STEP),
        ("lon", window.j1, LON0 + window.j1 * STEP),
    )
    for name, index, expected in checks:
        query = urllib.parse.urlencode({"dap4.ce": f"/{name}[{index}:1:{index}]"})
        body = read_url(f"{OPENDAP}/{GRANULE.format(GRID_ANCHOR)}.dap.csv?{query}",
                        token).decode("utf-8", "replace")
        try:
            actual = float(body.strip().splitlines()[-1].split(",")[-1])
        except (IndexError, ValueError):
            raise RuntimeError(f"网格校验失败，服务器返回：{body.strip()[:120]}") from None
        if abs(actual - expected) > STEP / 2:
            raise RuntimeError(
                f"网格校验失败：{name}[{index}] 期望 {expected:.2f}，实际 {actual}。"
                f"上游网格可能已变更，请核对后再下载")


def download_day(day: dt.date, target: Path, ce: str, token: str,
                 variables: tuple[str, ...]) -> int:
    """OPeNDAP 响应是 chunked、无 Content-Length 也不支持 Range，失败只能整个重下。"""
    query = urllib.parse.urlencode({"dap4.ce": ce})
    url = f"{OPENDAP}/{GRANULE.format(day)}.dap.nc4?{query}"
    partial = target.with_name(f"{target.name}.{os.getpid()}.part")

    last = "未知错误"
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request(url, token), timeout=TIMEOUT) as response:
                with partial.open("wb") as handle:
                    while block := response.read(CHUNK_SIZE):
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            if (reason := verify_nc4(partial, variables)) is not None:
                last = f"文件校验未通过：{reason}"
            else:
                size = partial.stat().st_size
                partial.replace(target)
                return size
        except urllib.error.HTTPError as exc:
            partial.unlink(missing_ok=True)
            last = classify(exc)          # 致命错误和"未发布"会在这里抛出
        except (OSError, http.client.HTTPException) as exc:
            last = str(exc) or exc.__class__.__name__
        partial.unlink(missing_ok=True)
        if attempt < RETRIES:
            time.sleep(backoff(attempt))
    raise RuntimeError(last)


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------

@dataclass
class Summary:
    done: int = 0
    skipped: int = 0
    missing: int = 0
    nbytes: int = 0
    failed: list[tuple[dt.date, str]] = field(default_factory=list)


def target_for(day: dt.date, root: Path) -> Path:
    return root / f"{day:%Y}" / f"MUR_SST_{day:%Y%m%d}.nc4"


def run_year(year: int, days: list[dt.date], args, token: str, ce: str,
             summary: Summary, label: str) -> None:
    (args.output_dir / str(year)).mkdir(parents=True, exist_ok=True)
    variables = tuple(args.vars)

    pending: list[tuple[dt.date, Path]] = []
    for day in days:
        target = target_for(day, args.output_dir)
        # 重下的旧文件不在这里删：download_day 校验通过后才 replace，
        # 提前删只会在中途失败时把旧文件也搭进去
        if target.exists() and not args.overwrite and verify_nc4(target, variables) is None:
            summary.skipped += 1
            continue
        pending.append((day, target))

    if not pending:
        out(f"{label} {year} 全部已完整，跳过 {len(days)} 天")
        return

    out(f"{label} {year}  待下 {len(pending)}/{len(days)} 天")
    progress = YearProgress(len(pending), str(year))

    def work(day: dt.date, target: Path):
        try:
            return day, "done", download_day(day, target, ce, token, variables), ""
        except Missing as exc:
            return day, "missing", 0, str(exc)
        except Fatal:
            raise
        except (RuntimeError, OSError, http.client.HTTPException) as exc:
            return day, "failed", 0, str(exc) or exc.__class__.__name__

    try:
        with futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            submitted = [pool.submit(work, day, target) for day, target in pending]
            try:
                for future in futures.as_completed(submitted):
                    day, status, size, detail = future.result()
                    progress.tick(status, size)
                    if status == "done":
                        summary.done += 1
                    elif status == "missing":
                        summary.missing += 1
                    else:
                        summary.failed.append((day, detail))
            except (KeyboardInterrupt, Fatal):
                pool.shutdown(wait=False, cancel_futures=True)
                raise
    finally:
        summary.nbytes += progress.nbytes
        progress.close()


def show_plan(days: list[dt.date], args) -> int:
    window = bbox_to_window(args.bbox)
    rows, cols = window.shape
    out(f"目标目录：{args.output_dir}")
    out(f"区域：{args.bbox_text}   网格窗口 lat[{window.i0}:{window.i1}] "
        f"lon[{window.j0}:{window.j1}]  {rows} x {cols} 格点")
    out(f"变量：{', '.join(args.vars)}")
    out("")
    out(cell("年份", 6, "<") + cell("总天数", 8) + cell("已完成", 8)
        + cell("待下载", 8) + cell("预估体积", 14))

    by_year: dict[int, list[dt.date]] = {}
    for day in days:
        by_year.setdefault(day.year, []).append(day)

    variables = tuple(args.vars)
    total_pending = 0
    for year, group in sorted(by_year.items()):
        have = sum(1 for day in group
                   if (target := target_for(day, args.output_dir)).exists()
                   and verify_nc4(target, variables) is None)
        need = len(group) - have
        size = need * EST_PER_DAY
        total_pending += size
        out(cell(str(year), 6, "<") + cell(str(len(group)), 8) + cell(str(have), 8)
            + cell(str(need), 8) + cell(format_bytes(size), 14))

    free = shutil.disk_usage(args.output_dir).free
    out(f"\n合计 {len(days)} 天，待下载约 {format_bytes(total_pending)}（估算），"
        f"目标盘可用 {format_bytes(free)}")
    if total_pending > free:
        log("注意：可用空间可能不足，需分批或先腾空间")
    return 0


def main() -> int:
    this_year = dt.date.today().year
    parser = argparse.ArgumentParser(
        description="下载 GHRSST MUR L4 海表温度（MUR-JPL-L4-GLOB-v4.1）的区域子集。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""日期写法（可用逗号组合）：
  {this_year}                   整年（省略参数时的默认值）
  2020-2024               年份区间
  202601 或 2026-01       整月
  202601-202603           月份区间
  20260115 或 2026-01-15  单日
  20260101-20260131       日期区间
  2020,2023-2024          组合

可用范围 {FIRST_DAY} 至今；数据集逐日连续无缺。
中断后重跑同样的命令即可，已完整的文件会自动跳过。""")
    parser.add_argument("dates", nargs="?", default=str(this_year),
                        help=f"日期或范围，默认今年（{this_year}）")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                        help="输出根目录，按年建子目录，默认当前工作目录")
    parser.add_argument("--bbox", default=",".join(str(v) for v in DEFAULT_BBOX),
                        help="区域 西,南,东,北，默认南海 105,0,125,25")
    parser.add_argument("--vars", default=",".join(DEFAULT_VARS),
                        help=f"变量，逗号分隔，默认 {','.join(DEFAULT_VARS)}")
    parser.add_argument("-j", "--jobs", type=int, default=4,
                        help=f"并发数，默认 4，上限 {MAX_JOBS}")
    parser.add_argument("--token-file", type=Path, default=None,
                        help=f"Earthdata token 文件，默认 {DEFAULT_TOKEN_FILE}"
                             f"，也可用环境变量 EARTHDATA_TOKEN")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="只列出计划和本地状态，不下载")
    parser.add_argument("--overwrite", action="store_true",
                        help="已存在的文件也重新下载")
    args = parser.parse_args()

    try:
        import h5py  # noqa: F401
    except ImportError:
        log("错误：需要 h5py 才能校验下载的文件是否完整，请先 pip install h5py")
        return 2

    try:
        args.bbox = parse_bbox(args.bbox)
        args.vars = [v.strip() for v in args.vars.split(",") if v.strip()]
        if not args.vars:
            raise argparse.ArgumentTypeError("--vars 不能为空")
        window = bbox_to_window(args.bbox)
        days = clip_dates(parse_dates(args.dates))
    except argparse.ArgumentTypeError as exc:
        log(f"参数错误：{exc}")
        return 2
    args.bbox_text = ",".join(f"{v:g}" for v in args.bbox)

    if not days:
        log("没有可下载的日期")
        return 2
    args.jobs = max(1, min(args.jobs, MAX_JOBS))

    args.output_dir = args.output_dir.expanduser().resolve()
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"错误：无法创建输出目录 {args.output_dir}：{exc}")
        return 1

    if args.dry_run:
        try:
            return show_plan(days, args)
        except KeyboardInterrupt:
            return 130

    try:
        token = load_token(args.token_file)
        check_token(token)
    except RuntimeError as exc:
        log(f"错误：{exc}")
        log(f"生成方法：登录 https://urs.earthdata.nasa.gov 后在 profile 里 "
            f"Generate Token，把它单独存成一行文本放到 {DEFAULT_TOKEN_FILE}")
        return 2

    try:
        verify_window(window, token)
    except (RuntimeError, Missing) as exc:
        log(f"错误：{exc}")
        # OPeNDAP 对失效 token 返回的是 500 而不是 401，光看状态码看不出来
        log("若上面是 HTTP 401/403/500，多半是 token 失效或未授权 PO.DAAC 应用，"
            "请到 https://urs.earthdata.nasa.gov 重新生成后覆盖 token 文件")
        return 1
    rows, cols = window.shape
    out(f"网格窗口 {rows} x {cols} 格点已校验（{args.bbox_text}）")

    by_year: dict[int, list[dt.date]] = {}
    for day in days:
        by_year.setdefault(day.year, []).append(day)

    out(f"目标目录：{args.output_dir}")
    out(f"计划处理 {len(by_year)} 个年份，共 {len(days)} 天，{args.jobs} 并发\n")

    ce = build_ce(tuple(args.vars), window)
    summary = Summary()
    interrupted = False
    try:
        for index, (year, group) in enumerate(sorted(by_year.items()), start=1):
            run_year(year, group, args, token, ce, summary, f"[{index}/{len(by_year)}]")
    except KeyboardInterrupt:
        interrupted = True
        log("\n已中断，重跑同样的命令可继续。")
    except Fatal as exc:
        log(f"\n中止：{exc}")
        return 1

    out(f"\n汇总：完成 {summary.done}，跳过 {summary.skipped}，"
        f"未发布 {summary.missing}，失败 {len(summary.failed)}，"
        f"合计 {format_bytes(summary.nbytes)}")
    if summary.failed:
        out("失败日期（重跑同样的命令可补齐）：")
        for day, detail in summary.failed[:20]:
            out(f"  {day}  {detail}")
        if len(summary.failed) > 20:
            out(f"  ...还有 {len(summary.failed) - 20} 天")
    if interrupted:
        return 130
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
