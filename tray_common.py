# -*- coding: utf-8 -*-
"""tray_common.py —— 托盘程序与界面共用的配置 / 开机自启 / 进程间通信

托盘程序（tray_app.py）和简洁界面（my_devices.py）是两个进程，
共用这里的配置文件与窗口消息，界面里改设置后能立刻通知托盘生效。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from typing import Optional

import win_link
import win_shell

# 单文件 exe（PyInstaller）下：配置/pid 要写到 exe 同目录，不能写进临时解包目录 _MEIPASS
_IS_FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = (os.path.dirname(sys.executable) if _IS_FROZEN
            else os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "tray_config.json")
PID_FILE = os.path.join(BASE_DIR, "tray_pid.txt")

APP_NAME = "DeviceBatteryTray"
WND_CLASS = "DeviceBatteryTrayWnd"
MUTEX_NAME = "Global\\DeviceBatteryTray_single_instance"

# 界面标题（托盘用它来找到已打开的窗口；改名两边都会跟着这个常量走）
UI_TITLE = "2.4G 设备电量"

DEFAULT_CONFIG = {
    "interval": 60,        # 托盘轮询间隔（秒）
    "autostart": True,     # 开机自启
    "theme": "system",     # 界面主题：system / dark / light
    "mini_scale": 1.0,     # 极简模式小浮窗大小（0.85 / 1.0 / 1.15 / 1.3）
    "mini_pos": None,      # 极简模式浮窗位置 [x, y]，None = 右上角
    "bg_path": "",         # 自定义背景：图片或视频的路径（空 = 默认底色）
    "bg_dim": 0.45,        # 背景压暗强度 0..0.9
    "bg_blur": 6.0,        # 背景模糊半径 0..20
    "ambient": "none",     # 氛围粒子：none 无 / petals 花瓣 / rain 雨滴
}

INTERVAL_CHOICES = [(30, "30 秒"), (60, "1 分钟"), (120, "2 分钟"),
                    (300, "5 分钟"), (600, "10 分钟"), (1800, "30 分钟")]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

KERNEL32.CreateMutexW.restype = ctypes.c_void_p
KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]

USER32.FindWindowW.restype = ctypes.c_void_p
USER32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
USER32.PostMessageW.restype = wintypes.BOOL
USER32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                ctypes.c_size_t, ctypes.c_ssize_t]
USER32.RegisterWindowMessageW.restype = wintypes.UINT
USER32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
USER32.ShowWindow.restype = wintypes.BOOL
USER32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.SetForegroundWindow.argtypes = [ctypes.c_void_p]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    clean = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def system_is_dark() -> bool:
    """读注册表判断系统当前是浅色还是深色主题。

    AppsUseLightTheme: 1=浅色 / 0=深色；读不到就默认深色（我们的默认配色）。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return int(val) == 0
    except Exception:
        return True


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 开机自启：启动文件夹里的快捷方式（.lnk），不再用注册表
# --------------------------------------------------------------------------
def pythonw_path() -> str:
    # 单文件 exe 下没有 pythonw，直接用 exe 自身
    if _IS_FROZEN:
        return sys.executable
    exe = sys.executable or ""
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def app_icon_path() -> str:
    """程序图标 assets/app.ico 的路径；找不到就返回空串。"""
    root = (getattr(sys, "_MEIPASS", BASE_DIR) if _IS_FROZEN else BASE_DIR)
    p = os.path.join(root, "assets", "app.ico")
    return p if os.path.isfile(p) else ""


def tray_script() -> str:
    # 单进程入口：my_battery.py 自带 GUI + 内置托盘线程（最新版本）。
    # 独立 tray_app.py 已废弃（它是旧的双进程方案），开机自启/桌面链接都走这里。
    if _IS_FROZEN:
        return sys.executable
    return os.path.join(BASE_DIR, "my_battery.py")


def autostart_command() -> str:
    if _IS_FROZEN:
        return f'"{sys.executable}"'
    return f'"{pythonw_path()}" "{tray_script()}"'


STARTUP_LNK_NAME = "电量托盘.lnk"

# 任务管理器/系统用这个键记录启动项的"启用/禁用"状态
#   值名 = 启动文件夹里的文件名；数据 12 字节，首字节 02=启用、03=禁用
STARTUP_APPROVED_FOLDER = (r"Software\Microsoft\Windows\CurrentVersion"
                           r"\Explorer\StartupApproved\StartupFolder")


