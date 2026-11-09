# 可复用 Prompt：Windows 2.4G 外设电量小工具

> 用法：把下面「=== 提示词开始 ===」到「=== 提示词结束 ===」之间的内容整段复制，
> 粘给一个**能执行命令、读写文件的 Agent**（WorkBuddy / CodeBuddy / Cursor / Claude Code 等），
> 即可从零复刻这个工具。**纯聊天窗口不够**——枚举 HID、抓包、逆向厂商软件都得真跑起来。
> 里面包含了已经验证过的协议细节和踩过的坑，照做基本不会返工。

=== 提示词开始 ===

你是一个熟悉 Windows 底层开发的工程师，擅长用 Python `ctypes` 直接调用系统 DLL。
请帮我做一个 **Windows 10/11 上的无线外设电量小工具**，要求如下。

## 一、硬性约束

1. **零第三方依赖**：只用 Python 标准库 + `ctypes`（GUI 用 `tkinter`）。
   不要用 hidapi、pystray、Pillow、pywin32。
2. **单进程单入口**：一个程序同时提供简洁面板 + 托盘常驻 + 弹窗。
   - 简洁面板：显示我这两台设备的两张卡片（大号百分比 + 圆角电池条 + 数据来源）。
   - 托盘常驻：通知区域一个圆圈电量图标（取两台设备最低电量），颜色随电量变化。
   - 完整调试版：单独入口，能列出所有 HID 设备/接口、转储 Feature Report、
     探测 Report ID、试发命令、手动标定协议。
3. **托盘交互**：左键托盘图标 → 弹出小卡片显示各设备电量 + 圆角进度条；
   右键 → 弹出「打开主页面 / 退出」菜单；悬停 tooltip 显示各设备电量。
   点主窗口 ✕ 时**隐藏进托盘而非退出**。
4. **低 CPU**：用 `SetTimer` 驱动刷新（默认 60 秒，可调 30 秒~30 分钟），
   空闲时不轮询、不 sleep 循环；读取放后台线程，界面不卡。
5. **开机自启**：做成「启动」文件夹里的快捷方式（`.lnk`），
   这样任务管理器 →「启动」标签里能直接看到、能启用/禁用。
   不要用注册表 Run。快捷方式目标是 `pythonw.exe` + 脚本路径，**不能弹黑窗**。
6. **桌面入口用软连接**：桌面放一个指向项目目录的目录链接 + 指向启动脚本的文件链接，
   不要复制脚本。目录优先真符号链接，没有管理员/开发者模式时降级用 junction（`mklink /J`），
   文件降级用硬链接（`os.link`），跨分区降级为 `.lnk` 快捷方式。
7. **不写任何日志文件**：只保留 `--debug` 参数时的控制台输出。
8. 关闭托盘时进程必须真正退出，**不能留僵尸进程**（详见"已知的坑"）。

## 二、要读的两台设备

| 设备 | VID:PID | 说明 |
|---|---|---|
| VGN S99 键盘 | `320F:5088` | 2.4G Dongle，Telink 方案 |
| Logitech Pro X Superlight 鼠标 | `046D:C547` | Lightspeed 游戏接收器（非 Unifying） |

## 三、已验证的协议（直接照抄，不用再逆向）

### 1) VGN S99 键盘（Telink 方案）

- 接口：`usage_page = 0xFF1C`、`usage = 0x0092`，输入 64 字节 / 输出 64 字节，**没有 Feature Report**。
- Report ID：`0x04`（即报文首字节）。
- 帧布局（63 字节，`frame[0..1]` = 递增 requestCode 小端 uint16，`frame[2]` = command id）：
  - `power_info = 0x1A`（读电量）
  - 其它命令：`start=1`、`end=2`、`get_keyboard_info=3`、`get_keyboard_function=5`、
    `get_default_key_matrix=7`、`get_current_key_matrix=8`、`set_key_value=9`、`reset_device=13` 等
- 发送：`[0x04] + frame` 补齐到 64 字节，用 `WriteFile` **直写**（`HidD_SetOutputReport` 无响应）。
- 回复解析（Python 含 rid 偏移）：`resp[0]=0x04`，`resp[1..2]`=requestCode 回显，
  `resp[3]`=command id。**电量 = resp[8]**，**充电标志 = resp[9]（非 0 表示充电）**。
- 键盘深度休眠时，前几条命令只回 ACK 帧（`resp[3] == 0xFF`），要继续重发直到收到
  `resp[3] == 0x1A` 的真实数据帧（实测最多要 5~10 次）。

### 2) 罗技 HID++ 2.0（Lightspeed 接收器 `046D:C547`）

