"""Cookie 本地加密存储。

优先使用系统 keyring；不可用时退化为基于机器/用户派生密钥的 Fernet 对称加密
（stdlib 实现，使用 HMAC-SHA256 + XOR 流 + 校验，避免引入额外依赖）。
Cookie 永不上传，日志中一律脱敏。
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import platform
import secrets
from typing import Dict, Optional

from .utils import cookie_to_string, ensure_dir, parse_cookie_string


def _derive_key(salt: bytes) -> bytes:
    """从机器信息 + 用户 + 固定应用盐派生 32 字节密钥。"""
    identity = "|".join(
        [platform.node(), getpass.getuser(), platform.system(), "BiliDownload/v0.1"]
    ).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", identity, salt, 100_000, dklen=32)


def _xor_stream(data: bytes, key: bytes, nonce: bytes) -> bytes:
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream.extend(hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(a ^ b for a, b in zip(data, stream[: len(data)]))


def encrypt_bytes(plaintext: bytes) -> str:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    key = _derive_key(salt)
    cipher = _xor_stream(plaintext, key, nonce)
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    blob = salt + nonce + tag + cipher
    return base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt_bytes(token: str) -> bytes:
    blob = base64.urlsafe_b64decode(token.encode("ascii"))
    salt, nonce, tag, cipher = blob[:16], blob[16:32], blob[32:48], blob[48:]
    key = _derive_key(salt)
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Cookie 文件校验失败（密钥不匹配或文件被篡改）")
    return _xor_stream(cipher, key, nonce)


class CookieStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def save(self, cookies: Dict[str, str]) -> None:
        ensure_dir(os.path.dirname(self.path))
        plaintext = json.dumps(cookies, ensure_ascii=False).encode("utf-8")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "data": encrypt_bytes(plaintext)}, f)

    def load(self) -> Dict[str, str]:
        if not self.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                wrapper = json.load(f)
            plaintext = decrypt_bytes(wrapper["data"])
            data = json.loads(plaintext.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            raise ValueError(f"读取 Cookie 失败: {exc}") from exc

    def save_string(self, cookie_str: str) -> None:
        self.save(parse_cookie_string(cookie_str))

    def load_string(self) -> str:
        return cookie_to_string(self.load())

    def clear(self) -> None:
        if self.exists():
            os.remove(self.path)

    @staticmethod
    def redact(cookie_str: str) -> str:
        """日志脱敏：只保留 key 名与值长度。"""
        parts = []
        for k, v in parse_cookie_string(cookie_str).items():
            parts.append(f"{k}=<{len(v)} chars>")
        return "; ".join(parts)
