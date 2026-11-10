# -*- coding: utf-8 -*-
"""2.4G Device Battery Check —— VGN S99 键盘 + 罗技 Lightspeed 鼠标的简洁面板

用法：
    python my_battery.py            打开界面（默认自动刷新）
    python my_battery.py --once     命令行读一次就退出
    python my_battery.py --install-links      在桌面重建软连接
    python my_battery.py --autostart on|off|status    开关/查看开机自启
    pythonw my_battery.py           后台运行不弹黑窗

布局：两张正方形卡片，官方产品图占上方 2/3，下方设备名，右下角横向小电池图标。
支持主题切换：夜间 / 白天 / 跟随系统。

零第三方依赖：只用 stdlib + 本项目 hid_win.py / battery.py / tray_common.py。
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Optional

import hid_win as hw
import battery as bt
import devices
import tray_common as tc
import wallpaper as wp
from hid_win import HidInterface

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 单文件 exe（PyInstaller）下：assets 这类只读资源被打包进 _MEIPASS 临时解包目录，
# 配置/pid 由 tray_common 写到 exe 同目录。这里只为找 assets 做 frozen 感知。
_RESOURCE_DIR = (getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
                 if getattr(sys, "frozen", False) else BASE_DIR)
ASSET_DIR = os.path.join(_RESOURCE_DIR, "assets")
KEYBOARD_IMG = os.path.join(ASSET_DIR, "keyboard.png")
MOUSE_IMG = os.path.join(ASSET_DIR, "mouse.png")
APP_ICON_PNG = os.path.join(ASSET_DIR, "app.png")
APP_ICON_ICO = os.path.join(ASSET_DIR, "app.ico")

# 任务栏分组用的 AppUserModelID：不设的话 Windows 会把窗口归到「Python」那一组
AUMID = "VGN.2GBatteryCheck.1"

# 极简模式（毛玻璃小浮窗）：尺寸由 _mini_metrics(scale) 按比例算
_MINI_KEY = "#0d1117"      # 透明色键：这块颜色会被抠掉，露出毛玻璃

FONT = ("Microsoft YaHei UI", 10)

# 卡片尺寸（正方形）
CARD_SIZE = 340
IMG_MAX_W = 286   # 两侧各留 ~27px 内边距，键盘图别顶到卡片边
IMG_MAX_H = 180   # 图片区高度上限：给标题/状态行留足呼吸感（LANCZOS 满尺寸渲染后不能放太高）

# 卡片兜底图标（没有官方产品图的设备）
_CARD_ICON_D = 120

# 电池图标尺寸（原 72×26 的 70%）
BAT_W = 50
BAT_H = 18


# ==========================================================================
# 主题
# ==========================================================================
THEMES = {
    "dark": {
        "bg": "#1b1c1f", "panel": "#232529", "panel2": "#2b2e33",
        "fg": "#e8e8ea", "muted": "#9aa0a6", "accent": "#4f8cff",
        "active": "#3a3f47", "disabled_fg": "#6b7076",
        "card_bg": "#232529", "canvas_bg": "#232529",
    },
    "light": {
        "bg": "#f4f5f7", "panel": "#ffffff", "panel2": "#e9ebf0",
        "fg": "#1c1e21", "muted": "#6c737a", "accent": "#2f6fe0",
        "active": "#e2e6ec", "disabled_fg": "#a0a6ad",
        "card_bg": "#ffffff", "canvas_bg": "#ffffff",
    },
}

THEME_CHOICES = [("system", "跟随系统"), ("dark", "夜间"), ("light", "白天")]
THEME_LABELS = dict(THEME_CHOICES)
THEME_KEYS = dict((label, key) for key, label in THEME_CHOICES)


def resolve_theme(mode: str) -> str:
    """把 mode（system/dark/light）解析成实际配色键。"""
    if mode == "system":
        return "dark" if tc.system_is_dark() else "light"
    return mode if mode in THEMES else "dark"


LEVEL_STYLE = [
    (60, "#5ddc8a"),
    (30, "#f5c451"),
    (0,  "#ff6b6b"),
]


def level_color(level: Optional[int]) -> str:
    if level is None:
        return "#9aa0a6"
    for threshold, color in LEVEL_STYLE:
        if level >= threshold:
            return color
    return LEVEL_STYLE[-1][1]


def fmt_eta(mins: Optional[int]) -> str:
    """把剩余充电分钟数排成一句话（None/0 以内 = 即将充满）。"""
    if not mins or mins < 1:
        return "即将充满"
    if mins < 60:
        return f"预计 {mins} 分钟充满"
    h, m = divmod(mins, 60)
    if m:
        return f"预计 {h} 小时 {m} 分充满"
    return f"预计 {h} 小时充满"


# ==========================================================================
# 读取层：设备表与通用读取都在 devices.py（扫描 / 添加 / 移除也由它管）
# ==========================================================================
Reading = devices.Reading
read_all = devices.read_all
read_spec = devices.read_spec


def cli_once() -> None:
    specs = devices.load_specs()
    if not specs:
        print("设备表为空：先用 `python my_battery.py --scan` 扫描并添加设备")
        return
    for r in devices.read_all(specs):
        if r.ok:
            tag = f"  {r.status}" if r.status else ""
            print(f"{r.title}: {r.level}%{tag}   {r.detail}")
        else:
            print(f"{r.title}: 读取失败 —— {r.error}")


# ==========================================================================
# 界面
# ==========================================================================
def fit_image(tk, path: str, max_w: int, max_h: int, master=None):
    """加载 PNG 并按比例缩小到 max_w×max_h 以内。

    优先 Pillow 的 LANCZOS 缩放（平滑抗锯齿）——Tk 的 subsample 是整数抽点，
    1200px 的原图直接丢掉 3/4 像素，缩出来边缘全是毛刺，看起来就是"模糊"。
    没有 Pillow 时退回 subsample（能用，但明显糙一档）。
    """
    try:
        from PIL import Image
        im = Image.open(path)
        if im.mode not in ("RGBA", "RGB"):
            im = im.convert("RGBA")
        im.thumbnail((max(1, int(max_w)), max(1, int(max_h))), Image.LANCZOS)
        try:
            from PIL import ImageTk
            photo = (ImageTk.PhotoImage(im, master=master) if master
                     else ImageTk.PhotoImage(im))
        except Exception:
            import base64 as _b64
            import io as _io
            buf = _io.BytesIO()
            im.save(buf, format="PNG")
            photo = tk.PhotoImage(
                master=master,
                data=_b64.b64encode(buf.getvalue()).decode("ascii"),
                format="png")
        return photo, photo
    except Exception:
        pass
    img = tk.PhotoImage(file=path, master=master)
    w, h = img.width(), img.height()
    f = max(1, math.ceil(max(w / max_w, h / max_h)))
    small = img.subsample(f, f) if f > 1 else img
    return img, small


def _hex_rgb(h: str):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rrect_sdf(px: float, py: float, cx: float, cy: float,
               hw: float, hh: float, r: float) -> float:
    """圆角矩形有向距离场（负值在内部，单位=像素），用于抗锯齿。"""
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    ox = max(qx, 0.0)
    oy = max(qy, 0.0)
    return math.hypot(ox, oy) + min(max(qx, qy), 0.0) - r


def _png_encode(w: int, h: int, rgba: bytes) -> bytes:
    """纯 Python 编码 RGBA 字节为 PNG（透明通道保留，无第三方依赖）。"""
    import struct
    import zlib

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    idat = zlib.compress(bytes(raw), 9)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _render_battery_rgba(level, color: str, outline: str,
                         w: int = 72, h: int = 26,
                         phase: Optional[int] = None):
    """渲染横置电池图标：圆角主体 + 长方形色块电极头 + 抗锯齿。

    `phase` 不为 None 时画「充电扫光」：填充里有一条更亮的带子滚过，
    界面按 120ms 换一帧就成了动态充电效果。

    返回 (w, h, RGBA bytes)。曲线用 SDF 做亚像素抗锯齿，比 tk.Canvas 的
    create_arc 平滑得多；整块是透明 PNG，不会像 Canvas 那样盖住卡片内容。
    """
    cr = _hex_rgb(color)
    light_c = tuple(min(255, int(v + (255 - v) * 0.62)) for v in cr)
    # 电极头颜色变浅：取 outline 向白色靠拢
    orr = _hex_rgb(outline)
    head_c = tuple(min(255, int(v + (255 - v) * 0.45)) for v in orr)

    head_w = max(4, int(h * 0.21))        # 电极头宽度减半（原先 0.42）
    body_w = w - head_w
    r_body = min(4.0, h / 2.0 - 1.0)      # 主体圆角半径
    stroke = 1.0
    pad = 2.0

    def cov(sdf):
        return max(0.0, min(1.0, 0.5 - sdf))

    def put(x, y, c, a):
        i = (y * w + x) * 4
        px[i] = c[0]
        px[i + 1] = c[1]
        px[i + 2] = c[2]
        px[i + 3] = int(max(px[i + 3], a * 255))

    # 电极头（长方形色块，居中，高度约占主体 0.6）
    head_cx = body_w + head_w / 2.0
    head_cy = h / 2.0
    head_hw = head_w / 2.0
    head_hh = h * 0.30

    # 主体描边环
    body_cx = body_w / 2.0
    body_cy = h / 2.0
    body_hw = body_w / 2.0
    body_hh = h / 2.0

    # 填充长度
    fill_len = 0.0
    if level is not None and level > 0:
        fill_len = (body_w - 2 * pad) * min(level, 100) / 100.0

    px = bytearray(w * h * 4)
    for y in range(h):
        py = y + 0.5
        for x in range(w):
            pxx = x + 0.5
            c = None
            a = 0.0
            if fill_len > 0:
                fc = pad + fill_len / 2.0
                fs = _rrect_sdf(pxx, py, fc, h / 2.0, fill_len / 2.0,
                                (h - 2 * pad) / 2.0, max(1.0, r_body - pad))
                a = cov(fs)
                if a > 0:
                    c = cr
                    if phase is not None:
                        rel = ((pxx - pad) / max(1.0, fill_len) * 2.0
                               - (phase % 12) / 6.0) % 1.0
                        if rel < 0.30:
                            c = light_c
            if c is None:
                hs = _rrect_sdf(pxx, py, head_cx, head_cy, head_hw, head_hh, 1.0)
                a = cov(hs)
                if a > 0:
                    c = head_c
            if c is None:
                so = _rrect_sdf(pxx, py, body_cx, body_cy, body_hw, body_hh, r_body)
                si = _rrect_sdf(pxx, py, body_cx, body_cy,
                                body_hw - stroke, body_hh - stroke,
                                max(1.0, r_body - stroke))
                a = max(0.0, min(1.0, cov(so) - cov(si)))
                if a > 0:
                    c = orr
            if c is not None and a > 0:
                put(x, y, c, a)
    return w, h, bytes(px)


def _tri_sdf(px: float, py: float, x1: float, y1: float,
             x2: float, y2: float, x3: float, y3: float) -> float:
    import math as _m

    def seg_sdf(ax, ay, bx, by):
        axx, ayy = ax - px, ay - py
        bxx, byy = bx - ax, by - ay
        t = max(0.0, min(1.0, -(axx * bxx + ayy * byy) / max(1e-9, bxx * bxx + byy * byy)))
        return _m.hypot(axx + bxx * t, ayy + byy * t)

    d1 = seg_sdf(x1, y1, x2, y2)
    d2 = seg_sdf(x2, y2, x3, y3)
    d3 = seg_sdf(x3, y3, x1, y1)
    d = min(d1, d2, d3)
    # 判断点在三角形内外（叉积同号）
    s1 = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    s2 = (x3 - x2) * (py - y2) - (y3 - y2) * (px - x2)
    s3 = (x1 - x3) * (py - y3) - (y1 - y3) * (px - x3)
    neg = (s1 < 0) or (s2 < 0) or (s3 < 0)
    pos = (s1 > 0) or (s2 > 0) or (s3 > 0)
    inside = not (neg and pos)
    return -d if inside else d


def _poly_sdf(px: float, py: float, pts) -> float:
    """任意（凹）多边形的有向距离场，负值在内部。用偶奇规则判内外 + 最小边距。"""
    import math as _m

    n = len(pts)
    d = 1e18
    # 逐边求到线段的最小距离
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        axx, ayy = ax - px, ay - py
        bxx, byy = bx - ax, by - ay
        t = max(0.0, min(1.0, -(axx * bxx + ayy * byy) / max(1e-9, bxx * bxx + byy * byy)))
        d = min(d, _m.hypot(axx + bxx * t, ayy + byy * t))
    # 偶奇规则判定内外：从点向右引射线，统计与边相交次数
    inside = False
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        if (ay > py) != (by > py):
            xint = (bx - ax) * (py - ay) / (by - ay) + ax
            if px < xint:
                inside = not inside
    return -d if inside else d


def _polyline_sdf(px: float, py: float, pts, thick: float) -> float:
    """折线（多段线段）的有向距离场，负值在折线两侧 thick 范围内。"""
    import math as _m

    n = len(pts)
    d = 1e18
    for i in range(n - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        axx, ayy = ax - px, ay - py
        bxx, byy = bx - ax, by - ay
        t = max(0.0, min(1.0, -(axx * bxx + ayy * byy) / max(1e-9, bxx * bxx + byy * byy)))
        d = min(d, _m.hypot(axx + bxx * t, ayy + byy * t))
    return d - thick


_battery_img_cache: dict = {}   # 缓存渲染出的 PNG bytes（跨 master 安全）
_dot_img_cache: dict = {}
_bar_img_cache: dict = {}
_bolt_img_cache: dict = {}


def _render_bar_rgba(level, w: int, h: int, fill_hex: str, track_hex: str):
    """渲染圆角进度条 PNG（轨道 + 按比例填充），SDF 抗锯齿。

    返回 (w, h, RGBA bytes)。level=None 或 0 时只画轨道。
    """
    fc = _hex_rgb(fill_hex)
    tc = _hex_rgb(track_hex)
    r = max(1.0, h / 2.0 - 0.5)
    hw = w / 2.0
    hh = h / 2.0

    def cov(sdf):
        return max(0.0, min(1.0, 0.5 - sdf))

    def put(x, y, c, a):
        i = (y * w + x) * 4
        px[i] = c[0]
        px[i + 1] = c[1]
        px[i + 2] = c[2]
        px[i + 3] = int(max(px[i + 3], a * 255))

    px = bytearray(w * h * 4)
    # 轨道（通栏圆角）
    for y in range(h):
        for x in range(w):
            s = _rrect_sdf(x + 0.5, y + 0.5, hw, hh, hw, hh, r)
            a = cov(s)
            if a > 0:
                put(x, y, tc, a)
    # 填充（左对齐，按百分比）
    if level and level > 0:
        fill_w = max(0.0, w * min(level, 100) / 100.0)
        if fill_w > 0.5:
            fcx = fill_w / 2.0
            for y in range(h):
                for x in range(w):
                    s = _rrect_sdf(x + 0.5, y + 0.5, fcx, hh, fill_w / 2.0, hh, r)
                    a = cov(s)
                    if a > 0:
                        put(x, y, fc, a)
    return w, h, bytes(px)


def bar_photoimage(master, level, w: int, h: int,
                   fill_hex: str, track_hex: str):
    """渲染圆角进度条 PNG → tk.PhotoImage。缓存 PNG bytes。"""
    import base64
    import tkinter as tk

    key = (level, w, h, fill_hex, track_hex)
    png = _bar_img_cache.get(key)
    if png is None:
        ww, hh, rgba = _render_bar_rgba(level, w, h, fill_hex, track_hex)
        png = _png_encode(ww, hh, rgba)
        _bar_img_cache[key] = png
    return tk.PhotoImage(master=master,
                         data=base64.b64encode(png).decode("ascii"))


def _render_dot_rgba(color: str, d: int = 10):
    """渲染一个实心圆点 PNG（抗锯齿）。返回 (d, d, RGBA bytes)。"""
    c = _hex_rgb(color)
    cx = cy = d / 2.0
    r = d / 2.0 - 0.5
    px = bytearray(d * d * 4)
    for y in range(d):
        py = y + 0.5
        for x in range(d):
            pxx = x + 0.5
            sdf = math.hypot(pxx - cx, py - cy) - r
            a = max(0.0, min(1.0, 0.5 - sdf))
            if a > 0:
                i = (y * d + x) * 4
                px[i] = c[0]
                px[i + 1] = c[1]
                px[i + 2] = c[2]
                px[i + 3] = int(a * 255)
    return d, d, bytes(px)


def dot_photoimage(master, color: str, d: int = 10):
    """渲染状态圆点 PNG → tk.PhotoImage。每次新建，避免跨 master 复用失效。"""
    import base64
    import tkinter as tk

    key = (color, d)
    png = _dot_img_cache.get(key)
    if png is None:
        ww, hh, rgba = _render_dot_rgba(color, d)
        png = _png_encode(ww, hh, rgba)
        _dot_img_cache[key] = png
    return tk.PhotoImage(master=master,
                         data=base64.b64encode(png).decode("ascii"))


def _render_bolt_rgba(color: str, d: int = 14):
    """渲染一个闪电 PNG（极简模式的充电标记）。返回 (d, d, RGBA bytes)。"""
    c = _hex_rgb(color)
    pts = [(0.56 * d, 0.04 * d), (0.14 * d, 0.56 * d), (0.43 * d, 0.56 * d),
           (0.33 * d, 0.97 * d), (0.82 * d, 0.43 * d), (0.53 * d, 0.43 * d)]
    px = bytearray(d * d * 4)
    for y in range(d):
        py = y + 0.5
        for x in range(d):
            a = max(0.0, min(1.0, 0.5 - _poly_sdf(x + 0.5, py, pts)))
            if a > 0:
                i = (y * d + x) * 4
                px[i] = c[0]
                px[i + 1] = c[1]
                px[i + 2] = c[2]
                px[i + 3] = int(a * 255)
    return d, d, bytes(px)


def bolt_photoimage(master, color: str = "#ffffff", d: int = 14):
    """闪电 PNG → tk.PhotoImage（充电时显示在百分比旁边）。"""
    import base64
    import tkinter as tk

    key = (color, d)
    png = _bolt_img_cache.get(key)
    if png is None:
        ww, hh, rgba = _render_bolt_rgba(color, d)
        png = _png_encode(ww, hh, rgba)
        _bolt_img_cache[key] = png
    return tk.PhotoImage(master=master,
                         data=base64.b64encode(png).decode("ascii"))


def battery_photoimage(master, level, color: str, outline: str,
                       w: int = 72, h: int = 26,
                       phase: Optional[int] = None):
    """渲染电池 PNG → tk.PhotoImage。缓存 PNG bytes，每次新建 PhotoImage。

    `phase` 用于充电动的画：同一张图按 phase 换光带位置。
    """
    import base64
    import tkinter as tk

    key = (level, color, outline, w, h, phase)
    png = _battery_img_cache.get(key)
    if png is None:
        ww, hh, rgba = _render_battery_rgba(level, color, outline, w, h, phase)
        png = _png_encode(ww, hh, rgba)
        _battery_img_cache[key] = png
    return tk.PhotoImage(master=master,
                         data=base64.b64encode(png).decode("ascii"))


CARD_RADIUS = 16


class DeviceCard:
    """一张卡片，整个用 Canvas 画：产品图 / 名称 / 状态 / 电量。

    为什么不用 ttk 控件堆：ttk 的 Frame/Label 都是**不透明**的，
    一旦窗口要放自定义背景（图片/视频），卡片下面那块背景就穿不过去。
    Canvas 可以自己画圆角面板，也能把「背景图的一块」糊成毛玻璃垫在最底下。
    """

    def __init__(self, parent, spec: dict, pal: dict, glass=None):
        import tkinter as tk

        self.spec = dict(spec or {})
        self.title = self.spec.get("name") or "设备"
        self.kind = (self.spec.get("kind")
                     or devices.kind_of(self.title, self.spec.get("product", "")))
        self.pal = pal
        self.reading: Optional[Reading] = None
        self.glass = glass              # callable(x, y, w, h) -> PhotoImage / None
        self._anim_id = None
        self._phase = 0
        self._charging = False
        self._origin = (0, 0)
        self._bg_job = None             # 玻璃重画的防抖句柄
        self.W = self.H = CARD_SIZE

        self.canvas = tk.Canvas(parent, width=self.W, height=self.H,
                                highlightthickness=0, bd=0, bg=pal["bg"],
                                takefocus=0)
        self.frame = self.canvas       # 兼容旧布局代码（它是可 grid/place 的控件）

        self.bg_img = None
        self.i_bg = self.canvas.create_image(0, 0, anchor="nw")
        self.i_panel = self.canvas.create_polygon(
            _rrect_points(0, 0, self.W, self.H, CARD_RADIUS),
            fill=pal["card_bg"], outline="")

        # 产品图
        self.img_orig = None
        self.img = None
        self.icon_img = None
        self.photo_item = None
        image_path = (KEYBOARD_IMG if self.kind == "keyboard"
                      else MOUSE_IMG if self.kind == "mouse" else "")
        try:
            if image_path and os.path.isfile(image_path):
                self.img_orig, self.img = fit_image(tk, image_path,
                                                    IMG_MAX_W, IMG_MAX_H,
                                                    master=self.canvas)
                self.photo_item = self.canvas.create_image(
                    self.W / 2, self.H * 0.30, anchor="center", image=self.img)
        except Exception:
            self.img = None
        if self.img is None:
            self.icon_img = _device_icon_photo(self.canvas, self.kind,
                                               pal["accent"], d=_CARD_ICON_D)
            self.photo_item = self.canvas.create_image(
                self.W / 2, self.H * 0.30, anchor="center", image=self.icon_img)

        self.t_title = self.canvas.create_text(
            self.W / 2, self.H * 0.685, text=self.title, anchor="center",
            fill=pal["fg"], font=("Microsoft YaHei UI", 13, "bold"))
        self.t_sub = self.canvas.create_text(
            self.W / 2, self.H * 0.755, text="", anchor="center",
            fill=pal["muted"], font=("Microsoft YaHei UI", 9))

        # 左下：状态圆点 + 文本（充电时的预计充满时间放在副标题行，不挤这一行）
        self.i_dot = self.canvas.create_image(26, self.H - 28, anchor="center")
        self.t_state = self.canvas.create_text(
            38, self.H - 28, text="", anchor="w",
            fill=pal["muted"], font=("Microsoft YaHei UI", 9))

        # 右下：百分比 + 小电池
        self.t_pct = self.canvas.create_text(
            self.W - 26 - BAT_W - 8, self.H - 28, text="--", anchor="e",
            fill=pal["muted"], font=("Segoe UI", 11, "bold"))
        self.i_bat = self.canvas.create_image(
            self.W - 26, self.H - 28, anchor="e")

        self.battery_img = None
        self.dot_img = None

    # ---- 背景 --------------------------------------------------------
    def set_origin(self, x: int, y: int) -> None:
        """告诉卡片自己在窗口里的位置，毛玻璃底才对得上。

        拖动窗口大小时 <Configure> 事件非常密，裁一块玻璃又贵，
        所以这里只记位置，真正的重画攒到 90ms 后一次做。
        """
        x, y = int(x), int(y)
        if (x, y) == self._origin:
            if self.bg_img is not None or self.glass is None:
                return                  # 位置没变：底图还在 / 根本不用玻璃
        self._origin = (x, y)
        if self.glass is None and self.bg_img is None:
            return                      # 没背景：保持主题色圆角面板就行
        if self._bg_job is not None:
            try:
                self.canvas.after_cancel(self._bg_job)
            except Exception:
                pass
        self._bg_job = self.canvas.after(90, self._bg_job_run)

    def _bg_job_run(self) -> None:
        self._bg_job = None
        self.refresh_bg()

    def refresh_bg(self) -> None:
        """重画卡片底：有自定义背景就糊一块毛玻璃，否则就是主题色圆角。"""
        photo = None
        if self.glass is not None:
            try:
                photo = self.glass(self._origin[0], self._origin[1],
                                   self.W, self.H)
            except Exception:
                photo = None
        self.bg_img = photo
        if photo is not None:
            self.canvas.itemconfigure(self.i_bg, image=photo, state="normal")
            self.canvas.itemconfigure(self.i_panel, state="hidden")
        else:
            self.canvas.itemconfigure(self.i_bg, image="", state="hidden")
            self.canvas.itemconfigure(self.i_panel, state="normal",
                                      fill=self.pal["card_bg"])

    # ---- 充电动画 ----------------------------------------------------
    def _start_anim(self) -> None:
        if self._anim_id is not None:
            return
        self._phase = 0
        self._anim_step()

    def _anim_step(self) -> None:
        try:
            if not self._charging:
                self._anim_id = None
                return
            self._phase = (self._phase + 1) % 12
            self._render_battery(self.pal, phase=self._phase)
            self._anim_id = self.canvas.after(120, self._anim_step)
        except Exception:
            self._anim_id = None

    def _stop_anim(self) -> None:
        if self._anim_id is not None:
            try:
                self.canvas.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None
        self._phase = 0

    def destroy(self) -> None:
        self._charging = False
        self._stop_anim()
        if self._bg_job is not None:
            try:
                self.canvas.after_cancel(self._bg_job)
            except Exception:
                pass
            self._bg_job = None
        try:
            self.canvas.destroy()
        except Exception:
            pass

    # ---- 渲染 --------------------------------------------------------
    def _render_battery(self, pal: dict, phase: Optional[int] = None) -> None:
        r = self.reading
        outline = pal["muted"]
        level = r.level if (r is not None and r.ok) else None
        if level is not None:
            color = "#54d67a" if self._charging else level_color(level)
            self.canvas.itemconfigure(self.t_pct, text=f"{level}%", fill=color)
        else:
            color = pal["muted"]
            self.canvas.itemconfigure(self.t_pct, text="--", fill=pal["muted"])
        self.battery_img = battery_photoimage(
            self.canvas, level, color, outline,
            w=BAT_W, h=BAT_H, phase=phase)
        self.canvas.itemconfigure(self.i_bat, image=self.battery_img)

    def _update_status(self, pal: dict) -> None:
        r = self.reading
        ls = r.link_state if r else ""
        if ls == "sleep":
            self.dot_img = dot_photoimage(self.canvas, pal["muted"])
            self.canvas.itemconfigure(self.i_dot, image=self.dot_img,
                                      state="normal")
            self.canvas.itemconfigure(self.t_state, text="休眠",
                                      fill=pal["muted"])
        elif self._charging:
            self.dot_img = dot_photoimage(self.canvas, "#5ddc8a")
            self.canvas.itemconfigure(self.i_dot, image=self.dot_img,
                                      state="normal")
            text = "充电中"
            if r is not None and r.level is not None:
                text = f"充电中 {r.level}%"
            self.canvas.itemconfigure(self.t_state, text=text, fill="#5ddc8a")
        elif ls == "link":
            self.dot_img = dot_photoimage(self.canvas, "#5ddc8a")
            self.canvas.itemconfigure(self.i_dot, image=self.dot_img,
                                      state="normal")
            self.canvas.itemconfigure(self.t_state, text="2.4G",
                                      fill=pal["muted"])
        else:
            self.dot_img = None
            self.canvas.itemconfigure(self.i_dot, image="", state="hidden")
            self.canvas.itemconfigure(self.t_state, text="", fill=pal["muted"])

    @staticmethod
    def _charging_hint(r) -> bool:
        """这条读数是否处于充电状态（和 _charging 的判定保持一致）。"""
        return bool(getattr(r, "charging", False)
                    or "充电" in (getattr(r, "status", "") or ""))

    def update(self, r: Reading, pal: dict) -> None:
        self.reading = r
        self.pal = pal
        # 副标题行：充电时临时换成预计充满时间（居中、空间足，绝不和电量打架）
        sub = r.subtitle
        eta = getattr(r, "charge_eta_min", None)
        if self._charging_hint(r) and eta:
            sub = fmt_eta(eta)
        self.canvas.itemconfigure(self.t_sub, text=sub, fill=pal["muted"])
        self.canvas.itemconfigure(self.t_title,
                                  text=getattr(r, "title", "") or self.title)
        self._charging = bool(getattr(r, "charging", False)
                              or "充电" in (r.status or "")
                              or "已充满" in (r.status or ""))
        self._render_battery(pal)
        self._update_status(pal)
        if self._charging:
            self._start_anim()
        else:
            self._stop_anim()

    def restyle(self, pal: dict) -> None:
        """切主题：底色、文字色、电池外框、状态点都要跟着变。"""
        self.pal = pal
        self.canvas.configure(bg=pal["bg"])
        self.canvas.itemconfigure(self.t_title, fill=pal["fg"])
        self.canvas.itemconfigure(self.t_sub, fill=pal["muted"])
        self.canvas.itemconfigure(self.t_state, fill=pal["muted"])
        self.canvas.itemconfigure(self.t_pct, fill=pal["muted"])
        self.canvas.itemconfigure(self.i_panel, fill=pal["card_bg"])
        self.refresh_bg()
        if self.reading is not None:
            self._render_battery(pal, phase=self._phase if self._charging else None)
            self._update_status(pal)


# ==========================================================================
# 托盘左键小窗口（弹出式，深色背景 + 标题 + 每设备一行：图标+名称+圆角进度条）
# ==========================================================================
POPUP_PAL = {
    "bg": "#1b1b1e", "panel": "#26262a", "panel2": "#333438",
    "fg": "#f2f3f5", "muted": "#8b8d93",
    "fill": "#3ccb5f", "track": "#333438",
    "link": "#3ccb5f", "sleep": "#8b8d93",
    "active": "#333438",
}

_POPUP_BAR_W = 300
_POPUP_BAR_H = 14
_POPUP_ICON_D = 34


def popup_pal(mode_key: str) -> dict:
    """根据主页面主题（dark/light）生成弹窗配色，让左右键弹窗跟随主页面设置。"""
    th = THEMES.get(mode_key, THEMES["dark"])
    if mode_key == "light":
        return {
            "bg": th["bg"], "panel": th["panel"], "panel2": th["panel2"],
            "fg": th["fg"], "muted": th["muted"],
            "fill": "#2fae54", "track": "#e4e7ec",
            "link": "#2fae54", "sleep": th["muted"],
            "active": "#e2e6ec",
        }
    return {
        "bg": th["bg"], "panel": th["panel"], "panel2": th["panel2"],
        "fg": th["fg"], "muted": th["muted"],
        "fill": "#3ccb5f", "track": "#333438",
        "link": "#3ccb5f", "sleep": th["muted"],
        "active": "#333438",
    }


def _device_kind(title: str) -> str:
    """按名字猜设备类型（弹窗里的简笔画图标用）。"""
    return devices.kind_of(title)


def _seg_sdf(px: float, py: float, x1: float, y1: float,
             x2: float, y2: float) -> float:
    dx = x2 - x1
    dy = y2 - y1
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _render_device_icon(kind: str, d: int, bg_hex: str):
    """渲染键盘/鼠标简笔画图标：白色线条画在彩色圆底上（纯 SDF，无依赖）。

    返回 (d, d, RGBA bytes)。
    """
    bg = _hex_rgb(bg_hex)
    fg = (0xF2, 0xF3, 0xF5)
    cx = cy = d / 2.0
    px = bytearray(d * d * 4)

    # 圆底
    r = d / 2.0 - 0.5
    for y in range(d):
        for x in range(d):
            s = math.hypot(x + 0.5 - cx, y + 0.5 - cy) - r
            a = max(0.0, min(1.0, 0.5 - s))
            if a > 0:
                i = (y * d + x) * 4
                px[i] = bg[0]
                px[i + 1] = bg[1]
                px[i + 2] = bg[2]
                px[i + 3] = int(a * 255)

    def blend(x, y, c, a):
        if a <= 0 or not (0 <= x < d and 0 <= y < d):
            return
        i = (y * d + x) * 4
        base = (px[i], px[i + 1], px[i + 2])
        px[i] = int(c[0] * a + base[0] * (1 - a))
        px[i + 1] = int(c[1] * a + base[1] * (1 - a))
        px[i + 2] = int(c[2] * a + base[2] * (1 - a))
        px[i + 3] = max(px[i + 3], int(a * 255))

    def stroke_rrect(cxx, cyy, hw, hh, rr, thick):
        for y in range(d):
            for x in range(d):
                s = abs(_rrect_sdf(x + 0.5, y + 0.5, cxx, cyy, hw, hh, rr))
                a = max(0.0, min(1.0, thick - s))
                blend(x, y, fg, a)

    def stroke_seg(x1, y1, x2, y2, thick):
        for y in range(d):
            for x in range(d):
                a = max(0.0, min(1.0, thick - _seg_sdf(x + 0.5, y + 0.5, x1, y1, x2, y2)))
                blend(x, y, fg, a)

    if kind == "keyboard":
        # 键盘俯视：外框 + 3排×4列键帽
        stroke_rrect(cx, cy, d * 0.34, d * 0.23, d * 0.07, 1.2)
        for row in range(3):
            ry = cy + (row - 1) * d * 0.115
            for col in range(4):
                rx = cx + (col - 1.5) * d * 0.115
                stroke_rrect(rx, ry, d * 0.042, d * 0.030, d * 0.012, 0.8)
    elif kind in ("device", "headset"):
        # 通用 2.4G 接收器：外框里画一个小电池
        stroke_rrect(cx, cy, d * 0.31, d * 0.21, d * 0.06, 1.2)
        stroke_rrect(cx - d * 0.02, cy, d * 0.15, d * 0.08, d * 0.03, 1.0)
        stroke_rrect(cx + d * 0.16, cy, d * 0.030, d * 0.035, d * 0.012, 0.9)
    else:
        # 鼠标俯视：竖圆角矩形主体 + 滚轮竖线 + 左右键分界横线
        stroke_rrect(cx, cy, d * 0.19, d * 0.30, d * 0.095, 1.3)
        stroke_seg(cx - d * 0.19, cy - d * 0.05, cx + d * 0.19, cy - d * 0.05, 0.9)
        stroke_seg(cx, cy - d * 0.30, cx, cy + d * 0.02, 0.9)

    return d, d, bytes(px)


_device_icon_cache: dict = {}


def _device_icon_photo(master, kind: str, color: str, d: int = _POPUP_ICON_D):
    import base64
    import tkinter as tk

    key = ("device_icon", kind, color, d)
    cached = _device_icon_cache.get(key)
    if cached is not None:
        return tk.PhotoImage(master=master,
                             data=base64.b64encode(cached).decode("ascii"))
    w, h, rgba = _render_device_icon(kind, d, color)
    png = _png_encode(w, h, rgba)
    _device_icon_cache[key] = png
    return tk.PhotoImage(master=master, data=base64.b64encode(png).decode("ascii"))


_POPUP_REFRESH_MS = 10000   # 弹窗自动刷新间隔
_POPUP_RADIUS = 16          # 圆角半径（像素）


def _get_hwnd(win) -> int:
    """取 Toplevel 的顶层窗口句柄（overrideredirect 下 winfo_id 可能是子窗口）。"""
    try:
        hwnd = int(win.winfo_id())
    except Exception:
        return 0
    user32 = ctypes.windll.user32
    parent = user32.GetParent(hwnd)
    return parent if parent else hwnd


def _apply_rounded(win, radius: int = _POPUP_RADIUS) -> None:
    """给无边框弹窗加圆角：优先 Win11 DWM 圆角，失败回退 SetWindowRgn。"""
    hwnd = _get_hwnd(win)
    if not hwnd:
        return
    # Windows 11：DWM 圆角（最平滑，不裁剪内容）
    try:
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
        if hr == 0:
            return
    except Exception:
        pass
    # Win10 回退：圆角区域
    try:
        win.update_idletasks()
        w = max(1, win.winfo_width())
        h = max(1, win.winfo_height())
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, w + 1, h + 1, radius * 2, radius * 2)
        ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, True)
    except Exception:
        pass


def _work_area() -> tuple[int, int, int, int]:
    """取桌面工作区（屏幕去掉任务栏后的区域），返回 (left, top, right, bottom)。"""
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    rect = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


def _cursor_pos() -> tuple[int, int]:
    """取当前鼠标屏幕坐标（点击托盘图标时鼠标就在图标上，用作弹窗定位锚点）。"""
    from ctypes import wintypes

    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _animate_in(win, tx: int, ty: int) -> None:
    """弹出动画：从目标位置下方 12px 淡入 + 上滑（ease-out，短促不拖沓）。"""
    steps = 5
    delay = 10

    def step(i):
        if not win.winfo_exists():
            return
        t = i / steps
        e = 1 - (1 - t) ** 2
        try:
            win.attributes("-alpha", max(0.0, min(1.0, e)))
        except Exception:
            pass
        y = int(ty + 12 - 12 * e)
        try:
            win.geometry(f"+{tx}+{y}")
        except Exception:
            pass
        if i < steps:
            win.after(delay, lambda: step(i + 1))
        else:
            try:
                win.attributes("-alpha", 1.0)
            except Exception:
                pass

    win.attributes("-alpha", 0.0)
    step(0)


def build_popup(root, readings: list[Reading], pal: dict = None):
    """托盘左键弹的小窗口：深色背景 + 标题 + 每设备一行（键盘/鼠标图标 + 名称 + 圆角进度条）。

    - 圆角无边框卡片（Win11 DWM 圆角 / Win10 SetWindowRgn）
    - 弹出淡入 + 上滑动效
    - 每 _POPUP_REFRESH_MS 毫秒后台自动刷新（线程读 + 主线程更新）
    - 定位到工作区右下角（不遮挡任务栏）
    """
    import tkinter as tk
    from tkinter import ttk

    pal = pal or POPUP_PAL
    win = tk.Toplevel(root)
    win.title("2.4G Device Battery Check")
    win.configure(bg=pal["bg"])
    win.overrideredirect(True)          # 无边框小卡片
    win.attributes("-topmost", True)    # 浮在最上

    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("Popup.TFrame", background=pal["bg"])
    style.configure("PopupPanel.TFrame", background=pal["panel"])
    style.configure("PopupTitle.TLabel", background=pal["bg"],
                    foreground=pal["fg"], font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("PopupName.TLabel", background=pal["panel"],
                    foreground=pal["fg"], font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("PopupState.TLabel", background=pal["panel"],
                    foreground=pal["muted"], font=("Microsoft YaHei UI", 8))
    style.configure("PopupPct.TLabel", background=pal["panel"],
                    foreground=pal["fg"], font=("Segoe UI", 11, "bold"))

    # 顶部标题
    title_bar = ttk.Frame(win, style="Popup.TFrame", padding=(16, 12, 16, 8))
    title_bar.pack(fill="x")
    ttk.Label(title_bar, text="2.4G Device Battery Check",
              style="PopupTitle.TLabel").pack(side="left")

    # 设备条目
    rows = ttk.Frame(win, style="Popup.TFrame", padding=(16, 4, 16, 14))
    rows.pack(fill="x")

    row_widgets = []
    first = True
    for r in readings:
        panel = ttk.Frame(rows, style="PopupPanel.TFrame", padding=(12, 10))
        panel.pack(fill="x", pady=(0 if first else 8, 0))
        first = False

        # 左：键盘/鼠标简笔画图标
        kind = _device_kind(r.title)
        icon_color = pal["link"] if r.ok else pal["muted"]
        lab_icon = ttk.Label(panel, style="PopupName.TLabel")
        lab_icon.pack(side="left", padx=(0, 10))
        icon_img = _device_icon_photo(lab_icon.tk, kind, icon_color)
        lab_icon.configure(image=icon_img)

        # 中：名称 + 状态
        mid = ttk.Frame(panel, style="PopupPanel.TFrame")
        mid.pack(side="left", fill="x", expand=True)
        lab_name = ttk.Label(mid, text=r.title, style="PopupName.TLabel")
        lab_name.pack(anchor="w")
        lab_state = ttk.Label(mid, text=_popup_state_text(r),
                              style="PopupState.TLabel")
        lab_state.pack(anchor="w")

        # 右：百分比
        lab_pct = ttk.Label(panel, text=_popup_pct_text(r),
                            style="PopupPct.TLabel")
        lab_pct.pack(side="right", padx=(10, 0))

        # 进度条（通栏，在面板下方）
        bar = ttk.Label(panel, style="PopupPanel.TFrame")
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        bar_img = bar_photoimage(bar.tk, r.level, _POPUP_BAR_W, _POPUP_BAR_H,
                                 pal["fill"], pal["track"])
        bar.configure(image=bar_img)

        row_widgets.append({"icon": lab_icon, "icon_img": icon_img, "kind": kind,
                            "name": lab_name, "state": lab_state,
                            "pct": lab_pct, "bar": bar, "bar_img": bar_img})

    # ---- 自动刷新（后台线程读 + 主线程 after 轮询更新）----
    refresh_q = queue.Queue()
    refreshing = {"flag": False}

    def _update_row(rw: dict, r: Reading):
        color = pal["link"] if r.ok else pal["muted"]
        img = _device_icon_photo(rw["icon"].tk, rw["kind"], color)
        rw["icon"].configure(image=img)
        rw["icon_img"] = img
        rw["state"].configure(text=_popup_state_text(r))
        rw["pct"].configure(text=_popup_pct_text(r))
        bimg = bar_photoimage(rw["bar"].tk, r.level, _POPUP_BAR_W, _POPUP_BAR_H,
                              pal["fill"], pal["track"])
        rw["bar"].configure(image=bimg)
        rw["bar_img"] = bimg

    def _read_async():
        if refreshing["flag"]:
            return
        refreshing["flag"] = True
        try:
            new = read_all()
        except Exception:
            new = None
        finally:
            refreshing["flag"] = False
        if new is not None:
            refresh_q.put(new)

    def _poll():
        if not win.winfo_exists():
            return
        try:
            while True:
                new = refresh_q.get_nowait()
                for rw, r in zip(row_widgets, new):
                    _update_row(rw, r)
        except queue.Empty:
            pass
        threading.Thread(target=_read_async, daemon=True).start()
        win.after(_POPUP_REFRESH_MS, _poll)

    # 关闭：Esc / 失去焦点
    def close(_e=None):
        try:
            win.destroy()
        except Exception:
            pass

    win.bind("<Escape>", close)
    win.bind("<FocusOut>", lambda _e: close())
    win.focus_force()

    # 定位到工作区右下角（不遮挡任务栏），加圆角，然后淡入动画
    tx, ty = _place_popup(win)
    win.update_idletasks()
    _apply_rounded(win)
    win.after(_POPUP_REFRESH_MS, _poll)
    _animate_in(win, tx, ty)
    return win


def _popup_pct_text(r: Reading) -> str:
    if r.ok and r.level is not None:
        return f"{r.level}%"
    return "--"


def _popup_state_text(r: Reading) -> str:
    if r.charging or "充电" in (r.status or ""):
        base = f"充电中 {r.level}%" if r.ok else "充电中"
        eta = getattr(r, "charge_eta_min", None)
        if eta:
            base += f" · 约{eta}分钟充满"
        return base
    if r.link_state == "sleep":
        return "休眠"
    if r.link_state == "link":
        return "2.4G · 电压估算" if r.estimated else "2.4G"
    if r.error:
        return r.error
    return ""


def _place_popup(win) -> tuple[int, int]:
    """把弹窗定位到托盘图标上方，不遮挡任务栏。

    - 锚点 = 当前鼠标屏幕坐标（点击托盘图标时鼠标就在图标上）。
    - 水平：弹窗左边缘与托盘图标左边缘对齐（图标半宽约 8px），弹窗向右展开；
      若右边缘会超出工作区，则整体左移贴工作区右边界。
    - 垂直：弹窗底边在图标上方 20px（比之前更靠上，远离任务栏）；
      上方放不下时翻到图标下方。
    返回 (x, y) 供动画使用。
    """
    win.update_idletasks()
    w = win.winfo_width()
    h = win.winfo_height()
    left, top, right, bottom = _work_area()
    mx, my = _cursor_pos()

    # 水平：左边缘对齐图标左边缘（图标半宽约 8px）
    x = mx - 8
    # 右边缘溢出工作区 → 左移贴右边界
    if x + w > right - 8:
        x = right - w - 8
    x = max(left + 8, x)

    # 垂直：优先放在图标上方 20px，但绝不遮挡任务栏（弹窗底边 ≤ 工作区底边）
    y = my - h - 20
    # 任务栏从工作区 bottom 开始；弹窗底边必须收进工作区
    y = min(y, bottom - h - 8)
    if y < top + 8:
        y = my + 20

    win.geometry(f"+{x}+{y}")
    return x, y


def build_menu_popup(root, pal: dict = None):
    """托盘右键弹的菜单小窗口：打开主页面 / 退出（圆角、动画、不遮挡任务栏）。

    - 主题背景跟随主页面设置（popup_pal）
    - 淡入 + 上滑动效（复用 _animate_in）
    - Esc / 失去焦点关闭
    """
    import tkinter as tk
    from tkinter import ttk

    pal = pal or popup_pal("dark")
    win = tk.Toplevel(root)
    win.title("2.4G Device Battery Check")
    win.configure(bg=pal["bg"])
    win.overrideredirect(True)
    win.attributes("-topmost", True)

    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("MenuPopup.TFrame", background=pal["panel"])
    style.configure("MenuPopup.TLabel", background=pal["panel"],
                    foreground=pal["fg"], font=("Microsoft YaHei UI", 10))

    body = ttk.Frame(win, style="MenuPopup.TFrame", padding=(6, 6))
    body.pack(fill="both", expand=True)

    def close(_e=None):
        try:
            win.destroy()
        except Exception:
            pass

    def open_main(_e=None):
        close()
        try:
            root._signal_q.put(("show", None))
        except Exception:
            pass

    def do_exit(_e=None):
        close()
        try:
            root._signal_q.put(("quit", None))
        except Exception:
            pass

    items = [
        ("打开主页面", open_main),
        ("退出", do_exit),
    ]
    for label, handler in items:
        row = ttk.Frame(body, style="MenuPopup.TFrame", padding=(14, 10))
        row.pack(fill="x")
        lab = ttk.Label(row, text=label, style="MenuPopup.TLabel")
        lab.pack(fill="x")
        for w in (row, lab):
            w.bind("<Button-1>", handler)

    win.bind("<Escape>", close)
    win.bind("<FocusOut>", lambda _e: close())
    win.focus_force()

    tx, ty = _place_popup(win)
    win.update_idletasks()
    _apply_rounded(win)
    _animate_in(win, tx, ty)
    return win


# ==========================================================================
# 设置窗口的「设备」页：一键扫描 → 选择添加 / 移除
# ==========================================================================
def build_device_page(parent, pal: dict, on_change):
    """在设置窗口的内容区里建「设备」页：扫描本机能读到电量的设备。

    on_change(): 设备表被改过（添加/移除）时回调主界面重建卡片。
    返回 (page, auto_scan)：page 是页面框架；auto_scan() 用来触发自动扫描。
    """
    import tkinter as tk
    from tkinter import ttk

    page = ttk.Frame(parent, style="SetPage.TFrame")
    page.pack(fill="both", expand=True)

    style = ttk.Style(page)
    style.theme_use("clam")
    style.configure("Mgr.TFrame", background=pal["bg"])
    style.configure("MgrMuted.TLabel", background=pal["bg"],
                    foreground=pal["muted"], font=("Microsoft YaHei UI", 9))
    style.configure("MgrCard.TFrame", background=pal["panel"])
    style.configure("MgrName.TLabel", background=pal["panel"],
                    foreground=pal["fg"],
                    font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("MgrCardSub.TLabel", background=pal["panel"],
                    foreground=pal["muted"], font=("Microsoft YaHei UI", 9))

    state = {"found": [], "specs": devices.load_specs(), "scanning": False}
    q: "queue.Queue[tuple]" = queue.Queue()

    ttk.Label(page, text="扫描本机的 2.4G 接收器 / 设备，只列出真能读到电量的；"
                         "点「添加」后才会出现在主界面，随时可以移除。",
              style="MgrMuted.TLabel", wraplength=430,
              justify="left").pack(anchor="w", pady=(0, 10))

    bar = ttk.Frame(page, style="Mgr.TFrame")
    bar.pack(fill="x")
    btn_scan = ttk.Button(bar, text="一键扫描", style="Accent.TButton")
    btn_scan.pack(side="left")
    lab_prog = ttk.Label(bar, text="", style="MgrMuted.TLabel")
    lab_prog.pack(side="left", padx=(12, 0))
    lab_unsup = ttk.Label(page, text="", style="MgrMuted.TLabel",
                          wraplength=430, justify="left")
    lab_unsup.pack(anchor="w", pady=(6, 0))

    wrap = ttk.Frame(page, style="Mgr.TFrame")
    wrap.pack(fill="both", expand=True, pady=(10, 0))
    canvas = tk.Canvas(wrap, bg=pal["bg"], highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas, style="Mgr.TFrame")
    canvas.create_window((0, 0), window=inner, anchor="nw",
                         tags=("inner",))
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    def _fit(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        try:
            canvas.itemconfigure("inner", width=canvas.winfo_width())
        except Exception:
            pass

    inner.bind("<Configure>", _fit)
    canvas.bind("<Configure>", _fit)

    def _wheel(e):
        canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

    # 滚轮只在「设备」页可见时生效（页面销毁时解绑，别影响别的页面）
    canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
    page.bind("<Destroy>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    # ---- 行 ----------------------------------------------------------
    def _toggle(spec: dict, added: bool) -> None:
        if added:
            state["specs"] = devices.remove_spec(spec["key"])
        else:
            state["specs"] = devices.add_spec(spec)
        on_change()
        render()

    def _row(spec: dict, reading, added: bool) -> None:
        panel = ttk.Frame(inner, style="MgrCard.TFrame", padding=(12, 10))
        panel.pack(fill="x", pady=(0, 8))

        kind = spec.get("kind") or devices.kind_of(spec.get("name", ""))
        icon = _device_icon_photo(panel.tk, kind, pal["accent"])
        lab_icon = ttk.Label(panel, style="MgrName.TLabel")
        lab_icon.configure(image=icon)
        lab_icon.image = icon
        lab_icon.pack(side="left", padx=(0, 10))

        mid = ttk.Frame(panel, style="MgrCard.TFrame")
        mid.pack(side="left", fill="x", expand=True)
        ttk.Label(mid, text=spec.get("name") or "设备",
                  style="MgrName.TLabel").pack(anchor="w")
        sub = f"{int(spec.get('vid', 0)):04X}:{int(spec.get('pid', 0)):04X}"
        if reading is None:
            sub += " · 未连接"
        elif reading.ok:
            sub += f" · {reading.level}%"
            if reading.status:
                sub += f" · {reading.status}"
            if reading.estimated:
                sub += "（电压估算）"
        else:
            sub += " · " + (reading.error or "读不到")
        ttk.Label(mid, text=sub, style="MgrCardSub.TLabel").pack(anchor="w")

        ttk.Button(panel, text="移除" if added else "添加",
                   command=lambda s=spec, a=added: _toggle(s, a)).pack(side="right")

    def render() -> None:
        for w in inner.winfo_children():
            w.destroy()
        added_keys = {s.get("key") for s in state["specs"]}
        found_keys = {s.get("key") for s, _ in state["found"]}
        rows = [(s, r, s.get("key") in added_keys) for s, r in state["found"]]
        for spec in state["specs"]:
            if spec.get("key") not in found_keys:
                rows.append((spec, None, True))
        if not rows:
            ttk.Label(inner, text="还没有扫描 —— 点上方「一键扫描」看看有哪些设备能读电量",
                      style="MgrMuted.TLabel").pack(pady=30)
        for spec, reading, added in rows:
            _row(spec, reading, added)
        _fit()

    # ---- 扫描 --------------------------------------------------------
    def _scan() -> None:
        if state["scanning"]:
            return
        state["scanning"] = True
        state["found"] = []
        btn_scan.configure(state="disabled", text="扫描中…")
        lab_prog.configure(text="正在扫描…")
        render()

        def work():
            def prog(done, total, label):
                q.put(("prog", done, total, label))
            try:
                found = devices.scan_devices(prog)
            except Exception:
                traceback.print_exc()
                found = []
            q.put(("done", found))

        threading.Thread(target=work, daemon=True).start()

    def pump() -> None:
        try:
            if not page.winfo_exists():
                return
        except Exception:
            return
        try:
            while True:
                kind, *rest = q.get_nowait()
                if kind == "prog":
                    done, total, label = rest
                    text = f"正在扫描 {done}/{total}"
                    if label:
                        text += f" · {label}"
                    lab_prog.configure(text=text)
                elif kind == "done":
                    state["found"] = rest[0]
                    state["specs"] = devices.load_specs()
                    state["scanning"] = False
                    btn_scan.configure(state="normal", text="重新扫描")
                    n = len(state["found"])
                    lab_prog.configure(
                        text=f"找到 {n} 个可读电量的设备" if n
                        else "没找到能读电量的设备（接收器插着、设备别休眠再试）")
                    unsup = devices.unsupported_seen()
                    lab_unsup.configure(
                        text=("检测到但协议暂不支持读电量：" + "、".join(unsup)
                              + "（如需看它的电量请用厂商软件）") if unsup else "")
                    render()
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        page.after(120, pump)

    btn_scan.configure(command=_scan)
    render()
    page.after(120, pump)

    def auto_scan():
        # 打开页面就扫一遍（本机 0.5 秒左右），不用先点按钮
        page.after(200, _scan)

    return page, auto_scan


# ==========================================================================
# 程序图标 / 任务栏身份
# ==========================================================================
def apply_app_icon(root) -> None:
    """给窗口装自己的图标，并把进程从 Windows 的「Python」分组里摘出来。

    不设 AppUserModelID 的话，即使窗口有自己的图标，任务栏也常常还是挂
    pythonw.exe 的图标和「Python」名字。

    图标只走两条可靠路径：
      1) iconbitmap(ico) —— 同时作用于 root 本身；
      2) WM_SETICON 挂 ICO 里的原生 32/16px 图。
    千万不要用 iconphoto(256px PNG)：Tk 把大图转 HICON 会静默失败，
    回退成 Tk 默认图标，还会把上面两条设好的正确图标一起盖掉。

    另外 Tk 在窗口隐藏/重映射等时机会重新断言默认图标，所以这里装上
    一个「哨兵」：每秒用 WM_GETICON 校验，发现被换掉就立刻重挂。
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:
        pass
    if os.path.isfile(APP_ICON_ICO):
        try:
            root.iconbitmap(APP_ICON_ICO)        # 直接作用于 root
        except Exception:
            pass

    if not os.path.isfile(APP_ICON_ICO):
        return
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    WM_SETICON, WM_GETICON = 0x0080, 0x007F
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
    icons = []                                   # [(which, hicon), ...]
    for size in (32, 16):                        # (大图标, 小图标)
        h = user32.LoadImageW(None, APP_ICON_ICO, IMAGE_ICON,
                              size, size, LR_LOADFROMFILE)
        if h:
            icons.append((1 if size == 32 else 0, h))
    if not icons:
        return
    hicons = {which: h for which, h in icons}

    def _apply():
        hwnd = _get_hwnd(root)
        if not hwnd:
            return False
        for which, h in icons:
            user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON,
                                which, h)
        return True

    def ensure(force: bool = False) -> bool:
        """图标还在就什么都不做；被 Tk 换掉了就重挂。"""
        hwnd = _get_hwnd(root)
        if not hwnd:
            return False
        if not force:
            for which, h in hicons.items():
                cur = user32.SendMessageW(ctypes.c_void_p(hwnd), WM_GETICON,
                                          which, 0)
                if cur and cur != h:
                    return _apply()
        return True

    if not _apply():                             # 窗口还没映射：映射后再补
        root.after(120, lambda: (_apply() or root.after(400, _apply)))
    root._ensure_icon = ensure


