"""数据模型：解析结果、分集、任务状态。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .constants import QUALITY_MAP


@dataclass
class Episode:
    """一个可下载的分集（对应一个 cid）。"""

    aid: int
    cid: int
    bvid: str = ""
    title: str = ""
    series: str = ""           # 系列/番剧名，用于命名模板
    index: int = 0             # 在系列中的序号（从 1 开始）
    duration: int = 0          # 秒
    cover: str = ""
    is_pgc: bool = False       # 是否为番剧（走 pgc playurl 接口）
    pubdate: int = 0
    page: int = 0              # 分 P 序号

    @property
    def display_title(self) -> str:
        return self.title or f"P{self.page or self.index}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Episode":
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class ParseResult:
    """链接解析结果。"""

    media_id: str                       # bvid 或 ssid，用于去重/标识
    series_title: str
    episodes: List[Episode] = field(default_factory=list)
    cover: str = ""
    description: str = ""
    quality_options: List[int] = field(default_factory=list)  # 可用 qn 列表
    type: str = "video"                 # video / pgc / collection / space / fav

    def quality_names(self) -> str:
        return ", ".join(
            f"{qn}:{QUALITY_MAP.get(qn, str(qn))}" for qn in sorted(set(self.quality_options))
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StreamInfo:
    """一个分集的可用音视频流。"""

    cid: int
    quality: int
    quality_name: str
    format: str
    timelength: int = 0
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    video_size: int = 0
    audio_size: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    backup_urls: List[str] = field(default_factory=list)  # 备用 CDN

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StreamInfo":
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


# 任务状态常量
STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_PAUSED = "paused"
STATUS_MERGING = "merging"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_DOWNLOADING, STATUS_PAUSED, STATUS_MERGING)
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)


@dataclass
class DownloadTask:
    """下载队列中的单个任务。"""

    task_id: str
    episode: Episode
    stream: Optional[StreamInfo] = None
    output_path: str = ""        # 最终输出文件
    work_dir: str = ""           # 临时分片目录
    status: str = STATUS_PENDING
    progress: float = 0.0        # 0-100
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    retries: int = 0
    error: str = ""
    created_at: str = ""
    finished_at: str = ""
    quality: int = 0             # 用户选择的 qn
    codec_pref: str = "AVC"
    extra: Dict[str, Any] = field(default_factory=dict)  # subtitle/danmaku/cover/nfo 等开关

    def to_row(self) -> dict:
        data = asdict(self)
        data["episode"] = json.dumps(self.episode.to_dict(), ensure_ascii=False)
        data["stream"] = json.dumps(self.stream.to_dict(), ensure_ascii=False) if self.stream else None
        data["extra"] = json.dumps(self.extra, ensure_ascii=False)
        return data

    @classmethod
    def from_row(cls, row: dict) -> "DownloadTask":
        r = dict(row)
        r["episode"] = Episode.from_dict(json.loads(r["episode"])) if r.get("episode") else None
        r["stream"] = StreamInfo.from_dict(json.loads(r["stream"])) if r.get("stream") else None
        r["extra"] = json.loads(r["extra"]) if r.get("extra") else {}
        fields = {k: v for k, v in r.items() if k in cls.__dataclass_fields__}
        return cls(**fields)
