"""命名模板引擎：支持丰富占位符、中文别名与格式说明符。

模板语法兼容 Python str.format 的子集，并额外支持中文别名：
    {series}/{index} - {title}.{ext}
    {系列名}/S{season:02d}E{index:02d} - {标题}.{ext}

可用占位符：
    series / 系列名      系列或番剧名
    title  / 标题        分集标题
    index  / 集数        集序号（默认补零两位，可用 :02d / :03d 控制）
    season / 季          赛季号（番剧可用，默认 1）
    page   / 分P         分 P 序号
    bvid               BV 号
    aid                av 号
    cid                cid
    quality / 画质       画质名称，如 1080P
    qn                 画质数值，如 80
    codec / 编码         视频编码，如 AVC
    year  / 年份         发布年份
    date  / 日期         发布日期 YYYY-MM-DD
    pubdate             发布时间戳
    duration / 时长      时长（秒）
    ext                 文件扩展名（mp4/m4a）
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, Optional

from .models import Episode
from .utils import sanitize_filename

# 中文别名 -> 标准名
ALIASES: Dict[str, str] = {
    "系列名": "series", "系列": "series", "番剧名": "series", "剧名": "series",
    "标题": "title", "分集标题": "title",
    "集数": "index", "集": "index", "话数": "index",
    "季": "season", "季度": "season", "season": "season",
    "分P": "page", "分p": "page", "P": "page", "p": "page",
    "画质": "quality", "清晰度": "quality",
    "编码": "codec", "视频编码": "codec",
    "年份": "year", "年": "year",
    "日期": "date",
    "时长": "duration",
}

# 标准占位符集合（用于校验与预览）
FIELDS = {
    "series", "title", "index", "season", "page", "bvid", "aid", "cid",
    "quality", "qn", "codec", "year", "date", "pubdate", "duration", "ext",
}

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def build_variables(episode: Episode, ext: str = "mp4",
                    quality_name: str = "", qn: int = 0, codec: str = "",
                    season: int = 1, series_title: str = "") -> Dict[str, Any]:
    """构建模板变量字典（已做文件名清洗）。"""
    series = episode.series or series_title or ""
    title = episode.display_title
    pub = int(episode.pubdate or 0)
    if pub:
        dt = _dt.datetime.fromtimestamp(pub)
        year = str(dt.year)
        date = dt.strftime("%Y-%m-%d")
    else:
        year, date = "", ""
    return {
        "series": sanitize_filename(series),
        "title": sanitize_filename(title),
        "index": int(episode.index or 1),
        "season": int(season or 1),
        "page": int(episode.page or 0),
        "bvid": episode.bvid or "",
        "aid": str(episode.aid or ""),
        "cid": str(episode.cid or ""),
        "quality": sanitize_filename(quality_name) if quality_name else "",
        "qn": int(qn or 0),
        "codec": codec or "",
        "year": year,
        "date": date,
        "pubdate": pub,
        "duration": int(episode.duration or 0),
        "ext": ext,
    }


def render(template: str, variables: Dict[str, Any],
           fallback: str = "{series}/{index} - {title}.{ext}") -> str:
    """渲染模板。中文别名自动映射；未知占位符用空串替代。"""
    def convert(match: re.Match) -> str:
        expr = match.group(1)
        # 拆分字段名与格式说明符，如 "index:03d" -> ("index", "03d")
        if ":" in expr:
            name, spec = expr.split(":", 1)
        else:
            name, spec = expr, ""
        name = name.strip()
        name = ALIASES.get(name, name)
        if name not in FIELDS:
            return ""
        value = variables.get(name, "")
        if not spec:
            # 数字类型默认补零两位（index/season/page），其余原样
            if name in ("index", "season", "page") and isinstance(value, int):
                return f"{value:02d}"
            if name == "duration" and isinstance(value, int):
                h, rem = divmod(value, 3600)
                m, s = divmod(rem, 60)
                return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            return str(value)
        try:
            return format(value, spec)
        except (ValueError, TypeError):
            return str(value)

    try:
        result = _PLACEHOLDER_RE.sub(convert, template)
    except Exception:
        result = _PLACEHOLDER_RE.sub(convert, fallback)
    # 清洗路径分隔符之外的非法字符
    parts = result.split("/")
    parts = [sanitize_filename(p) if p else p for p in parts]
    return "/".join(parts)


def validate(template: str) -> list:
    """返回模板中未知占位符列表。"""
    unknown = []
    for expr in _PLACEHOLDER_RE.findall(template):
        name = expr.split(":", 1)[0].strip()
        name = ALIASES.get(name, name)
        if name not in FIELDS:
            unknown.append(name)
    return unknown


def preview(template: str, episode: Optional[Episode] = None) -> str:
    """预览模板渲染结果。"""
    if episode is None:
        episode = Episode(
            aid=12345678, cid=98765432, bvid="BV1xx411c7mD",
            title="示例分集标题", series="示例番剧", index=3,
            duration=1420, pubdate=1700000000,
        )
    variables = build_variables(episode, ext="mp4", quality_name="1080P", qn=80,
                                codec="AVC", season=1)
    return render(template, variables)


AVAILABLE_PLACEHOLDERS = sorted(FIELDS)
