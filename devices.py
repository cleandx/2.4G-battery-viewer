# -*- coding: utf-8 -*-
"""设备表 + 通用读取层。

界面只认「设备表」里的设备：`devices.json` 里每一条对应一个 HID 接口
（接收器或设备本体）。三个入口：

- `scan_devices()`  把当前插着的接口逐个试一遍，挑出**真能读到电量**的
- `read_all()`      按设备表读一遍，给主界面 / 托盘 / 弹窗用
- `add_spec()/remove_spec()`  设备表的增删（写回 devices.json）

这样别人拿到这份代码，先一键扫描，再决定添加哪些设备。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

import battery as bt
import hid_win as hw

_IS_FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = (os.path.dirname(sys.executable) if _IS_FROZEN
            else os.path.dirname(os.path.abspath(__file__)))
STORE_PATH = os.path.join(BASE_DIR, "devices.json")

_LOGITECH_VID = 0x046D
_VGN_VID = 0x320F
_VGN_USAGE_PAGE = 0xFF1C

_ASC = "ascii"


# ==========================================================================
# 读数
# ==========================================================================
@dataclass
class Reading:
    title: str
    subtitle: str = ""
    level: Optional[int] = None
    status: str = ""
    detail: str = ""
    error: str = ""
    link_state: str = ""      # "" 无连接 / "link" 2.4G 在线 / "sleep" 设备休眠
    estimated: bool = False    # 百分比由电压曲线换算（G502 这类只报 mV 的机型）
    charging: bool = False
    voltage_mv: Optional[int] = None
    charge_eta_min: Optional[int] = None   # 充电中：预计剩余分钟数（None=不显示）

    @property
    def ok(self) -> bool:
        return self.level is not None


# ==========================================================================
# 设备名 / 类型
# ==========================================================================
_NAME_NOISE = (
    " wireless gaming mouse", " wireless gaming keyboard", " wireless mouse",
    " wireless keyboard", " gaming mouse", " gaming keyboard", " mouse",
    " keyboard", " wireless headset", " headset", " 2.4g dongle", " dongle",
    " usb receiver", " receiver",
)


def short_name(name: str, limit: int = 24) -> str:
    """把设备自报的长名字压成卡片上放得下的型号，如 'G502 LIGHTSPEED'。"""
    out = (name or "").strip()
    low = out.lower()
    for suffix in _NAME_NOISE:
        if low.endswith(suffix):
            out = out[: -len(suffix)].strip()
            low = out.lower()
    return out[:limit]


def kind_of(*texts: str) -> str:
    """从名字/产品串猜设备类型：keyboard / mouse / headset / device。"""
    s = " ".join(t for t in texts if t).lower()
    if any(k in s for k in ("keyboard", "键盘", "s99", "keychron", "wooting")):
        return "keyboard"
    if any(k in s for k in ("mouse", "鼠标", "g502", "g304", "g pro",
                            "superlight", "g903", "g703")):
        return "mouse"
    if any(k in s for k in ("headset", "耳机", "g733", "g435")):
        return "headset"
    return "device"


# ==========================================================================
# spec（设备表条目）
# ==========================================================================
def key_of(d: hw.HidInterface) -> str:
    return (f"{d.vid:04X}:{d.pid:04X}:{d.usage_page:04X}:"
            f"{d.usage:04X}:{d.output_len}")


def spec_of(d: hw.HidInterface, name: str = "") -> dict:
    return {
        "key": key_of(d),
        "vid": int(d.vid), "pid": int(d.pid),
        "usage_page": int(d.usage_page), "usage": int(d.usage),
        "output_len": int(d.output_len),
        "name": name or short_name(d.product or "") or d.vid_pid,
    }


def find_iface(spec: dict) -> Optional[hw.HidInterface]:
    """按设备表条目找当前插着的接口。

    先按完整 key 精确匹配；找不到时退一步只比 VID/PID/UsagePage/Usage
    （换了个 USB 口 output_len 一般不变，但顺序可能变，所以兜底很必要）。
    """
    key = spec.get("key") or ""
    loose = (spec.get("vid"), spec.get("pid"),
             spec.get("usage_page"), spec.get("usage"))
    same = None
    for d in hw.enumerate_hid():
        if key_of(d) == key:
            return d
        if same is None and (d.vid, d.pid, d.usage_page, d.usage) == loose:
            same = d
    return same


# ==========================================================================
# 设备表读写
# ==========================================================================
def load_specs() -> list:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    items = data.get("devices") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    seen = set()
    for it in items:
        if not isinstance(it, dict) or not it.get("key"):
            continue
        if it["key"] in seen:
            continue
        seen.add(it["key"])
        out.append(it)
    return out


def save_specs(specs: list) -> None:
    data = {"version": 1, "devices": list(specs)}
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def add_spec(spec: dict) -> list:
    specs = load_specs()
    if not any(s.get("key") == spec.get("key") for s in specs):
        specs.append(dict(spec))
        save_specs(specs)
    return specs


def remove_spec(key: str) -> list:
    specs = [s for s in load_specs() if s.get("key") != key]
    save_specs(specs)
    return specs


# ==========================================================================
# 单接口试读
# ==========================================================================
def _probe_iface(iface: hw.HidInterface) -> Optional[bt.BatteryResult]:
    """按「机型专属协议 → 厂商协议表 → 标准 HID 电量字段」的顺序试。

    返回值：None = 这个接口根本不像是电量接口；BatteryResult = 试过了
    （ok=False 也返回，用来给界面显示「休眠 / 没回复」这类原因）。
    """
    vid, up = iface.vid, iface.usage_page
    if (vid == _VGN_VID and up == _VGN_USAGE_PAGE and iface.usage == 0x0092
            and iface.output_len == 64):
        return bt.strategy_vgn_s99(iface)
    if vid == _LOGITECH_VID and up == 0xFF00 and iface.output_len in (7, 20):
        return bt.strategy_logitech_hidpp(iface)

    if iface.output_len:
        res = bt.strategy_vendor_protocol(iface)   # 表里没这个 VID 时立刻返回 None
        if res is not None and res.ok:
            return res
    else:
        res = None

    if iface.feature_len:
        std = bt.strategy_standard_battery_usage(iface)
        if std is not None and std.ok:
            return std
        if res is None:
            res = std
    return res


def _reading_from(iface: hw.HidInterface, res: bt.BatteryResult,
                  spec: Optional[dict] = None) -> Reading:
    """把 BatteryResult 翻译成界面用的 Reading。"""
    extra = res.extra or {}
    name = short_name(extra.get("device_name") or "")
    title = name or (spec or {}).get("name") or short_name(iface.product or "")
    if not title:
        title = iface.vid_pid
    r = Reading(title)
    mv = extra.get("voltage_mv") or res.voltage_mv
    r.voltage_mv = mv
    if mv:
        r.subtitle = f"{iface.vid_pid} · {mv / 1000:.2f} V"
        r.estimated = True
    else:
        r.subtitle = f"{iface.vid_pid} · {iface.product or '2.4G'}"
    r.level = res.level
    r.status = res.status_text
    r.detail = res.source
    r.charging = res.status in ("charging", "full")
    r.link_state = "link"
    r.error = "" if res.ok else (res.detail or "读取失败")
    return r


# ==========================================================================
# 充电剩余时间估计
# ==========================================================================
# 设备不直接上报充电功率，而且不少键盘（如 VGN S99）充电时计量芯片不实时
# 采样，电量读数会长时间停在同一个值。所以用「倒计时」模型：
#   充电开始时按名义充满时长（键盘 150 分钟 / 鼠标 120 分钟）折算总时长，
#   之后每分钟递减；电量读数一旦上涨，立刻按新电平重新起算——既不会像
#   实测速率外推那样被稀疏的跳变带偏，也不会永远冻结在同一个数。
_NOMINAL_FULL_MIN = {"keyboard": 150, "mouse": 120, "headset": 120, "device": 120}
_CHARGE_PREV: dict = {}          # spec key -> 上一次充电中的电量读数
_CHARGE_START: dict = {}         # spec key -> [起算时刻 monotonic, 起算电平]


def _charge_eta(key: str, kind: str, level: Optional[int],
                charging: bool, full: bool) -> Optional[int]:
    """估计充电剩余分钟数。None = 不显示（没在充电/读不到电量）。"""
    if full or not charging or level is None:
        _CHARGE_PREV.pop(key, None)
        _CHARGE_START.pop(key, None)
        return None
    now = time.monotonic()
    prev = _CHARGE_PREV.get(key)
    if prev is not None and level < prev:
        _CHARGE_START.pop(key, None)         # 充电中电量回落 = 拔过线：重新起算
    _CHARGE_PREV[key] = level
    st = _CHARGE_START.get(key)
    if st is None or level > st[1]:          # 首次样本 / 电量上涨：重新起算
        st = _CHARGE_START[key] = [now, level]
    nominal = _NOMINAL_FULL_MIN.get(kind, 120)
    total = nominal * (100.0 - st[1]) / 100.0
    remain = total - (now - st[0]) / 60.0
    return max(3, min(599, round(remain)))


def read_spec(spec: dict) -> Reading:
    """读一个设备表条目。设备不在、休眠、读不到都给明确的 Reading.error。"""
    label = (spec or {}).get("name") or "设备"
    key = (spec or {}).get("key") or ""
    iface = find_iface(spec)
    if iface is None:
        _CHARGE_HIST.pop(key, None)              # 接收器都不在了，充电历史作废
        r = Reading(label)
        r.error = "没插上（找不到这个接收器）"
        r.detail = key
        return r
    try:
        res = _probe_iface(iface)
    except Exception as e:                       # 设备被拔 / 驱动异常
        r = Reading(label)
        r.error = f"读取出错：{e}"
        r.detail = key
        return r
    if res is None:
        r = Reading(label)
        r.subtitle = f"{iface.vid_pid} · {iface.product or '2.4G'}"
        r.error = "没有可用的读取方式"
        r.detail = key
        return r
    if res.ok:
        r = _reading_from(iface, res, spec)
        kind = spec.get("kind") or kind_of(r.title, iface.product)
        r.charge_eta_min = _charge_eta(key, kind, r.level, r.charging,
                                       res.status == "full")
        return r
    r = Reading(label)
    r.subtitle = f"{iface.vid_pid} · {iface.product or '2.4G'}"
    r.error = res.detail or "读取失败"
    r.detail = key
    if "没有回复" in (res.detail or "") or "超时" in (res.detail or ""):
        r.link_state = "sleep"
    return r


def read_all(specs: Optional[list] = None) -> list:
    """按设备表读一遍（顺序与设备表一致）。"""
    items = load_specs() if specs is None else specs
    out = []
    for spec in items:
        try:
            out.append(read_spec(spec))
        except Exception as e:                   # 兜底：绝不让一条设备炸掉整轮
            r = Reading(spec.get("name") or "设备")
            r.error = f"读取出错：{e}"
            out.append(r)
    return out


# ==========================================================================
# 扫描
# ==========================================================================
# 扫描时发现的「像 2.4G 接收器、但协议暂不支持」的设备（给界面展示用）。
# 例如 DaiLing G3 耳机：接收器插着、能看到厂商接口，但电量协议未破解。
_UNSUPPORTED_SEEN: list = []


def unsupported_seen() -> list:
    """上次扫描里检测到、但读不出电量的设备名（去重）。"""
    return list(_UNSUPPORTED_SEEN)


def _looks_like_dongle(d: hw.HidInterface) -> bool:
    """像「2.4G 接收器/无线设备」的厂商接口：厂商页或音频消费者页，带收发通道。"""
    prod = (d.product or "").strip()
    if not prod or "virtual" in prod.lower():
        return False
    if d.usage_page >= 0xFF00 or d.usage_page == 0x000C:
        return d.output_len in (32, 64) or d.feature_len >= 8
    return False


def _candidate_ifaces() -> list:
    """挑出值得一试的接口。

    - 没有输出端点也没有 Feature 端点的接口读不了电量，直接跳过；
    - 罗技 32 字节那条是固件更新通道（写进去必然失败），跳过；
      有 20 字节长报文接口时，7 字节那条也不用试（它答不了配对设备）。
    """
    ifs = hw.enumerate_hid()
    long_logi = any(d.vid == _LOGITECH_VID and d.usage_page == 0xFF00
                    and d.output_len == 20 for d in ifs)
    out = []
    for d in ifs:
        if not d.output_len and not d.feature_len:
            continue
        if d.vid == _LOGITECH_VID and d.usage_page == 0xFF00:
            if d.output_len not in (7, 20):
                continue
            if d.output_len == 7 and long_logi:
                continue
        out.append(d)
    # 厂商页（>=0xFF00）的接口最可能带私有电量协议，排前面先试
    out.sort(key=lambda d: (0 if d.usage_page >= 0xFF00 else 1,
                            d.vid, d.pid, d.usage))
    return out


def scan_devices(progress: Optional[Callable] = None,
                 should_stop: Optional[Callable] = None) -> list:
    """扫描本机能读到电量的设备。

    返回 [(spec, Reading), ...]，只包含**真的读到数**的设备。
    progress(done, total, label) 用来更新界面上的进度文字。
    """
    ifaces = _candidate_ifaces()
    total = len(ifaces)
    found = []
    seen = set()
    unsupported: list = []
    for i, iface in enumerate(ifaces):
        if should_stop is not None and should_stop():
            break
        if progress is not None:
            label = short_name(iface.product or "") or iface.vid_pid
            try:
                progress(i, total, label)
            except Exception:
                pass
        try:
            res = _probe_iface(iface)
        except Exception:
            res = None
        if res is None or not res.ok:
            # 读不出来但长得就像 2.4G 接收器：记下来，界面里告诉用户
            # 「检测到了，但这个型号的电量协议暂不支持」
            if _looks_like_dongle(iface):
                name = short_name(iface.product or "") or iface.vid_pid
                if name not in unsupported:
                    unsupported.append(name)
            continue
        spec = spec_of(iface)
        reading = _reading_from(iface, res)
        spec["name"] = reading.title
        spec["kind"] = kind_of(reading.title, iface.product)
        key = spec["key"]
        if key in seen:
            continue
        seen.add(key)
        found.append((spec, reading))
    if progress is not None:
        try:
            progress(total, total, "")
        except Exception:
            pass
    # 键盘排前面（和界面习惯一致），其次是鼠标、其它设备
    order = {"keyboard": 0, "mouse": 1, "headset": 2, "device": 3}
    found.sort(key=lambda p: (order.get(devices_kind(p[0]), 9),
                              p[1].title.lower()))
    _UNSUPPORTED_SEEN.clear()
    _UNSUPPORTED_SEEN.extend(unsupported[:6])
    return found


def devices_kind(spec: dict) -> str:
    """兼容旧数据：设备表里没存 kind 时按名字猜。"""
    return spec.get("kind") or kind_of(spec.get("name", ""))