# ==========================================================================
# 极简模式：毛玻璃小浮窗
# ==========================================================================
MINI_SIZES = [("小", 0.85), ("中", 1.0), ("大", 1.15), ("特大", 1.3)]
MINI_SCALE_OF = dict(MINI_SIZES)
MINI_LABEL_OF = {round(v, 2): k for k, v in MINI_SIZES}
MINI_MARGIN = 18          # 默认离屏幕边缘的距离
MINI_SNAP = 26            # 拖到离边缘这么近就吸附过去

_FONT_UI = "Microsoft YaHei UI"
_MINI_TINT = 0x5510141A     # 亚克力底色：0xAA 白→不透明，数值越小越通透


def _apply_acrylic(win, tint: int = 0x9910151C) -> bool:
    """给无边框窗口加毛玻璃（Win10 1803+ / Win11）。返回是否成功。"""
    class ACCENTPOLICY(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_uint),
                    ("AccentFlags", ctypes.c_uint),
                    ("GradientColor", ctypes.c_uint),
                    ("AnimationId", ctypes.c_uint)]

    class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_uint),
                    ("Data", ctypes.c_void_p),
                    ("SizeOfData", ctypes.c_size_t)]

    hwnd = _get_hwnd(win)
    if not hwnd:
        return False

    accent = ACCENTPOLICY()
    accent.AccentState = 4          # ACCENT_ENABLE_ACRYLICBLURBEHIND
    accent.AccentFlags = 0
    accent.GradientColor = tint     # 0xAABBGGRR
    accent.AnimationId = 0

    data = WINDOWCOMPOSITIONATTRIBDATA()
    data.Attribute = 19             # WCA_ACCENT_POLICY
    data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
    data.SizeOfData = ctypes.sizeof(accent)
    try:
        fn = ctypes.windll.user32.SetWindowCompositionAttribute
    except Exception:
        return False
    try:
        return bool(fn(ctypes.c_void_p(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def _abbrev(name: str, limit: int = 8) -> str:
    """设备名缩写：取第一个词（VGN S99 → VGN，G502 LIGHTSPEED → G502）。"""
    parts = [p for p in str(name or "").replace("·", " ").split() if p]
    if not parts:
        return "?"
    word = parts[0]
    return word if len(word) <= limit else word[:limit]


def _dwm_round(win, radius: int) -> bool:
    """Win11：交给 DWM 自己切圆角。

    亚克力模糊是 DWM 画的，**不受 SetWindowRgn 约束**（区域裁掉了文字和边框，
    却裁不掉模糊层），所以圆角只能让系统来做。
    """
    hwnd = _get_hwnd(win)
    if not hwnd:
        return False
    try:
        pref = ctypes.c_int(2 if radius > 8 else 3)   # 2=ROUND 3=ROUNDSMALL
        rc = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
        return rc == 0
    except Exception:
        return False


def _mini_label(scale: float) -> str:
    try:
        return MINI_LABEL_OF.get(round(float(scale), 2), "中")
    except Exception:
        return "中"


def _mini_metrics(scale: float = 1.0) -> dict:
    """按比例算小浮窗的尺寸与字号（字号有下限，别小到看不清）。"""
    s = max(0.7, min(1.6, float(scale or 1.0)))
    return {
        "scale": s,
        "w": int(round(132 * s)),
        "h": int(round(34 * s)),
        "gap": max(4, int(round(6 * s))),
        "radius": max(7, int(round(13 * s))),
        "pad": int(round(11 * s)),
        "dot": max(2.0, round(2.2 * s, 1)),
        "name_dx": int(round(19 * s)),
        "name_size": max(7, int(round(9 * s))),
        "pct_size": max(9, int(round(11 * s))),
        "bolt": max(8, int(round(11 * s))),
        "bolt_gap": max(2, int(round(3 * s))),
    }


def _soften(hex_color: str, amount: float = 0.25) -> str:
    """把颜色往白色提一点：毛玻璃上的字别用纯色，容易显脏。"""
    r, g, b = _hex_rgb(hex_color)
    f = lambda v: int(round(v + (255 - v) * amount))  # noqa: E731
    return "#%02x%02x%02x" % (f(r), f(g), f(b))


def _rrect_points(x0: float, y0: float, x1: float, y1: float,
                  r: float, steps: int = 5):
    """圆角矩形的轮廓点（canvas 的 create_polygon 画玻璃边框用）。"""
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
                       (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class MiniCard:
    """极简模式下的一块毛玻璃小浮窗。

    设计取向：**字小、色淡、边框细**，让玻璃本身是主角；充电时电量旁边
    多一个小闪电。拖动由外部 dock 统一处理（整组一起走）。
    """

    _BORDER_IDLE = "#6f7a89"
    _BORDER_HOVER = "#8d9aac"
    _BORDER_DRAG = "#b3c1d2"

    def __init__(self, root, reading, pal, x: int, y: int, scale: float = 1.0,
                 on_click=None, on_drag=None, on_drag_end=None):
        import tkinter as tk

        self.m = _mini_metrics(scale)
        self.W, self.H = self.m["w"], self.m["h"]
        self.on_click = on_click
        self.on_drag = on_drag
        self.on_drag_end = on_drag_end
        self.moved = 0
        self._last = None

        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry(f"{self.W}x{self.H}+{x}+{y}")
        win.configure(bg=_MINI_KEY)

        canvas = tk.Canvas(win, width=self.W, height=self.H, bg=_MINI_KEY,
                           highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        win.update_idletasks()

        # 亚克力加得很淡：越淡越像玻璃，而不是一块板
        self.acrylic = _apply_acrylic(win, tint=_MINI_TINT)
        if self.acrylic:
            try:
                win.attributes("-transparentcolor", _MINI_KEY)
            except Exception:
                self.acrylic = False
        if not _dwm_round(win, self.m["radius"]):
            self._round_region(win)          # 只有老系统才退回窗口区域
        if not self.acrylic:
            try:
                win.configure(bg="#1b2028")
                canvas.configure(bg="#1b2028")
                win.attributes("-alpha", 0.9)
            except Exception:
                pass

        self.win = win
        self.canvas = canvas
        cy = self.H / 2.0
        self.border = canvas.create_polygon(
            _rrect_points(1.5, 1.5, self.W - 1.5, self.H - 1.5,
                          max(4, self.m["radius"] - 5)),
            fill="", outline=self._BORDER_IDLE, width=1)
        self.dot = canvas.create_oval(
            self.m["pad"], cy - self.m["dot"],
            self.m["pad"] + 2 * self.m["dot"], cy + self.m["dot"],
            fill="#4dd07a", outline="")
        from tkinter import font as tkfont
        self._name_font = tkfont.Font(root=canvas, family=_FONT_UI,
                                      size=self.m["name_size"])
        self._pct_font = tkfont.Font(root=canvas, family=_FONT_UI,
                                     size=self.m["pct_size"], weight="bold")
        self.t_name = canvas.create_text(
            self.m["name_dx"], cy, text="", anchor="w",
            fill="#b6c2d3", font=self._name_font)
        # 百分比下面垫一层 1px 深色描影：玻璃透到白底上也依然清楚
        self.t_pct_shadow = canvas.create_text(
            self.W - self.m["pad"], cy + 1, text="", anchor="e",
            fill="#0d1219", font=self._pct_font)
        self.t_pct = canvas.create_text(
            self.W - self.m["pad"], cy, text="", anchor="e",
            fill="#ffffff", font=self._pct_font)
        self._bolt_photo = bolt_photoimage(canvas, "#b8f2ce", self.m["bolt"])
        self.i_bolt = canvas.create_image(
            self.W - self.m["pad"], cy, anchor="e",
            image=self._bolt_photo, state="hidden")

        # 色键透明的像素本身「点不到」：盖一层几乎看不见的窗口专门接鼠标
        hit = tk.Toplevel(root)
        hit.overrideredirect(True)
        hit.attributes("-topmost", True)
        hit.geometry(f"{self.W}x{self.H}+{x}+{y}")
        hit.configure(bg="#0e1218")
        try:
            hit.attributes("-alpha", 0.02)
            hit.configure(cursor="fleur")
        except Exception:
            pass
        hit.bind("<ButtonPress-1>", self._press)
        hit.bind("<B1-Motion>", self._drag)
        hit.bind("<ButtonRelease-1>", self._release)
        hit.bind("<Enter>", lambda _e: self._set_border(self._BORDER_HOVER))
        hit.bind("<Leave>", lambda _e: self._set_border(self._BORDER_IDLE))
        self.hit = hit

        canvas.bind("<ButtonPress-1>", self._press)
        canvas.bind("<B1-Motion>", self._drag)
        canvas.bind("<ButtonRelease-1>", self._release)
        win.bind("<Escape>", lambda _e: self._click())
        self.update(reading, pal)

    # -- 圆角 -------------------------------------------------------------
    def _round_region(self, win) -> None:
        hwnd = _get_hwnd(win)
        if not hwnd:
            return
        try:
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, self.W + 1, self.H + 1,
                self.m["radius"] * 2 + 2, self.m["radius"] * 2 + 2)
            ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, True)
        except Exception:
            pass

    # -- 内容 -------------------------------------------------------------
    def update(self, reading, pal: dict) -> None:
        lvl = getattr(reading, "level", None)
        charging = bool(getattr(reading, "charging", False))
        base = level_color(lvl) if lvl is not None else pal.get("muted", "#9aa0a6")
        try:
            cy = self.H / 2.0
            pct_text = "--" if lvl is None else f"{lvl}%"
            # 名字按剩余宽度自动截断，别和百分比挤在一起
            right_edge = (self.W - self.m["pad"] - self.m["bolt"]
                          - self.m["bolt_gap"]) if charging else (
                              self.W - self.m["pad"])
            pct_w = self._pct_font.measure(pct_text)
            room = right_edge - pct_w - 9 - self.m["name_dx"]
            name = _abbrev(reading.title, 9)
            while name and self._name_font.measure(name) > room:
                name = name[:-1]
            self.canvas.itemconfigure(self.t_name, text=name.rstrip() or "?")
            self.canvas.itemconfigure(self.t_pct, text=pct_text,
                                      fill=_soften(base, 0.18))
            self.canvas.itemconfigure(self.t_pct_shadow, text=pct_text)
            self.canvas.itemconfigure(self.dot, fill=_soften(base, 0.08))
            if charging:
                self.canvas.itemconfigure(self.i_bolt, state="normal")
                px = self.W - self.m["pad"] - self.m["bolt"] - self.m["bolt_gap"]
            else:
                self.canvas.itemconfigure(self.i_bolt, state="hidden")
                px = self.W - self.m["pad"]
            self.canvas.coords(self.t_pct, px, cy)
            self.canvas.coords(self.t_pct_shadow, px, cy + 1)
        except Exception:
            pass

    def _set_border(self, color: str) -> None:
        try:
            self.canvas.itemconfigure(self.border, outline=color)
        except Exception:
            pass

    def _set_dragging(self, on: bool) -> None:
        """拖起来的时候让玻璃「实」一点 + 边框提亮，像把卡片拿起来了。"""
        self._set_border(self._BORDER_DRAG if on else self._BORDER_IDLE)
        if getattr(self, "acrylic", False):
            _apply_acrylic(self.win, tint=(0x9910141A if on else _MINI_TINT))

    # -- 位置 -------------------------------------------------------------
    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def move_to(self, x: int, y: int) -> None:
        pos = f"+{int(x)}+{int(y)}"
        for w in (self.win, getattr(self, "hit", None)):
            if w is None:
                continue
            try:
                w.geometry(pos)
            except Exception:
                pass

    # -- 点击 / 拖动 -------------------------------------------------------
    def _press(self, event) -> None:
        self._last = (event.x_root, event.y_root)
        self.moved = 0
        self._set_dragging(True)

    def _drag(self, event) -> None:
        if not self._last:
            return
        dx = event.x_root - self._last[0]
        dy = event.y_root - self._last[1]
        if not dx and not dy:
            return
        self.moved += abs(dx) + abs(dy)
        self._last = (event.x_root, event.y_root)
        if self.on_drag is not None:
            self.on_drag(dx, dy)          # 整组一起动
        else:
            self.move_to(self.win.winfo_x() + dx, self.win.winfo_y() + dy)

    def _release(self, _event) -> None:
        self._set_dragging(False)
        if self.moved < 4:
            self._click()
        elif self.on_drag_end is not None:
            self.on_drag_end()

    def _click(self) -> None:
        if self.on_click is not None:
            self.on_click()

    def destroy(self) -> None:
        for w in (getattr(self, "hit", None), self.win):
            try:
                if w is not None:
                    w.destroy()
            except Exception:
                pass


def build_ui(start_minimal: bool = False):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(tc.UI_TITLE)
    apply_app_icon(root)

    cfg = tc.load_config()
    mode = str(cfg.get("theme", "system"))
    pal_key = resolve_theme(mode)

    style = ttk.Style(root)
    style.theme_use("clam")

    def apply_theme(key: str):
        pal = THEMES[key]
        root.configure(bg=pal["bg"])
        style.configure(".", background=pal["bg"], foreground=pal["fg"], font=FONT)
        style.configure("TFrame", background=pal["bg"])
        style.configure("Card.TFrame", background=pal["card_bg"])
        style.configure("Panel.TFrame", background=pal["panel"])
        style.configure("TLabel", background=pal["bg"], foreground=pal["fg"], font=FONT)
        style.configure("H1.TLabel", background=pal["bg"], foreground=pal["fg"],
                        font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Muted.TLabel", background=pal["bg"], foreground=pal["muted"],
                        font=("Microsoft YaHei UI", 9))
        style.configure("Card.TLabel", background=pal["card_bg"], foreground=pal["fg"])
        style.configure("CardTitle.TLabel", background=pal["card_bg"], foreground=pal["fg"],
                        font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("CardSub.TLabel", background=pal["card_bg"], foreground=pal["muted"],
                        font=("Microsoft YaHei UI", 9))
        style.configure("CardBig.TLabel", background=pal["card_bg"], foreground=pal["accent"],
                        font=("Microsoft YaHei UI", 40, "bold"))
        style.configure("Pct.TLabel", background=pal["card_bg"], foreground=pal["fg"],
                        font=("Segoe UI", 11, "bold"))
        style.configure("TButton", background=pal["panel2"], foreground=pal["fg"],
                        borderwidth=0, focusthickness=0, padding=(14, 7), font=FONT)
        style.map("TButton", background=[("active", pal["active"]),
                                         ("disabled", pal["panel2"])],
                  foreground=[("disabled", pal["disabled_fg"])])
        style.configure("Accent.TButton", background=pal["accent"],
                        foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", pal["accent"])])
        style.configure("Head.TButton", background=pal["panel2"],
                        foreground=pal["fg"], borderwidth=0, focusthickness=0,
                        padding=(11, 6), font=("Microsoft YaHei UI", 9))
        style.map("Head.TButton",
                  background=[("active", pal["active"]),
                              ("disabled", pal["panel2"])],
                  foreground=[("disabled", pal["disabled_fg"])])
        style.configure("HeadAccent.TButton", background=pal["accent"],
                        foreground="#ffffff", borderwidth=0, focusthickness=0,
                        padding=(11, 6), font=("Microsoft YaHei UI", 9))
        style.map("HeadAccent.TButton", background=[("active", pal["accent"])])
        style.configure("TCheckbutton", background=pal["panel"], foreground=pal["fg"],
                        font=FONT)
        style.configure("TCombobox", fieldbackground=pal["panel2"],
                        background=pal["panel2"], foreground=pal["fg"])
        style.configure("TSeparator", background=pal["panel2"])
        return pal

    pal = apply_theme(pal_key)

    root.geometry("760x600")
    root.minsize(620, 520)

    state = {"busy": False, "auto": True, "interval": 60, "after_id": None,
             "starting": False, "started_at": 0.0, "notice": "",
             "notice_until": 0.0, "theme_key": pal_key, "readings": None,
             "pal": pal, "minimal": False, "minis": [], "mini_anchor": None,
             "settings_win": None, "vframe": None, "vlast": None,
             "vphoto": None, "vlast_glass": 0.0, "wall_on": False,
             "low_flag": {}}
    # Tkinter 不是线程安全的：后台线程只往队列里放结果，主线程统一取出来处理
    results: "queue.Queue[tuple]" = queue.Queue()

    state["interval"] = int(cfg["interval"])

    # ---- 背景（图片 / 视频）----
    wp.set_window_size(760, 600)
    wallpaper = wp.Wallpaper()
    try:
        wallpaper.dim = float(cfg.get("bg_dim", 0.45))
        wallpaper.blur = float(cfg.get("bg_blur", 6.0))
    except Exception:
        wallpaper.dim, wallpaper.blur = 0.45, 6.0
    bg_ok = False
    if cfg.get("bg_path"):
        if not wp.has_pil():
            state["notice"] = "背景功能需要 Pillow（pip install pillow），已回到默认底色"
            state["notice_until"] = time.time() + 12
        else:
            bg_ok = wallpaper.set_source(cfg["bg_path"])
            if not bg_ok:
                state["notice"] = "背景文件找不到了，已回到默认底色"
                state["notice_until"] = time.time() + 8

    # ---- 整窗一个 Canvas 当舞台：背景、标题、页脚都画在上面 ----
    stage = tk.Canvas(root, highlightthickness=0, bd=0, bg=pal["bg"])
    stage.place(x=0, y=0, relwidth=1, relheight=1)

    i_wall = stage.create_image(0, 0, anchor="nw", state="hidden")
    # 系统标题栏已经带名字和图标,窗口内不再重复标题——顶栏只留按钮行
    # 背景是亮图时，页脚垫一层 1px 深色描影，压什么图都读得清
    i_foot_sh = stage.create_text(21, 1, text="正在读取…", anchor="w",
                                  fill="#10131a", state="hidden",
                                  font=("Microsoft YaHei UI", 9))
    t_foot = stage.create_text(20, 0, text="正在读取…", anchor="w",
                               fill=pal["muted"],
                               font=("Microsoft YaHei UI", 9))
    t_empty = stage.create_text(0, 0, text="", anchor="center",
                                fill=pal["muted"], state="hidden",
                                font=("Microsoft YaHei UI", 10),
                                justify="center")

    v_interval = tk.StringVar(
        value=dict((s, l) for s, l in tc.INTERVAL_CHOICES).get(state["interval"], "1 分钟"))
    v_theme = tk.StringVar(value=THEME_LABELS.get(mode, "跟随系统"))
    v_mini_size = tk.StringVar(value=_mini_label(float(cfg.get("mini_scale") or 1.0)))
    v_boot = tk.BooleanVar(value=tc.is_autostart_enabled())
    v_bg_dim = tk.DoubleVar(value=float(cfg.get("bg_dim", 0.45)) * 100)
    v_bg_blur = tk.DoubleVar(value=float(cfg.get("bg_blur", 6.0)))

    # ---- 顶部按钮：设置 / 极简模式 / 立即刷新（极简是常用切换，留在主页一键直达）----
    btn = ttk.Button(root, text="立即刷新", style="HeadAccent.TButton")
    btn_mini = ttk.Button(root, text="极简模式", style="Head.TButton",
                          command=lambda: on_minimal())
    btn_set = ttk.Button(root, text="设置", style="Head.TButton",
                         command=lambda: open_settings())
    # 从右往左摆：列表里第一个 → 最右边（立即刷新）
    head_btns = [btn, btn_mini, btn_set]
    for b in head_btns:
        b.place_forget()

    cards: list = []

    def _sync_shadows():
        """背景真显示出来了才开描影；纯色底色下藏着。"""
        st = "normal" if state.get("wall_on") else "hidden"
        stage.itemconfigure(i_foot_sh, state=st)

    def set_foot(text: str) -> None:
        stage.itemconfigure(t_foot, text=text)
        stage.itemconfigure(i_foot_sh, text=text)

    def foot_text() -> str:
        readings = state.get("readings")
        if readings is None:            # 还没读到过：0 个设备也要显示统计行
            return "正在读取…"
        ok = sum(1 for r in readings if r.ok)
        total = len(readings)
        return (f"更新于 {time.strftime('%H:%M:%S')}   已添加 {total} 个设备，"
                f"成功读取 {ok}/{total}"
                + ("" if state["auto"] else "   （自动刷新已关闭）"))

    def _draw_wall():
        """按当前窗口大小重画背景（图片会重新裁剪/模糊）。"""
        w = max(200, root.winfo_width())
        h = max(200, root.winfo_height())
        wp.set_window_size(w, h)
        if wallpaper.kind == "video":
            return                      # 视频由 pump 每帧贴图
        if wallpaper.kind != "image":
            state["wall_on"] = False
            _sync_shadows()
            stage.itemconfigure(i_wall, state="hidden")
            return
        try:
            im = wallpaper.image(w, h)
        except Exception:
            im = None
        if im is None:
            state["wall_on"] = False
            _sync_shadows()
            stage.itemconfigure(i_wall, state="hidden")
            return
        state["wall_photo"] = wp.to_photo(root, im)
        stage.itemconfigure(i_wall, image=state["wall_photo"], state="normal")
        state["wall_on"] = True
        _sync_shadows()

    def _refresh_card_bgs():
        for card in cards:
            try:
                card.set_origin(card._origin[0], card._origin[1])
                card.refresh_bg()
            except Exception:
                pass

    def _glass(x, y, w, h):
        """卡片要的毛玻璃：裁一块背景 → 混一层主题色白霜底 → 加描边。

        只裁背景不混色的话，玻璃和身后的背景颜色几乎一样，
        卡片看起来就像「没有背景」——所以必须往主题色上混一层。
        """
        if wallpaper.kind not in ("image", "video"):
            return None
        pal = state["pal"]
        dark = state.get("theme_key") == "dark"
        try:
            return wallpaper.glass(
                root, x, y, w, h, radius=CARD_RADIUS,
                tint=pal["card_bg"], tint_alpha=0.62,
                border=True,             # 双圈 rim：亮暗背景上轮廓都可见
                extra_dark=0.10 if dark else 0.0)
        except Exception:
            return None

    # ---- 极简模式 ----
    def _mini_scale() -> float:
        try:
            return float(cfg.get("mini_scale") or 1.0)
        except Exception:
            return 1.0

    def _close_minis():
        for m in state.get("minis") or []:
            m.destroy()
        state["minis"] = []

    def _mini_dock_box():
        mets = _mini_metrics(_mini_scale())
        n = len(state.get("minis") or [])
        return mets, n * mets["h"] + max(0, n - 1) * mets["gap"]

    def _mini_apply_layout(save: bool = False) -> None:
        mets, dock_h = _mini_dock_box()
        left, top, right, bottom = _work_area()
        anchor = state.get("mini_anchor")
        if not anchor:
            anchor = [right - mets["w"] - MINI_MARGIN, top + MINI_MARGIN]
        ax, ay = int(anchor[0]), int(anchor[1])
        ax = max(left + 2, min(ax, right - mets["w"] - 2))
        ay = max(top + 2, min(ay, bottom - dock_h - 2))
        state["mini_anchor"] = [ax, ay]
        for i, m in enumerate(state.get("minis") or []):
            m.move_to(ax, ay + i * (mets["h"] + mets["gap"]))
        if save:
            cfg["mini_pos"] = [ax, ay]
            tc.save_config(cfg)

    def _mini_drag(dx: int, dy: int) -> None:
        anchor = state.get("mini_anchor") or [0, 0]
        state["mini_anchor"] = [anchor[0] + dx, anchor[1] + dy]
        _mini_apply_layout(save=False)

    def _mini_snap_save() -> None:
        mets, dock_h = _mini_dock_box()
        left, top, right, bottom = _work_area()
        anchor = state.get("mini_anchor")
        if not anchor:
            return
        ax, ay = anchor
        if ax - left < MINI_SNAP:
            ax = left + MINI_MARGIN
        if right - (ax + mets["w"]) < MINI_SNAP:
            ax = right - mets["w"] - MINI_MARGIN
        if ay - top < MINI_SNAP:
            ay = top + MINI_MARGIN
        if bottom - (ay + dock_h) < MINI_SNAP:
            ay = bottom - dock_h - MINI_MARGIN
        state["mini_anchor"] = [ax, ay]
        _mini_apply_layout(save=True)

    def _spawn_minis(readings, keep_anchor: bool = False) -> None:
        _close_minis()
        mets, _dock_h = _mini_dock_box()
        pos = None if keep_anchor else cfg.get("mini_pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                state["mini_anchor"] = [int(pos[0]), int(pos[1])]
            except Exception:
                state["mini_anchor"] = None
        else:
            state["mini_anchor"] = None
        left, top, right, bottom = _work_area()
        anchor = state.get("mini_anchor") or [
            right - mets["w"] - MINI_MARGIN, top + MINI_MARGIN]
        ax, ay = int(anchor[0]), int(anchor[1])
        scale = _mini_scale()
        for i, r in enumerate(readings):
            try:
                m = MiniCard(root, r, state["pal"], ax,
                             ay + i * (mets["h"] + mets["gap"]),
                             scale=scale, on_click=exit_minimal,
                             on_drag=_mini_drag,
                             on_drag_end=_mini_snap_save)
            except Exception:
                traceback.print_exc()
                continue
            state["minis"].append(m)
        _mini_apply_layout(save=False)

    def exit_minimal():
        if not state.get("minimal"):
            return
        _mini_snap_save()
        state["minimal"] = False
        _close_minis()
        show_gui()

    def enter_minimal():
        readings = state.get("readings") or []
        if not readings:
            notify("没有可显示的设备：先去「设置」→「设备」里添加")
            return
        state["minimal"] = True
        root.withdraw()
        wallpaper.pause_video(True)     # 主窗收起了，别再解码视频
        _spawn_minis(readings)

    def on_minimal():
        close_settings()           # 进极简前把设置窗口收掉，别让它飘在桌面上
        if state.get("minimal"):
            exit_minimal()
        else:
            enter_minimal()

    def on_mini_scale(_event=None):
        scale = MINI_SCALE_OF.get(v_mini_size.get(), 1.0)
        old_w = _mini_metrics(_mini_scale())["w"]
        new_w = _mini_metrics(scale)["w"]
        cfg["mini_scale"] = scale
        tc.save_config(cfg)
        if state.get("minimal"):
            anchor = state.get("mini_anchor")
            _left, _top, right, _bottom = _work_area()
            if anchor and right - (anchor[0] + old_w) < 60:
                state["mini_anchor"] = [anchor[0] + old_w - new_w, anchor[1]]
            _spawn_minis(state.get("readings") or [], keep_anchor=True)

    def reset_mini_pos():
        cfg["mini_pos"] = None
        tc.save_config(cfg)
        state["mini_anchor"] = None
        if state.get("minimal"):
            _mini_apply_layout(save=True)
        notify("极简浮窗位置已复位（回到右上角）")

    # ---- 背景设置 ----
    def _stop_video():
        wallpaper.stop_video()
        state["vphoto"] = None
        state["vframe"] = None
        state["vlast"] = None

    def _start_video():
        _stop_video()
        if wallpaper.kind != "video":
            return
        if not wp.ffmpeg_path():
            notify("没有找到 ffmpeg，视频背景放不了（放个图片同样好看）", 10)
            return

        def on_frame(im):
            # 在解码线程里顺手压暗，主线程只负责贴图
            try:
                from PIL import ImageEnhance
                im = ImageEnhance.Brightness(im).enhance(
                    max(0.15, 1.0 - wallpaper.dim))
            except Exception:
                pass
            state["vlast"] = im
            state["vframe"] = im

        # 解码尺寸上限由 wallpaper 管；每帧输出会自动对齐当前窗口大小
        wallpaper.start_video(on_frame, wp.window_size)

    def sync_settings_bg():
        """把背景相关的当前值刷到设置窗口（没开 / 不在外观页就什么都不做）。"""
        win = state.get("settings_win")
        if win is None:
            return
        try:
            if not win.winfo_exists():
                return
            lab_bg = getattr(win, "_lab_bg", None)
            if lab_bg is None:
                return                      # 当前不在「外观」页
            if cfg.get("bg_path"):
                lab_bg.configure(text="当前：" + wp.describe(cfg["bg_path"]))
            else:
                lab_bg.configure(
                    text="当前：默认底色（可以放一张图片或一段 MP4）")
            getattr(win, "_lab_dim").configure(
                text=f"{int(round(wallpaper.dim * 100))}%")
            getattr(win, "_lab_blur").configure(text=f"{wallpaper.blur:.0f}")
        except Exception:
            pass

    def reload_wallpaper() -> None:
        """配置变了：重新加载背景（图片重画 / 视频重开解码）。

        视觉清理（隐藏壁纸 + 卡片回主题面板）放在最前并且分步兜底——
        后面任何一步出异常都不能让旧背景残留在界面上（exe 没有控制台，
        Tk 回调的异常会被静默吞掉，之前「清除不干净」就是这来的）。
        """
        def _safe(fn, label):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                notify(f"背景操作在 {label} 出错：{traceback.format_exc(limit=1)}"
                       f"把这段发给我", 15)

        _safe(lambda: wallpaper.clear(), "停止旧背景")
        state["wall_on"] = False
        stage.itemconfigure(i_wall, state="hidden")
        stage.itemconfigure(i_wall, image="")

        def _refresh():
            for card in cards:               # 不走防抖，直接强制重画
                try:
                    card._bg_job = None
                    card.refresh_bg()
                except Exception:
                    traceback.print_exc()
        _safe(_refresh, "卡片恢复面板")

        if cfg.get("bg_path") and not wp.has_pil():
            notify("背景功能需要 Pillow（pip install pillow），已回到默认底色", 10)
            sync_settings_bg()
            return
        if cfg.get("bg_path"):
            try:
                ok = wallpaper.set_source(cfg["bg_path"])
                if not ok:
                    notify("这个文件放不了背景（格式不支持或文件坏了）")
            except Exception:
                traceback.print_exc()
                notify(f"背景文件读取失败：{traceback.format_exc(limit=1)}", 15)
        _safe(_draw_wall, "重画背景")
        if wallpaper.kind == "video":
            _safe(_start_video, "启动视频解码")
        _safe(_refresh, "卡片玻璃重画")
        sync_settings_bg()

    def choose_bg():
        from tkinter import filedialog
        if not wp.has_pil():
            notify("背景功能需要 Pillow：pip install pillow", 10)
            return
        types = [("图片或视频", "*.png *.jpg *.jpeg *.webp *.bmp *.gif "
                              "*.mp4 *.mov *.webm *.mkv *.avi *.m4v"),
                 ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("所有文件", "*.*")]
        path = filedialog.askopenfilename(parent=root, title="选一张背景图或视频",
                                          filetypes=types)
        if not path:
            return
        kind = wp.kind_of(path)
        if not kind:
            notify("不支持这种格式：图片用 png/jpg/webp，视频用 mp4/mov/webm")
            return
        if kind == "video" and not wp.ffmpeg_path():
            notify("视频背景需要 ffmpeg（放个图片也一样好看）", 10)
            return
        cfg["bg_path"] = path
        tc.save_config(cfg)
        reload_wallpaper()
        notify(f"背景已换成 {wp.describe(path)}", 8)

    def clear_bg():
        cfg["bg_path"] = ""
        tc.save_config(cfg)
        reload_wallpaper()
        notify("背景已清除")

    # 拖滑块的过程里事件非常密，重渲染（整图模糊+每张卡裁玻璃）又贵，
    # 所以拖动时只改数值和标签，停稳 220ms 后才真正重画。
    _look_job = {"id": None}

    def _apply_wall_look():
        _look_job["id"] = None
        cfg["bg_dim"] = round(wallpaper.dim, 2)
        cfg["bg_blur"] = round(wallpaper.blur, 1)
        tc.save_config(cfg)
        wallpaper.clear_cache()
        _draw_wall()
        _refresh_card_bgs()

    def _queue_wall_look():
        if _look_job["id"] is not None:
            try:
                root.after_cancel(_look_job["id"])
            except Exception:
                pass
        _look_job["id"] = root.after(220, _apply_wall_look)

    def set_dim(_v=None):
        wallpaper.dim = max(0.0, min(0.9, float(v_bg_dim.get()) / 100.0))
        sync_settings_bg()
        _queue_wall_look()

    def set_blur(_v=None):
        wallpaper.blur = max(0.0, min(20.0, float(v_bg_blur.get())))
        sync_settings_bg()
        _queue_wall_look()

    # ---- 氛围粒子（花瓣 / 雨滴）：纯 Canvas 绘制，几十个 item 的坐标更新 ----
    AMBIENT_CHOICES = [("none", "无"), ("petals", "花瓣"), ("rain", "雨滴")]
    _AMBIENT_LABEL_OF = dict(AMBIENT_CHOICES)
    _AMBIENT_KIND_OF = {v: k for k, v in AMBIENT_CHOICES}
    v_ambient = tk.StringVar(
        value=_AMBIENT_LABEL_OF.get(str(cfg.get("ambient") or "none"), "无"))

    LOWBAT_CHOICES = [(0, "关"), (10, "10%"), (15, "15%"), (20, "20%"),
                      (30, "30%")]
    _LOWBAT_LABEL_OF = dict(LOWBAT_CHOICES)

    def _lowbat_label() -> str:
        try:
            v = int(cfg.get("low_battery") or 0)
        except Exception:
            v = 20
        return _LOWBAT_LABEL_OF.get(v, f"{v}%")

    v_lowbat = tk.StringVar(value=_lowbat_label())

    PETAL_SHAPE = [(0.00, -1.00), (0.55, -0.35), (0.62, 0.30),
                   (0.22, 0.95), (-0.22, 0.95), (-0.62, 0.30),
                   (-0.55, -0.35)]
    # (填充色, 描边色)：描边让花瓣在浅色/深色背景上都看得清
    PETAL_COLORS = [("#ee9ab8", "#c97a9c"), ("#f2a7c0", "#d1819f"),
                    ("#e98bad", "#c4738f"), ("#f3b8ce", "#d190ab"),
                    ("#ec8fb0", "#c86f92")]
    RAIN_COLORS = ["#a9c1de", "#b9cde4", "#93aed0", "#c4d5ea"]

    def _ambient_kind() -> str:
        return str(cfg.get("ambient") or "none")

    def _ambient_spawn(part: dict, first: bool = False) -> None:
        """生成/重置一个粒子（first=True 时铺满全屏，否则从顶部进来）。"""
        import random
        W = max(320, root.winfo_width())
        H = max(240, root.winfo_height())
        if _ambient_kind() == "petals":
            part["size"] = random.uniform(7.0, 13.0)
            part["x"] = random.uniform(-20, W + 20)
            part["y"] = random.uniform(-H, -12) if not first else random.uniform(0, H)
            part["vy"] = random.uniform(0.6, 1.6)
            part["sway"] = random.uniform(0.8, 2.2)     # 水平摆动幅度
            part["phase"] = random.uniform(0, math.tau)
            part["rot"] = random.uniform(0, math.tau)
            part["vrot"] = random.uniform(-0.09, 0.09)
            part["color"], part["edge"] = random.choice(PETAL_COLORS)
        else:                                          # rain
            part["x"] = random.uniform(-30, W + 30)
            part["y"] = random.uniform(-H, -20) if not first else random.uniform(-H, H)
            part["vy"] = random.uniform(6.0, 13.0)
            part["len"] = random.uniform(13, 24)
            part["vx"] = random.uniform(1.2, 2.4)      # 微风斜落
            part["color"] = random.choice(RAIN_COLORS)
            part["width"] = random.choice((1, 1, 1, 2))

    def _ambient_petal_points(part: dict) -> list:
        """花瓣多边形顶点（旋转 + 随呼吸轻微缩放）。"""
        s = part["size"] * (0.9 + 0.1 * math.sin(part["phase"] * 2))
        c, si = math.cos(part["rot"]), math.sin(part["rot"])
        out = []
        for px, py in PETAL_SHAPE:
            out.extend((part["x"] + (px * c - py * si) * s,
                        part["y"] + (px * si + py * c) * s))
        return out

    ambient = {"parts": [], "running": False}

    def _ambient_build() -> None:
        for p in ambient["parts"]:
            try:
                stage.delete(p["item"])
            except Exception:
                pass
        ambient["parts"] = []
        kind = _ambient_kind()
        if kind == "none":
            return
        import random
        n = 30 if kind == "petals" else 42
        for _ in range(n):
            part: dict = {}
            _ambient_spawn(part, first=True)
            if kind == "petals":
                item = stage.create_polygon(_ambient_petal_points(part),
                                            fill=part["color"],
                                            outline=part["edge"], width=1)
            else:
                item = stage.create_line(part["x"], part["y"],
                                         part["x"] - part["vx"] * 3,
                                         part["y"] - part["len"],
                                         fill=part["color"],
                                         width=part["width"], capstyle="round")
            part["item"] = item
            ambient["parts"].append(part)
        _ambient_lift()

    def _ambient_lift() -> None:
        """粒子垫到背景图上面（粒子在 i_wall 之后创建，天然在上，这里兜底）。"""
        try:
            for p in ambient["parts"]:
                stage.tag_lower(p["item"], i_foot_sh)
        except Exception:
            pass

    def _ambient_tick() -> None:
        kind = _ambient_kind()
        if (kind != "none" and ambient["parts"]
                and not state.get("minimal")
                and root.state() == "normal"):
            W = max(320, root.winfo_width())
            H = max(240, root.winfo_height())
            for part in ambient["parts"]:
                if kind == "petals":
                    part["phase"] += 0.045
                    part["y"] += part["vy"]
                    part["x"] += math.sin(part["phase"]) * part["sway"] * 0.55
                    part["rot"] += part["vrot"]
                    if part["y"] > H + 16:
                        _ambient_spawn(part)
                    stage.coords(part["item"], _ambient_petal_points(part))
                else:
                    part["y"] += part["vy"]
                    part["x"] += part["vx"]
                    if part["y"] > H + 8:
                        _ambient_spawn(part)
                    stage.coords(part["item"],
                                 part["x"], part["y"],
                                 part["x"] - part["vx"] * 3,
                                 part["y"] - part["len"])
        # 帧率：花瓣慢悠悠 22fps，雨丝顺滑 30fps；隐藏时只空转定时器
        delay = 45 if kind == "petals" else 33
        root.after(delay, _ambient_tick)

    def on_ambient(_event=None) -> None:
        cfg["ambient"] = _AMBIENT_KIND_OF.get(v_ambient.get(), "none")
        tc.save_config(cfg)
        _ambient_build()

    def on_lowbat(_event=None) -> None:
        label = v_lowbat.get()
        val = next((k for k, l in LOWBAT_CHOICES if l == label), 0)
        cfg["low_battery"] = val
        tc.save_config(cfg)
        state["low_flag"].clear()          # 换阈值后重新允许提醒
        if val:
            notify(f"低电量提醒已设为 {val}%")
        else:
            notify("低电量提醒已关闭")

    # ---- 设备（管理收进设置窗口的「设备」页）----
    def on_devices_changed():
        rebuild_cards()
        refresh()

    def rebuild_cards():
        for card in cards:
            card.destroy()
        cards.clear()
        specs = devices.load_specs()
        for spec in specs:
            # 玻璃回调常驻：有没有背景由 _glass 自己判断（返回 None 就是主题面板）。
            # 之前按「启动时有没有背景」决定挂不挂，导致运行中换背景的卡片
            # 永远显示纯色面板——就是这个 bug。
            cards.append(DeviceCard(root, spec, state["pal"], glass=_glass))
        if specs:
            stage.itemconfigure(t_empty, state="hidden")
        else:
            stage.itemconfigure(t_empty, state="normal",
                                text="还没有添加设备\n点右上角「设置」→「设备」一键扫描，挑出要显示的设备")
        relayout()
        return specs

    # ---- 布局 ----
    _wall_job = {"id": None}

    def relayout(_event=None):
        W = max(320, root.winfo_width())
        H = max(240, root.winfo_height())

        # 顶部按钮：从右往左排（顶栏只有按钮行，紧凑贴上沿）
        x = W - 20
        for b in head_btns:
            try:
                w = max(64, b.winfo_reqwidth())
            except Exception:
                w = 90
            x -= w
            b.place(x=x, y=16, width=w, height=30)
            x -= 8

        # 卡片：居中排布，自动换行
        top = 58
        if cards:
            cols = max(1, (W - 40 + 16) // (CARD_SIZE + 16))
            cols = min(cols, len(cards))
            rows = (len(cards) + cols - 1) // cols
            total_w = cols * CARD_SIZE + (cols - 1) * 16
            total_h = rows * CARD_SIZE + (rows - 1) * 16
            x0 = (W - total_w) // 2
            # 窗口太矮时整体上移，别把页脚顶掉
            avail = H - top - 44
            y0 = top + max(0, (avail - total_h) // 2)
            for i, card in enumerate(cards):
                cx = x0 + (i % cols) * (CARD_SIZE + 16)
                cy = y0 + (i // cols) * (CARD_SIZE + 16)
                card.frame.place(x=cx, y=cy)
                card.set_origin(cx, cy)
        stage.coords(t_empty, W // 2, H * 0.45)
        stage.coords(t_foot, 20, H - 18)
        stage.coords(i_foot_sh, 21, H - 17)

        if _wall_job["id"]:
            try:
                root.after_cancel(_wall_job["id"])
            except Exception:
                pass
        _wall_job["id"] = root.after(120, lambda: (_draw_wall(), _refresh_card_bgs()))

    # ---- 刷新逻辑 ----
    # ---- 低电量提醒 + 电量历史 ----
    HISTORY_PATH = os.path.join(tc.BASE_DIR, "battery_history.csv")

    def _balloon(title: str, text: str) -> None:
        """借托盘图标弹一条 Windows 系统通知（NIM_MODIFY + NIF_INFO）。"""
        try:
            hwnd = tc.tray_hwnd()
            if not hwnd:
                return
            import tray_app as ta
            nid = ta.NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(ta.NOTIFYICONDATAW)
            nid.hWnd = ctypes.c_void_p(hwnd)
            nid.uID = ta.TRAY_ICONS[0][0]
            nid.uFlags = ta.NIF_INFO
            nid.dwInfoFlags = ta.NIIF_INFO
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            ta.SHELL32.Shell_NotifyIconW(ta.NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def _is_charging(r) -> bool:
        return bool(getattr(r, "charging", False)
                    or "充电" in (getattr(r, "status", "") or ""))

    def _check_low_battery(readings: list) -> None:
        """低于阈值弹通知；带迟滞（恢复到 阈值+5% 或开始充电后才会再次提醒）。"""
        thr = int(cfg.get("low_battery") or 0)
        if thr <= 0:
            state["low_flag"].clear()
            return
        flags = state.setdefault("low_flag", {})
        seen = set()
        for r in readings:
            if not r.ok:
                continue
            seen.add(r.title)
            charging = _is_charging(r)
            if not charging and r.level is not None and r.level <= thr:
                if not flags.get(r.title):
                    flags[r.title] = True
                    _balloon("电量低提醒", f"{r.title} 电量 {r.level}%，记得充电")
                    notify(f"⚠ {r.title} 电量低：{r.level}%（提醒阈值 {thr}%）", 10)
            elif charging or r.level is None or r.level >= thr + 5:
                flags.pop(r.title, None)
        for t in list(flags):
            if t not in seen:
                flags.pop(t, None)

    def _history_append(readings: list) -> None:
        """把一轮读数追加到本地 CSV（电量曲线的数据源，3MB 自动轮转）。"""
        try:
            rows = [f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{r.title},{r.level},"
                    f"{1 if _is_charging(r) else 0},{r.voltage_mv or ''}"
                    for r in readings if r.ok]
            if not rows:
                return
            header = "time,title,level,charging,voltage_mv\n"
            new = not os.path.isfile(HISTORY_PATH)
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                if new:
                    f.write(header)
                f.write("\n".join(rows) + "\n")
            if os.path.getsize(HISTORY_PATH) > 3_000_000:
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                    f.write(header + "".join(lines[-40000:]))
        except Exception:
            pass

    def apply(readings: list):
        nonlocal start_minimal
        if start_minimal and readings:
            start_minimal = False
            state["readings"] = readings
            root.after(120, enter_minimal)
        if len(readings) != len(cards):
            rebuild_cards()
        state["readings"] = readings
        for card, r in zip(cards, readings):
            card.update(r, state["pal"])
        if state.get("minimal"):
            if len(readings) != len(state.get("minis") or []):
                _spawn_minis(readings)
            else:
                for m, r in zip(state["minis"], readings):
                    m.update(r, state["pal"])
        ok = sum(1 for r in readings if r.ok)
        total = len(readings)
        _check_low_battery(readings)
        _history_append(readings)
        if not (state.get("notice") and time.time() < state.get("notice_until", 0)):
            set_foot(foot_text())
        state["busy"] = False
        btn.configure(state="normal")

    def worker():
        try:
            readings = devices.read_all()
        except Exception:
            traceback.print_exc()
            readings = []
        results.put(("readings", readings))

    def refresh():
        if state["busy"]:
            return
        state["busy"] = True
        btn.configure(state="disabled")
        if not (state.get("notice") and time.time() < state.get("notice_until", 0)):
            set_foot("正在读取…")
        threading.Thread(target=worker, daemon=True).start()

    def _reschedule():
        if state["after_id"]:
            root.after_cancel(state["after_id"])
            state["after_id"] = None
        if state["auto"]:
            state["after_id"] = root.after(state["interval"] * 1000, _tick)

    def _tick():
        refresh()
        _reschedule()

    btn.configure(command=refresh)

    def on_auto():
        state["auto"] = not state["auto"]
        if state.get("settings_win") is not None:
            try:
                state["settings_win"]._btn_auto.configure(
                    text="自动刷新：开" if state["auto"] else "自动刷新：关")
            except Exception:
                pass
        _reschedule()

    # ---- 设置窗口 ----
    def close_settings():
        """关掉设置窗口（没开就什么都不做）。"""
        win = state.get("settings_win")
        state["settings_win"] = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def refresh_tray_state():
        if state.get("starting"):
            return
        win = state.get("settings_win")
        if win is None:
            return
        try:
            if not win.winfo_exists():
                state["settings_win"] = None
                return
        except Exception:
            state["settings_win"] = None
            return
        lab_tray = getattr(win, "_lab_tray", None)
        btn_tray = getattr(win, "_btn_tray", None)
        if lab_tray is None or btn_tray is None:
            return                      # 当前不在「启动与托盘」页
        try:
            if state.get("notice") and time.time() < state.get("notice_until", 0):
                lab_tray.configure(text=state["notice"])
                btn_tray.configure(
                    state="normal",
                    text="退出托盘程序" if tc.is_tray_running() else "启动托盘程序")
                return
            state["notice"] = ""
            running = tc.is_tray_running()
            btn_tray.configure(state="normal",
                               text="退出托盘程序" if running else "启动托盘程序")
            lab_tray.configure(
                text="托盘运行中：右键托盘图标即可打开本窗口"
                     if running else "托盘未运行：不会后台自动刷新")
        except Exception:
            pass

    def notify(text: str, seconds: float = 8.0) -> None:
        state["notice"] = text
        state["notice_until"] = time.time() + seconds
        set_foot(text)
        refresh_tray_state()
        root.after(int(seconds * 1000) + 80, _restore_foot)

    def _restore_foot():
        """通知到期后把页脚恢复成常规状态文字（不然最后一条通知会一直挂着）。"""
        if time.time() < state.get("notice_until", 0):
            return                      # 有更新的通知在展示，不动
        state["notice"] = ""
        set_foot(foot_text())
        refresh_tray_state()

    def on_interval(_event=None):
        sec = dict((label, s) for s, label in tc.INTERVAL_CHOICES)[v_interval.get()]
        cfg["interval"] = sec
        tc.save_config(cfg)
        state["interval"] = sec
        tc.post_to_tray(tc.MSG_RELOAD)
        _reschedule()
        refresh()

    def on_theme(_event=None):
        key = THEME_KEYS.get(v_theme.get(), "system")
        cfg["theme"] = key
        tc.save_config(cfg)
        _apply_theme(key)

    def _apply_theme(key: str):
        mode_key = resolve_theme(key)
        pal = apply_theme(mode_key)
        state["theme_key"] = mode_key
        state["pal"] = pal
        stage.configure(bg=pal["bg"])
        stage.itemconfigure(t_foot, fill=pal["muted"])
        stage.itemconfigure(t_empty, fill=pal["muted"])
        for card in cards:
            card.restyle(pal)
        _draw_wall()
        _refresh_card_bgs()

    def on_boot():
        want = bool(v_boot.get())
        ok = tc.set_autostart(want)
        cfg["autostart"] = want
        tc.save_config(cfg)
        tc.post_to_tray(tc.MSG_RELOAD)
        if want and not ok:
            notify("开机自启设置失败：无法创建启动文件夹快捷方式")
        elif want:
            notify("已加入开机自启（任务管理器 →「启动」标签里能看到 电量托盘）", 12)
        else:
            notify("已取消开机自启")

    def tray_done(ok: bool):
        state["starting"] = False
        refresh_tray_state()
        if not ok:
            notify("启动失败：可双击桌面「电量托盘」再试", 12)

    def on_links():
        win = state.get("settings_win")
        try:
            win._btn_link.configure(state="disabled", text="正在创建…")
        except Exception:
            pass

        def work():
            try:
                r = tc.install_desktop_links()
            except Exception as e:
                traceback.print_exc()
                r = {"folder": "", "links": {}, "errors": [str(e)]}
            results.put(("links", r))

        threading.Thread(target=work, daemon=True).start()

    def links_done(r: dict):
        import tkinter.messagebox as mb
        win = state.get("settings_win")
        try:
            win._btn_link.configure(state="normal", text="重建桌面快捷方式")
        except Exception:
            pass
        bits = []
        if r.get("shortcut"):
            bits.append("桌面快捷方式已就绪（双击即可启动）")
        for name in (r.get("removed") or []):
            bits.append(f"已清理旧链接 {name}")
        if bits:
            notify("桌面入口已更新：" + "；".join(bits), 12)
        else:
            notify("桌面快捷方式创建失败", 12)
        if r.get("errors"):
            mb.showwarning("桌面快捷方式", "\n".join(r["errors"]))

    def on_tray_toggle():
        if tc.is_tray_running():
            tc.post_to_tray(tc.MSG_QUIT)
            for delay in (600, 1500, 3000):
                root.after(delay, refresh_tray_state)
            return

        state["starting"] = True
        state["started_at"] = time.time()
        win = state.get("settings_win")
        try:
            win._btn_tray.configure(state="disabled", text="正在启动…")
            win._lab_tray.configure(text="正在启动托盘程序…")
        except Exception:
            pass

        def work():
            try:
                ok = tc.start_tray_app()
            except Exception:
                traceback.print_exc()
                ok = False
            results.put(("tray", ok))

        threading.Thread(target=work, daemon=True).start()

    def open_settings(initial_page: str = "外观"):
        """设置窗口：左侧分类导航 + 右侧内容页（外观 / 设备 / 刷新 / 启动与托盘 / 关于）。"""
        win = state.get("settings_win")
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift()
                    win.focus_force()
                    return
            except Exception:
                pass
        pal = state["pal"]
        win = tk.Toplevel(root)
        win.title("设置")
        win.configure(bg=pal["bg"])
        win.transient(root)
        win.resizable(False, False)
        w, h = 660, 540
        sx = root.winfo_rootx() + max(0, (root.winfo_width() - w) // 2)
        sy = max(40, root.winfo_rooty() + 40)
        win.geometry(f"{w}x{h}+{sx}+{sy}")
        try:
            win.iconbitmap(default=root.iconbitmap())
        except Exception:
            pass

        def close():
            close_settings()
        win.protocol("WM_DELETE_WINDOW", close)

        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("Set.TFrame", background=pal["bg"])
        style.configure("SetPage.TFrame", background=pal["bg"])
        style.configure("SetNav.TFrame", background=pal["panel"])
        style.configure("SetCard.TFrame", background=pal["panel"])
        style.configure("SetH1.TLabel", background=pal["bg"], foreground=pal["fg"],
                        font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("SetLabel.TLabel", background=pal["panel"],
                        foreground=pal["fg"], font=("Microsoft YaHei UI", 10))
        style.configure("SetSub.TLabel", background=pal["panel"],
                        foreground=pal["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("SetSubBg.TLabel", background=pal["bg"],
                        foreground=pal["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Nav.TButton", background=pal["panel"], foreground=pal["muted"],
                        borderwidth=0, focusthickness=0, padding=(16, 11),
                        font=("Microsoft YaHei UI", 10))
        style.map("Nav.TButton", background=[("active", pal["panel2"])])
        style.configure("NavActive.TButton", background=pal["panel2"],
                        foreground=pal["accent"], borderwidth=0, focusthickness=0,
                        padding=(16, 11), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("NavActive.TButton", background=[("active", pal["panel2"])])

        # ---- 左侧导航 + 右侧内容 ----
        nav = ttk.Frame(win, style="SetNav.TFrame", padding=(0, 12, 0, 12))
        nav.pack(side="left", fill="y")
        content = ttk.Frame(win, style="Set.TFrame", padding=(18, 14, 18, 14))
        content.pack(side="left", fill="both", expand=True)

        pages = ["外观", "设备", "趋势", "刷新", "启动与托盘", "关于"]
        nav_btns: dict = {}
        page_host: dict = {}

        def show_page(name: str):
            # 换页：销毁旧页面重建新的（控件都是轻量的，没必要常驻）
            host = content.winfo_children()
            for old in host:
                old.destroy()
            for btn_name, b in nav_btns.items():
                b.configure(style="NavActive.TButton" if btn_name == name
                            else "Nav.TButton")
            build_page[name]()
            sync_settings_bg()      # 外观页的背景/滑块标签回填当前值
            refresh_tray_state()    # 启动页的托盘状态回填

        def section(parent, text):
            ttk.Label(parent, text=text,
                      style="SetH1.TLabel").pack(anchor="w", pady=(0, 8))
            box = ttk.Frame(parent, style="SetCard.TFrame", padding=(14, 12))
            box.pack(fill="x", pady=(0, 16))
            return box

        # ---- 各页面 -----------------------------------------------------
        def page_look():
            page = ttk.Frame(content, style="SetPage.TFrame")
            page.pack(fill="both", expand=True)

            box = section(page, "主题与极简")
            row = ttk.Frame(box, style="SetCard.TFrame")
            row.pack(fill="x")
            ttk.Label(row, text="主题", style="SetSub.TLabel").pack(side="left")
            cb_theme = ttk.Combobox(row, textvariable=v_theme, width=10,
                                    state="readonly",
                                    values=[label for _, label in THEME_CHOICES])
            cb_theme.pack(side="left", padx=(8, 22))
            cb_theme.bind("<<ComboboxSelected>>", on_theme)
            ttk.Label(row, text="极简大小", style="SetSub.TLabel").pack(side="left")
            cb_mini = ttk.Combobox(row, textvariable=v_mini_size, width=5,
                                   state="readonly",
                                   values=[label for label, _ in MINI_SIZES])
            cb_mini.pack(side="left", padx=(8, 8))
            cb_mini.bind("<<ComboboxSelected>>", on_mini_scale)
            ttk.Button(row, text="浮窗复位",
                       command=lambda: reset_mini_pos()).pack(side="left")

            row_m = ttk.Frame(box, style="SetCard.TFrame")
            row_m.pack(fill="x", pady=(12, 0))
            lab_m = ttk.Label(row_m, text="极简模式：只留两块毛玻璃小浮窗",
                              style="SetSub.TLabel")
            lab_m.pack(side="left")
            ttk.Button(row_m, text="进入极简模式", style="Accent.TButton",
                       command=on_minimal).pack(side="right")

            box2 = section(page, "背景（图片 / 视频）")
            row2 = ttk.Frame(box2, style="SetCard.TFrame")
            row2.pack(fill="x")
            ttk.Button(row2, text="选择图片 / 视频", command=choose_bg).pack(side="left")
            ttk.Button(row2, text="清除", command=clear_bg).pack(side="left",
                                                                 padx=(8, 0))
            lab_bg = ttk.Label(box2, text="", style="SetSub.TLabel",
                               wraplength=430, justify="left")
            lab_bg.pack(anchor="w", pady=(8, 0))

            row3 = ttk.Frame(box2, style="SetCard.TFrame")
            row3.pack(fill="x", pady=(10, 0))
            ttk.Label(row3, text="压暗", style="SetSub.TLabel").pack(side="left")
            sc_dim = ttk.Scale(row3, from_=0, to=90, variable=v_bg_dim,
                               command=lambda _v: set_dim(), length=150)
            sc_dim.pack(side="left", padx=(8, 4))
            lab_dim = ttk.Label(row3, text="", style="SetSub.TLabel")
            lab_dim.pack(side="left", padx=(0, 18))
            ttk.Label(row3, text="模糊", style="SetSub.TLabel").pack(side="left")
            sc_blur = ttk.Scale(row3, from_=0, to=18, variable=v_bg_blur,
                                command=lambda _v: set_blur(), length=130)
            sc_blur.pack(side="left", padx=(8, 4))
            lab_blur = ttk.Label(row3, text="", style="SetSub.TLabel")
            lab_blur.pack(side="left")

            ttk.Label(page, text="视频背景需要 ffmpeg；窗口收进托盘时自动暂停解码",
                      style="SetSubBg.TLabel").pack(anchor="w")

            box3 = section(page, "氛围")
            row_a = ttk.Frame(box3, style="SetCard.TFrame")
            row_a.pack(fill="x")
            ttk.Label(row_a, text="粒子效果", style="SetSub.TLabel").pack(side="left")
            cb_amb = ttk.Combobox(row_a, textvariable=v_ambient, width=8,
                                  state="readonly",
                                  values=[label for _, label in AMBIENT_CHOICES])
            cb_amb.pack(side="left", padx=(8, 16))
            cb_amb.bind("<<ComboboxSelected>>", on_ambient)
            ttk.Label(row_a, text="花瓣 / 雨滴飘落（极低占用，窗口隐藏时自动暂停）",
                      style="SetSub.TLabel").pack(side="left")

            win._lab_bg = lab_bg
            win._lab_dim = lab_dim
            win._lab_blur = lab_blur

        def page_devices():
            build_device_page(content, pal, on_devices_changed)[1]()

        def page_trend():
            """电量趋势页：读本地 CSV 画曲线（多设备叠加，充电段绿色）。"""
            import datetime
            page = ttk.Frame(content, style="SetPage.TFrame")
            page.pack(fill="both", expand=True)
            box = section(page, "电量趋势")
            range_state = {"hours": 24}

            bar = ttk.Frame(box, style="SetCard.TFrame")
            bar.pack(fill="x")
            ttk.Label(bar, text="范围", style="SetSub.TLabel").pack(side="left")
            range_btns: dict = {}

            legend = ttk.Frame(box, style="SetCard.TFrame")
            legend.pack(fill="x", pady=(8, 0))
            chart = tk.Canvas(box, bg=pal["bg"], highlightthickness=0, bd=0,
                              height=300)
            chart.pack(fill="x", pady=(8, 0))
            ttk.Label(page, text="数据自动记录在本地 battery_history.csv（约 3MB 自动轮转）；"
                                 "绿色段 = 充电中",
                      style="SetSubBg.TLabel").pack(anchor="w")

            def set_range(hours: int) -> None:
                range_state["hours"] = hours
                for hh, b in range_btns.items():
                    b.configure(style="Accent.TButton" if hh == hours
                                else "TButton")
                draw()

            def draw() -> None:
                for w in legend.winfo_children():
                    w.destroy()
                chart.delete("all")
                w = max(360, chart.winfo_width() or 560)
                h = 300
                ml, mr, mt, mb = 40, 14, 12, 26
                pw, ph = w - ml - mr, h - mt - mb
                # 坐标轴
                chart.create_line(ml, mt, ml, mt + ph, fill=pal["panel2"])
                chart.create_line(ml, mt + ph, ml + pw, mt + ph,
                                  fill=pal["panel2"])
                for v in (0, 25, 50, 75, 100):
                    y = mt + ph - ph * v / 100.0
                    chart.create_text(ml - 6, y, text=f"{v}", anchor="e",
                                      fill=pal["muted"], font=("Segoe UI", 8))
                now = time.time()
                window = range_state["hours"] * 3600.0
                # 读历史
                series: dict = {}
                try:
                    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                        next(f, None)
                        for ln in f:
                            parts = ln.strip().split(",")
                            if len(parts) < 4:
                                continue
                            try:
                                t = time.mktime(time.strptime(
                                    parts[0], "%Y-%m-%dT%H:%M:%S"))
                                lv = int(parts[2])
                                ch = parts[3] == "1"
                            except Exception:
                                continue
                            if now - t > window:
                                continue
                            series.setdefault(parts[1], []).append((t, lv, ch))
                except OSError:
                    pass
                if not series:
                    chart.create_text(ml + pw / 2, mt + ph / 2,
                                      text="还没有历史数据 —— 用一会儿再来看",
                                      fill=pal["muted"],
                                      font=("Microsoft YaHei UI", 10))
                    return
                palette = ["#4f8cff", "#f5a623", "#b18cff", "#ff7eb6",
                           "#3ddc84", "#54c7ec"]
                for di, title in enumerate(sorted(series)):
                    pts = sorted(series[title])
                    color = palette[di % len(palette)]
                    # 降采样：点数超过画布宽度就按像素分桶取平均
                    if len(pts) > pw:
                        buckets: dict = {}
                        t0 = now - window
                        for t, lv, ch in pts:
                            k = int((t - t0) / window * pw)
                            buckets.setdefault(k, []).append((lv, ch))
                        pts = [(now - window + (k + 0.5) / pw * window,
                                sum(v[0] for v in vals) // len(vals),
                                vals[-1][1])
                               for k, vals in sorted(buckets.items())]
                    xy = []
                    for t, lv, ch in pts:
                        x = ml + (t - (now - window)) / window * pw
                        y = mt + ph - ph * max(0, min(100, lv)) / 100.0
                        xy.append((x, y, ch))
                    for i in range(1, len(xy)):
                        x0, y0, _ = xy[i - 1]
                        x1, y1, c1 = xy[i]
                        chart.create_line(x0, y0, x1, y1, width=2,
                                          fill="#3ddc84" if c1 else color)
                    if xy:
                        r = 2.5
                        xe, ye, _ = xy[-1]
                        chart.create_oval(xe - r, ye - r, xe + r, ye + r,
                                          fill=color, outline="")
                    last = pts[-1]
                    ttk.Label(legend, text=f"● {title}  最近 {last[1]}%",
                              style="SetSub.TLabel",
                              foreground=color).pack(side="left", padx=(0, 18))
                # X 轴时间刻度
                for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                    t = now - window * (1.0 - frac)
                    label = (datetime.datetime.fromtimestamp(t).strftime("%m-%d")
                             if window > 86400 else
                             datetime.datetime.fromtimestamp(t).strftime("%H:%M"))
                    chart.create_text(ml + frac * pw, mt + ph + 12, text=label,
                                      fill=pal["muted"], font=("Segoe UI", 8))

            for hh, label in ((24, "24 小时"), (24 * 7, "7 天"),
                              (24 * 30, "30 天")):
                b = ttk.Button(bar, text=label, style="TButton",
                               command=lambda h=hh: set_range(h))
                b.pack(side="left", padx=(8, 0))
                range_btns[hh] = b
            set_range(24)
            page.after(150, draw)          # 等布局拿到真实宽度再画一次

        def page_refresh():
            page = ttk.Frame(content, style="SetPage.TFrame")
            page.pack(fill="both", expand=True)
            box = section(page, "自动刷新")
            row4 = ttk.Frame(box, style="SetCard.TFrame")
            row4.pack(fill="x")
            ttk.Label(row4, text="刷新间隔", style="SetSub.TLabel").pack(side="left")
            cb_int = ttk.Combobox(row4, textvariable=v_interval, width=8,
                                  state="readonly",
                                  values=[label for _, label in tc.INTERVAL_CHOICES])
            cb_int.pack(side="left", padx=(8, 18))
            cb_int.bind("<<ComboboxSelected>>", on_interval)
            btn_auto = ttk.Button(row4,
                                  text=("自动刷新：开" if state["auto"]
                                        else "自动刷新：关"),
                                  command=lambda: on_auto())
            btn_auto.pack(side="left")
            row_lb = ttk.Frame(box, style="SetCard.TFrame")
            row_lb.pack(fill="x", pady=(10, 0))
            ttk.Label(row_lb, text="低电量提醒", style="SetSub.TLabel").pack(side="left")
            cb_lb = ttk.Combobox(row_lb, textvariable=v_lowbat, width=8,
                                 state="readonly",
                                 values=[label for _, label in LOWBAT_CHOICES])
            cb_lb.pack(side="left", padx=(8, 16))
            cb_lb.bind("<<ComboboxSelected>>", on_lowbat)
            ttk.Label(row_lb, text="低于阈值弹系统通知（一次低电只提醒一次，充电即恢复）",
                      style="SetSub.TLabel").pack(side="left")
            ttk.Label(page, text="窗口关闭后会隐藏到托盘继续刷新；托盘退出才真正结束。",
                      style="SetSubBg.TLabel").pack(anchor="w")
            win._btn_auto = btn_auto

        def page_boot():
            page = ttk.Frame(content, style="SetPage.TFrame")
            page.pack(fill="both", expand=True)
            box = section(page, "开机自启")
            row5 = ttk.Frame(box, style="SetCard.TFrame")
            row5.pack(fill="x")
            ttk.Checkbutton(row5, text="开机自启（启动文件夹快捷方式，"
                                       "任务管理器可见）",
                            variable=v_boot, command=on_boot).pack(side="left")
            box2 = section(page, "桌面与托盘")
            row6 = ttk.Frame(box2, style="SetCard.TFrame")
            row6.pack(fill="x")
            btn_link = ttk.Button(row6, text="重建桌面快捷方式", command=on_links)
            btn_link.pack(side="left")
            row7 = ttk.Frame(box2, style="SetCard.TFrame")
            row7.pack(fill="x", pady=(10, 0))
            btn_tray = ttk.Button(row7, text="退出托盘程序", command=on_tray_toggle)
            btn_tray.pack(side="right")
            lab_tray = ttk.Label(row7, text="", style="SetSub.TLabel")
            lab_tray.pack(side="left")
            win._btn_link = btn_link
            win._btn_tray = btn_tray
            win._lab_tray = lab_tray

        def page_about():
            page = ttk.Frame(content, style="SetPage.TFrame")
            page.pack(fill="both", expand=True)
            section(page, "关于")
            ttk.Label(page, text="2.4G 设备电量（2.4G Device Battery Check）",
                      style="SetH1.TLabel").pack(anchor="w", pady=(0, 10))
            for line in (
                    "实时读取 2.4G 无线外设的电量，任务栏托盘常驻显示。",
                    "核心功能零第三方依赖：纯 Python 标准库 + ctypes 直调系统 DLL。",
                    "不联网、不写日志、不上传任何数据；所有读取都在本机 HID 层完成。",
                    "",
                    "支持：VGN S99（Telink 私有协议）· 罗技 HID++ 2.0",
                    "背景功能可选装 Pillow（exe 版已内置）。"):
                ttk.Label(page, text=line, style="SetSubBg.TLabel").pack(anchor="w")

        build_page = {"外观": page_look, "设备": page_devices,
                      "趋势": page_trend, "刷新": page_refresh,
                      "启动与托盘": page_boot, "关于": page_about}

        for name in pages:
            b = ttk.Button(nav, text=name, style="Nav.TButton",
                           command=lambda n=name: show_page(n))
            b.pack(fill="x")
            nav_btns[name] = b

        state["settings_win"] = win
        show_page(initial_page if initial_page in build_page else "外观")
        refresh_tray_state()
        sync_settings_bg()

    # ---- 视频帧 → 舞台 ----
    def video_pump():
        if wallpaper.kind == "video" and not state["minimal"]:
            im = state["vframe"]
            if im is not None:
                state["vframe"] = None
                try:
                    if state["vphoto"] is None:
                        state["vphoto"] = wp.to_photo(root, im)
                        stage.itemconfigure(i_wall, image=state["vphoto"],
                                            state="normal")
                        state["wall_on"] = True
                        _sync_shadows()
                    elif not wp.paste_photo(state["vphoto"], im):
                        # 窗口改了尺寸，旧照片贴不进去：按新尺寸重建
                        state["vphoto"] = wp.to_photo(root, im)
                        stage.itemconfigure(i_wall, image=state["vphoto"],
                                            state="normal")
                except Exception:
                    pass
                # 卡片玻璃每 0.35 秒跟一次，够用且不烧 CPU
                now = time.time()
                if now - state.get("vlast_glass", 0) > 0.35:
                    state["vlast_glass"] = now
                    _refresh_card_bgs()
        root.after(45, video_pump)

    # ---- 主线程 pump ----
    _sys_theme_check = 0.0

    def pump():
        nonlocal _sys_theme_check
        if hasattr(root, "_ensure_icon"):    # 标题栏图标被 Tk 换掉就立刻重挂
            try:
                root._ensure_icon()
            except Exception:
                pass
        try:
            while True:
                kind, payload = results.get_nowait()
                if kind == "readings":
                    apply(payload)
                elif kind == "tray":
                    tray_done(payload)
                elif kind == "links":
                    links_done(payload)
                elif kind == "show":
                    show_gui()
                elif kind == "popup":
                    show_popup()
                elif kind == "_popup_data":
                    try:
                        if getattr(root, "_popup_win", None) is not None:
                            root._popup_win.destroy()
                    except Exception:
                        pass
                    root._popup_win = build_popup(root, payload,
                                                  popup_pal(state["theme_key"]))
                elif kind == "menu":
                    show_menu_popup()
                elif kind == "quit":
                    quit_gui()
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
            state["busy"] = False
            state["starting"] = False
            btn.configure(state="normal")

        if state["starting"] and time.time() - state["started_at"] > 20:
            state["starting"] = False
            refresh_tray_state()

        if v_theme.get() == THEME_LABELS["system"] and time.time() - _sys_theme_check > 2:
            _sys_theme_check = time.time()
            want = resolve_theme("system")
            if want != state["theme_key"]:
                _apply_theme("system")

        refresh_tray_state()
        root.after(1000, pump)

    def raise_me():
        try:
            root.lift()
            root.focus_force()
        except Exception:
            pass

    # ---- 单进程：窗口关闭→隐藏进托盘；托盘→GUI 信号 ----
    def show_gui():
        try:
            root.deiconify()
            root.lift()
            root.focus_force()
        except Exception:
            pass
        wallpaper.pause_video(False)    # 窗口回来了，恢复解码

    def show_popup():
        try:
            if hasattr(root, "_popup_win") and root._popup_win is not None:
                try:
                    root._popup_win.destroy()
                except Exception:
                    pass
                root._popup_win = None
            readings = state.get("readings")
            if readings:
                root._popup_win = build_popup(root, readings,
                                              popup_pal(state["theme_key"]))
            else:
                root._popup_win = build_popup(
                    root,
                    [Reading("VGN S99", "", error="读取中…"),
                     Reading("Logitech", "", error="读取中…")],
                    popup_pal(state["theme_key"]))

                def _fill(new_readings):
                    try:
                        if getattr(root, "_popup_win", None) is not None:
                            root._popup_win.destroy()
                    except Exception:
                        pass
                    root._popup_win = build_popup(root, new_readings,
                                                  popup_pal(state["theme_key"]))

                def _bg():
                    results.put(("_popup_data", read_all()))

                threading.Thread(target=_bg, daemon=True).start()
        except Exception:
            traceback.print_exc()

    def show_menu_popup():
        try:
            if hasattr(root, "_menu_win") and root._menu_win is not None:
                try:
                    root._menu_win.destroy()
                except Exception:
                    pass
                root._menu_win = None
            root._menu_win = build_menu_popup(root, popup_pal(state["theme_key"]))
        except Exception:
            traceback.print_exc()

    def hide_to_tray():
        wallpaper.pause_video(True)     # 藏进托盘就别解码了，省 CPU 省电
        root.withdraw()

    def quit_gui():
        _stop_video()
        root.quit()

    root.protocol("WM_DELETE_WINDOW", hide_to_tray)
    root.bind("<Configure>", relayout)
    root.bind("<Visibility>", lambda _e: wallpaper.pause_video(
        root.state() != "normal"))

    # ---- 启动 ----
    rebuild_cards()
    relayout()
    if wallpaper.kind == "video":
        _start_video()
    else:
        _draw_wall()
    if state.get("notice"):
        notify(state["notice"], 12)     # 背景/Pillow 之类的启动提示，真展示出来
    _ambient_build()
    root.after(260, _ambient_tick)
    root.after(300, refresh)
    _reschedule()
    root.after(1000, pump)
    root.after(120, video_pump)
    root.after(250, raise_me)

    # 供 main() 里的托盘线程回调塞信号进来（线程安全：只往 queue.put）
    root._signal_q = results
    return root


def main() -> None:
    parser = argparse.ArgumentParser(
        description="2.4G Device Battery Check —— 扫描 / 添加 / 查看 2.4G 设备电量")
    parser.add_argument("--once", action="store_true", help="命令行读一次就退出")
    parser.add_argument("--scan", action="store_true",
                        help="扫描本机能读到电量的设备（只列出，不添加）")
    parser.add_argument("--add", action="store_true",
                        help="扫描并把找到的设备全部加入设备表")
    parser.add_argument("--install-links", action="store_true",
                        help="在桌面重建快捷方式（桌面上只留一个入口）")
    parser.add_argument("--autostart", choices=["on", "off", "status"],
                        help="开机自启：on 打开 / off 关闭 / status 查看")
    parser.add_argument("--minimal", action="store_true",
                        help="启动后直接进极简模式（只有毛玻璃小浮窗）")
    args = parser.parse_args()

    if args.scan or args.add:
        print("正在扫描…")
        found = devices.scan_devices()
        if not found:
            print("没找到能读电量的设备（接收器插着、设备别休眠再试）")
            return
        have = {s.get("key") for s in devices.load_specs()}
        print(f"找到 {len(found)} 个：")
        for spec, r in found:
            cur = "（已在设备表）" if spec["key"] in have else ""
            print(f"  {r.title: <22} {spec['key']}  {r.level}%  "
                  f"{r.status}{cur}")
        if args.add:
            for spec, _ in found:
                devices.add_spec(spec)
            print(f"已加入设备表（共 {len(devices.load_specs())} 个）："
                  f"{devices.STORE_PATH}")
        return

    if args.install_links:
        r = tc.install_desktop_links()
        print("桌面快捷方式：", tc.desktop_link_path())
        print("  状态：", r.get("shortcut") or "创建失败")
        for name in r.get("removed") or []:
            print("  已清理旧链接：", name)
        for msg in r.get("errors") or []:
            print("  注意：", msg)
        return

    if args.autostart:
        if args.autostart == "status":
            st = tc.autostart_status()
            print("开机自启：", "已启用" if st["enabled"] else "未启用")
            print("  方式：", st["method"])
            print("  快捷方式：", st["shortcut"],
                  "(存在)" if st["shortcut_exists"] else "(不存在)")
            print("  任务管理器状态：", st["taskmanager_state"])
            print("  目标：", st["target"])
            print("  旧注册表项：", "还在" if st["legacy_registry"] else "已清理")
        else:
            ok = tc.set_autostart(args.autostart == "on")
            print("已" + ("加入" if args.autostart == "on" else "取消") + "开机自启",
                  "" if ok else "（失败）")
        return

    if args.once:
        cli_once()
        return

    # ============================================================
    # 单进程模式：主线程跑 GUI，后台线程跑托盘消息循环。
    # 打开软件自动开托盘；点叉号隐藏进托盘；托盘右键打开恢复窗口；
    # 托盘退出才真正结束进程。
    # ============================================================
    if not tc.acquire_single_instance():
        # 已有实例在跑：通知它把主界面显示到前台，本进程直接退出
        tc.second_instance_notify()
        return

    try:
        tc.USER32.SetProcessDPIAware()   # 托盘图标不糊
    except Exception:
        pass

    try:
        root = build_ui(start_minimal=args.minimal)
    except Exception:
        print("无法启动图形界面，改用命令行模式：")
        traceback.print_exc()
        cli_once()
        return

    # 托盘线程 → 主线程的信号（线程安全：只往 queue.put）
    def on_show():
        root._signal_q.put(("show", None))

    def on_popup():
        root._signal_q.put(("popup", None))

    def on_menu():
        root._signal_q.put(("menu", None))

    def on_exit():
        root._signal_q.put(("quit", None))

    import tray_app as ta
    tray_thread = threading.Thread(
        target=lambda: ta.TrayApp(debug=False, on_show=on_show, on_popup=on_popup,
                                  on_menu=on_menu, on_exit=on_exit).run(),
        name="tray", daemon=True)
    tray_thread.start()

    root.mainloop()

    # 主循环结束（托盘退出触发 quit_gui）：等托盘线程收尾
    tray_thread.join(timeout=2.0)


if __name__ == "__main__":
    main()