def startup_dir() -> str:
    return win_shell.startup_dir()


def startup_lnk() -> str:
    """启动文件夹里的快捷方式路径。"""
    return os.path.join(win_shell.startup_dir(), STARTUP_LNK_NAME)


# 以前版本写在注册表里的项，启动时顺手清掉（迁移用）
LEGACY_RUN_VALUE = APP_NAME


def legacy_autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, LEGACY_RUN_VALUE)
            return bool(val)
    except OSError:
        return False


def _clear_legacy_autostart() -> bool:
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, LEGACY_RUN_VALUE)
            return True
    except OSError:
        return False


def _startup_approved() -> Optional[bool]:
    """任务管理器里的启用状态：True 启用 / False 禁用 / None 没记录（默认启用）"""
    import winreg
    base = os.path.basename(startup_lnk())
    names = (base, os.path.splitext(base)[0])       # 两种命名都兼容
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_FOLDER,
                            0, winreg.KEY_READ) as k:
            for name in names:
                try:
                    data, _ = winreg.QueryValueEx(k, name)
                except OSError:
                    continue
                if isinstance(data, (bytes, bytearray)) and data:
                    return data[0] != 0x03          # 0x03 = 已禁用
    except OSError:
        pass
    return None


def _set_startup_approved(enabled: bool) -> None:
    """写启用/禁用状态——写完之后任务管理器里看到的开关状态是一致的。"""
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_FOLDER,
                                0, winreg.KEY_SET_VALUE) as k:
            data = bytes([0x02 if enabled else 0x03]) + bytes(11)
            winreg.SetValueEx(k, os.path.basename(startup_lnk()), 0,
                              winreg.REG_BINARY, data)
    except OSError:
        pass


def _clear_startup_approved() -> None:
    import winreg
    base = os.path.basename(startup_lnk())
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_FOLDER,
                                0, winreg.KEY_SET_VALUE) as k:
            for name in (base, os.path.splitext(base)[0]):
                try:
                    winreg.DeleteValue(k, name)
                except OSError:
                    pass
    except OSError:
        pass


def is_autostart_enabled() -> bool:
    """开机自启是否生效：快捷方式在、且没被任务管理器禁用。"""
    if win_shell.shortcut_exists(startup_lnk()):
        return _startup_approved() is not False
    return legacy_autostart_enabled()       # 兼容旧版注册表项


def set_autostart(enabled: bool) -> bool:
    """开关开机自启。

    实现 = 「启动」文件夹里的快捷方式，这样任务管理器「启动」标签里
    能直接看到它（名字就是 电量托盘），并且可以在那里直接启用/禁用。
    """
    if enabled:
        ok = win_shell.create_shortcut(
            startup_lnk(), pythonw_path(),
            arguments=f'"{tray_script()}"',
            workdir=BASE_DIR,
            description="2.4G Device Battery Check：常驻显示 VGN 键盘 / 罗技鼠标电量")
        _set_startup_approved(True)
    else:
        _clear_startup_approved()
        ok = win_shell.remove_shortcut(startup_lnk()) or \
            not win_shell.shortcut_exists(startup_lnk())
    _clear_legacy_autostart()               # 迁移：清掉旧的注册表项
    return ok


def autostart_status() -> dict:
    """给界面/命令行看的自启状态摘要。"""
    lnk = startup_lnk()
    approved = _startup_approved()
    return {
        "enabled": is_autostart_enabled(),
        "method": "启动文件夹快捷方式（任务管理器可见）",
        "shortcut": lnk,
        "shortcut_exists": win_shell.shortcut_exists(lnk),
        "taskmanager_state": ("已启用" if approved is not False else "已被任务管理器禁用")
                              if approved is not None else "未记录（默认启用）",
        "target": autostart_command(),
        "legacy_registry": legacy_autostart_enabled(),
    }


# --------------------------------------------------------------------------
# 桌面快捷方式（只放**一个**入口，别把项目目录和两个 .pyw 都堆到桌面）
# --------------------------------------------------------------------------
DESKTOP_LINK_NAME = "2.4G设备电量"          # 桌面快捷方式显示名（不带 .lnk）
LAUNCHER_NAME = "2.4G设备电量.pyw"          # 项目里的启动脚本
# 旧版本会在桌面留下这些软链接/目录链接，安装时顺手清掉
LEGACY_DESKTOP_ITEMS = ["2.4G设备电量", "2.4G设备电量.pyw", "电量托盘.pyw"]


