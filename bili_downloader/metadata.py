"""辅助资源下载：字幕、弹幕、封面、NFO（Emby/Jellyfin/Plex 刮削）。"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from .api import BiliApi
from .config import Config
from .constants import API, DEFAULT_HEADERS
from .models import Episode, StreamInfo
from .utils import ensure_dir, sanitize_filename

logger = logging.getLogger("bili.metadata")


class MetadataFetcher:
    def __init__(self, api: BiliApi, config: Config):
        self.api = api
        self.config = config

    def _fetch_bytes(self, url: str, referer: str = "https://www.bilibili.com/") -> bytes:
        import zlib
        headers = dict(DEFAULT_HEADERS)
        headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=int(self.config.get("timeout") or 20)) as resp:
            data = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
        # B站弹幕接口返回 deflate（raw DEFLATE，wbits=-15），urllib 不自动解压
        if encoding == "gzip":
            import gzip
            return gzip.decompress(data)
        if encoding == "deflate":
            try:
                return zlib.decompress(data, -zlib.MAX_WBITS)  # raw deflate
            except zlib.error:
                try:
                    return zlib.decompress(data)  # zlib 包装
                except zlib.error:
                    pass
        if data[:1] == b"\x78" and len(data) > 2:  # zlib 魔数
            try:
                return zlib.decompress(data)
            except zlib.error:
                pass
        return data

    def download_cover(self, episode: Episode, dest_dir: str) -> Optional[str]:
        if not episode.cover:
            return None
        ext = ".jpg"
        m = re.search(r"\.(jpg|jpeg|png|webp)", episode.cover, re.I)
        if m:
            ext = "." + m.group(1).lower()
        dest = os.path.join(dest_dir, "cover" + ext)
        if os.path.isfile(dest):
            return dest
        try:
            with open(dest, "wb") as f:
                f.write(self._fetch_bytes(episode.cover))
            return dest
        except Exception as exc:
            logger.debug("封面下载失败: %s", exc)
            return None

    def download_subtitle(self, episode: Episode, dest_dir: str) -> List[str]:
        """下载字幕（优先 srt，无则 ass），返回保存的文件列表。"""
        saved: List[str] = []
        if not episode.bvid or not episode.cid:
            return saved
        try:
            data = self.api.get(API["player_v2"], params={"bvid": episode.bvid, "cid": episode.cid})
        except Exception as exc:
            logger.debug("字幕列表获取失败: %s", exc)
            return saved
        subs = (data or {}).get("subtitle", {}).get("subtitles", []) if isinstance(data, dict) else []
        for sub in subs:
            url = sub.get("subtitle_url", "")
            if url.startswith("//"):
                url = "https:" + url
            if not url:
                continue
            lan = sanitize_filename(sub.get("lan_doc") or sub.get("lan", "zh"), 40)
            ext = "srt" if "srt" in url.lower() else "ass"
            dest = os.path.join(dest_dir, f"{lan}.{ext}")
            if os.path.isfile(dest):
                saved.append(dest)
                continue
            try:
                payload = self._fetch_bytes(url)
                obj = json.loads(payload.decode("utf-8-sig"))
                if isinstance(obj, dict) and "body" in obj:
                    content = self._srt_from_json(obj) if ext == "srt" else self._ass_from_json(obj)
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(content)
                    saved.append(dest)
            except Exception as exc:
                logger.debug("字幕 %s 下载失败: %s", lan, exc)
        return saved

    @staticmethod
    def _srt_from_json(obj: dict) -> str:
        def ts(sec: float) -> str:
            ms = int(sec * 1000)
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        blocks = []
        for i, line in enumerate(obj.get("body", []), 1):
            start = float(line.get("from", 0))
            end = float(line.get("to", 0))
            blocks.append(f"{i}\n{ts(start)} --> {ts(end)}\n{line.get('location', '')}\n")
        return "\n".join(blocks)

    @staticmethod
    def _ass_from_json(obj: dict) -> str:
        header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

        def ts(sec: float) -> str:
            cs = int(sec * 100)
            h, cs = divmod(cs, 360000)
            m, cs = divmod(cs, 6000)
            s, cs = divmod(cs, 100)
            return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

        lines = []
        for item in obj.get("body", []):
            text = item.get("location", "").replace("\n", "\\N")
            lines.append(f"Dialogue: 0,{ts(float(item.get('from', 0)))},"
                         f"{ts(float(item.get('to', 0)))},Default,,0,0,0,,{text}")
        return header + "\n".join(lines) + "\n"

    def download_danmaku(self, episode: Episode, dest_dir: str) -> Optional[str]:
        dest = os.path.join(dest_dir, "danmaku.xml")
        if os.path.isfile(dest):
            return dest
        url = API["danmaku"].format(cid=episode.cid)
        try:
            with open(dest, "wb") as f:
                f.write(self._fetch_bytes(url, f"https://www.bilibili.com/video/{episode.bvid}"))
            return dest
        except Exception as exc:
            logger.debug("弹幕下载失败: %s", exc)
            return None

    def write_nfo(self, episode: Episode, dest_dir: str, video_file: str,
                  series_meta: Optional[dict] = None) -> Optional[str]:
        """写入剧集 NFO（episodedetails），并按需生成 tvshow.nfo / season.nfo。

        series_meta 可选字段：title, description, cover, season, episodes, year。
        """
        dest = os.path.splitext(video_file)[0] + ".nfo"
        if not os.path.isfile(dest):
            season = int((series_meta or {}).get("season") or 1)
            desc = str((series_meta or {}).get("description") or "")
            lines = self._episode_nfo_lines(episode, season, desc)
            try:
                with open(dest, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            except OSError as exc:
                logger.debug("NFO 写入失败: %s", exc)
                return None

        # 系列级 NFO（放在系列根目录）
        if series_meta:
            series_title = str(series_meta.get("title") or episode.series)
            show_dir = os.path.dirname(dest_dir) if os.path.basename(dest_dir).lower().startswith("season") else dest_dir
            self._write_tvshow_nfo(show_dir, series_title, series_meta)
        return dest

    @staticmethod
    def _episode_nfo_lines(episode: Episode, season: int = 1,
                           description: str = "") -> List[str]:
        title = xml_escape(episode.display_title)
        series = xml_escape(episode.series)
        # plot：分集自身没简介时（番剧接口通常只给「第N话」），
        # 退而用整季简介，总比把标题重复一遍有用。
        plot = xml_escape(episode.title or episode.display_title)
        if description and description.strip() and description.strip() != episode.title:
            plot = xml_escape(description.strip()[:800])
        lines = [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<episodedetails>",
            f"  <title>{title}</title>",
            f"  <showtitle>{series}</showtitle>",
            f"  <season>{season}</season>",
            f"  <episode>{max(1, episode.index)}</episode>",
            f"  <plot>{plot}</plot>",
            f"  <runtime>{episode.duration // 60}</runtime>",
            "  <source>Bilibili</source>",
            f"  <uniqueid type=\"bilibili\" default=\"true\">{episode.aid}:{episode.cid}</uniqueid>",
        ]
        if episode.pubdate:
            import datetime as _dt
            dt = _dt.datetime.fromtimestamp(episode.pubdate)
            lines.append(f"  <aired>{dt.strftime('%Y-%m-%d')}</aired>")
            lines.append(f"  <year>{dt.year}</year>")
        if episode.cover:
            lines += ["  <art>", f"    <poster>{xml_escape(episode.cover)}</poster>", "  </art>"]
        lines.append("</episodedetails>")
        return lines

    def _write_tvshow_nfo(self, show_dir: str, title: str, meta: dict) -> None:
        """写入 tvshow.nfo（Emby/Jellyfin/Plex 系列刮削）。"""
        dest = os.path.join(show_dir, "tvshow.nfo")
        if os.path.isfile(dest):
            return
        lines = [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<tvshow>",
            f"  <title>{xml_escape(title)}</title>",
            f"  <showtitle>{xml_escape(title)}</showtitle>",
        ]
        if meta.get("description"):
            lines.append(f"  <plot>{xml_escape(str(meta['description'])[:800])}</plot>")
        year = meta.get("year")
        if year:
            import datetime as _dt
            try:
                year = _dt.datetime.fromtimestamp(int(year)).year
            except (ValueError, OSError, TypeError):
                pass
            lines.append(f"  <year>{year}</year>")
        if meta.get("episodes"):
            lines.append(f"  <episode>{int(meta['episodes'])}</episode>")
        if meta.get("cover"):
            lines += ["  <art>", f"    <poster>{xml_escape(str(meta['cover']))}</poster>", "  </art>"]
        lines += [
            "  <source>Bilibili</source>",
            f"  <uniqueid type=\"bilibili\" default=\"true\">{xml_escape(str(meta.get('id', '')))}</uniqueid>",
            "</tvshow>",
        ]
        try:
            ensure_dir(show_dir)
            with open(dest, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as exc:
            logger.debug("tvshow.nfo 写入失败: %s", exc)

    def convert_danmaku_to_ass(self, episode: Episode, dest_dir: str) -> Optional[str]:
        """把下载的 danmaku.xml 转换为 ASS 弹幕文件（便于播放器加载）。"""
        src = os.path.join(dest_dir, "danmaku.xml")
        dest = os.path.join(dest_dir, "danmaku.ass")
        if not os.path.isfile(src):
            return None
        if os.path.isfile(dest):
            return dest
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(src)
            root = tree.getroot()
            header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,55,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
            def ts(sec: float) -> str:
                cs = int(sec * 100)
                h, cs = divmod(cs, 360000)
                m, cs = divmod(cs, 6000)
                s, cs = divmod(cs, 100)
                return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

            lines = []
            for i, d in enumerate(root.findall(".//d")):
                p = (d.get("p") or "").split(",")
                if len(p) < 5:
                    continue
                try:
                    start = float(p[0])
                    mode = p[1]
                    fontsize = int(float(p[2]))
                    color = int(p[3]) & 0xFFFFFF
                    text = (d.text or "").replace("\n", "\\N").replace("{", "\\{").replace("}", "\\}")
                except (ValueError, IndexError):
                    continue
                # BGR -> ASS &HAABBGGRR
                ass_color = f"&H00{(color & 0xFF):02X}{(color >> 8 & 0xFF):02X}{(color >> 16 & 0xFF):02X}"
                pos = ""
                if mode == "4":  # 底部弹幕
                    pos = "{\\an8}"
                elif mode == "5":  # 顶部弹幕
                    pos = "{\\an8}"
                elif mode in ("6", "7", "8"):  # 高级弹幕，简化处理
                    pass
                else:
                    pass
                line = f"Dialogue: 0,{ts(start)},{ts(start + 8)},Default,,0,0,0,,{pos}{{\\fs{fontsize}}}{{\\c{ass_color}}}{text}"
                lines.append(line)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(header + "\n".join(lines) + "\n")
            return dest
        except Exception as exc:
            logger.debug("弹幕转 ASS 失败: %s", exc)
            return None

    def write_season_nfo(self, season_dir: str, season: int, cover: str = "") -> None:
        """写入 season.nfo。"""
        dest = os.path.join(season_dir, "season.nfo")
        if os.path.isfile(dest):
            return
        lines = [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<season>",
            f"  <seasonnumber>{season}</seasonnumber>",
        ]
        if cover:
            lines += ["  <art>", f"    <poster>{xml_escape(cover)}</poster>", "  </art>"]
        lines.append("</season>")
        try:
            ensure_dir(season_dir)
            with open(dest, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as exc:
            logger.debug("season.nfo 写入失败: %s", exc)
