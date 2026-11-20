"""极简二维码编码器（纯标准库，零第三方依赖）。

只实现 B 站登录场景需要的能力：
- 字节模式（UTF-8）
- 纠错等级 M
- 版本 1-10（最多 216 字节，足够放登录链接）

对外接口：
    make_matrix(text) -> List[List[bool]]     二维码点阵
    to_png_bytes(text, scale=4, border=2)     黑白 PNG 字节
    to_data_url(text, ...)                    data:image/png;base64,...
    to_ascii(text)                            终端里用 ██ 打印

实现依据 ISO/IEC 18004。位序、掩码与罚分规则按标准表格实现。
"""

from __future__ import annotations

import base64
import io
import struct
import zlib
from typing import List, Optional, Sequence

# ---- 各版本参数（纠错等级 M）：每项 = (总码字, 每块纠错码字, 组1块数, 组1数据码字, 组2块数, 组2数据码字)
_VERSION_TABLE_M = {
    1:  (26, 10, 1, 16, 0, 0),
    2:  (44, 16, 1, 28, 0, 0),
    3:  (70, 26, 1, 44, 0, 0),
    4:  (100, 18, 2, 32, 0, 0),
    5:  (134, 24, 2, 43, 0, 0),
    6:  (172, 16, 4, 27, 0, 0),
    7:  (196, 18, 4, 31, 0, 0),
    8:  (242, 22, 2, 38, 2, 39),
    9:  (292, 22, 3, 36, 2, 37),
    10: (346, 26, 4, 43, 1, 44),
}

# 对齐图案中心坐标
_ALIGN_POS = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

_ECC_M_BITS = 0b00     # 格式信息中纠错等级 M 的两位编码


# ---------------------------------------------------------------- GF(256)
def _build_gf() -> tuple:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _build_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(n: int) -> List[int]:
    """生成 n 个纠错码字对应的生成多项式系数。"""
    poly = [1]
    for i in range(n):
        # 乘以 (x - α^i)
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _GF_EXP[i])
        poly = nxt
    return poly


def _rs_ecc(data: Sequence[int], n: int) -> List[int]:
    gen = _rs_generator(n)
    res = [0] * n
    for byte in data:
        factor = byte ^ res[0]
        res = res[1:] + [0]
        if factor:
            for i in range(n):
                res[i] ^= _gf_mul(gen[i + 1], factor)
    return res


# ---------------------------------------------------------------- 数据编码
def _encode_data(text: str, version: int) -> List[int]:
    """字节模式编码 + 终止符 + 填充，返回数据码字列表。"""
    payload = text.encode("utf-8")
    total, ec_per_block, n1, d1, n2, d2 = _VERSION_TABLE_M[version]
    capacity = n1 * d1 + n2 * d2

    bits: List[int] = []

    def push(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                              # 字节模式
    push(len(payload), 8 if version <= 9 else 16)  # 字符计数
    for byte in payload:
        push(byte, 8)

    # 终止符：最多 4 个 0
    for _ in range(min(4, capacity * 8 - len(bits))):
        bits.append(0)

    # 补齐到字节边界
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]

    # 交替填充 0xEC / 0x11
    pad = (0xEC, 0x11)
    i = 0
    while len(codewords) < capacity:
        codewords.append(pad[i % 2])
        i += 1
    return codewords


def _build_codewords(text: str, version: int) -> List[int]:
    """分块计算纠错码并交织，返回最终码字序列。"""
    data = _encode_data(text, version)
    total, ec_per_block, n1, d1, n2, d2 = _VERSION_TABLE_M[version]

    blocks: List[List[int]] = []
    pos = 0
    for _ in range(n1):
        blocks.append(data[pos:pos + d1])
        pos += d1
    for _ in range(n2):
        blocks.append(data[pos:pos + d2])
        pos += d2

    ecc_blocks = [_rs_ecc(block, ec_per_block) for block in blocks]

    out: List[int] = []
    max_data = max(len(b) for b in blocks)
    for i in range(max_data):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_per_block):
        for block in ecc_blocks:
            out.append(block[i])
    return out


# ---------------------------------------------------------------- 矩阵构建
def _bch_format(mask: int) -> int:
    """格式信息：5 位数据 + BCH(15,5) 校验，再异或掩码常量。"""
    data = (_ECC_M_BITS << 3) | mask
    value = data << 10
    for i in range(4, -1, -1):
        if value & (1 << (i + 10)):
            value ^= 0x537 << i
    return ((data << 10) | value) ^ 0x5412


def _bch_version(version: int) -> int:
    """版本信息（版本 7 及以上）：6 位版本号 + BCH(18,6)。"""
    value = version << 12
    for i in range(5, -1, -1):
        if value & (1 << (i + 12)):
            value ^= 0x1F25 << i
    return (version << 12) | value


def _new_matrix(size: int) -> tuple:
    return ([[False] * size for _ in range(size)],
            [[False] * size for _ in range(size)])


