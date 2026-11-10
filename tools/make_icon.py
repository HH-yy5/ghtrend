#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 GHTrend 应用图标（纯标准库，不依赖 Pillow / cairosvg）。

产出：
  electron/build/icon.ico   多尺寸 ICO（16/32/48/64/128/256），给 electron-builder
  electron/build/icon.png   256x256 预览用

为什么要自己画：
  项目零第三方依赖是硬约束；临时装 Pillow 只为了一个图标不值得。
  PNG 是 zlib + 扫描线，ICO 是定长头 + 内嵌 PNG，都能手写。

图形：圆角渐变方块 + 白色五角星（星速榜的"星"），4x4 超采样抗锯齿。
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "electron" / "build"

SS = 4  # 每像素 4x4 超采样

# 配色：和前端 --accent (#0969da) 同源
C_TOP = (31, 139, 255)     # #1f8bff
C_BOTTOM = (3, 82, 174)    # #0352ae
C_STAR = (255, 255, 255)


def _rounded_rect_inside(x: float, y: float, size: float, radius: float) -> bool:
    """圆角矩形 SDF 判定：内部返回 True。"""
    half = size / 2.0
    cx = abs(x - half) - (half - radius)
    cy = abs(y - half) - (half - radius)
    dx = max(cx, 0.0)
    dy = max(cy, 0.0)
    dist = math.hypot(dx, dy) + min(max(cx, cy), 0.0) - radius
    return dist <= 0.0


def _star_polygon(cx: float, cy: float, outer: float, inner: float, points: int = 5):
    """正五角星顶点（从正上方开始，交替外/内半径）。"""
    verts = []
    for i in range(points * 2):
        r = outer if i % 2 == 0 else inner
        # -90° 起始 => 尖角朝上
        ang = -math.pi / 2 + i * math.pi / points
        verts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return verts


def _inside_polygon(x: float, y: float, poly) -> bool:
    """射线法判断点是否在多边形内。"""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def render(size: int) -> bytes:
    """渲染 RGBA 像素（返回裸字节，尺寸 = size*size*4）。"""
    scale = size / 256.0
    radius = 56.0 * scale
    star_c = size / 2.0
    # 星形稍微上移一点，视觉重心更稳
    star_cy = star_c - 4.0 * scale
    star = _star_polygon(star_c, star_cy, 88.0 * scale, 36.0 * scale)

    px = bytearray(size * size * 4)
    step = 1.0 / SS
    offsets = [(i + 0.5) * step for i in range(SS)]

    for y in range(size):
        for x in range(size):
            hit_bg = 0
            hit_star = 0
            for oy in offsets:
                sy = y + oy
                for ox in offsets:
                    sx = x + ox
                    if not _rounded_rect_inside(sx, sy, size, radius):
                        continue
                    hit_bg += 1
                    if _inside_polygon(sx, sy, star):
                        hit_star += 1
            total = SS * SS
            if hit_bg == 0:
                continue  # 圆角外，保持全透明

            # 渐变：左上亮、右下深
            t = (x + y) / (2.0 * size)
            t = min(max(t, 0.0), 1.0)
            r = round(C_TOP[0] + (C_BOTTOM[0] - C_TOP[0]) * t)
            g = round(C_TOP[1] + (C_BOTTOM[1] - C_TOP[1]) * t)
            b = round(C_TOP[2] + (C_BOTTOM[2] - C_TOP[2]) * t)

            # 星形按覆盖率与底色混合
            a_star = hit_star / total
            if a_star > 0:
                r = round(r * (1 - a_star) + C_STAR[0] * a_star)
                g = round(g * (1 - a_star) + C_STAR[1] * a_star)
                b = round(b * (1 - a_star) + C_STAR[2] * a_star)

            idx = (y * size + x) * 4
            px[idx] = r
            px[idx + 1] = g
            px[idx + 2] = b
            px[idx + 3] = round(255 * hit_bg / total)  # 边缘抗锯齿
    return bytes(px)


def to_png(size: int, raw: bytes) -> bytes:
    """RGBA 裸数据 -> PNG（每行前加 filter 字节 0）。"""
    stride = size * 4
    rows = b"".join(b"\x00" + raw[y * stride:(y + 1) * stride] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def build_ico(pngs: dict[int, bytes]) -> bytes:
    """多尺寸 ICO：每个条目内嵌完整 PNG（Vista+ 支持）。"""
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))  # reserved, type=icon, count
    entries = b""
    blob = b""
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        data = pngs[s]
        # 256 用 0 表示（字段只有 1 字节）
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blob += data
        offset += len(data)
    return header + entries + blob


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sizes = [16, 32, 48, 64, 128, 256]
    pngs: dict[int, bytes] = {}
    for s in sizes:
        print(f"  渲染 {s}x{s} ...")
        pngs[s] = to_png(s, render(s))

    (OUT_DIR / "icon.png").write_bytes(pngs[256])
    (OUT_DIR / "icon.ico").write_bytes(build_ico(pngs))
    print(f"已生成：\n  {OUT_DIR / 'icon.ico'}\n  {OUT_DIR / 'icon.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
