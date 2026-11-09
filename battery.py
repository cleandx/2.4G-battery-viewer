# -*- coding: utf-8 -*-
"""
battery.py —— 电量读取核心

对每个 HID 接口依次尝试多条策略，第一条成功的即可用：

  A. 标准 HID 电量字段（Usage Page 0x0006 / Usage 0x0020 Battery Strength，
     以及数字化仪页 0x000D/0x003B、电池系统页 0x0085 等）—— 读 Feature Report
  B. 已收录的厂商协议（Endgame Gear / VAXEE / Nordic nRF ...）
  C. 罗技 HID++ 2.0（Feature 0x1000 / 0x1004 / 0x1001）
  D. 用户自定义协议（protocols_user.json，由界面上的“手动标定”生成）
  E. 监听 Input Report 里上报的电量字段
  F. 原始 Feature Report 全量转储（给人看的，配合手动标定）
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import hid_win as hw
from hid_win import HidDevice, HidError, HidInterface, HidP_Feature, HidP_Input

# 单进程模式下，GUI 与托盘可能同时触发读取；同一 HID 设备不能并发收发，
# 用一把全局锁串行化所有 HID 电量读取（避免句柄冲突导致进程崩溃）。
_HID_READ_LOCK = threading.Lock()

# 罗技 feature 索引表进程级缓存：设备的 feature 表是固定的，
# 首次查询后缓存 (path, rid, index, feature_id) -> feature_index（None 表示不支持）。
# 关键收益：Lightspeed 接收器对"不支持的 feature"（如 0x1000）响应极慢（实测 ~1.1s），
# 若不缓存，每次读电量都要为它白等近 1 秒。
_HIDPP_FEATURE_CACHE: dict = {}
# 设备名（0x0005）也是固定值，缓存住省两三次往返。
_HIDPP_NAME_CACHE: dict = {}
# 上一次 ping 通的设备索引（接收器一般固定是 1 号）：下一次先探它。
_HIDPP_LIVE_INDEX: dict = {}

USER_PROTOCOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "protocols_user.json")


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------
STATUS_TEXT = {
    "discharging": "放电中",
    "charging": "充电中",
    "full": "已充满",
    "unknown": "未知",
}


@dataclass
class BatteryResult:
    ok: bool
    level: Optional[int] = None           # 0-100
    raw_level: Optional[int] = None       # 原始数值
    status: str = "unknown"
    voltage_mv: Optional[int] = None
    source: str = ""                      # 命中的策略
    detail: str = ""
    raw: bytes = b""
    extra: dict = field(default_factory=dict)

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status or "未知")


def _scale(value: int, logical_max: int = 100) -> int:
    """把原始数值换算成 0-100。"""
    if value is None:
        return None
    if logical_max and logical_max > 0 and logical_max != 100:
        if logical_max == 0xFF and value > 100:
            return max(0, min(100, round(value * 100 / 255)))
        return max(0, min(100, round(value * 100 / logical_max)))
    if value > 100:
        return max(0, min(100, round(value * 100 / 255)))
    return max(0, value)


# --------------------------------------------------------------------------
# 厂商协议表（均来自公开的协议文档，见文件末尾注释）
# --------------------------------------------------------------------------
@dataclass
class ProtocolSpec:
    name: str
    vid: int
    usage_page: int
    report_id: int
    payload: list                       # 紧跟 report id 之后的请求字节
    length: int                         # 报文总长度（含 report id）
    level_offset: int                   # 从报文第 0 字节算起的电量字节下标
    wait_ms: int = 120
    scale: int = 1                      # level = raw * scale
    charging_offset: Optional[int] = None
    charging_nonzero: bool = True
    voltage_offset: Optional[int] = None  # 小端 16 位
    level_max: Optional[int] = None       # 超过该值视为无效
    mode: str = ""                        # "feature" / "output"，留空则自动判断
    expect: tuple = ()                    # ((字节下标, 期望值), ...) 用于校验回复
    retries: int = 2                      # 重试次数（第一次常被当作唤醒）
    notes: str = ""

    @property
    def kind(self) -> str:
        return self.mode or "feature"


BUILTIN_PROTOCOLS: list[ProtocolSpec] = [
    # ---- ATK / VXE / VGN 无线鼠标（VID 0x373B 为 ATK，0x3554 为 VXE/VGN）----
    # 协议 2：64 字节 Output Report，Report ID 0x08，命令 0x72；Nordic 54L15
    ProtocolSpec(
        name="ATK / VXE 无线鼠标（协议 2）",
        vid=0x373B, usage_page=0xFF05, report_id=0x08,
        payload=[0x7D, 0x72, 0x02, 0x00, 0x01, 0x07, 0x01],
        length=64, level_offset=7, wait_ms=100, scale=1, level_max=100,
        mode="output", expect=((1, 0x72), (5, 0x07)), retries=3,
        notes="电量在回复第 7 字节；第 1 字节固定 0x72 为有效回复标志",
    ),
    # 协议 1：17 字节 Output Report，Report ID 0x08；Nordic 52840
    ProtocolSpec(
        name="ATK / VXE / VGN 无线鼠标（协议 1）",
        vid=0x373B, usage_page=0xFF02, report_id=0x08,
        payload=[0x04] + [0x00] * 15 + [0x49],
        length=17, level_offset=6, wait_ms=100, scale=1, level_max=100,
        mode="output", expect=(), retries=3,
        notes="电量在回复第 6 字节，第 7 字节非 0 表示有线连接",
    ),
    ProtocolSpec(
        name="VXE / VGN 无线鼠标（协议 1）",
        vid=0x3554, usage_page=0xFF02, report_id=0x08,
        payload=[0x04] + [0x00] * 15 + [0x49],
        length=17, level_offset=6, wait_ms=100, scale=1, level_max=100,
        mode="output", expect=(), retries=3,
        notes="VXE R1 Pro Max / R1 SE+ / VGN F1 Pro",
    ),
    # Endgame Gear：VID 0x3367，usage page 0xFF01，Report ID 0xA1，64 字节 Feature
    ProtocolSpec(
        name="Endgame Gear（XM2w / OP1W / 无线接收器）",
        vid=0x3367, usage_page=0xFF01, report_id=0xA1,
        payload=[0xB4], length=64,
        level_offset=16, wait_ms=350, scale=1,
        voltage_offset=17, mode="feature",
        expect=((1, 0x01),), retries=3,
        notes="byte1 状态：0x01 有效 / 0x03 忙 / 0x07 不支持 / 0x08 鼠标休眠；"
              "需连读两次，第一次是唤醒",
    ),
    # VAXEE / ZYGEN：VID 0x3057，usage page 0xFF05，Report ID 0x0E，64 字节 Feature
    ProtocolSpec(
        name="VAXEE / ZYGEN（含 4K 接收器）",
        vid=0x3057, usage_page=0xFF05, report_id=0x0E,
        payload=[0xA5, 0x0B, 0x01, 0x01], length=64,
        level_offset=5, wait_ms=120, scale=5, level_max=20, mode="feature",
        notes="以 5% 为步长上报；充电状态用命令 0x10",
    ),
    # Nordic nRF 方案（VID 0x1915）：usage page 0xFFA0，Feature Report 5，32 字节
    ProtocolSpec(
        name="Nordic nRF 方案 2.4G 接收器",
        vid=0x1915, usage_page=0xFFA0, report_id=0x05,
        payload=[0x15, 0x00, 0x00, 0x01], length=32,
        level_offset=9, wait_ms=90, scale=1, mode="feature",
        notes="请求 0x15 读取电量",
    ),
]


def load_user_protocols() -> list[ProtocolSpec]:
    """读取用户通过“手动标定”保存的协议。"""
    if not os.path.exists(USER_PROTOCOL_FILE):
        return []
    try:
        with open(USER_PROTOCOL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        try:
            out.append(ProtocolSpec(
                name=item.get("name", "自定义协议"),
                vid=int(str(item["vid"]), 16) if isinstance(item["vid"], str) else int(item["vid"]),
                usage_page=int(str(item["usage_page"]), 16) if isinstance(item["usage_page"], str) else int(item["usage_page"]),
                report_id=int(str(item["report_id"]), 16) if isinstance(item["report_id"], str) else int(item["report_id"]),
                payload=list(item.get("payload", [])),
                length=int(item.get("length", 64)),
                level_offset=int(item.get("level_offset", 0)),
                wait_ms=int(item.get("wait_ms", 150)),
                scale=int(item.get("scale", 1)),
                charging_offset=item.get("charging_offset"),
                voltage_offset=item.get("voltage_offset"),
                level_max=item.get("level_max"),
                notes=item.get("notes", "用户自定义"),
            ))
        except Exception:
            continue
    return out


def save_user_protocol(spec: ProtocolSpec) -> None:
    raw = []
    if os.path.exists(USER_PROTOCOL_FILE):
        try:
            with open(USER_PROTOCOL_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = []
    raw.append({
        "name": spec.name,
        "vid": f"{spec.vid:04X}",
        "usage_page": f"{spec.usage_page:04X}",
        "report_id": f"{spec.report_id:02X}",
        "payload": list(spec.payload),
        "length": spec.length,
        "level_offset": spec.level_offset,
        "wait_ms": spec.wait_ms,
        "scale": spec.scale,
        "charging_offset": spec.charging_offset,
        "voltage_offset": spec.voltage_offset,
        "level_max": spec.level_max,
        "notes": spec.notes,
    })
    with open(USER_PROTOCOL_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def all_protocols() -> list[ProtocolSpec]:
    return BUILTIN_PROTOCOLS + load_user_protocols()


# --------------------------------------------------------------------------
# 策略 A：标准 HID 电量字段
# --------------------------------------------------------------------------
def strategy_standard_battery_usage(iface: HidInterface) -> Optional[BatteryResult]:
    if not iface.feature_len:
        return None
    try:
        with HidDevice(iface.path, write=False) as dev:
            caps_list = dev.value_caps(HidP_Feature)
            for cap in caps_list:
                key = (cap.UsagePage, cap.u.NotRange.Usage)
                match = next((b for b in hw.BATTERY_USAGES if b[0] == key[0] and b[1] == key[1]), None)
                if not match:
                    continue
                try:
                    report = dev.get_feature_report(cap.ReportID)
                except HidError as e:
                    return BatteryResult(
                        ok=False, source="标准 HID 电量字段",
                        detail=f"接口声明了 {match[2]}，但读取 Feature Report "
                               f"0x{cap.ReportID:02X} 失败：{e}",
                    )
                value = dev.usage_value(HidP_Feature, report, cap)
                if value is None:
                    continue
                level = _scale(value, cap.LogicalMax)
                return BatteryResult(
                    ok=True, level=level, raw_level=value,
                    status="unknown",
                    source=f"标准 HID 电量字段（{match[2]}）",
                    detail=f"UsagePage=0x{cap.UsagePage:04X} Usage=0x{cap.u.NotRange.Usage:04X} "
                           f"ReportID=0x{cap.ReportID:02X} 原始值={value} 上限={cap.LogicalMax}",
                    raw=report,
                )
    except HidError as e:
        return BatteryResult(ok=False, source="标准 HID 电量字段", detail=str(e))
    return None


# --------------------------------------------------------------------------
# 策略 A3：VGN S99 2.4G 键盘（Telink 方案）—— 逆向 VGN HUB 前端 JS 得到
# --------------------------------------------------------------------------
_VGN_REQ_CODE = 0


def _vgn_next_req() -> int:
    """requestCode 按 1..65535 递增（对应 VGN HUB 前端 rs 类的行为）。"""
    global _VGN_REQ_CODE
    _VGN_REQ_CODE = (_VGN_REQ_CODE + 1) & 0xFFFF
    if _VGN_REQ_CODE == 0:
        _VGN_REQ_CODE = 1
    return _VGN_REQ_CODE


def strategy_vgn_s99(iface: HidInterface) -> Optional[BatteryResult]:
    """VGN S99 2.4G 键盘电量（0x320F:0x5088，厂商接口 0xFF1C:0x0092）。

    协议来自 VGN HUB 前端 dist\\index-80cadad4.js 静态还原：
      frame[0..1] = requestCode（小端 uint16，递增）
      frame[2]    = commandId（0x1A = power_info）
      frame[7]    = 电量（0-100，超过 100 截断为 100）
      frame[8]    = 充电标志（非 0 = 充电中）
    通过 Output Report（Report ID 0x04，64 字节）发送，再收 Input Report。
    """
    if not (iface.vid == 0x320F and iface.usage_page == 0xFF1C
            and iface.usage == 0x0092 and iface.output_len == 64):
        return None
    with _HID_READ_LOCK:
        return _strategy_vgn_s99_locked(iface)


def _strategy_vgn_s99_locked(iface: HidInterface) -> Optional[BatteryResult]:
    """strategy_vgn_s99 的加锁实现（调用方需已持有 _HID_READ_LOCK）。"""
    try:
        with HidDevice(iface.path, write=True) as dev:
            # 键盘深度休眠时，前几条命令都只回 ACK 帧（cmd=0xFF），
            # 实测最多要连戳 5~6 次才回真正的数据帧。这里给足上限，
            # 每次收到 ACK 立即重发（requestCode 递增），直到拿到数据帧。
            for _ in range(10):
                req = _vgn_next_req()
                frame = bytearray(63)
                frame[0] = req & 0xFF
                frame[1] = (req >> 8) & 0xFF
                frame[2] = 0x1A                       # power_info
                out = bytes([0x04]) + bytes(frame)
                if len(out) < 64:
                    out = out + b"\x00" * (64 - len(out))
                try:
                    # 该接口必须走 WriteFile 直写；HidD_SetOutputReport 无响应
                    dev.write_output(out, use_hidd=False)
                except HidError as e:
                    return BatteryResult(ok=False, source="VGN S99 键盘协议",
                                         detail=f"发送失败：{e}")
                # 发完循环收 Input Report：跳过 ACK 帧（cmd=0xFF），等真正的数据帧
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    resp = dev.read_input(timeout_ms=200)
                    if not resp or len(resp) < 9:
                        continue
                    if resp[0] != 0x04:
                        continue
                    if resp[3] == 0xFF:               # ACK 帧：唤醒完成，重发一次
                        break
                    if resp[3] != 0x1A:               # 不是 power_info 的回复，继续等
                        continue
                    if resp[1] | (resp[2] << 8) != req:  # requestCode 要匹配本次请求
                        continue
                    raw = resp[8]
                    raw = raw if raw <= 100 else 100
                    # 直接按设备上报的实际电量显示，不做取整。
                    power = raw
                    charging = resp[9] != 0
                    status = "charging" if charging else "discharging"
                    return BatteryResult(
                        ok=True, level=power, raw_level=raw, status=status,
                        source="VGN S99 键盘协议（power_info 0x1A）",
                        detail=f"ReportID=0x04 电量字节@8=0x{raw:02X}（实际电量 {power}%） "
                               f"充电标志@9=0x{resp[9]:02X}",
                        raw=resp,
                    )
            return BatteryResult(ok=False, source="VGN S99 键盘协议",
                                 detail="键盘没有回复（确认接收器已插好、键盘已开机）")
    except HidError as e:
        return BatteryResult(ok=False, source="VGN S99 键盘协议",
                             detail=f"无法打开厂商接口：{e}")


# --------------------------------------------------------------------------
# 策略 B / D：厂商协议（含用户自定义）
# --------------------------------------------------------------------------
def _matches(report: bytes, expect: tuple) -> bool:
    return bool(report) and all(
        len(report) > off and report[off] == val for off, val in expect
    )


def _read_report_after_request(dev: HidDevice, spec: ProtocolSpec,
                               kind: Optional[str] = None) -> bytes:
    """按协议发请求、等回复、再读回报文。

    kind="feature"：发 Feature Report，直接读回 Feature Report。
    kind="output" ：发 Output Report，然后等 Input Report；
                    有 expect 时只接受符合特征的回复，否则拿最后一条。
    """
    kind = kind or spec.kind
    body = ([spec.report_id] + list(spec.payload) + [0] * spec.length)[: spec.length]
    last = b""
    for _ in range(max(1, spec.retries)):
        dev.drain()
        if kind == "feature":
            dev.set_feature_report(bytes(body))
            time.sleep(spec.wait_ms / 1000.0)
            last = dev.get_feature_report(spec.report_id, spec.length)
            if not spec.expect or _matches(last, spec.expect):
                return last
            continue
        dev.write_output(bytes(body))
        time.sleep(spec.wait_ms / 1000.0)
        end = time.monotonic() + 0.6
        while time.monotonic() < end:
            r = dev.read_input(timeout_ms=150)
            if not r:
                continue
            last = r
            if not spec.expect or _matches(r, spec.expect):
                return r
    return last


def _parse_spec_response(spec: ProtocolSpec, report: bytes, iface: HidInterface,
                         kind: str) -> BatteryResult:
    if len(report) <= spec.level_offset:
        return BatteryResult(ok=False, source=spec.name,
                             detail=f"回复长度不足（{len(report)} 字节）", raw=report)
    raw_level = report[spec.level_offset]
    if spec.level_max is not None and raw_level > spec.level_max:
        return BatteryResult(ok=False, source=spec.name,
                             detail=f"电量字节 0x{raw_level:02X} 超出预期范围", raw=report)
    level = max(0, min(100, raw_level * spec.scale))

    status = "unknown"
    if spec.charging_offset is not None and len(report) > spec.charging_offset:
        v = report[spec.charging_offset]
        if spec.charging_nonzero:
            status = "charging" if v else "discharging"
        else:
            status = "discharging" if v else "charging"

    voltage = None
    if spec.voltage_offset is not None and len(report) > spec.voltage_offset + 1:
        voltage = report[spec.voltage_offset] | (report[spec.voltage_offset + 1] << 8)
        if not (2500 <= voltage <= 5000):
            voltage = None

    return BatteryResult(
        ok=True, level=level, raw_level=raw_level, status=status,
        voltage_mv=voltage, source=spec.name,
        detail=f"ReportID=0x{spec.report_id:02X} 电量字节@{spec.level_offset}=0x{raw_level:02X}"
               f" ×{spec.scale}",
        raw=report,
    )


def strategy_vendor_protocol(iface: HidInterface) -> Optional[BatteryResult]:
    specs = [s for s in all_protocols()
             if s.vid == iface.vid and s.usage_page == iface.usage_page]
    if not specs:
        return None
    last = None
    for spec in specs:
        kind = spec.mode or ("feature" if iface.feature_len else "output")
        try:
            with HidDevice(iface.path, write=True) as dev:
                report = _read_report_after_request(dev, spec, kind)
                if not report:
                    last = BatteryResult(ok=False, source=spec.name,
                                         detail="设备没有回复（多半是外设正在休眠，"
                                                "动一下鼠标/开机后重试）")
                    continue
                res = _parse_spec_response(spec, report, iface, kind)
                if res.ok:
                    return res
                last = res
        except HidError as e:
            last = BatteryResult(ok=False, source=spec.name,
                                 detail=f"收发失败：{e}（可能是权限被占用，"
                                        f"请关闭厂商驱动软件后重试）")
    return last


# --------------------------------------------------------------------------
# 策略 C：罗技 HID++ 2.0
# --------------------------------------------------------------------------
HIDPP_DEVICE_INDICES = [1, 2, 3, 4, 5, 6, 0xFF]

_FEATURE_ROOT = 0x0000
_FEATURE_DEVICE_NAME = 0x0005
_FEATURE_BATTERY_STATUS = 0x1000
_FEATURE_BATTERY_VOLTAGE = 0x1001
_FEATURE_UNIFIED_BATTERY = 0x1004

# 单节锂聚合物电池的 毫伏 → 电量 曲线。
# G502 LIGHTSPEED / G903 LIGHTSPEED / G915 这类设备**没有** 0x1000 和 0x1004，
# 只提供 0x1001 电池电压（大端毫伏）+ 一个标志字节，百分比必须由这条曲线换算。
# 端点与 OpenLogi 的实现一致：3.50V 记 0%，4.186V 记 100%。
# 想微调手感只改这张表就行，不用动逻辑。
VOLTAGE_CURVE = [
    (3500, 0), (3550, 3), (3600, 8), (3650, 15), (3700, 24),
    (3750, 35), (3800, 46), (3850, 57), (3900, 68), (3950, 78),
    (4000, 86), (4100, 95), (4186, 100),
]


def voltage_to_percent(mv: Optional[int]) -> Optional[int]:
    """把电池电压（毫伏）换算成 0-100 的估算电量。"""
    if not mv:
        return None
    if mv <= VOLTAGE_CURVE[0][0]:
        return 0
    if mv >= VOLTAGE_CURVE[-1][0]:
        return 100
    for (mv0, p0), (mv1, p1) in zip(VOLTAGE_CURVE, VOLTAGE_CURVE[1:]):
        if mv0 <= mv <= mv1:
            return int(round(p0 + (p1 - p0) * (mv - mv0) / (mv1 - mv0)))
    return None

_HIDPP_STATUS = {
    0: ("discharging", "放电中"),
    1: ("charging", "充电中"),
    2: ("charging", "充电中（末段）"),
    3: ("full", "已充满"),
    4: ("charging", "充电中（慢速）"),
    5: ("unknown", "电池类型无效"),
    6: ("unknown", "温度异常"),
    7: ("unknown", "充电异常"),
}


def _hidpp_unified_status(st: int) -> tuple[str, str]:
    """0x1004 统一电池的状态字节是枚举，和 0x1000 电池状态共用同一套 BatteryStatus。

    权威定义（Solaar logitech_receiver/common.py BatteryStatus）：
      0x00 = 放电中，0x01 = 充电中，0x02 = 快充满，0x03 = 已充满，
      0x04 = 慢速充电，0x05 = 电池类型无效，0x06 = 温度异常。
    """
    return _HIDPP_STATUS.get(st, ("unknown", f"状态 {st}"))


def _hidpp_error(report: bytes) -> Optional[int]:
    """HID++ 出错时回包第 2 字节（feature index 位）会置 bit7，错误码在第 4 字节。

    原来判断的是第 3 字节，那是「function<<4 | swid」，正常回包也带 swid，
    于是所有正常帧都被当成错误 —— G502 因此一个数都读不出来。
    """
    if len(report) < 5:
        return None
    if report[2] & 0x80:
        return report[4]
    return None


def _hidpp_voltage_status(flags: int) -> tuple[str, str]:
    """0x1001 的标志字节：bit7=接着外部电源，bit0-2=充电状态，bit5=电量告急。"""
    if not (flags & 0x80):
        return ("discharging", "放电中")
    low = flags & 0x07
    if low == 0:
        return ("charging", "充电中")
    if low == 1:
        return ("full", "已充满")
    return ("charging", "已接电源")


def _hidpp_call(dev: HidDevice, rid: int, index: int, feature_index: int,
                function: int, params: list, timeout_ms: int = 260,
                total_len: Optional[int] = None, retries: int = 3) -> Optional[bytes]:
    body = [rid, index, feature_index, ((function & 0x0F) << 4) | 0x01] + list(params)
    length = total_len or (20 if rid == 0x11 else 7)
    body = (body + [0] * length)[:length]
    # 罗技接收器常需要先"唤醒"：第一次命令往往不回，重发几次才能拿到数据。
    # 同时每次写之前 drain 掉上一次命令的延迟回复，避免读到错位帧。
    for attempt in range(max(1, retries)):
        try:
            dev.drain()
            dev.write_output(bytes(body))
        except HidError:
            return None
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            report = dev.read_input(
                timeout_ms=int(min(0.15, deadline - time.monotonic()) * 1000) or 1)
            if not report or len(report) < 4:
                continue                       # 超时/短帧继续等，别提前退出
            if report[0] != rid or report[1] != index or report[2] != feature_index:
                continue
            if ((report[3] >> 4) & 0x0F) != function:
                continue
            return report
    return None


def _hidpp_feature_index(dev: HidDevice, rid: int, index: int, feature_id: int,
                         length: Optional[int] = None) -> Optional[int]:
    # 进程级缓存：feature 表固定，不支持的 feature 查一次就够了，
    # 避免每次为 0x1000 这类不支持项白等（接收器实测极慢）。
    cache_key = (dev.path, rid, index, feature_id)
    if cache_key in _HIDPP_FEATURE_CACHE:
        return _HIDPP_FEATURE_CACHE[cache_key]

    reply = _hidpp_call(dev, rid, index, _FEATURE_ROOT, 0x00,
                        [feature_id >> 8, feature_id & 0xFF],
                        timeout_ms=800, retries=4, total_len=length)
    val = None
    if reply and _hidpp_error(reply) is None and len(reply) > 4:
        val = reply[4] or None          # 0x00 = 该 feature 不存在
    _HIDPP_FEATURE_CACHE[cache_key] = val
    return val


def _hidpp_ping(dev: HidDevice, rid: int, index: int,
                length: Optional[int] = None, timeout_ms: int = 350,
                retries: int = 2) -> bool:
    """探测某个设备索引后面有没有活着的设备（收不到回包 = 没配对 / 已休眠）。"""
    reply = _hidpp_call(dev, rid, index, _FEATURE_ROOT, 0x00, [0x00, 0x05],
                        timeout_ms=timeout_ms, retries=retries, total_len=length)
    return bool(reply) and _hidpp_error(reply) is None


def _hidpp_device_name(dev: HidDevice, rid: int, index: int,
                       length: Optional[int] = None) -> str:
    """读设备名（feature 0x0005），用来在界面上显示真实型号。"""
    key = (dev.path, rid, index)
    if key in _HIDPP_NAME_CACHE:
        return _HIDPP_NAME_CACHE[key]
    name = ""
    fidx = _hidpp_feature_index(dev, rid, index, _FEATURE_DEVICE_NAME, length)
    if fidx:
        head = _hidpp_call(dev, rid, index, fidx, 0x00, [],
                           timeout_ms=600, retries=3, total_len=length)
        if head and _hidpp_error(head) is None and len(head) > 4:
            total = head[4]
            if 0 < total <= 128:
                buf = b""
                for off in range(0, total, 16):
                    part = _hidpp_call(dev, rid, index, fidx, 0x01, [off],
                                       timeout_ms=600, retries=3, total_len=length)
                    if not part or _hidpp_error(part) is not None:
                        break
                    buf += bytes(part[4:20])
                name = buf[:total].split(b"\x00")[0].decode("utf-8", "replace").strip()
    _HIDPP_NAME_CACHE[key] = name
    return name


def strategy_logitech_hidpp(iface: HidInterface) -> Optional[BatteryResult]:
    if iface.vid != 0x046D or not iface.output_len:
        return None
    with _HID_READ_LOCK:
        return _strategy_logitech_hidpp_locked(iface)


def _strategy_logitech_hidpp_locked(iface: HidInterface) -> Optional[BatteryResult]:
    alive = False
    try:
        with HidDevice(iface.path, write=True) as dev:
            for rid, length in ((0x11, 20), (0x10, 7)):
                # 短报文(0x10)只能走 7 字节那条接口；长报文(0x11)走 20 字节那条。
                # 32 字节那条是给固件更新用的，写进去直接报 err=1，跳过。
                if rid == 0x10 and iface.output_len != 7:
                    continue
                if iface.output_len < length:
                    continue
                res, pinged = _logitech_probe_device(dev, iface, rid, length)
                alive = alive or pinged
                if res is not None:
                    return res
    except HidError as e:
        return BatteryResult(ok=False, source="罗技 HID++",
                             detail=f"无法打开厂商接口：{e}")
    if not alive:
        return BatteryResult(
            ok=False, source="罗技 HID++ 2.0",
            detail="没有回复（鼠标休眠或接收器没配对，动一下鼠标再试）")
    return None


def _logitech_probe_device(dev: HidDevice, iface: HidInterface, rid: int,
                           length: int) -> tuple[Optional[BatteryResult], bool]:
    """先 ping 出活着的设备索引（接收器一般只有 1 号），再只对它查 feature。

    这样鼠标休眠时 ~2s 就能给出结论；以前是不管死活每个索引都查三遍 feature，
    单次读要 90~200 秒，界面看起来就是「卡死 / 读不到电量」。
    返回 (结果, 是否有设备应答)。
    """
    alive = False
    cached = _HIDPP_LIVE_INDEX.get((iface.path, rid))
    if cached:
        # 上次通的索引先重试一次、且给更长超时：刚唤醒的鼠标常漏掉第一帧。
        if _hidpp_ping(dev, rid, cached, length, timeout_ms=700, retries=3):
            alive = True
            res = _logitech_probe_index(dev, iface, rid, cached, length)
            if res is not None:
                return res, alive
    for index in HIDPP_DEVICE_INDICES:
        if index == cached:
            continue
        # 其余索引基本都没配对，用短超时快速排除，避免读一次卡十几秒。
        if not _hidpp_ping(dev, rid, index, length, timeout_ms=250, retries=1):
            continue
        alive = True
        _HIDPP_LIVE_INDEX[(iface.path, rid)] = index
        res = _logitech_probe_index(dev, iface, rid, index, length)
        if res is not None:
            return res, alive
    return None, alive


def _logitech_probe_index(dev: HidDevice, iface: HidInterface, rid: int,
                          index: int, length: Optional[int] = None
                          ) -> Optional[BatteryResult]:
    name = _hidpp_device_name(dev, rid, index, length)

    fidx = _hidpp_feature_index(dev, rid, index, _FEATURE_BATTERY_STATUS, length)
    if fidx:
        reply = _hidpp_call(dev, rid, index, fidx, 0x00, [],
                            timeout_ms=600, retries=3, total_len=length)
        if reply and _hidpp_error(reply) is None and len(reply) >= 7:
            raw_level, next_level, st = reply[4], reply[5], reply[6]
            status, text = _HIDPP_STATUS.get(st, ("unknown", f"状态 {st}"))
            level = raw_level
            if level == 0 and status == "full":
                level = 100
            elif level == 0 and status == "charging":
                # 充电中很多设备会暂时报 0，别拿 80% 这种假数字糊弄人：
                # 交给界面显示「充电中」+ 原来的百分比（没有就显示 --）。
                level = None
            return BatteryResult(
                ok=True,
                level=None if level is None else max(0, min(100, level)),
                raw_level=raw_level,
                status=status, source="罗技 HID++ 2.0（0x1000 电池状态）",
                detail=f"设备索引 {index}：电量={raw_level}% 状态={text}",
                raw=reply, extra={"device_name": name},
            )

    fidx = _hidpp_feature_index(dev, rid, index, _FEATURE_UNIFIED_BATTERY, length)
    if fidx:
        # 0x1004 统一电池读电量用 function=1（GetStatus）；function=0 是 GetCapabilities，
        # 会返回能力字节而非电量。Solaar get_battery_unified 也是 function=0x10。
        # GetStatus 需要外设（鼠标）实时上报，有无线延迟，超时要给足，否则拿不到回包。
        reply = _hidpp_call(dev, rid, index, fidx, 0x01, [],
                            timeout_ms=1000, retries=5, total_len=length)
        if reply and _hidpp_error(reply) is None and len(reply) >= 7:
            # GetStatus 返回（对齐 Solaar decipher_battery_unified）：
            #   byte4 = 电量百分比(discharge)，byte5 = 电量档位(8满/4良/2低/1临界)，
            #   byte6 = 状态枚举（0x00放电/0x01充电/0x02快满/0x03满/0x04慢充/0x05无效/0x06热异常）
            raw_level, next_level, st = reply[4], reply[5], reply[6]
            status, text = _hidpp_unified_status(st)
            return BatteryResult(
                ok=True, level=max(0, min(100, raw_level)), raw_level=raw_level,
                status=status, source="罗技 HID++ 2.0（0x1004 统一电池）",
                detail=f"设备索引 {index}：电量={raw_level}% 状态={text}", raw=reply,
                extra={"device_name": name},
            )

    fidx = _hidpp_feature_index(dev, rid, index, _FEATURE_BATTERY_VOLTAGE, length)
    if fidx:
        # 0x1001 只有电压，没有百分比（G502 LIGHTSPEED / G903 / G915 都属于这类）。
        # 回包：byte4-5 = 电压，**大端**毫伏；byte6 = 标志位。
        # 原代码按小端算，3581mV 被算成 64781 而丢弃，所以永远「读不到电量」。
        reply = _hidpp_call(dev, rid, index, fidx, 0x00, [],
                            timeout_ms=700, retries=3, total_len=length)
        if reply and _hidpp_error(reply) is None and len(reply) >= 7:
            mv = (reply[4] << 8) | reply[5]
            flags = reply[6]
            if 2500 <= mv <= 5000:
                status, text = _hidpp_voltage_status(flags)
                pct = voltage_to_percent(mv)
                critical = bool(flags & 0x20)
                return BatteryResult(
                    ok=True, level=pct, raw_level=mv, voltage_mv=mv,
                    status=status,
                    source="罗技 HID++ 2.0（0x1001 电池电压）",
                    detail=(f"设备索引 {index}：{mv} mV ≈ {pct}%（按单节锂电曲线估算，"
                            f"{text}" + ("，电量告急" if critical else "") + "）"),
                    raw=reply,
                    extra={"device_name": name, "voltage_mv": mv, "flags": flags},
                )
    return None


# --------------------------------------------------------------------------
# 策略 E：监听 Input Report 上报的电量
# --------------------------------------------------------------------------
def monitor_input_reports(iface: HidInterface, seconds: float = 3.0,
                          on_report: Optional[Callable[[bytes], None]] = None,
                          stop_flag: Optional[Callable[[], bool]] = None
                          ) -> tuple[Optional[BatteryResult], list[bytes]]:
    """在若干秒内监听 Input Report，找上报的电量字段。"""
    if not iface.input_len:
        return None, []
    try:
        dev = HidDevice(iface.path, write=False)
    except HidError as e:
        return BatteryResult(ok=False, source="输入报告监听", detail=str(e)), []
    found: list[bytes] = []
    result: Optional[BatteryResult] = None
    try:
        battery_caps = []
        for cap in dev.value_caps(HidP_Input):
            key = (cap.UsagePage, cap.u.NotRange.Usage)
            if any(b[0] == key[0] and b[1] == key[1] for b in hw.BATTERY_USAGES):
                battery_caps.append(cap)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if stop_flag is not None and stop_flag():
                break
            report = dev.read_input(timeout_ms=250, stop_flag=stop_flag)
            if not report:
                continue
            found.append(report)
            if on_report:
                on_report(report)
            if result is not None:
                continue
            if battery_caps:
                for cap in battery_caps:
                    value = dev.usage_value(HidP_Input, report, cap)
                    if value is None:
                        continue
                    result = BatteryResult(
                        ok=True, level=_scale(value, cap.LogicalMax), raw_level=value,
                        status="unknown",
                        source=f"输入报告电量字段（UsagePage=0x{cap.UsagePage:04X} "
                               f"Usage=0x{cap.u.NotRange.Usage:04X}）",
                        detail=f"监听 Input Report 得到 原始值={value} 上限={cap.LogicalMax}",
                        raw=report,
                    )
                    break
    except HidError as e:
        return BatteryResult(ok=False, source="输入报告监听", detail=str(e)), found
    finally:
        dev.close()
    if result:
        found = found[-16:]
    return result, found


# --------------------------------------------------------------------------
# 策略 F：原始 Feature Report 转储
# --------------------------------------------------------------------------
def dump_feature_reports(iface: HidInterface) -> list[tuple[int, bytes, str]]:
    """扫描并读回所有报告 ID 的 Feature Report。返回 (report_id, data, 备注)。"""
    out: list[tuple[int, bytes, str]] = []
    if not iface.feature_len:
        return out
    ids: list[int] = []
    try:
        with HidDevice(iface.path, write=False) as dev:
            for cap in dev.value_caps(HidP_Feature):
                if cap.ReportID not in ids:
                    ids.append(cap.ReportID)
    except HidError:
        pass
    for rid in range(0x00, 0x21):
        if rid not in ids:
            ids.append(rid)

    try:
        with HidDevice(iface.path, write=False) as dev:
            for rid in ids:
                try:
                    data = dev.get_feature_report(rid)
                    note = "有数据"
                    if any(1 <= b <= 100 for b in data[1:]):
                        note = "含疑似电量字节"
                    out.append((rid, data, note))
                except HidError as e:
                    out.append((rid, b"", f"读取失败：{e}"))
    except HidError as e:
        out = [(0, b"", f"无法打开接口：{e}")]
    return out


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 工具：探测 Report ID / 试发任意命令（配合手动标定）
# --------------------------------------------------------------------------
def probe_report_ids(iface: HidInterface, max_id: int = 0x20) -> list[int]:
    """试出该接口接受哪些 Output Report 的 Report ID（不认识的设备很有用）。

    Windows 对 Report ID 不匹配的写请求会返回 87（参数错误），
    因此用全 0 报文逐个试就能把合法 ID 列出来。
    """
    if not iface.output_len:
        return []
    ok_ids: list[int] = []
    try:
        with HidDevice(iface.path, write=True) as dev:
            length = dev.caps.OutputReportByteLength or iface.output_len
            for rid in range(0, max_id + 1):
                try:
                    dev.write_output(bytes([rid] + [0] * (length - 1)))
                    ok_ids.append(rid)
                except HidError:
                    continue
    except HidError:
        return []
    return ok_ids


def send_command(iface: HidInterface, rid: int, payload: list[int],
                 kind: str = "output", length: Optional[int] = None,
                 wait_ms: int = 350, repeat: int = 2) -> list[bytes]:
    """按指定方式发一条命令并收集回复（原始探测用）。"""
    replies: list[bytes] = []
    try:
        with HidDevice(iface.path, write=True) as dev:
            size = length or (dev.caps.OutputReportByteLength or iface.output_len)
            body = ([rid] + list(payload) + [0] * size)[:size]
            for _ in range(max(1, repeat)):
                try:
                    if kind == "feature":
                        iface_len = dev.caps.FeatureReportByteLength or size
                        body = ([rid] + list(payload) + [0] * iface_len)[:iface_len]
                        dev.set_feature_report(bytes(body))
                        time.sleep(wait_ms / 1000.0)
                        replies.append(dev.get_feature_report(rid, iface_len))
                    else:
                        dev.drain()
                        dev.write_output(bytes(body))
                        end = time.monotonic() + wait_ms / 1000.0
                        while time.monotonic() < end:
                            r = dev.read_input(timeout_ms=100)
                            if r:
                                replies.append(r)
                except HidError as e:
                    replies.append(b"")
                    break
    except HidError:
        return replies
    return replies


def read_battery(iface: HidInterface, listen_seconds: float = 3.0,
                 progress: Optional[Callable[[str], None]] = None
                 ) -> list[BatteryResult]:
    """按顺序跑所有策略，返回所有尝试记录（第一条 ok=True 的是结论）。"""
    attempts: list[BatteryResult] = []
    steps = [
        ("标准 HID 电量字段", lambda: strategy_standard_battery_usage(iface)),
        ("VGN S99 键盘协议", lambda: strategy_vgn_s99(iface)),
        ("厂商协议", lambda: strategy_vendor_protocol(iface)),
        ("罗技 HID++", lambda: strategy_logitech_hidpp(iface)),
    ]
    for label, fn in steps:
        if progress:
            progress(f"尝试：{label}")
        try:
            res = fn()
        except Exception as e:               # 单个策略异常不影响其它策略
            res = BatteryResult(ok=False, source=label, detail=f"异常：{e}")
        if res is not None:
            attempts.append(res)
            if res.ok:
                return attempts

    if progress:
        progress(f"尝试：监听输入报告 {listen_seconds:.0f} 秒")
    res, _ = monitor_input_reports(iface, seconds=listen_seconds)
    if res is not None:
        attempts.append(res)
    return attempts


def best_result(attempts: list[BatteryResult]) -> Optional[BatteryResult]:
    for a in attempts:
        if a.ok:
            return a
    return attempts[0] if attempts else None
