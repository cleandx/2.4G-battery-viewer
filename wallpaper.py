# -*- coding: utf-8 -*-
"""自定义背景：图片 / 视频（ffmpeg 流式解码）。

设计目标：**好看，但绝不占内存**。

- 图片：只保留一份「缩放到窗口大小」的 RGB 图（760×600 ≈ 1.4 MB），
  模糊/暗化都在这个小图上做；不同尺寸只缓存最近两份。
- 视频：ffmpeg 输出原始 RGB 帧，**一次只有一帧在内存里**（解码上限 640×420 ≈ 800 KB，
  窗口更大时显示前才补一档 bilinear 放大，仍是同一帧的体积量级），
  交给界面后旧帧随即丢弃；窗口隐藏/最小化自动暂停。
- 没有 Pillow 就只支持 PNG/GIF（Tk 自带解码）；找不到 ffmpeg 就不能放视频，
  会自动退回「视频第一帧当静态图」都做不到——那就提示用户。
"""
from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

try:                                    # Pillow 是可选增强：没有也能跑图片(部分格式)
    from PIL import Image, ImageEnhance, ImageFilter
    _HAS_PIL = True
except Exception:                       # pragma: no cover
    Image = None                        # type: ignore
    _HAS_PIL = False

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv")

MAX_VIDEO_W = 640          # 视频解码宽度上限：再大纯粹是在烧内存和 CPU
MAX_VIDEO_H = 420
VIDEO_FPS = 10             # 背景视频 10 帧/秒足够了（还在背后虚化）
GLASS_FPS = 3              # 卡片玻璃底多久跟一次视频帧（虚化了看不出差别）

_HERE = os.path.dirname(os.path.abspath(__file__))


