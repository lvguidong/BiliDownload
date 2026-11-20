"""下载管理器：任务队列、并发调度、暂停/继续/取消/重试、断点恢复。

高层流程（每个任务）：
  1. 解析 cid（空间/收藏夹条目）→ 获取 StreamInfo（画质/编码选择）
  2. 下载视频流与音频流（多线程分片、断点续传）
  3. ffmpeg -c copy 无损混流（DASH）或直接使用 mp4
  4. 下载辅助资源（字幕/弹幕/封面/NFO）→ 去重历史记录
  5. 更新 SQLite 状态；失败自动重试并汇总
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

from .api import BiliApi, BiliApiError
from .config import Config
from .constants import QUALITY_MAP
from .downloader import ChunkedDownloader, DownloadError, RateLimiter
from .metadata import MetadataFetcher
from .models import (ACTIVE_STATUSES, STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED,
                     STATUS_MERGING, STATUS_PAUSED, STATUS_PENDING,
                     STATUS_DOWNLOADING, DownloadTask, Episode, ParseResult,
                     StreamInfo, TERMINAL_STATUSES)
from .muxer import Muxer, MuxError, need_mux
from .naming import build_variables as naming_variables, render as naming_render
from .storage import Storage
from .utils import (ensure_dir, format_duration, format_size, format_speed,
                    now_iso, sanitize_filename, shorten)

logger = logging.getLogger("bili.manager")


class DownloadManager:
    def __init__(self, config: Config, api: BiliApi, storage: Storage):
        self.config = config
        self.api = api
        self.storage = storage
        self.limiter = RateLimiter(int(config.get("speed_limit_kbps") or 0) * 1024)
        self.downloader = ChunkedDownloader(config, self.limiter)
        self.muxer = Muxer(config)
        self.metadata = MetadataFetcher(api, config)
        self._cancel_ids: Set[str] = set()
        self._pause_ids: Set[str] = set()
        self._lock = threading.Lock()
        self._running = False
        # 统计
        self.stats: Dict[str, int] = {"done": 0, "failed": 0, "cancelled": 0}

    # ---------- 任务构建 ----------
    def build_tasks(
        self,
        result: ParseResult,
        indices: Optional[List[int]] = None,
        quality: int = 0,
        codec_pref: str = "",
        audio_only: Optional[bool] = None,
    ) -> List[DownloadTask]:
        """从解析结果构建任务列表。indices 为基于 1 的序号。"""
        quality = quality or int(self.config.get("quality") or 0)
        codec_pref = codec_pref or str(self.config.get("codec_pref") or "AVC,HEVC,AV1")
        audio_only = bool(audio_only if audio_only is not None else self.config.get("download_audio_only"))
        dedup = bool(self.config.get("dedup"))

        tasks: List[DownloadTask] = []
        for ep in result.episodes:
            if indices is not None and ep.index not in indices:
                continue
            if dedup and self.storage.is_downloaded(ep.aid, ep.cid):
                logger.info("已下载，跳过: %s", shorten(ep.display_title, 40))
                continue
            task_id = f"{ep.aid}-{ep.cid}-{uuid.uuid4().hex[:8]}"
            task = DownloadTask(
                task_id=task_id,
                episode=ep,
                status=STATUS_PENDING,
                created_at=now_iso(),
                quality=quality,
                codec_pref=codec_pref,
                extra={
                    "audio_only": audio_only,
                    "media_id": result.media_id,
                    "series": result.series_title,
                    "save_subtitle": bool(self.config.get("save_subtitle")),
                    "save_danmaku": bool(self.config.get("save_danmaku")),
                    "danmaku_ass": bool(self.config.get("danmaku_ass")),
                    "save_cover": bool(self.config.get("save_cover")),
                    "save_nfo": bool(self.config.get("save_nfo")),
                    "season": int(self.config.get("season") or 1),
                    "series_description": result.description,
                    "name_template": str(self.config.get("name_template")),
                },
            )
            tasks.append(task)
            self.storage.upsert_task(task)
        return tasks

    # ---------- 输出路径 ----------
    def _output_path(self, task: DownloadTask) -> str:
        ep = task.episode
        template = task.extra.get("name_template") or str(self.config.get("name_template"))
        ext = "m4a" if task.extra.get("audio_only") else "mp4"
        stream = task.stream
        variables = naming_variables(
            ep, ext=ext,
            quality_name=stream.quality_name if stream else "",
            qn=task.quality or (stream.quality if stream else 0),
            codec=stream.video_codec if stream else "",
            season=int(task.extra.get("season") or 1),
            series_title=task.extra.get("series", ""),
        )
        name = naming_render(template, variables)
        if not name.lower().endswith("." + ext):
            name = name + "." + ext
        return os.path.join(self.config.download_dir, name)

    # ---------- 单任务执行 ----------
    def _execute(self, task: DownloadTask) -> None:
        if self._is_cancelled(task.task_id):
            self._set_status(task, STATUS_CANCELLED, "用户取消")
            return
        self._set_status(task, STATUS_DOWNLOADING)

        try:
            # 1. 解析 cid
            if not task.episode.cid:
                cid = self.api.get("https://api.bilibili.com/x/web-interface/view",
                                   params={"bvid": task.episode.bvid})
                task.episode.cid = int((cid or {}).get("cid", 0))
                task.episode.aid = int((cid or {}).get("aid", task.episode.aid))

            # 2. 获取播放流
            stream = self.api.parse_stream(
                aid=task.episode.aid,
                cid=task.episode.cid,
                bvid=task.episode.bvid,
                qn=task.quality,
                is_pgc=task.episode.is_pgc,
                codec_pref=task.codec_pref,
                audio_only=bool(task.extra.get("audio_only")),
            )
            task.stream = stream
            task.total_bytes = stream.video_size + stream.audio_size
            self.storage.upsert_task(task)

            output = self._output_path(task)
            task.output_path = output
            ensure_dir(os.path.dirname(output))
            work_dir = os.path.join(os.path.dirname(output), f".bili-{task.task_id}")
            ensure_dir(work_dir)
            task.work_dir = work_dir

            # 3. 下载音视频流
            def progress(done: int, total: int, speed: float) -> None:
                with self._lock:
                    task.downloaded_bytes = done
                    task.speed = speed
                    if total:
                        task.progress = min(100.0, done / total * 100)
                self.storage.upsert_task(task)

            video_file = None
            if stream.video_url:
                video_file = os.path.join(work_dir, "video.m4s")
                self.downloader.download(stream.video_url, video_file,
                                         stream.backup_urls, progress, "video")
                self.downloader.cleanup_markers(video_file)

            audio_file = None
            if stream.audio_url:
                audio_file = os.path.join(work_dir, "audio.m4s")
                self.downloader.download(stream.audio_url, audio_file, [], progress, "audio")
                self.downloader.cleanup_markers(audio_file)

            if self._is_cancelled(task.task_id):
                self._set_status(task, STATUS_CANCELLED, "用户取消")
                return

            # 4. 混流封装
            self._set_status(task, STATUS_MERGING)
            metadata = {
                "title": task.episode.display_title,
                "show": task.episode.series,
                "comment": "Bilibili",
            }
            if need_mux(stream):
                self.muxer.mux(
                    output,
                    video_path=video_file,
                    audio_path=audio_file,
                    metadata=metadata,
                    audio_only=bool(task.extra.get("audio_only")),
                )
            elif video_file:
                os.replace(video_file, output)
            elif audio_file:
                os.replace(audio_file, output)
            else:
                raise DownloadError("没有可封装的媒体流")

            # 5. 辅助资源
            try:
                if task.extra.get("save_cover"):
                    self.metadata.download_cover(task.episode, os.path.dirname(output))
                if task.extra.get("save_subtitle"):
                    self.metadata.download_subtitle(task.episode, os.path.dirname(output))
                if task.extra.get("save_danmaku"):
                    self.metadata.download_danmaku(task.episode, os.path.dirname(output))
                    if task.extra.get("danmaku_ass"):
                        self.metadata.convert_danmaku_to_ass(task.episode, os.path.dirname(output))
                if task.extra.get("save_nfo"):
                    series_id = task.extra.get("media_id", "")
                    self.metadata.write_nfo(
                        task.episode, os.path.dirname(output), output,
                        series_meta={
                            "title": task.extra.get("series", "") or task.episode.series,
                            "description": task.extra.get("series_description", ""),
                            "cover": task.episode.cover,
                            "season": int(task.extra.get("season") or 1),
                            "year": task.episode.pubdate,
                            "id": series_id,
                        } if series_id else None,
                    )
            except Exception as exc:
                logger.warning("辅助资源下载失败（不影响主文件）: %s", exc)

            # 6. 完成
            self._set_status(task, STATUS_DONE, progress=100.0)
            task.finished_at = now_iso()
            task.error = ""
            self.storage.add_history(task, task.extra.get("media_id", ""))
            self.storage.upsert_task(task)
            with self._lock:
                self.stats["done"] += 1
            self._log_done(task)
            self._cleanup_work(task)

        except (BiliApiError, DownloadError, MuxError, OSError) as exc:
            self._handle_failure(task, str(exc))
        except Exception as exc:  # 兜底，保证队列不崩
            logger.exception("任务 %s 未预期异常", task.task_id)
            self._handle_failure(task, f"未预期异常: {exc}")

    def _handle_failure(self, task: DownloadTask, error: str) -> None:
        max_retries = int(self.config.get("retries") or 3)
        if task.retries < max_retries:
            task.retries += 1
            task.status = STATUS_PENDING
            task.error = f"失败（重试 {task.retries}/{max_retries}）: {shorten(error, 120)}"
            task.finished_at = ""
            self.storage.upsert_task(task)
            logger.warning("任务失败，重试 %d/%d: %s", task.retries, max_retries,
                           shorten(task.episode.display_title, 40))
            time.sleep(float(self.config.get("retry_delay") or 2))
            if not self._is_cancelled(task.task_id):
                self._execute(task)
            return
        self._set_status(task, STATUS_FAILED, error=shorten(error, 200))
        task.finished_at = now_iso()
        self.storage.upsert_task(task)
        with self._lock:
            self.stats["failed"] += 1
        logger.error("任务失败: %s - %s", shorten(task.episode.display_title, 40), shorten(error, 100))

    def _set_status(self, task: DownloadTask, status: str,
                    error: str = "", progress: Optional[float] = None) -> None:
        with self._lock:
            task.status = status
            if error:
                task.error = error
            if progress is not None:
                task.progress = progress
        self.storage.upsert_task(task)

    def _is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancel_ids or task_id in self._pause_ids

    def _cleanup_work(self, task: DownloadTask) -> None:
        if not task.work_dir or not os.path.isdir(task.work_dir):
            return
        try:
            import shutil

            shutil.rmtree(task.work_dir)
        except OSError:
            pass
        task.work_dir = ""

    def _log_done(self, task: DownloadTask) -> None:
        ep = task.episode
        qn = task.stream.quality_name if task.stream else QUALITY_MAP.get(task.quality, str(task.quality))
        print(f"  ✓ {shorten(ep.display_title, 42):<44} "
              f"{format_size(os.path.getsize(task.output_path)):>9}  "
              f"{qn}  → {shorten(task.output_path, 60)}")

    # ---------- 队列调度 ----------
    def run(self, tasks: List[DownloadTask], concurrency: Optional[int] = None) -> Dict[str, int]:
        """运行任务队列（阻塞直到全部结束）。"""
        concurrency = concurrency or self.config.concurrency
        pending = [t for t in tasks if t.status in (STATUS_PENDING, STATUS_PAUSED, STATUS_DOWNLOADING)]
        if not pending:
            return dict(self.stats)
        self._running = True
        print(f"\n开始下载：{len(pending)} 个任务，并发 {concurrency}，"
              f"单文件 {self.config.threads} 线程，限速 "
              f"{'不限' if not self.config.get('speed_limit_kbps') else str(self.config.get('speed_limit_kbps')) + ' KB/s'}")
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(self._execute, t): t for t in pending}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.exception("任务调度异常 %s", task.task_id)
                    self._set_status(task, STATUS_FAILED, str(exc))
        self._running = False
        print(f"\n下载结束：成功 {self.stats['done']}，失败 {self.stats['failed']}，"
              f"取消 {self.stats['cancelled']}")
        if self.stats["failed"]:
            failed = self.storage.list_tasks([STATUS_FAILED])
            print("失败列表：")
            for t in failed:
                print(f"  - {shorten(t.episode.display_title, 50)}: {shorten(t.error, 60)}")
        return dict(self.stats)

    def resume_all(self, concurrency: Optional[int] = None) -> Dict[str, int]:
        """恢复上次未完成的任务。"""
        interrupted = self.storage.mark_interrupted()
        pending = self.storage.list_tasks([STATUS_PENDING, STATUS_PAUSED, STATUS_DOWNLOADING])
        if interrupted:
            print(f"已重置 {interrupted} 个中断任务为待下载状态")
        return self.run(pending, concurrency)

    # ---------- 控制 ----------
    def pause(self, task_id: str) -> bool:
        task = self.storage.get_task(task_id)
        if not task or task.status in TERMINAL_STATUSES:
            return False
        with self._lock:
            self._pause_ids.add(task_id)
        if task.status in (STATUS_PENDING, STATUS_DOWNLOADING):
            self._set_status(task, STATUS_PAUSED)
        return True

    def resume(self, task_id: str) -> bool:
        task = self.storage.get_task(task_id)
        if not task or task.status != STATUS_PAUSED:
            return False
        with self._lock:
            self._pause_ids.discard(task_id)
        self._set_status(task, STATUS_PENDING)
        return True

    def cancel(self, task_id: str) -> bool:
        task = self.storage.get_task(task_id)
        if not task or task.status in TERMINAL_STATUSES:
            return False
        with self._lock:
            self._cancel_ids.add(task_id)
            self._pause_ids.discard(task_id)
        self.downloader.cancel()
        if task.status in (STATUS_PENDING, STATUS_DOWNLOADING, STATUS_PAUSED):
            self._set_status(task, STATUS_CANCELLED, "用户取消")
            with self._lock:
                self.stats["cancelled"] += 1
        return True

    def retry_failed(self) -> List[DownloadTask]:
        """把所有失败任务重置为 pending，便于一键重试。"""
        failed = self.storage.list_tasks([STATUS_FAILED])
        for t in failed:
            t.status = STATUS_PENDING
            t.retries = 0
            t.error = ""
            self.storage.upsert_task(t)
        if failed:
            print(f"已重置 {len(failed)} 个失败任务，可运行 resume 重试")
        return failed
