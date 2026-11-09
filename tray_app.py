# -*- coding: utf-8 -*-
"""tray_app.py —— 托盘电量监视器（纯 ctypes，无第三方依赖）

界面约定
--------
* 通知区域**一个圆圈图标**：圆环按电量百分比着色（绿 ≥60 / 黄 30-59 / 红 <30），
  中心显示当前**最低设备的电量**。
* **悬停显示 tooltip**：软件名称（2.4G Device Battery Check）。
* **左键图标 → 弹出气泡**：显示各设备具体电量，充电时显示「充电中」。
* **右键图标 → 弹出菜单**：打开 2.4G Device Battery Check / 退出 2.4G Device Battery Check。

其它
----
* 开机自启用「启动」文件夹里的快捷方式（`电量托盘.lnk`，任务管理器可见）+ pythonw.exe。
* 低 CPU：SetTimer 驱动，空闲不轮询；读取在后台线程；默认 60 秒一轮。
* **不写日志文件**：只有 `--debug` 时才往控制台打印。
* 全 ctypes 调 shell32/user32，不依赖 pystray / Pillow。

启动：
    pythonw tray_app.py        # 静默驻留（开机自启用的就是这个）
    python  tray_app.py --debug   # 前台运行并打印过程信息（排查问题用）
    python  tray_app.py --status
    python  tray_app.py --quit
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes
from typing import Optional

import hid_win as hw      # 只为 hw.GUID（NOTIFYICONDATAW 的 guidItem 字段类型）
import devices
import win_icon
import tray_common as tc

BASE_DIR = tc.BASE_DIR
APP_NAME = tc.APP_NAME
MUTEX_NAME = tc.MUTEX_NAME
WND_CLASS = tc.WND_CLASS

USER32 = tc.USER32
KERNEL32 = tc.KERNEL32
SHELL32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
MSG_TRAY_CALLBACK = WM_APP + 1
MSG_REFRESH_DONE = WM_APP + 2

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10

NIIF_INFO = 0x00000001

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

MENU_OPEN_UI = 1
MENU_QUIT = 2

SW_RESTORE = 9

# --------------------------------------------------------------------------
# Win32 结构体与原型
# --------------------------------------------------------------------------
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", hw.GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", ctypes.c_void_p), ("message", wintypes.UINT),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", wintypes.DWORD), ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG)]


USER32.RegisterClassExW.restype = wintypes.WORD
USER32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
USER32.CreateWindowExW.restype = ctypes.c_void_p
USER32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
USER32.DefWindowProcW.restype = ctypes.c_ssize_t
USER32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
USER32.DestroyWindow.restype = wintypes.BOOL
USER32.DestroyWindow.argtypes = [ctypes.c_void_p]
USER32.SetTimer.restype = ctypes.c_size_t
USER32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
USER32.KillTimer.restype = wintypes.BOOL
USER32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
USER32.GetMessageW.restype = ctypes.c_int
USER32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p,
                               wintypes.UINT, wintypes.UINT]
USER32.PostQuitMessage.restype = None
USER32.PostQuitMessage.argtypes = [ctypes.c_int]
USER32.TranslateMessage.restype = wintypes.BOOL
USER32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
USER32.DispatchMessageW.restype = ctypes.c_ssize_t
USER32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]

SHELL32.Shell_NotifyIconW.restype = wintypes.BOOL
SHELL32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]

KERNEL32.GetModuleHandleW.restype = ctypes.c_void_p
KERNEL32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
KERNEL32.CreateMutexW.restype = ctypes.c_void_p
KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.LPCWSTR]

# 弹出菜单
USER32.CreatePopupMenu.restype = ctypes.c_void_p
USER32.CreatePopupMenu.argtypes = []
USER32.AppendMenuW.restype = wintypes.BOOL
USER32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t,
                               wintypes.LPCWSTR]
USER32.TrackPopupMenu.restype = wintypes.BOOL
USER32.TrackPopupMenu.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_void_p]
USER32.DestroyMenu.restype = wintypes.BOOL
USER32.DestroyMenu.argtypes = [ctypes.c_void_p]
USER32.GetCursorPos.restype = wintypes.BOOL
USER32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

TRAY_ICONS = [
    # (uID, key, 提示名, 是否带底板)
    (1, "all", "2.4G Device Battery Check", False),
]

# 设备显示名（英文型号）
# 设备表为空时的提示（不再写死具体型号：设备由「设备管理」决定）


# --------------------------------------------------------------------------
# 设备读取（轻量版）
# --------------------------------------------------------------------------
def read_devices() -> list:
    """按设备表读一遍；设备表空/一条都不通时返回空列表。"""
    try:
        return devices.read_all()
    except Exception:
        return []


# --------------------------------------------------------------------------
# 托盘程序
# --------------------------------------------------------------------------
class TrayApp:
    def __init__(self, debug: bool = False, on_show=None, on_exit=None,
                 on_popup=None, on_menu=None):
        self.debug = debug
        # 单进程模式回调：托盘线程通知主线程显示/退出 GUI / 弹出小窗口 / 右键菜单
        # （无回调则走独立进程逻辑：左键弹气泡）
        self.on_show = on_show
        self.on_exit = on_exit
        self.on_popup = on_popup
        self.on_menu = on_menu
        self.cfg = tc.load_config()
        self.hwnd: Optional[int] = None
        self.wndproc_ref = None
        self.busy = False
        # 图标只有一个（uID=1）：显示所有已添加设备里的最低电量
        self.levels = {"all": (None, "", "正在读取…")}
        self.readings: list = []
        self.icons_added = False
        self.ui_proc = None                 # 由托盘启动的主界面进程（退出时一并关闭）
        self.taskbar_created_msg = USER32.RegisterWindowMessageW("TaskbarCreated")
        self.msg_quit = USER32.RegisterWindowMessageW(tc.MSG_QUIT)
        self.msg_reload = USER32.RegisterWindowMessageW(tc.MSG_RELOAD)
        self.msg_show = USER32.RegisterWindowMessageW(tc.MSG_SHOW)
        self.log("启动，配置 = " + json.dumps(self.cfg, ensure_ascii=False))

    # ---- 输出（默认不落盘）---------------------------------------------
    def log(self, msg: str) -> None:
        """只在 --debug 时输出到控制台。

        默认**不写任何日志文件**（不占磁盘也不留痕）；需要排查问题时用
        `python tray_app.py --debug` 前台跑一次就能看到全部输出。
        """
        if self.debug:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)

    # ---- 图标 ----------------------------------------------------------
    @staticmethod
    def _color(level: Optional[int]) -> tuple[int, int, int]:
        if level is None:
            return (150, 155, 160)
        if level >= 60:
            return (93, 220, 138)
        if level >= 30:
            return (245, 196, 81)
        return (255, 107, 107)

    def _nid(self, uID: int):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = uID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = MSG_TRAY_CALLBACK
        return nid

    def _apply_icon(self, action: int, uID: int, key: str, title: str,
                    plate: bool) -> bool:
        level, _, _ = self.levels[key]
        # 圆圈电量图标：外圈按百分比画弧，中心显示最低电量数字
        nid = self._nid(uID)
        nid.hIcon = win_icon.make_ring_hicon(level, self._color(level))
        nid.szTip = title                        # 悬停显示软件名称
        return bool(SHELL32.Shell_NotifyIconW(action, ctypes.byref(nid)))

    def _tooltip(self) -> str:
        """悬停显示软件名称。"""
        return "2.4G Device Battery Check"

    def _balloon_text(self) -> str:
        """左键气泡内容：各设备电量，充电时显示「充电中」。"""
        if not self.readings:
            return "还没有添加设备（打开主界面 → 设备管理 → 一键扫描）"
        lines = []
        for r in self.readings:
            txt = f"{r.level}%" if r.ok else (r.error or "未读到")
            if r.charging:
                txt += "（充电中）"
            lines.append(f"{r.title}: {txt}")
        return "\n".join(lines[:6])

    def add_icons(self) -> None:
        for uID, key, title, plate in TRAY_ICONS:
            ok = self._apply_icon(NIM_ADD, uID, key, title, plate)
            self.log(f"NIM_ADD {title} -> {'ok' if ok else '失败'}")
        self.icons_added = True

    def update_icons(self) -> None:
        if not self.icons_added:
            self.add_icons()
            return
        for uID, key, title, plate in TRAY_ICONS:
            self._apply_icon(NIM_MODIFY, uID, key, title, plate)

    def remove_icons(self) -> None:
        for uID, _, _, _ in TRAY_ICONS:
            nid = self._nid(uID)
            nid.uFlags = 0
            SHELL32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self.icons_added = False

    # ---- 定时器 / 刷新 --------------------------------------------------
    def apply_timer(self) -> None:
        if not self.hwnd:
            return
        USER32.KillTimer(self.hwnd, 1)
        USER32.SetTimer(self.hwnd, 1, int(self.cfg["interval"]) * 1000, None)

    def apply_config(self) -> None:
        """同步界面里改过的设置。

        注意：开机自启这里**只读不改**——托盘绝不替用户动系统设置。
        （早先的版本会自动"纠正"，结果一次读到不一致就把用户的启动项删了。）
        """
        new = tc.load_config()
        changed = new["interval"] != self.cfg["interval"]
        self.cfg = new
        if changed:
            self.log(f"刷新间隔 -> {new['interval']} 秒")
            self.apply_timer()

        actual = tc.is_autostart_enabled()
        if bool(self.cfg.get("autostart")) != actual:
            # 只把真实状态写回配置，让界面上的勾选和任务管理器保持一致
            self.cfg["autostart"] = actual
            tc.save_config(self.cfg)
            self.log(f"开机自启实际为 {actual}，已同步到配置（未改动系统设置）")

    def refresh_now(self) -> None:
        if self.busy or not self.hwnd:
            return
        self.busy = True
        threading.Thread(target=self._worker, args=(self.hwnd,), daemon=True).start()

    def _worker(self, hwnd: int) -> None:
        try:
            self.readings = read_devices()
            # 合并图标：取所有已添加设备里的最低电量（都读不到则显示空圈）
            got = [r.level for r in self.readings if r.level is not None]
            agg = min(got) if got else None
            err = "" if got else (
                self.readings[0].error if self.readings else "还没有添加设备")
            self.levels["all"] = (agg, "", err)
        except Exception:
            self.log("读取异常:\n" + traceback.format_exc())
        USER32.PostMessageW(hwnd, MSG_REFRESH_DONE, 0, 0)

    def on_refresh_done(self) -> None:
        self.busy = False
        a, _, ae = self.levels["all"]
        detail = "，".join(
            f"{r.title}={r.level if r.ok else (r.error or '读不到')}"
            for r in self.readings) or "无设备"
        self.log(f"刷新完成：{detail}；合并最低={a}（{ae or 'ok'}）")
        self.update_icons()

    # ---- 左键气泡 ---------------------------------------------------
    def on_left_click(self) -> None:
        """左键：单进程模式回调主线程弹小窗口；独立进程模式退回气泡。"""
        if self.on_popup is not None:
            try:
                self.on_popup()
            except Exception as e:
                self.log(f"弹小窗口回调出错：{e}")
            return
        self.show_balloon()

    def show_balloon(self) -> None:
        """左键：弹气泡显示各设备电量。"""
        nid = self._nid(TRAY_ICONS[0][0])
        nid.uFlags = NIF_INFO
        nid.dwInfoFlags = NIIF_INFO
        nid.szInfoTitle = "2.4G Device Battery Check"
        text = self._balloon_text()
        nid.szInfo = text[:255]
        if not SHELL32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            self.log("气泡显示失败")
        # 恢复常规图标标志（下次 NIM_MODIFY 仍是纯图标）
        nid2 = self._nid(TRAY_ICONS[0][0])
        nid2.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid2.hIcon = win_icon.make_ring_hicon(
            self.levels["all"][0], self._color(self.levels["all"][0]))
        nid2.szTip = self._tooltip()
        SHELL32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid2))

    # ---- 右键菜单 ---------------------------------------------------
    def show_menu(self) -> None:
        """右键：单进程模式回调主线程弹菜单小窗口；独立进程模式弹原生菜单。"""
        if self.on_menu is not None:
            try:
                self.on_menu()
            except Exception as e:
                self.log(f"弹菜单小窗口回调出错：{e}")
            return
        menu = USER32.CreatePopupMenu()
        if not menu:
            return
        USER32.AppendMenuW(menu, MF_STRING, MENU_OPEN_UI, "打开 2.4G Device Battery Check")
        USER32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        USER32.AppendMenuW(menu, MF_STRING, MENU_QUIT, "退出 2.4G Device Battery Check")

        pos = wintypes.POINT()
        USER32.GetCursorPos(ctypes.byref(pos))
        # 必须 SetForegroundWindow，否则菜单点一下不会消失
        USER32.SetForegroundWindow(self.hwnd)
        cmd = USER32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
            pos.x, pos.y, 0, self.hwnd, None)
        USER32.DestroyMenu(menu)

        if cmd == MENU_OPEN_UI:
            self.open_ui()
        elif cmd == MENU_QUIT:
            self.quit()

    # ---- 打开简洁界面 ---------------------------------------------------
    def open_ui(self) -> None:
        # 单进程模式：主线程 GUI 与本托盘同进程，直接回调主线程恢复窗口
        if self.on_show is not None:
            try:
                self.on_show()
            except Exception as e:
                self.log(f"恢复主界面回调出错：{e}")
            return
        hwnd = tc.ui_hwnd()
        if hwnd:
            USER32.ShowWindow(hwnd, SW_RESTORE)
            USER32.SetForegroundWindow(hwnd)
            self.log("简洁界面已在前台")
            return
        import subprocess
        try:
            self.ui_proc = subprocess.Popen([tc.pythonw_path(),
                                             os.path.join(BASE_DIR, "my_battery.py")],
                                            cwd=BASE_DIR, creationflags=0x00000008)
            self.log(f"已打开简洁界面 (pid={self.ui_proc.pid})")
        except Exception as e:
            self.log(f"打开界面失败：{e}")

    def quit(self) -> None:
        self.log("退出")
        # 单进程模式：先清理图标/临时文件，最后通知主线程退出 GUI，
        # 再由托盘线程自己销毁窗口结束消息循环
        if self.on_exit is not None:
            self.remove_icons()
            try:
                win_icon.destroy_all()
                win_icon.cleanup_files()
            except Exception:
                self.log("清理图标时出错（忽略）：\n" + traceback.format_exc())
            try:
                self.on_exit()
            except Exception as e:
                self.log(f"退出回调出错：{e}")
            if self.hwnd:
                # DestroyWindow 必须在创建窗口的线程（本托盘线程）里调用，
                # 触发 WM_DESTROY -> PostQuitMessage，消息循环随之结束
                USER32.DestroyWindow(self.hwnd)
            return
        # 先把托盘打开的主界面一起彻底关闭（不残留窗口）
        self._close_ui()
        self.remove_icons()
        try:
            win_icon.destroy_all()
            win_icon.cleanup_files()
        except Exception:
            self.log("清理图标时出错（忽略）：\n" + traceback.format_exc())
        if self.hwnd:
            # DestroyWindow 会触发 WM_DESTROY -> PostQuitMessage，消息循环随之结束
            USER32.DestroyWindow(self.hwnd)

    def _close_ui(self) -> None:
        """关闭由托盘启动的主界面进程；若查不到 pid，则按窗口标题兜底关闭。"""
        proc = self.ui_proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
                self.log(f"已关闭主界面 (pid={proc.pid})")
            except Exception as e:
                self.log(f"关闭主界面进程失败：{e}")
            finally:
                self.ui_proc = None
        # 兜底：按标题找窗口并关闭（可能不是本进程拉起的，也要一起关掉）
        hwnd = tc.ui_hwnd()
        if hwnd:
            USER32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    # ---- 窗口过程 -------------------------------------------------------
    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == MSG_TRAY_CALLBACK:
            event = int(lparam) & 0xFFFF
            if event == WM_RBUTTONUP:
                self.show_menu()                   # 右键：弹出菜单
            elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.on_left_click()                # 左键：弹小窗口（单进程）/气泡（独立进程）
            return 0
        if msg == MSG_REFRESH_DONE:
            self.on_refresh_done()
            return 0
        if msg == WM_TIMER:
            self.apply_config()                    # 顺手同步一下界面里改的设置
            self.refresh_now()
            return 0
        if msg == self.msg_quit:
            self.quit()
            return 0
        if msg == self.msg_reload:
            self.apply_config()
            return 0
        if msg == self.msg_show:
            self.open_ui()
            return 0
        if msg == self.taskbar_created_msg:
            self.log("任务栏重建，重新添加图标")
            self.add_icons()
            return 0
        if msg == WM_DESTROY:
            # 重要：销毁窗口后必须让消息循环退出，否则进程会残留成"僵尸"
            # ——它已经没有窗口/图标了，却还占着单实例锁，导致托盘再也起不来。
            USER32.PostQuitMessage(0)
            return 0
        return USER32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 启动 ----------------------------------------------------------
    def run(self) -> int:
        hinst = KERNEL32.GetModuleHandleW(None)
        self.wndproc_ref = WNDPROC(self.wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self.wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = WND_CLASS
        if not USER32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):
                self.log(f"RegisterClassExW 失败 err={err}")
                return 1

        hwnd = USER32.CreateWindowExW(0, WND_CLASS, APP_NAME, 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            self.log(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")
            return 1
        self.hwnd = hwnd

        self.add_icons()
        self.apply_config()
        self.apply_timer()
        self.refresh_now()

        msg = MSG()
        while USER32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            USER32.TranslateMessage(ctypes.byref(msg))
            USER32.DispatchMessageW(ctypes.byref(msg))
        self.log("消息循环结束")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="托盘电量监视器")
    parser.add_argument("--debug", action="store_true", help="前台运行并输出日志")
    parser.add_argument("--status", action="store_true", help="打印当前状态")
    parser.add_argument("--quit", action="store_true", help="让正在运行的实例退出")
    args = parser.parse_args()

    if args.status:
        print("配置：", json.dumps(tc.load_config(), ensure_ascii=False))
        print("开机自启：", tc.is_autostart_enabled())
        print("启动命令：", tc.autostart_command())
        print("托盘是否在运行：", tc.is_tray_running())
        return 0
    if args.quit:
        ok = tc.post_to_tray(tc.MSG_QUIT)
        print("已通知托盘退出" if ok else "托盘没在运行")
        return 0

    # 单实例
    KERNEL32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:
        if tc.is_tray_running():
            # 真有一个正常实例在跑：让它重读配置
            tc.post_to_tray(tc.MSG_RELOAD)
            print("已经在运行了（托盘里两个数字图标就是它；右键图标打开界面）")
            return 0
        # 有人占着锁但已经没有窗口 —— 上一版留下的僵尸进程，清掉后继续启动
        tc.cleanup_zombie()

    tc.write_pid()

    try:
        USER32.SetProcessDPIAware()            # 图标不糊
    except Exception:
        pass

    app = TrayApp(debug=args.debug)
    try:
        return app.run()
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        tc.remove_pid()


if __name__ == "__main__":
    sys.exit(main())