def ffmpeg_path() -> str:
    """找 ffmpeg：先看程序目录，再看 PATH。找不到返回空串。"""
    local = os.path.join(_HERE, "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    return shutil.which("ffmpeg") or ""


def has_pil() -> bool:
    """图片/视频背景都靠 Pillow 解码；没有它背景功能整体不可用。"""
    return _HAS_PIL


def kind_of(path: str) -> str:
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return ""


def describe(path: str) -> str:
    """给界面用的一句话说明（带文件大小，方便判断是不是塞了个 4K 视频）。"""
    try:
        size = os.path.getsize(path) / 1024.0 / 1024.0
        return f"{os.path.basename(path)} · {size:.1f} MB"
    except OSError:
        return os.path.basename(str(path))


def _cover(im, w: int, h: int, resample=None):
    """等比缩放 + 居中裁切到 w×h（不拉伸变形）。"""
    if resample is None:
        resample = Image.LANCZOS
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        return im.resize((w, h))
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    im = im.resize((nw, nh), resample)
    left = max(0, (nw - w) // 2)
    top = max(0, (nh - h) // 2)
    return im.crop((left, top, left + w, top + h))


def load_image(path: str, w: int, h: int, dim: float = 0.45,
               blur: float = 6.0, extra_dark: float = 0.0):
    """读图片 → 覆盖式缩放到 w×h → 模糊 → 压暗。返回 PIL RGB 图（或 None）。"""
    if not _HAS_PIL:
        return None
    try:
        im = Image.open(path)
        im = im.convert("RGB")
    except Exception:
        return None
    w = max(2, int(w))
    h = max(2, int(h))
    im = _cover(im, w, h)
    if blur and blur > 0:
        # 先缩到 1/3 再模糊，成本降一个数量级，视觉上几乎无差
        small = im.resize((max(2, w // 3), max(2, h // 3)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(max(0.4, blur / 3.0)))
        im = small.resize((w, h), Image.BILINEAR)
    total = min(0.95, max(0.0, dim + extra_dark))
    if total > 0:
        im = ImageEnhance.Brightness(im).enhance(1.0 - total)
    return im


def to_photo(master, im):
    """PIL 图 → tk.PhotoImage。

    优先用 Pillow 的 ImageTk（直接往 Tk 的图片里塞像素，比编码成 PNG 再解码快
    一个数量级）；没有 ImageTk 时退回 PNG data（Tk 8.6 原生支持）。
    """
    try:
        from PIL import ImageTk
        return ImageTk.PhotoImage(im, master=master)
    except Exception:
        pass
    import tkinter as tk
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return tk.PhotoImage(master=master,
                         data=base64.b64encode(buf.getvalue()).decode("ascii"),
                         format="png")


def paste_photo(photo, im) -> bool:
    """把新一帧塞进已有的 PhotoImage（视频用，避免每帧都分配新对象）。"""
    try:
        photo.paste(im)
        return True
    except Exception:
        return False


def rounded(im, radius: int = 16):
    """给 PIL 图切圆角（四角透明）。"""
    if not _HAS_PIL:
        return im
    w, h = im.size
    radius = max(0, min(radius, min(w, h) // 2))
    mask = Image.new("L", (w, h), 0)
    from PIL import ImageDraw
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1),
                                           radius=radius, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


class Wallpaper:
    """背景状态 + 缓存。界面只跟它要「一张铺满 w×h 的图」或「某块玻璃」。"""

    def __init__(self) -> None:
        self.path = ""
        self.kind = ""          # "" / "image" / "video"
        self.dim = 0.45         # 压暗强度 0..0.9
        self.blur = 6.0         # 模糊半径（对缩小的图做）
        self._cache: dict = {}  # (w,h,dim,blur) -> PIL 图
        self._cache_order: list = []
        self._frame_im = None   # 视频当前帧（PIL）
        self._lock = threading.Lock()
        self._video: Optional["_VideoStream"] = None

    # -- 加载 ---------------------------------------------------------
    def set_source(self, path: str) -> bool:
        self.stop_video()
        self._cache.clear()
        self._cache_order.clear()
        self.path = str(path or "")
        self.kind = kind_of(self.path)
        return bool(self.kind and os.path.isfile(self.path))

    def clear(self) -> None:
        self.stop_video()
        self.path = ""
        self.kind = ""
        self._cache.clear()
        self._cache_order.clear()
        self._frame_im = None

    # -- 图片 ---------------------------------------------------------
    def image(self, w: int, h: int):
        """返回铺满 w×h 的背景 PIL 图（带缓存）。"""
        if not _HAS_PIL:
            return None
        key = (int(w), int(h), round(self.dim, 2), round(self.blur, 1))
        if key in self._cache:
            return self._cache[key]
        im = None
        if self.kind == "image":
            im = load_image(self.path, w, h, dim=self.dim, blur=self.blur)
        if im is None:
            return None
        self._cache[key] = im
        self._cache_order.append(key)
        while len(self._cache_order) > 2:          # 只留最近两份，别攒内存
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)
        return im

    def glass(self, master, x: int, y: int, w: int, h: int,
              extra_dark: float = 0.18, radius: int = 16,
              tint: Optional[str] = None, tint_alpha: float = 0.0,
              border: Optional[str] = None):
        """截背景图的一块 → 处理成「白霜玻璃」卡片底 → PhotoImage。

        处理链：裁剪 → (视频帧补一层模糊，和图片背景手感一致) → 轻压暗
        → 往主题色上混一层 tint_alpha 的底（不混的话玻璃和身后的背景
        颜色几乎一样，卡片看起来就像“没有背景”）→ 圆角 → 可选 1px 描边。
        """
        if not _HAS_PIL:
            return None
        win_w, win_h = _window_size()
        if self.kind == "video" and self._frame_im is not None:
            src = self._frame_im
            sx = src.width / max(1, win_w)
            sy = src.height / max(1, win_h)
        else:
            src = self.image(win_w, win_h)
            sx = sy = 1.0
        if src is None:
            return None
        box = (int(x * sx), int(y * sy),
               int((x + w) * sx), int((y + h) * sy))
        try:
            part = src.crop(box)
        except Exception:
            return None
        part = _cover(part, w, h)
        if self.kind == "video":
            # 图片背景本身就是模糊过的；视频帧是清晰的，裁出来糊深一点
            try:
                part = part.filter(ImageFilter.GaussianBlur(6))
            except Exception:
                pass
        if extra_dark > 0:
            part = ImageEnhance.Brightness(part).enhance(1.0 - min(0.8, extra_dark))
        if tint and tint_alpha > 0:
            solid = Image.new("RGB", part.size, tint)
            part = Image.blend(part, solid, max(0.0, min(0.95, tint_alpha)))
        part = rounded(part, radius)
        if border:
            # 双圈 rim：外圈半透明深色 + 内圈半透明白。
            # 亮背景靠外圈勾边、暗背景靠内圈发亮，任何壁纸上轮廓都可见。
            try:
                from PIL import ImageDraw
                w_, h_ = part.size
                outer = Image.new("L", (w_, h_), 0)
                ImageDraw.Draw(outer).rounded_rectangle(
                    (0, 0, w_ - 1, h_ - 1), radius=radius,
                    outline=255, width=1)
                inner = Image.new("L", (w_, h_), 0)
                ImageDraw.Draw(inner).rounded_rectangle(
                    (1, 1, w_ - 2, h_ - 2), radius=max(1, radius - 1),
                    outline=255, width=1)
                dark_edge = Image.new("RGBA", (w_, h_), (0, 0, 0, 80))
                light_edge = Image.new("RGBA", (w_, h_), (255, 255, 255, 150))
                part = Image.composite(dark_edge, part, outer)
                part = Image.composite(light_edge, part, inner)
            except Exception:
                pass
        return to_photo(master, part)

    # -- 视频 ---------------------------------------------------------
    def start_video(self, on_frame: Callable[[object], None],
                    size_provider: Callable[[], tuple]) -> bool:
        """开始放视频；on_frame(PIL 图) 会在主线程外被调用。"""
        if self.kind != "video" or not _HAS_PIL:
            return False
        exe = ffmpeg_path()
        if not exe:
            return False
        self.stop_video()
        self._video = _VideoStream(exe, self.path, on_frame, size_provider,
                                   self._on_frame)
        self._video.start()
        return True

    def _on_frame(self, im) -> None:
        with self._lock:
            self._frame_im = im

    def pause_video(self, on: bool = True) -> None:
        """窗口隐藏/最小化时别解码，省 CPU 和电。"""
        if self._video is not None:
            self._video.pause(on)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_order.clear()

    def stop_video(self) -> None:
        if self._video is not None:
            self._video.stop()
            self._video = None

    @property
    def has_video(self) -> bool:
        return self._video is not None


_WIN_SIZE = [760, 600]


def set_window_size(w: int, h: int) -> None:
    _WIN_SIZE[0] = max(200, int(w))
    _WIN_SIZE[1] = max(200, int(h))


def window_size() -> tuple:
    """当前主窗口大小（视频解码线程用它决定每帧的输出尺寸）。"""
    return _window_size()


def _window_size() -> tuple:
    return _WIN_SIZE[0], _WIN_SIZE[1]


class _VideoStream(threading.Thread):
    """ffmpeg 原始帧 → PIL，一次一帧。"""

    def __init__(self, exe: str, path: str, on_frame, size_provider, keep):
        super().__init__(daemon=True, name="wallpaper-video")
        self.exe = exe
        self.path = path
        self.on_frame = on_frame
        self.size_provider = size_provider
        self.keep = keep
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    def pause(self, on: bool = True) -> None:
        if on:
            self._pause.set()
        else:
            self._pause.clear()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self) -> None:
        w, h = self.size_provider()
        w = min(MAX_VIDEO_W, max(160, int(w)))
        h = min(MAX_VIDEO_H, max(90, int(h)))
        # -re：按视频原本的速度读，不然 ffmpeg 会用几百 fps 把解码器跑满
        cmd = [self.exe, "-v", "error", "-nostdin", "-re",
               "-stream_loop", "-1", "-an", "-i", self.path,
               "-vf", f"fps={VIDEO_FPS},scale={w}:{h}:force_original_aspect_ratio=increase,"
                      f"crop={w}:{h}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            creation = 0x08000000 if os.name == "nt" else 0    # CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=creation)
        except Exception:
            return
        self._proc = proc
        frame_bytes = w * h * 3
        out = proc.stdout
        try:
            while not self._stop.is_set():
                if self._pause.is_set():
                    time.sleep(0.1)
                    continue
                raw = out.read(frame_bytes)
                if not raw or len(raw) < frame_bytes:
                    break
                try:
                    im = Image.frombytes("RGB", (w, h), raw)
                except Exception:
                    break
                im = self._fit_display(im)
                try:
                    self.keep(im)
                    self.on_frame(im)
                except Exception:
                    pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    def _fit_display(self, im):
        """把解码帧补一档缩放到当前窗口大小。

        解码尺寸有上限（MAX_VIDEO_W/H，省 CPU 和内存），窗口比上限大时
        原样贴图会只盖住左上一块。这里用 bilinear 放大 + cover 裁切，
        背景本身是虚化的，放大一档看不出画质差别；窗口改尺寸也自动跟上。
        """
        try:
            dw, dh = _window_size()
        except Exception:
            return im
        dw, dh = max(160, int(dw)), max(90, int(dh))
        if (dw, dh) == im.size:
            return im
        try:
            return _cover(im, dw, dh, resample=Image.BILINEAR)
        except Exception:
            return im
