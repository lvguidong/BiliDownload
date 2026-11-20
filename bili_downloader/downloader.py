"""多线程分片下载引擎。

- 每个文件按 chunk_size 切分，线程池并发下载到 .part 分片文件。
- 断点续传：分片文件记录已下载字节，重启后跳过已完成区间。
- 限速：全局令牌桶，按字节节流。
- 失败重试：单分片重试 + 主/备 CDN 节点切换。
- 进度回调：下载管理器可实时更新任务状态。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

from .config import Config
from .constants import DEFAULT_HEADERS
from .utils import ensure_dir, format_speed, shorten

logger = logging.getLogger("bili.downloader")

ProgressCallback = Callable[[int, int, float], None]  # (downloaded, total, speed)


class DownloadError(RuntimeError):
    pass


class RateLimiter:
    """简单令牌桶限速器（字节/秒），0 表示不限速。"""

    def __init__(self, rate_bytes: int = 0):
        self.rate = float(rate_bytes)
        self._tokens = float(rate_bytes)
        self._last = time.time()
        self._lock = threading.Lock()

    def set_rate(self, rate_bytes: int) -> None:
        with self._lock:
            self.rate = float(rate_bytes)
            self._tokens = float(rate_bytes)

    def acquire(self, size: int) -> None:
        if self.rate <= 0:
            return
        while True:
            with self._lock:
                now = time.time()
                self._tokens = min(self.rate, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= size:
                    self._tokens -= size
                    return
                wait = (size - self._tokens) / self.rate
            time.sleep(min(wait, 0.5))


class ChunkedDownloader:
    def __init__(self, config: Config, limiter: Optional[RateLimiter] = None):
        self.config = config
        self.limiter = limiter or RateLimiter(0)
        self._ssl_ctx = ssl.create_default_context()
        if os.environ.get("BILI_DISABLE_SSL_VERIFY") == "1":
            self._ssl_ctx = ssl._create_unverified_context()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def reset(self) -> None:
        self._cancel_event.clear()

    def _opener(self) -> urllib.request.OpenerDirector:
        proxy = str(self.config.get("proxy") or "").strip()
        handlers: List[Any] = [urllib.request.HTTPSHandler(context=self._ssl_ctx)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _head(self, url: str, headers: Dict[str, str]) -> Tuple[int, bool]:
        """返回 (content-length, accept-ranges)。"""
        req = urllib.request.Request(url, headers=headers, method="GET")
        opener = self._opener()
        try:
            with opener.open(req, timeout=int(self.config.get("timeout") or 20)) as resp:
                length = int(resp.headers.get("Content-Length") or 0)
                accept = str(resp.headers.get("Accept-Ranges", "")).lower() == "bytes"
                return length, accept
        except urllib.error.HTTPError as exc:
            length = int(exc.headers.get("Content-Length") or 0)
            if length:
                return length, True
            raise

    def download(
        self,
        url: str,
        dest: str,
        backup_urls: Optional[List[str]] = None,
        progress: Optional[ProgressCallback] = None,
        label: str = "",
    ) -> str:
        """下载单个 URL 到 dest，返回 dest 路径。"""
        self.reset()
        ensure_dir(os.path.dirname(dest))
        urls = [url] + list(backup_urls or [])
        headers = dict(DEFAULT_HEADERS)
        referer = self.config.get("referer") or "https://www.bilibili.com/"
        headers["Referer"] = referer

        last_error: Optional[Exception] = None
        for url_idx, current_url in enumerate(urls):
            try:
                return self._download_from_url(current_url, dest, headers, progress, label, url_idx)
            except DownloadError as exc:
                last_error = exc
                logger.warning("下载失败（节点 %d/%d）%s: %s",
                               url_idx + 1, len(urls), label, shorten(str(exc), 100))
                if url_idx + 1 < len(urls) and self.config.get("cdn_switch"):
                    time.sleep(1)
                    continue
        raise DownloadError(f"所有节点均失败: {shorten(str(last_error), 120)}")

    def _download_from_url(
        self,
        url: str,
        dest: str,
        headers: Dict[str, str],
        progress: Optional[ProgressCallback],
        label: str,
        url_idx: int,
    ) -> str:
        total, accept_ranges = self._head(url, headers)
        if total <= 0:
            raise DownloadError("无法获取文件大小")

        threads = int(self.config.get("threads") or 8)
        chunk_size = int(self.config.get("chunk_size_mb") or 4) * 1024 * 1024
        if not accept_ranges or total <= chunk_size:
            threads = 1
        threads = max(1, min(threads, 32))

        part = dest + ".part"
        # 已有完整文件则直接返回（断点续传）
        if os.path.isfile(dest) and os.path.getsize(dest) == total:
            if progress:
                progress(total, total, 0)
            return dest
        if not os.path.exists(part):
            # 预分配稀疏文件
            with open(part, "wb") as f:
                f.truncate(total)

        # 计算分片区间：按 chunk_size 对齐，支持从已有 .part 续传
        ranges: List[Tuple[int, int]] = []
        start = 0
        while start < total:
            end = min(start + chunk_size - 1, total - 1)
            ranges.append((start, end))
            start = end + 1

        completed = 0
        for s, e in ranges:
            if self._chunk_complete(part, s, e):
                completed += e - s + 1
        if progress:
            progress(completed, total, 0)

        self._progress_lock = threading.Lock()
        self._downloaded = completed
        self._start_time = time.time()
        self._last_report = 0.0
        reports: Dict[str, float] = {}
        retries = int(self.config.get("retries") or 3)

        def worker(idx: int, s: int, e: int) -> None:
            if self._cancel_event.is_set() or self._chunk_complete(part, s, e):
                return
            last_exc: Optional[Exception] = None
            for attempt in range(retries + 1):
                if self._cancel_event.is_set():
                    return
                try:
                    self._download_chunk(url, part, s, e, headers)
                    with self._progress_lock:
                        self._downloaded += e - s + 1
                    return
                except (DownloadError, urllib.error.URLError, OSError) as exc:
                    last_exc = exc
                    if attempt < retries:
                        time.sleep(float(self.config.get("retry_delay") or 2) * (attempt + 1))
            raise DownloadError(f"分片 {idx} ({s}-{e}) 失败: {shorten(str(last_exc), 80)}")

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(worker, i, s, e) for i, (s, e) in enumerate(ranges)]
            for future in as_completed(futures):
                if self._cancel_event.is_set():
                    pool.shutdown(wait=False)
                    raise DownloadError("用户取消下载")
                try:
                    future.result()
                except Exception as exc:
                    raise DownloadError(str(exc)) from exc
                if progress:
                    now = time.time()
                    with self._progress_lock:
                        done = self._downloaded
                    if now - self._last_report >= 0.2:
                        elapsed = max(now - self._start_time, 0.001)
                        speed = (done - completed) / elapsed
                        progress(done, total, speed)
                        self._last_report = now

        # 校验并重命名
        if os.path.getsize(part) != total:
            raise DownloadError("文件大小校验失败")
        if progress:
            progress(total, total, 0)
        os.replace(part, dest)
        return dest

    def _chunk_complete(self, part: str, s: int, e: int) -> bool:
        """通过检查哨兵标记文件判断分片是否完成。

        避免误判：.part 预分配后全为 0，不能仅凭数据判断。
        标记文件命名为 <part>.<s>-<e>.done
        """
        return os.path.isfile(f"{part}.{s}-{e}.done")

    def _download_chunk(self, url: str, part: str, s: int, e: int, headers: Dict[str, str]) -> None:
        chunk_headers = dict(headers)
        chunk_headers["Range"] = f"bytes={s}-{e}"
        req = urllib.request.Request(url, headers=chunk_headers)
        opener = self._opener()
        timeout = int(self.config.get("timeout") or 20)
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read()
        expected = e - s + 1
        if len(data) != expected:
            # 服务器忽略 Range 时返回完整文件，截取需要的区间
            if len(data) > expected and s == 0:
                data = data[:expected]
            else:
                raise DownloadError(f"分片长度不符: 期望 {expected}, 实得 {len(data)}")
        with open(part, "r+b") as f:
            f.seek(s)
            f.write(data)
        # 写入完成标记
        with open(f"{part}.{s}-{e}.done", "wb") as f:
            f.write(b"1")

    def cleanup_markers(self, part: str) -> None:
        """删除分片标记文件。"""
        import glob

        for marker in glob.glob(f"{part}.*.done"):
            try:
                os.remove(marker)
            except OSError:
                pass
