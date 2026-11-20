"""ffmpeg -c copy 无损混流封装与辅助资源下载。

不重新编码，仅做容器封装，保证原始画质/音质。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Dict, List, Optional

from .config import Config
from .models import StreamInfo
from .utils import ensure_dir, find_ffmpeg, sanitize_filename

logger = logging.getLogger("bili.muxer")


class MuxError(RuntimeError):
    pass


def _safe_output(path: str) -> str:
    """确保输出路径存在且后缀合法。"""
    ensure_dir(os.path.dirname(path))
    return path


def need_mux(stream: StreamInfo) -> bool:
    """是否需要混流：DASH 音视频分离时需要，mp4 单文件不需要。"""
    return stream.format == "dash" and bool(stream.video_url and stream.audio_url)


def build_ffmpeg_args(
    video_path: Optional[str],
    audio_path: Optional[str],
    output: str,
    cover_path: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
    audio_only: bool = False,
) -> List[str]:
    args: List[str] = ["-y", "-loglevel", "error"]
    has_cover = bool(cover_path and os.path.isfile(cover_path))
    if has_cover:
        if audio_only:
            # 纯音频：单张封面图，不需要 -loop（loop 会生成无限视频流）
            args += ["-i", cover_path]
        else:
            args += ["-loop", "1", "-framerate", "1", "-i", cover_path]
    if video_path and not audio_only:
        args += ["-i", video_path]
    if audio_path:
        args += ["-i", audio_path]

    # 输入索引：封面为 0（若有），视频为 1 或 0，音频随后
    cover_idx = 0 if has_cover else None
    video_idx = (1 if cover_idx is not None else 0) if video_path and not audio_only else None
    audio_idx = (video_idx + 1 if video_idx is not None else (1 if cover_idx is not None else 0)) if audio_path else None

    map_args: List[str] = []
    if audio_only:
        if audio_idx is not None:
            map_args += ["-map", f"{audio_idx}:a:0"]
        args += ["-c:a", "copy"]
    else:
        if video_idx is not None:
            map_args += ["-map", f"{video_idx}:v:0"]
        if audio_idx is not None:
            map_args += ["-map", f"{audio_idx}:a:0"]
        args += ["-c:v", "copy", "-c:a", "copy"]
    args += map_args
    if cover_idx is not None:
        # 封面作为附加图片流（attached_pic），音乐播放器借此显示专辑图
        if audio_only:
            # 纯音频：音频是输出流 0，封面是第一条（也是唯一一条）视频流 v:0
            args += ["-map", f"{cover_idx}:v:0", "-c:v:0", "copy",
                     "-disposition:v:0", "attached_pic"]
        else:
            # 视频模式：主视频是 v:0，附加封面是 v:1
            args += ["-map", f"{cover_idx}:v:0", "-c:v:1", "copy",
                     "-disposition:v:1", "attached_pic"]

    if metadata:
        for k, v in metadata.items():
            if v:
                args += ["-metadata", f"{k}={v}"]

    ext = os.path.splitext(output)[1].lower()
    if ext == ".m4a":
        args += ["-f", "mp4"]
    elif ext == ".flac":
        args += ["-f", "flac"]
    elif ext == ".mkv":
        args += ["-f", "matroska"]
    args += [_safe_output(output)]
    return args


class Muxer:
    def __init__(self, config: Config):
        self.config = config

    def mux(
        self,
        output: str,
        video_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        cover_path: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        audio_only: bool = False,
    ) -> str:
        ffmpeg = find_ffmpeg(str(self.config.get("ffmpeg_path") or ""))
        if not ffmpeg:
            raise MuxError(
                "未找到 ffmpeg。请安装 ffmpeg 或在配置中指定 ffmpeg_path。"
            )
        output = _safe_output(output)
        args = build_ffmpeg_args(video_path, audio_path, output, cover_path, metadata, audio_only)
        logger.debug("ffmpeg 参数: %s", " ".join(args))
        try:
            proc = subprocess.run([ffmpeg] + args, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MuxError(f"ffmpeg 执行失败: {exc}") from exc
        if proc.returncode != 0:
            raise MuxError(f"ffmpeg 混流失败: {proc.stderr.strip()[:300]}")
        if not os.path.isfile(output) or os.path.getsize(output) == 0:
            raise MuxError("ffmpeg 未生成有效输出文件")
        return output
