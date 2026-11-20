"""常量、请求头与清晰度/编码映射。"""

from __future__ import annotations

APP_NAME = "BiliDownload"
APP_VERSION = "0.1.0"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# qn(id) -> 可读名称。数值越大画质越高。
QUALITY_MAP = {
    6: "240P",
    16: "360P",
    32: "480P",
    64: "720P",
    74: "720P60",
    80: "1080P",
    112: "1080P+",
    116: "1080P60",
    120: "4K",
    125: "HDR",
    126: "杜比视界",
    127: "8K",
}

# 按 id 排序的清晰度优先级（高 -> 低）
QUALITY_ORDER = [127, 126, 125, 120, 116, 112, 80, 74, 64, 32, 16, 6]

CODEC_ALIASES = {
    "avc": "AVC",
    "h264": "AVC",
    "avc1": "AVC",
    "hevc": "HEVC",
    "h265": "HEVC",
    "hvc1": "HEVC",
    "hev1": "HEVC",
    "av1": "AV1",
    "av01": "AV1",
}

CODEC_PREFIXES = {
    "avc1": "AVC",
    "avc3": "AVC",
    "h264": "AVC",
    "hvc1": "HEVC",
    "hev1": "HEVC",
    "h265": "HEVC",
    "av01": "AV1",
    "av1": "AV1",
}

API = {
    "nav": "https://api.bilibili.com/x/web-interface/nav",
    "view": "https://api.bilibili.com/x/web-interface/view",
    "playurl": "https://api.bilibili.com/x/player/playurl",
    "pgc_playurl": "https://api.bilibili.com/pgc/player/web/playurl",
    "pgc_season": "https://api.bilibili.com/pgc/view/web/season",
    "bvid_aid": "https://api.bilibili.com/x/web-interface/view",
    "space_arc": "https://api.bilibili.com/x/space/wbi/arc/search",
    "space_info": "https://api.bilibili.com/x/space/wbi/acc/info",
    "fav_folders": "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
    "fav_resources": "https://api.bilibili.com/x/v3/fav/resource/list",
    "player_v2": "https://api.bilibili.com/x/player/v2",
    # 扫码登录：2024 年起 B 站迁移到 passport 域名的 web qrcode 接口，
    # 旧的 api.bilibili.com/x/qrcode/key 已废弃（返回 404）。
    "qr_generate": "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
    "qr_poll": "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
    "danmaku": "https://comment.bilibili.com/{cid}.xml",
}

# WBI 签名密钥重排表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

WBI_FILTER_CHARS = "!'()*"

DEFAULT_NAME_TEMPLATE = "{series}/{index} - {title}.{ext}"