def desktop_dir() -> str:
    return win_shell.desktop_dir()


def desktop_link_path() -> str:
    return os.path.join(win_shell.desktop_dir(), DESKTOP_LINK_NAME + ".lnk")


def desktop_link_status() -> dict:
    """桌面入口现状：{'shortcut': (路径, 类型), 'exists': bool, 'legacy': [路径]}"""
    d = win_shell.desktop_dir()
    p = desktop_link_path()
    return {
        "shortcut": (p, win_link.link_kind(p)),
        "exists": os.path.isfile(p),
        "legacy": [os.path.join(d, n) for n in LEGACY_DESKTOP_ITEMS],
    }


def install_desktop_links() -> dict:
    """桌面上只留一个「2.4G设备电量」快捷方式。

    - 先清掉旧版本留下的目录链接 / 两个 .pyw 软链接（桌面整洁）
    - 再建一个 pythonw.exe + 启动脚本的 .lnk，双击直接开主界面
    返回 {'shortcut': 类型, 'removed': [名字], 'errors': [消息]}
    """
    d = win_shell.desktop_dir()
    out: dict = {"shortcut": "", "removed": [], "errors": []}

    for name in LEGACY_DESKTOP_ITEMS:
        p = os.path.join(d, name)
        try:
            kind = win_link.link_kind(p)
            if kind:                      # 只删链接，绝不碰同名的真目录/真文件
                if win_link.remove_link(p):
                    out["removed"].append(name)
        except Exception as e:
            out["errors"].append(f"{name}: {e}")

    built_exe = os.path.join(BASE_DIR, "dist", "2.4G设备电量.exe")
    if _IS_FROZEN:
        # 打包成 exe 后没有 .pyw 了，快捷方式直接指向 exe 自己
        target = sys.executable
        target_args = ""
        icon = app_icon_path() or sys.executable
    elif os.path.isfile(built_exe):
        # 源码目录里已经打包过：桌面入口指向 exe（双击不弹黑框、任务栏有图标）
        target = built_exe
        target_args = ""
        icon = app_icon_path() or built_exe
    else:
        target = os.path.join(BASE_DIR, LAUNCHER_NAME)
        if not os.path.isfile(target):
            out["errors"].append(f"项目里没有 {LAUNCHER_NAME}")
            return out
        target_args = f'"{target}"'
        icon = app_icon_path() or pythonw_path()
    try:
        ok = win_shell.create_shortcut(
            desktop_link_path(), target, target_args,
            workdir=BASE_DIR, description="2.4G Device Battery Check",
            icon=icon)
    except Exception as e:
        out["errors"].append(f"创建快捷方式失败：{e}")
        return out
    if ok:
        out["shortcut"] = "快捷方式"
    else:
        out["errors"].append("创建快捷方式失败（桌面可能被占用，重试一次）")
    return out


# --------------------------------------------------------------------------
# 进程间通信（窗口消息，双方都注册同名的 message）
# --------------------------------------------------------------------------
MSG_QUIT = "DeviceBatteryTray_Quit"       # 让托盘退出
MSG_RELOAD = "DeviceBatteryTray_Reload"   # 让托盘重读配置
MSG_SHOW = "DeviceBatteryTray_Show"       # 让托盘显示主界面（单进程/跨进程通用）


# 单实例锁句柄（全局，进程存活期间持有）
_MUTEX_HANDLE = None


def acquire_single_instance() -> bool:
    """获取单实例互斥锁。返回 True=本进程是第一个实例，False=已有实例在跑。

    拿到锁的句柄要一直持有到进程结束（否则锁会被系统回收，单实例失效）。
    """
    global _MUTEX_HANDLE
    handle = KERNEL32.CreateMutexW(None, False, MUTEX_NAME)
    _MUTEX_HANDLE = handle
    if not handle:
        return True
    return ctypes.get_last_error() != 183


def second_instance_notify() -> None:
    """第二个实例启动时：让已有实例把主界面显示到前台。"""
    if is_tray_running():
        post_to_tray(MSG_SHOW)


def tray_hwnd() -> int:
    h = USER32.FindWindowW(WND_CLASS, None)
    return int(h) if h else 0


def is_tray_running() -> bool:
    return tray_hwnd() != 0


