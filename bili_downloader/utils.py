"""通用工具函数。"""

from __future__ import annotations

import datetime as _dt
import os
import re
from typing import Iterable, List, Optional, Tuple

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """移除文件系统非法字符并压缩空白。"""
    name = INVALID_FILENAME_CHARS.sub("", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return (name[:max_len] or "untitled")


def format_size(num_bytes: Optional[float]) -> str:
    if num_bytes is None:
        return "-"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def format_speed(bytes_per_sec: Optional[float]) -> str:
    if not bytes_per_sec:
        return "-"
    return format_size(bytes_per_sec) + "/s"


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def parse_range_selection(spec: str, total: int, start_index: int = 1) -> List[int]:
    """
    解析选集表达式，如 ``1-3,5,8-9``，返回基于 1 的序号列表。
    支持 ``all`` 与 ``*``。
    """
    if not spec or spec.strip().lower() in ("all", "*"):
        return list(range(start_index, total + 1))
    result: List[int] = []
    seen = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            for i in range(max(start_index, lo), min(total, hi) + 1):
                if i not in seen:
                    seen.add(i)
                    result.append(i)
        else:
            try:
                i = int(part)
            except ValueError:
                continue
            if start_index <= i <= total and i not in seen:
                seen.add(i)
                result.append(i)
    return result


def parse_cookie_string(cookie_str: str) -> dict:
    """把 ``a=1; b=2`` 形式的 Cookie 串解析为字典。"""
    cookies = {}
    for item in (cookie_str or "").split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def cookie_to_string(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def chunk_ranges(total_size: int, chunk_size: int) -> Iterable[Tuple[int, int]]:
    """生成 (start, end) 字节区间（end 为包含边界）。"""
    start = 0
    while start < total_size:
        end = min(start + chunk_size - 1, total_size - 1)
        yield start, end
        start = end + 1


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def shorten(text: str, width: int = 60) -> str:
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


def find_ffmpeg(configured: Optional[str] = None) -> Optional[str]:
    """查找 ffmpeg 可执行文件：优先配置项，其次 PATH 与常见安装位置。"""
    candidates = []
    if configured:
        candidates.append(configured)
    candidates += ["ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            from shutil import which

            found = which(candidate)
            if found:
                return found
    return None
