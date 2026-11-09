# -*- coding: utf-8 -*-
"""启动 2.4G 电量程序（VGN 键盘 + 罗技鼠标），不弹黑窗。

最新版本是单进程入口 my_battery.py（GUI + 内置托盘线程）。
作为桌面文件链接的目标：双击即以 pythonw 静默拉起 my_battery.py。
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
exe = sys.executable
pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
if not os.path.exists(pythonw):
    pythonw = exe

subprocess.Popen(
    [pythonw, os.path.join(BASE, "my_battery.py")],
    cwd=BASE,
    creationflags=0x00000008,  # DETACHED_PROCESS：脱离当前进程、无控制台
)
