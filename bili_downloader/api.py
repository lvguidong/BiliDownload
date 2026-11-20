"""B 站 API 客户端：请求封装、Cookie、WBI 签名、扫码登录、播放地址解析。

仅使用标准库 urllib；支持代理、超时、重试、请求间隔（降低风控触发概率）。
所有响应均做 code 校验，错误信息不含 Cookie。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .constants import (API, CODEC_PREFIXES, DEFAULT_HEADERS, MIXIN_KEY_ENC_TAB,
                        QUALITY_MAP, QUALITY_ORDER, WBI_FILTER_CHARS)
from .cookie_store import CookieStore
from .models import StreamInfo
from .utils import cookie_to_string, parse_cookie_string, shorten

logger = logging.getLogger("bili.api")


class BiliApiError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _get_mixin_key(raw: str) -> str:
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _filter_value(value: str) -> str:
    return "".join(ch for ch in value if ch not in WBI_FILTER_CHARS)


class BiliApi:
    def __init__(self, config: Config, cookie_store: CookieStore):
        self.config = config
        self.cookie_store = cookie_store
        self.cookies: Dict[str, str] = cookie_store.load() if cookie_store.exists() else {}
        self._wbi_keys: Optional[Tuple[str, str]] = None
        self._last_request_time = 0.0
        self._ssl_ctx = ssl.create_default_context()
        # 某些环境证书链不全时可选关闭校验（不默认开启）
        if os.environ.get("BILI_DISABLE_SSL_VERIFY") == "1":
            self._ssl_ctx = ssl._create_unverified_context()

    # ---------- Cookie ----------
    def set_cookie_string(self, cookie_str: str) -> None:
        self.cookies = parse_cookie_string(cookie_str)

    def save_cookies(self) -> None:
        self.cookie_store.save(self.cookies)

    def clear_cookies(self) -> None:
        self.cookies = {}
        self.cookie_store.clear()
        self._wbi_keys = None

    @property
    def cookie_header(self) -> str:
        return cookie_to_string(self.cookies)

    @property
    def is_logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA"))

    def nav_info(self) -> Dict[str, Any]:
        """获取 nav 原始 data（未登录时 code=-101 属正常状态）。"""
        payload = self.request(API["nav"])
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BiliApiError(f"nav 响应非法: {exc}") from exc
        if not isinstance(obj, dict):
            return {}
        if obj.get("code") not in (0, -101):
            raise BiliApiError(f"[{obj.get('code')}] {obj.get('message', 'nav 失败')}")
        data = obj.get("data")
        return data if isinstance(data, dict) else {}

    def account_info(self) -> Dict[str, Any]:
        """获取当前账号信息（昵称、会员状态）。"""
        data = self.nav_info()
        return {
            "isLogin": bool(data.get("isLogin")),
            "uname": data.get("uname", ""),
            "vip": bool((data.get("vipStatus") or 0) > 0),
            "avatar": data.get("face", ""),
        }

    # ---------- 底层请求 ----------
    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: List[Any] = [urllib.request.HTTPSHandler(context=self._ssl_ctx)]
        proxy = str(self.config.get("proxy") or "").strip()
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _throttle(self) -> None:
        interval = float(self.config.get("request_interval") or 0)
        if interval <= 0:
            return
        elapsed = time.time() - self._last_request_time
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        raw: bool = False,
        method: str = "GET",
    ) -> bytes:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req_headers = dict(DEFAULT_HEADERS)
        if self.cookie_header:
            req_headers["Cookie"] = self.cookie_header
        if headers:
            req_headers.update(headers)
        data_bytes: Optional[bytes] = None
        if method == "POST" and params is not None:
            data_bytes = urllib.parse.urlencode(params).encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"

        retries = int(self.config.get("retries") or 3)
        timeout = int(self.config.get("timeout") or 20)
        last_exc: Optional[Exception] = None
        opener = self._opener()
        for attempt in range(retries + 1):
            self._throttle()
            self._last_request_time = time.time()
            req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)
            try:
                with opener.open(req, timeout=timeout) as resp:
                    payload = resp.read()
                    # 更新 Cookie（服务器可能刷新 SESSDATA）
                    set_cookie = resp.headers.get("Set-Cookie")
                    if set_cookie:
                        self._update_cookies_from_header(set_cookie)
                return payload
            except urllib.error.HTTPError as exc:
                last_exc = exc
                logger.debug("HTTP %s for %s", exc.code, shorten(url, 80))
                if exc.code in (403, 404, 412):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                logger.debug("请求失败 (%s) %s", attempt + 1, shorten(str(exc), 80))
            if attempt < retries:
                time.sleep(float(self.config.get("retry_delay") or 2) * (attempt + 1))
        raise BiliApiError(f"网络请求失败: {shorten(str(last_exc), 120)}")

    def _update_cookies_from_header(self, header: str) -> None:
        for part in header.split(","):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                k = k.strip()
                if k in ("SESSDATA", "bili_jct", "DedeUserID", "sid", "buvid3"):
                    self.cookies[k] = v.split(";")[0]

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        # wbi 仅为调用方标记（已在 get_wbi 中完成签名），不透传给 request
        kwargs.pop("wbi", None)
        payload = self.request(url, params=params, **kwargs)
        if kwargs.get("raw"):
            return payload
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BiliApiError(f"响应不是合法 JSON: {exc}") from exc
        return self._check(obj)

    @staticmethod
    def _check(obj: Any) -> Any:
        if not isinstance(obj, dict):
            return obj
        code = obj.get("code")
        if code not in (None, 0):
            msg = obj.get("message") or obj.get("msg") or "未知错误"
            raise BiliApiError(f"[{code}] {msg}", code=code)
        # 普通接口用 data 包装，番剧/播放器接口（pgc）用 result 包装，两种都要解包
        if "data" in obj:
            return obj["data"]
        if "result" in obj:
            return obj["result"]
        return obj

    # ---------- WBI 签名 ----------
    def _fetch_wbi_keys(self) -> Tuple[str, str]:
        data = self.get(API["nav"], wbi=False)
        if not isinstance(data, dict):
            raise BiliApiError("无法获取 WBI 密钥")
        img = data.get("wbi_img", {})
        img_url = img.get("img_url", "")
        sub_url = img.get("sub_url", "")
        if not img_url or not sub_url:
            raise BiliApiError("WBI 密钥缺失，可能未登录或接口变动")
        img_key = os.path.splitext(img_url.rsplit("/", 1)[-1])[0]
        sub_key = os.path.splitext(sub_url.rsplit("/", 1)[-1])[0]
        return img_key, sub_key

    def _wbi_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._wbi_keys is None:
            self._wbi_keys = self._fetch_wbi_keys()
        img_key, sub_key = self._wbi_keys
        mixin_key = _get_mixin_key(img_key + sub_key)
        params = {k: _filter_value(str(v)) for k, v in params.items()}
        params["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(params.items()))
        params["w_rid"] = _md5(query + mixin_key)
        return params

    def get_wbi(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = self._wbi_params(params or {})
        return self.get(url, params=params)

    # ---------- 扫码登录 ----------
    def qr_login(self, timeout: int = 180, on_qr_url=None):
        """扫码登录。on_qr_url 为可选回调，拿到二维码链接时调用。"""
        data = self.get(API["qr_generate"], wbi=False)
        key = data.get("qrcode_key")
        url = data.get("url")
        if not key or not url:
            raise BiliApiError("获取二维码失败")
        if on_qr_url:
            on_qr_url(url, key)
        print(f"\n请用 B 站 App 扫描二维码登录：\n  {url}\n（等待扫码，最长 {timeout} 秒）")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                res = self.get(API["qr_poll"], params={"qrcode_key": key}, wbi=False)
            except BiliApiError:
                time.sleep(2)
                continue
            if not isinstance(res, dict):
                time.sleep(2)
                continue
            state = res.get("code")
            if state == 0:
                # poll 的 data.url 形如 https://passport.bilibili.com/...?SESSDATA=xxx&...
                login_url = res.get("url", "")
                query = urllib.parse.parse_qs(urllib.parse.urlparse(login_url).query)
                for k in ("SESSDATA", "bili_jct", "DedeUserID", "sid"):
                    if k in query:
                        self.cookies[k] = query[k][0]
                if self.cookies.get("SESSDATA"):
                    self.save_cookies()
                    self._wbi_keys = None
                    info = self.account_info()
                    print(f"登录成功：{info.get('uname', '用户')}"
                          f"{'（大会员）' if info.get('vip') else ''}")
                    return True
                raise BiliApiError("登录响应中缺少 SESSDATA，请重试")
            if state == 86038:
                raise BiliApiError("二维码已过期，请重新登录")
            if state not in (86039, 86101):  # 未确认 / 未扫描
                raise BiliApiError(f"登录失败: {res.get('message', state)}")
            time.sleep(2)
        raise BiliApiError("登录超时")

    # ---------- 播放地址 ----------
    def playurl(
        self,
        aid: int,
        cid: int,
        bvid: str = "",
        qn: int = 0,
        is_pgc: bool = False,
        fnval: int = 4048,
    ) -> Dict[str, Any]:
        """获取播放地址。fnval 默认同时支持 DASH 与 mp4。"""
        params: Dict[str, Any] = {
            "bvid": bvid,
            "cid": cid,
            "qn": qn,
            "fnval": fnval,
            "fnver": 0,
            "fourk": 1,
            "session": "",
        }
        if is_pgc:
            params["aid"] = aid
            return self.get(API["pgc_playurl"], params=params)
        params["aid"] = aid
        return self.get(API["playurl"], params=params)

    def parse_stream(
        self,
        aid: int,
        cid: int,
        bvid: str = "",
        qn: int = 0,
        is_pgc: bool = False,
        codec_pref: str = "AVC,HEVC,AV1",
        audio_only: bool = False,
    ) -> StreamInfo:
        """请求 playurl 并选择符合画质/编码偏好的音视频流。"""
        data = self.playurl(aid, cid, bvid, qn, is_pgc)
        if not isinstance(data, dict):
            raise BiliApiError("playurl 返回异常")

        # DASH 流
        dash = data.get("dash")
        if dash and dash.get("video"):
            videos = dash.get("video") or []
            audios = dash.get("audio") or []
            # 番剧可能有 dash.video 但音频在 dash.audio（杜比/音效在 dash.dolby）
            choices = videos if not audio_only else []
            chosen_vid = self._pick_video(videos, qn, codec_pref)
            chosen_aud = self._pick_audio(audios)
            if not chosen_vid and not audio_only:
                # 回退到第一个可用视频
                chosen_vid = videos[0] if videos else None

            backup: List[str] = []
            if chosen_vid:
                backup.extend([u for u in (chosen_vid.get("backupUrl") or []) if u])
                backup.extend([u for u in (chosen_vid.get("backup_url") or []) if u])
            if chosen_aud:
                backup.extend([u for u in (chosen_aud.get("backupUrl") or []) if u])

            video_url = chosen_vid.get("baseUrl") or chosen_vid.get("base_url") if chosen_vid else None
            audio_url = chosen_aud.get("baseUrl") or chosen_aud.get("base_url") if chosen_aud else None
            actual_qn = int(chosen_vid.get("id", qn)) if chosen_vid else qn
            return StreamInfo(
                cid=cid,
                quality=actual_qn,
                quality_name=QUALITY_MAP.get(actual_qn, str(actual_qn)),
                format="dash",
                timelength=int(data.get("timelength") or 0),
                video_url=None if audio_only else video_url,
                audio_url=audio_url,
                video_size=int(chosen_vid.get("bandwidth", 0)) * int(data.get("timelength", 0) or 0) // 8 // 1000
                if chosen_vid and data.get("timelength") else 0,
                audio_size=int(chosen_aud.get("bandwidth", 0)) * int(data.get("timelength", 0) or 0) // 8 // 1000
                if chosen_aud and data.get("timelength") else 0,
                video_codec=self._codec_name(chosen_vid.get("codecs", "")) if chosen_vid else "",
                audio_codec=self._codec_name(chosen_aud.get("codecs", "")) if chosen_aud else "",
                backup_urls=backup,
            )

        # mp4 兜底（durl）
        durl = data.get("durl") or []
        if durl:
            item = durl[0]
            url = item.get("url")
            backup = [u for u in (item.get("backup_url") or []) if u]
            actual_qn = int(data.get("quality", qn))
            return StreamInfo(
                cid=cid,
                quality=actual_qn,
                quality_name=QUALITY_MAP.get(actual_qn, str(actual_qn)),
                format="mp4",
                timelength=int(data.get("timelength") or 0),
                video_url=url,
                audio_url=None,
                video_size=int(item.get("size", 0)),
                video_codec="AVC",
                backup_urls=backup,
            )
        raise BiliApiError("没有找到可用播放流（可能需要大会员或该地区不可用）")

    @staticmethod
    def _codec_name(codecs: str) -> str:
        if not codecs:
            return ""
        head = codecs.split(".")[0].lower()
        return CODEC_PREFIXES.get(head, head.upper())

    def _pick_video(self, videos: List[dict], qn: int, codec_pref: str) -> Optional[dict]:
        """按画质与编码优先级选流。qn=0 表示选最高画质。"""
        if not videos:
            return None
        prefs = [c.strip().upper() for c in codec_pref.split(",") if c.strip()]
        # 画质：若指定 qn，优先完全匹配，否则选不超过 qn 的最高画质
        target_qn = qn
        if target_qn and not any(int(v.get("id", 0)) == target_qn for v in videos):
            candidates = [v for v in videos if int(v.get("id", 0)) <= target_qn]
            if candidates:
                target_qn = max(int(v.get("id", 0)) for v in candidates)

        def score(v: dict):
            vqn = int(v.get("id", 0))
            codec = self._codec_name(v.get("codecs", ""))
            try:
                codec_rank = prefs.index(codec) if prefs else 0
            except ValueError:
                codec_rank = len(prefs)
            if target_qn:
                qn_miss = 0 if vqn == target_qn else 1
            else:
                qn_miss = 0
            # 优先画质匹配，其次编码偏好，其次高码率
            return (qn_miss, codec_rank, -int(v.get("bandwidth", 0)))

        return sorted(videos, key=score)[0]

    @staticmethod
    def _pick_audio(audios: List[dict]) -> Optional[dict]:
        """选择最高码率音轨。"""
        if not audios:
            return None
        return sorted(audios, key=lambda a: -int(a.get("bandwidth", 0)))[0]
