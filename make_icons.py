# -*- coding: utf-8 -*-
"""make_icons.py —— 生成程序图标（assets/app.png + assets/app.ico）

纯标准库 + 本项目的 SDF 渲染（不依赖 Pillow）。
图形：深色圆角方块 + 电池轮廓 + 绿色电量 + 充电闪电。
每个尺寸都是**原生渲染**（不是缩放），所以 16×16 也清楚。

用法：
    python make_icons.py            # 生成 assets/app.png 与 assets/app.ico
"""
from __future__ import annotations

import io
import math
import os
import struct

import win_icon
from my_battery import _hex_rgb, _poly_sdf, _png_encode, _rrect_sdf

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(HERE, "assets")

BG_TOP = (44, 52, 68)          # #2C3444
BG_BOTTOM = (14, 18, 26)       # #0E121A
OUTLINE = (238, 242, 248)      # #EEF2F8
FILL = (61, 220, 132)          # #3DDC84
FILL_HI = (126, 240, 170)      # #7EF0AA
BOLT = (255, 255, 255)


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size: int) -> tuple[int, int, bytes]:
    """画一张 size×size 的图标，返回 (w, h, RGBA bytes)。"""
    S = float(size)
    px = bytearray(size * size * 4)

    def cov(sdf: float) -> float:
        return max(0.0, min(1.0, 0.5 - sdf))

    def put(x: int, y: int, c, a: float) -> None:
        if a <= 0:
            return
        i = (y * size + x) * 4
        al = a * 255
        px[i] = c[0]
        px[i + 1] = c[1]
        px[i + 2] = c[2]
        px[i + 3] = int(max(px[i + 3], al))

    # 圆角方块底（纵向渐变）
    pad = 0.02 * S
    cx = cy = S / 2.0
    hw = hh = S / 2.0 - pad
    radius = 0.23 * S

    # 电池几何
    stroke = max(0.9, 0.052 * S)
    body_cx = cx - 0.02 * S
    body_cy = cy
    body_hw = 0.295 * S
    body_hh = 0.185 * S
    body_r = 0.075 * S
    left = body_cx - body_hw
    inner_w = body_hw - stroke

    term_cx = body_cx + body_hw + 0.030 * S
    term_hw = 0.026 * S
    term_hh = 0.062 * S

    fill_frac = 0.78 if size >= 24 else 0.82
    fill_w = inner_w * 2 * fill_frac

    bolt = None
    if size >= 40:
        pts = [(0.605, 0.372), (0.487, 0.530), (0.556, 0.530),
               (0.520, 0.628), (0.638, 0.470), (0.569, 0.470)]
        bolt = [(S * px_, S * py_) for px_, py_ in pts]

    for y in range(size):
        py = y + 0.5
        t = y / max(1.0, S - 1)
        bg = _mix(BG_TOP, BG_BOTTOM, t)
        for x in range(size):
            pxx = x + 0.5
            c = None
            a = 0.0

            # 1) 电池填充（绿色，左侧一段）
            if fill_w > 0:
                fc = left + stroke / 2.0 + fill_w / 2.0
                fs = _rrect_sdf(pxx, py, fc, body_cy, fill_w / 2.0,
                                body_hh - stroke, max(0.5, body_r - stroke))
                a = cov(fs)
                if a > 0:
                    # 填充里做一点高光渐变，小尺寸看不出来也無妨
                    rel = (pxx - left) / max(1.0, inner_w)
                    c = _mix(FILL, FILL_HI, max(0.0, min(1.0, 1.0 - rel)))

            # 2) 闪电（压在高光上）
            if bolt is not None:
                bs = _poly_sdf(pxx, py, bolt)
                b = cov(bs)
                if b > 0.02:
                    base = c or FILL
                    c = _mix(base, BOLT, b)
                    a = max(a, b)

            # 3) 电极头
            if c is None:
                ts = _rrect_sdf(pxx, py, term_cx, body_cy, term_hw, term_hh,
                                0.020 * S)
                a = cov(ts)
                if a > 0:
                    c = OUTLINE

            # 4) 电池描边
            if c is None:
                so = _rrect_sdf(pxx, py, body_cx, body_cy, body_hw, body_hh,
                                body_r)
                si = _rrect_sdf(pxx, py, body_cx, body_cy,
                                body_hw - stroke, body_hh - stroke,
                                max(0.5, body_r - stroke))
                a = max(0.0, min(1.0, cov(so) - cov(si)))
                if a > 0:
                    c = OUTLINE

            # 5) 底色
            if c is None:
                a = cov(_rrect_sdf(pxx, py, cx, cy, hw, hh, radius))
                if a > 0:
                    c = bg

            if c is not None and a > 0:
                put(x, y, c, a)
    return size, size, bytes(px)


def main() -> None:
    os.makedirs(ASSET_DIR, exist_ok=True)
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    raws = []
    pngs = {}
    for s in sizes:
        w, h, rgba = render(s)
        raws.append((s, rgba))
        pngs[s] = _png_encode(w, h, rgba)

    ico = win_icon.build_ico(raws)
    ico_path = os.path.join(ASSET_DIR, "app.ico")
    with open(ico_path, "wb") as f:
        f.write(ico)

    png_path = os.path.join(ASSET_DIR, "app.png")
    big = pngs[256]
    with open(png_path, "wb") as f:
        f.write(big)

    print("写出:", ico_path, len(ico), "bytes")
    print("写出:", png_path, len(big), "bytes")


if __name__ == "__main__":
    main()