def post_to_tray(msg_name: str) -> bool:
    hwnd = tray_hwnd()
    if not hwnd:
        return False
    msg = USER32.RegisterWindowMessageW(msg_name)
    return bool(USER32.PostMessageW(hwnd, msg, 0, 0))


def ui_hwnd() -> int:
    h = USER32.FindWindowW(None, UI_TITLE)
    return int(h) if h else 0


def start_tray_app(wait_timeout: float = 6.0) -> bool:
    """启动托盘程序；返回托盘窗口是否真的起来了。

    启动前先清理"没有窗口却还占着单实例"的僵尸进程，
    启动后等托盘窗口出现（pythonw 是静默进程，只能靠窗口判断成败）。
    """
    import subprocess
    if is_tray_running():
        return True
    cleanup_zombie()
    # 打包成 exe 之后没有 tray_app.py：直接再起一个 exe（它自带托盘线程）
    argv = ([sys.executable] if _IS_FROZEN
            else [pythonw_path(), os.path.join(BASE_DIR, "tray_app.py")])
    try:
        subprocess.Popen(argv, cwd=BASE_DIR, creationflags=0x00000008)
    except Exception:
        return False
    return wait_for_tray(wait_timeout)


# --------------------------------------------------------------------------
# 进程辅助：用于识别 / 清理僵尸托盘进程
# --------------------------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259

KERNEL32.OpenProcess.restype = ctypes.c_void_p
KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
KERNEL32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(wintypes.DWORD)]
KERNEL32.TerminateProcess.restype = wintypes.BOOL
KERNEL32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL
KERNEL32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]

_NTDLL = ctypes.WinDLL("ntdll")
_NTDLL.NtQueryInformationProcess.restype = ctypes.c_long
_NTDLL.NtQueryInformationProcess.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG)]
_PROCESS_COMMAND_LINE_INFORMATION = 60


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


def process_cmdline(pid: int) -> str:
    """取进程命令行（用于确认它确实是我们的托盘程序）。"""
    h = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = 4096
        buf = ctypes.create_string_buffer(size)
        need = wintypes.ULONG(0)
        st = _NTDLL.NtQueryInformationProcess(
            h, _PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(need))
        if st < 0 and need.value:
            size = int(need.value) + 16
            buf = ctypes.create_string_buffer(size)
            st = _NTDLL.NtQueryInformationProcess(
                h, _PROCESS_COMMAND_LINE_INFORMATION, buf, size,
                ctypes.byref(need))
        if st < 0:
            return ""
        us = _UNICODE_STRING.from_buffer(buf)
        if not us.Buffer or not us.Length:
            return ""
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    except Exception:
        return ""
    finally:
        KERNEL32.CloseHandle(h)


def write_pid(pid: Optional[int] = None) -> None:
    """把自己的 PID 写下来（进程意外卡死时，下一个实例能据此清掉它）。"""
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(pid if pid is not None else os.getpid()))
    except OSError:
        pass


def read_pid() -> int:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    h = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        if not KERNEL32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        KERNEL32.CloseHandle(h)


def process_image(pid: int) -> str:
    h = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if KERNEL32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        KERNEL32.CloseHandle(h)


def kill_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    h = KERNEL32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(KERNEL32.TerminateProcess(h, 1))
    finally:
        KERNEL32.CloseHandle(h)


def cleanup_zombie(wait: float = 2.0) -> bool:
    """清理僵尸托盘进程：上一个实例还活着（占着单实例锁），但窗口已经销毁。

    双重确认后才动手，避免 PID 复用误杀无辜进程：
      1) 镜像名以 "python" 开头；
      2) 命令行里确实有 tray_app。
    任一不满足就只清掉自己的 pid 文件，绝不碰那个进程。
    """
    import time
    pid = read_pid()
    if pid in (0, os.getpid()):
        return False
    if not pid_alive(pid):
        remove_pid()
        return False
    image = os.path.basename(process_image(pid)).lower()
    cmd = process_cmdline(pid).lower()
    if not (image.startswith("python") and "tray_app" in cmd):
        remove_pid()
        return False
    if not kill_pid(pid):
        return False
    deadline = time.time() + max(wait, 0.2)
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.05)
    remove_pid()
    return True


def wait_for_tray(timeout: float = 6.0) -> bool:
    """等托盘窗口出现（启动是否成功只能靠它判断）。"""
    import time
    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        if is_tray_running():
            return True
        time.sleep(0.15)
    return is_tray_running()
