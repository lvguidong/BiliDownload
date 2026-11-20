"""链接解析层：BV/av、ep/ss 整季、分 P、合集、UP 主空间、收藏夹。

所有链接归一化为 ParseResult。解析层与下载层解耦，便于后续热更新/插件化。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .api import BiliApi, BiliApiError
from .constants import API, QUALITY_MAP
from .models import Episode, ParseResult
from .utils import shorten

logger = logging.getLogger("bili.parser")


class ParseError(ValueError):
    pass


_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:av|AV)(\d+)")
_EP_RE = re.compile(r"/bangumi/play/ep(\d+)")
_SS_RE = re.compile(r"/bangumi/play/ss(\d+)")
_EP_ID_RE = re.compile(r"[?&]ep_id=(\d+)")
_SEASON_ID_RE = re.compile(r"[?&]season_id=(\d+)")
# 允许直接粘贴裸编号：ep721702 / ss43981
_BARE_EP_RE = re.compile(r"^\s*ep(\d+)\s*$", re.IGNORECASE)
_BARE_SS_RE = re.compile(r"^\s*ss(\d+)\s*$", re.IGNORECASE)
_SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)")
_FAV_RE = re.compile(r"fid=(\d+)")
_COLLECTION_RE = re.compile(r"/medialist/detail/ml(\d+)")


class BiliParser:
    def __init__(self, api: BiliApi):
        self.api = api

    def parse(self, text: str, require_login: bool = False) -> ParseResult:
        """解析输入（可为完整链接或裸 BV/av 号）。"""
        text = (text or "").strip()
        if not text:
            raise ParseError("输入为空")
        if require_login and not self.api.is_logged_in:
            raise ParseError("该链接需要登录后解析，请先运行 login 命令")

        if (_EP_RE.search(text) or _EP_ID_RE.search(text)
                or _BARE_EP_RE.match(text)):
            return self.parse_ep(text)
        if (_SS_RE.search(text) or _SEASON_ID_RE.search(text)
                or _BARE_SS_RE.match(text)):
            return self.parse_season(text)
        if _SPACE_RE.search(text):
            return self.parse_space(text)
        if _FAV_RE.search(text) or "favlist" in text:
            return self.parse_favorites(text)
        if _COLLECTION_RE.search(text):
            return self.parse_collection(text)
        bv = _BVID_RE.search(text)
        if bv:
            return self.parse_bvid(bv.group(1))
        av = _AV_RE.search(text)
        if av:
            return self.parse_aid(int(av.group(1)))
        raise ParseError(f"无法识别链接: {shorten(text, 80)}")

    # ---------- 普通视频 ----------
    def parse_bvid(self, bvid: str) -> ParseResult:
        data = self.api.get(API["view"], params={"bvid": bvid})
        return self._from_view(data, media_id=bvid)

    def parse_aid(self, aid: int) -> ParseResult:
        data = self.api.get(API["view"], params={"aid": aid})
        return self._from_view(data, media_id=str(aid))

    def _from_view(self, data: Any, media_id: str = "") -> ParseResult:
        if not isinstance(data, dict) or "bvid" not in data:
            raise ParseError("视频信息接口未返回有效数据")
        bvid = data.get("bvid", media_id)
        aid = int(data.get("aid", 0))
        title = data.get("title", "")
        owner = data.get("owner") or {}
        series_name = title
        # 若属于合集/系列，使用合集名作为系列名
        if data.get("ugc_season"):
            series_name = data["ugc_season"].get("title") or title
        pics = data.get("pic", "")
        pages = data.get("pages") or []
        episodes: List[Episode] = []
        if pages:
            for page in pages:
                episodes.append(Episode(
                    aid=aid,
                    cid=int(page.get("cid", 0)),
                    bvid=bvid,
                    title=page.get("part") or title,
                    series=series_name,
                    index=int(page.get("page", len(episodes) + 1)),
                    duration=int(page.get("duration", 0)),
                    cover=pics,
                    page=int(page.get("page", 0)),
                    pubdate=int(data.get("pubdate", 0)),
                ))
        else:
            episodes.append(Episode(
                aid=aid,
                cid=int(data.get("cid", 0)),
                bvid=bvid,
                title=title,
                series=series_name,
                index=1,
                duration=int(data.get("duration", 0)),
                cover=pics,
                page=1,
                pubdate=int(data.get("pubdate", 0)),
            ))
        return ParseResult(
            media_id=bvid,
            series_title=series_name,
            episodes=episodes,
            cover=pics,
            description=str(data.get("desc", "")),
            quality_options=self._quality_hint(data),
            type="video",
        )

    @staticmethod
    def _quality_hint(data: dict) -> List[int]:
        qn = data.get("quality")
        if isinstance(qn, int) and qn:
            return [qn]
        return []

    # ---------- 番剧 ----------
    def parse_ep(self, text: str) -> ParseResult:
        m = _EP_RE.search(text) or _EP_ID_RE.search(text) or _BARE_EP_RE.match(text)
        if not m:
            raise ParseError("未找到 ep_id")
        ep_id = int(m.group(1))
        data = self.api.get(API["pgc_season"], params={"ep_id": ep_id})
        return self._from_season(data, preferred_ep=ep_id)

    def parse_season(self, text: str) -> ParseResult:
        m = _SS_RE.search(text) or _SEASON_ID_RE.search(text) or _BARE_SS_RE.match(text)
        if not m:
            raise ParseError("未找到 season_id")
        season_id = int(m.group(1))
        data = self.api.get(API["pgc_season"], params={"season_id": season_id})
        return self._from_season(data)

    def _from_season(self, data: Any, preferred_ep: Optional[int] = None) -> ParseResult:
        if not isinstance(data, dict):
            raise ParseError("番剧接口未返回有效数据")
        title = data.get("title") or ""
        season_id = int(data.get("season_id", 0))
        episodes_data = data.get("episodes") or []
        episodes: List[Episode] = []
        for ep in episodes_data:
            if int(ep.get("bad_type", 0) or 0) != 0:  # 跳过预告/花絮锁定项
                continue
            index = int(ep.get("i", len(episodes) + 1) or (len(episodes) + 1))
            raw_title = (ep.get("long_title") or ep.get("title") or "").strip()
            # 番剧接口的 title 常常只是集号（"1"、"2"），补成「第N话」更可读，
            # 否则分集列表和文件名都会变成孤零零的数字。
            if not raw_title:
                title_text = f"第{index}话"
            elif raw_title.isdigit():
                title_text = f"第{raw_title}话"
            else:
                title_text = raw_title
            episodes.append(Episode(
                aid=int(ep.get("aid", 0) or 0),
                cid=int(ep.get("cid", 0) or 0),
                bvid=ep.get("bvid", "") or "",
                title=title_text,
                series=title,
                index=index,
                duration=int((ep.get("duration") or 0) / 1000),
                cover=ep.get("cover", "") or data.get("cover", ""),
                is_pgc=True,
                # 老接口用 pub_ts，新接口用 pub_time（秒）
                pubdate=int(ep.get("pub_ts") or ep.get("pub_time") or 0),
            ))
        if not episodes:
            raise ParseError("该番剧暂无可下载分集（可能需要登录/大会员）")
        return ParseResult(
            media_id=f"ss{season_id}" if season_id else f"ep{preferred_ep or 0}",
            series_title=title,
            episodes=episodes,
            cover=data.get("cover", ""),
            description=str(data.get("evaluate", "")),
            quality_options=[],
            type="pgc",
        )

    # ---------- UP 主空间 ----------
    def parse_space(self, text: str) -> ParseResult:
        m = _SPACE_RE.search(text)
        if not m:
            raise ParseError("未找到 UID")
        mid = int(m.group(1))
        info = self.api.get_wbi(API["space_info"], {"mid": mid})
        uname = info.get("name", str(mid)) if isinstance(info, dict) else str(mid)
        page = 1
        episodes: List[Episode] = []
        # 保守翻页，最多 10 页，避免触发风控
        while page <= 10:
            data = self.api.get_wbi(API["space_arc"], {
                "mid": mid, "ps": 30, "pn": page, "order": "pubdate",
                "platform": "web", "web_location": "1550101",
            })
            vlist = (data or {}).get("list", {}).get("vlist", []) if isinstance(data, dict) else []
            if not vlist:
                break
            for v in vlist:
                episodes.append(Episode(
                    aid=int(v.get("aid", 0)),
                    cid=0,  # 下载时按 bvid 再解析 cid
                    bvid=v.get("bvid", ""),
                    title=v.get("title", ""),
                    series=f"{uname} 的投稿",
                    index=len(episodes) + 1,
                    duration=int(v.get("length", 0)) if str(v.get("length", "")).isdigit() else 0,
                    cover=v.get("pic", ""),
                    pubdate=int(v.get("created", 0)),
                ))
            if len(vlist) < 30:
                break
            page += 1
        if not episodes:
            raise ParseError("该 UP 主没有公开投稿")
        return ParseResult(
            media_id=f"uid{mid}",
            series_title=f"{uname} 的投稿",
            episodes=episodes,
            cover="",
            description="",
            quality_options=[],
            type="space",
        )

    # ---------- 收藏夹 ----------
    def parse_favorites(self, text: str) -> ParseResult:
        m = _FAV_RE.search(text)
        if not m:
            raise ParseError("未找到收藏夹 fid")
        fid = int(m.group(1))
        data = self.api.get(API["fav_resources"], {
            "media_id": fid, "ps": 30, "pn": 1, "type": 2,
        })
        info = data.get("info", {}) if isinstance(data, dict) else {}
        title = info.get("title", "收藏夹") if isinstance(info, dict) else "收藏夹"
        episodes: List[Episode] = []
        page = 1
        while page <= 20:
            data = self.api.get(API["fav_resources"], {
                "media_id": fid, "ps": 30, "pn": page, "type": 2,
            })
            medias = data.get("medias") or [] if isinstance(data, dict) else []
            if not medias:
                break
            for item in medias:
                if int(item.get("type", 0)) != 2:  # 2 = 视频
                    continue
                episodes.append(Episode(
                    aid=int(item.get("aid", 0)),
                    cid=0,
                    bvid=item.get("bvid", ""),
                    title=item.get("title", ""),
                    series=title,
                    index=len(episodes) + 1,
                    duration=int(item.get("duration", 0)),
                    cover=item.get("cover", ""),
                ))
            if len(medias) < 30:
                break
            page += 1
        if not episodes:
            raise ParseError("收藏夹为空或不可访问")
        return ParseResult(
            media_id=f"fav{fid}",
            series_title=title,
            episodes=episodes,
            cover="",
            description="",
            quality_options=[],
            type="fav",
        )

    # ---------- 合集 ----------
    def parse_collection(self, text: str) -> ParseResult:
        # 合集通过 ugc_season 接口解析：先取第一个 BV 再用 view 的 ugc_season 字段
        m = _COLLECTION_RE.search(text)
        if m:
            # 新版合集链接通常为 space.bilibili.com/xxx?sid=xxx，需登录；先提示
            raise ParseError("合集链接解析需要传入其中一个视频的 BV 号（暂不支持直接解析 ml 链接）")
        raise ParseError("未找到合集 ID")

    def resolve_cid(self, episode: Episode) -> int:
        """对 cid=0 的条目（空间/收藏夹）按 bvid 解析真实 cid。"""
        if episode.cid:
            return episode.cid
        if episode.bvid:
            data = self.api.get(API["view"], params={"bvid": episode.bvid})
            if isinstance(data, dict):
                episode.cid = int(data.get("cid", 0))
                episode.aid = int(data.get("aid", episode.aid))
        return episode.cid
