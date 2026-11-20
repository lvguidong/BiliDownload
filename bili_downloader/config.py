"""配置管理：JSON 配置文件 + 默认值。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .constants import DEFAULT_NAME_TEMPLATE
from .utils import ensure_dir, find_ffmpeg

DEFAULTS: Dict[str, Any] = {
    "download_dir": "./downloads",
    "threads": 8,                 # 单文件分片线程数 4-32
    "concurrency": 2,             # 同时下载任务数 2-3
    "quality": 0,                 # 0 = 自动最高画质
    "codec_pref": "AVC,HEVC,AV1", # 编码回退优先级
    "download_audio_only": False,
    "name_template": DEFAULT_NAME_TEMPLATE,
    "speed_limit_kbps": 0,        # 0 = 不限速，单位 KB/s
    "proxy": "",                  # http:// / socks5://
    "timeout": 20,
    "retries": 3,
    "retry_delay": 2.0,
    "chunk_size_mb": 4,
    "cdn_switch": True,
    "save_subtitle": False,
    "save_danmaku": False,
    "danmaku_ass": False,      # 弹幕同时转 ASS
    "save_cover": False,
    "save_nfo": False,
    "season": 1,               # 番剧赛季号（用于命名模板与 NFO）
    "dedup": True,
    "ffmpeg_path": "",
    "request_interval": 0.5,      # 保守请求间隔，降低风控风险
    "log_level": "INFO",
}


class Config:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.data: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, ValueError) as exc:
                print(f"[config] 读取配置失败，使用默认值: {exc}")

    def save(self) -> None:
        ensure_dir(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"未知配置项: {key}")
        self.data[key] = value

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            self.set(key, value)

    @property
    def download_dir(self) -> str:
        return os.path.abspath(os.path.expanduser(str(self.get("download_dir"))))

    @property
    def threads(self) -> int:
        return max(1, min(32, int(self.get("threads"))))

    @property
    def concurrency(self) -> int:
        return max(1, min(8, int(self.get("concurrency"))))

    @property
    def ffmpeg(self):
        return find_ffmpeg(str(self.get("ffmpeg_path") or ""))

    def resolve(self) -> Dict[str, Any]:
        """返回解析后的配置快照（含绝对路径）。"""
        snapshot = dict(self.data)
        snapshot["download_dir"] = self.download_dir
        snapshot["ffmpeg_path"] = self.ffmpeg or ""
        return snapshot

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Config {self.path}>"