- 接口：`usage_page = 0xFF00`，有两个 HID++ 接口：短报文（in/out=7，rid=0x10）和
  长报文（in/out=20，rid=0x11）。**长报文接口优先**（短报文接口读不到 0x1004）。
- 特性查询：`getFeatureIndex(0x1004)`（统一电池）→ feature index = 6。
  `0x1000`（电池状态）/`0x1001`（电压）在此接收器**不支持**（fidx=0），且查询极慢（~1.1s），
  务必做进程级缓存跳过。
- 读电量：`readBattery(feature=0x1004, function=0x01 GetStatus)`，回包布局
  `[rid, idx, fidx, fn, 电量byte4, 档位byte5, 状态byte6, ...]`。**电量 = byte4**。
- 状态字节（byte6）是枚举不是 bitfield（对齐 Solaar `BatteryStatus`）：
  `0x00`放电 / `0x01`充电 / `0x02`快满 / `0x03`满 / `0x04`慢充 / `0x05`无效 / `0x06`热异常。
- 首命令接收器常「静默」，需要 `drain()` + 重试唤醒。

### 3) 兜底（换设备时用）

优先级：标准 HID 电量字段（Generic Device Controls `0x0006/0x0020` Battery Strength）→
厂商协议表（Endgame `0x3367`、VAXEE `0x3057`、Nordic `0x1915`）→
罗技 HID++ 2.0（特性 `0x1000/0x1004/0x1001`）→ 监听 Input Report →
手动标定（把 Report ID / 电量字节下标 / 倍率存成 JSON）。

## 四、已知的坑（务必规避）

1. **绝对不要在同一 HID 句柄上"阻塞 ReadFile + 并发 WriteFile/IOCTL"** ——
   实测会直接杀掉进程（连 Python 异常栈都没有）。
   正确做法：用独立的、带 `FILE_FLAG_OVERLAPPED` 的读句柄 + 事件超时 + `CancelIoEx`。
2. **系统独占的顶层集合**（鼠标/键盘/笔/触摸屏）只能用 `dwDesiredAccess=0` 打开，
   能读属性和 Feature Report，但不能发指令。
3. **销毁托盘窗口后必须 `PostQuitMessage(0)`**（在 `WM_DESTROY` 里发）。
   只调 `DestroyWindow` 会留下"窗口没了、图标也没了、但进程还活着"的僵尸进程，
   它还占着单实例锁 → 之后再也启动不起来，而且 `pythonw` 没有控制台，用户看不到任何报错。
4. **Tkinter 不是线程安全的**：后台线程不要直接调 `root.after()`，
   用 `queue.Queue` 把结果交给主线程，主线程用 `after(100, pump)` 取。
   托盘窗口（Win32 消息循环）要在独立线程跑，GUI 关闭→隐藏、托盘退出→通知主线程真退出。
5. **托盘图标不要"删了再加"**：`NIM_DELETE` 删掉的图标，用 `NIM_MODIFY` 是改不回来的，
   必须重新 `NIM_ADD`。另外不做 DPI 声明的话图标会被系统缩放得发糊（要 `SetProcessDPIAware`）。
6. **创建 .lnk 用纯 ctypes**：`CoCreateInstance(CLSID_ShellLink)` → `IShellLinkW` →
   `QueryInterface(IPersistFile)` → `Save()`。vtable 下标：
   `SetPath=20 SetArguments=11 SetWorkingDirectory=9 SetDescription=7 SetIconLocation=17`、
   `GetPath=3 GetArguments=10 GetWorkingDirectory=8`；`Load=5 Save=6`。
   GUID 字符串要用 `split("-", 3)` 解析（最后一节里还有连字符）。
7. **别让常驻程序"自动纠正"用户设置**：托盘只应当**读**开机自启的真实状态，
   绝不能因为读到一次不一致就替用户删/建启动项。

## 五、验收标准

1. `python my_battery.py --once` 能打印两行电量（键盘 xx%、鼠标 xx%）。
2. 简洁面板里两张卡片显示正确百分比，颜色随电量变化（≥60 绿 / 30–59 黄 / <30 红）。
3. 任务管理器 →「启动」标签里能看到「电量托盘」，可直接启用/禁用，
   且与界面里的「开机自启」勾选框状态一致。
4. 桌面上是链接（不是副本）：指向项目目录的目录链接 + 指向启动脚本的文件链接。
5. 项目目录里**不产生任何日志文件**；`--debug` 时才有控制台输出。
6. 连续启停托盘 3 次正常；退出后无残留进程、无残留托盘图标。
7. 空闲时 CPU 占用接近 0（实测 75 秒内 < 0.5 秒 CPU）。

=== 提示词结束 ===