def _draw_function_patterns(matrix: List[List[bool]], func: List[List[bool]],
                           version: int) -> None:
    size = len(matrix)

    def set_func(r: int, c: int, dark: bool) -> None:
        if 0 <= r < size and 0 <= c < size:
            matrix[r][c] = dark
            func[r][c] = True

    # 定位图案（三个角）+ 分隔带
    for (br, bc) in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = br + dr, bc + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                dark = inside and (dr in (0, 6) or dc in (0, 6)
                                   or (2 <= dr <= 4 and 2 <= dc <= 4))
                set_func(r, c, dark)

    # 定时图案
    for i in range(8, size - 8):
        dark = (i % 2 == 0)
        set_func(6, i, dark)
        set_func(i, 6, dark)

    # 对齐图案
    centers = _ALIGN_POS.get(version, [])
    for r in centers:
        for c in centers:
            # 跳过与定位图案重叠的位置
            if (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (r >= size - 9 and c <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    set_func(r + dr, c + dc, dark)

    # 固定深色模块
    set_func(size - 8, 8, True)

    # 预留格式信息区域
    for i in range(9):
        if not func[i][8]:
            set_func(i, 8, False)
        if not func[8][i]:
            set_func(8, i, False)
    for i in range(8):
        set_func(8, size - 1 - i, False)
    for i in range(7):
        set_func(size - 1 - i, 8, False)

    # 版本信息（v7+）
    if version >= 7:
        bits = _bch_version(version)
        for i in range(18):
            dark = bool((bits >> i) & 1)
            r, c = i // 3, i % 3
            set_func(size - 11 + c, r, dark)
            set_func(r, size - 11 + c, dark)


def _place_data(matrix: List[List[bool]], func: List[List[bool]],
                codewords: List[int]) -> None:
    size = len(matrix)
    bits: List[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:            # 跳过竖向定时图案列
            col -= 1
        for row in range(size):
            r = size - 1 - row if upward else row
            for c in (col, col - 1):
                if not func[r][c] and idx < len(bits):
                    matrix[r][c] = bool(bits[idx])
                    idx += 1
        col -= 2
        upward = not upward


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply_mask(matrix: List[List[bool]], func: List[List[bool]], mask: int) -> List[List[bool]]:
    size = len(matrix)
    fn = _MASKS[mask]
    out = [row[:] for row in matrix]
    for r in range(size):
        for c in range(size):
            if not func[r][c] and fn(r, c):
                out[r][c] = not out[r][c]
    return out


def _penalty(matrix: List[List[bool]]) -> int:
    size = len(matrix)
    score = 0

    def line_score(values: List[bool]) -> int:
        s = 0
        run = 1
        for i in range(1, len(values)):
            if values[i] == values[i - 1]:
                run += 1
            else:
                if run >= 5:
                    s += 3 + (run - 5)
                run = 1
        if run >= 5:
            s += 3 + (run - 5)
        # 2x2 同色块
        return s

    for r in range(size):
        score += line_score(matrix[r])
    for c in range(size):
        score += line_score([matrix[r][c] for r in range(size)])

    for r in range(size - 1):
        for c in range(size - 1):
            v = matrix[r][c]
            if v == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3

    dark = sum(1 for row in matrix for v in row if v)
    total = size * size
    ratio = dark * 100 // total
    score += abs(ratio - 50) // 5 * 10
    return score


def _write_format(matrix: List[List[bool]], mask: int) -> None:
    size = len(matrix)
    bits = _bch_format(mask)
    for i in range(15):
        dark = bool((bits >> i) & 1)
        # 左上角
        if i < 6:
            matrix[i][8] = dark
        elif i == 6:
            matrix[7][8] = dark
        elif i == 7:
            matrix[8][8] = dark
        elif i == 8:
            matrix[8][7] = dark
        else:
            matrix[8][14 - i] = dark
        # 复制到右上 / 左下
        if i < 8:
            matrix[8][size - 1 - i] = dark
        else:
            matrix[size - 15 + i][8] = dark


def make_matrix(text: str) -> List[List[bool]]:
    """把文本编码为二维码点阵（含定位图案，不含静区）。"""
    payload_len = len(text.encode("utf-8"))
    version = None
    for v in sorted(_VERSION_TABLE_M):
        _, _, n1, d1, n2, d2 = _VERSION_TABLE_M[v]
        capacity = n1 * d1 + n2 * d2
        needed = payload_len + (2 if v <= 9 else 3)   # 模式 + 计数 + 数据 的粗略余量
        if needed <= capacity:
            version = v
            break
    if version is None:
        raise ValueError(f"内容过长（{payload_len} 字节），超出二维码支持范围")

    size = version * 4 + 17
    matrix, func = _new_matrix(size)
    _draw_function_patterns(matrix, func, version)
    _place_data(matrix, func, _build_codewords(text, version))

    best_mask = 0
    best = None
    best_score = None
    for mask in range(8):
        candidate = _apply_mask(matrix, func, mask)
        _write_format(candidate, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best_score, best, best_mask = score, candidate, mask
    return best


# ---------------------------------------------------------------- 输出
def to_png_bytes(matrix: List[List[bool]], scale: int = 4, border: int = 4) -> bytes:
    """生成黑白 PNG（无第三方库，手写最小 PNG 编码）。"""
    size = len(matrix)
    width = (size + border * 2) * scale

    rows: List[bytes] = []
    for y in range(width):
        my = y // scale - border
        row = bytearray()
        row.append(0)                       # 过滤类型：None
        for x in range(width):
            mx = x // scale - border
            dark = 0 <= my < size and 0 <= mx < size and matrix[my][mx]
            row.append(0 if dark else 255)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def to_data_url(text: str, scale: int = 4, border: int = 4) -> str:
    png = to_png_bytes(make_matrix(text), scale=scale, border=border)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def to_ascii(text: str, border: int = 2) -> str:
    """终端打印：用两个半角块字符表示一个模块，保证纵横比接近正方。"""
    matrix = make_matrix(text)
    size = len(matrix)
    lines = []
    for _ in range(border):
        lines.append("  " * (size + border * 2))
    for r in range(size):
        line = "  " * border
        for c in range(size):
            line += "██" if matrix[r][c] else "  "
        line += "  " * border
        lines.append(line)
    for _ in range(border):
        lines.append("  " * (size + border * 2))
    return "\n".join(lines)
