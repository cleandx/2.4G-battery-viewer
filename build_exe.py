# -*- coding: utf-8 -*-
"""build_exe.py —— 一键把它打包成单文件 exe（给不想装 Python 的人用）。

用法：
    python build_exe.py

产物：
    dist/2.4G设备电量.exe                 单文件、无控制台、自带图标
    dist/2.4G设备电量-v1.0.0-win64.zip    发布用压缩包（exe + 说明 + LICENSE）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
EXE_NAME = "2.4G设备电量.exe"
VERSION = "1.3.0"
ZIP_NAME = f"2.4G设备电量-v{VERSION}-win64.zip"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.check_call(cmd, cwd=HERE)


def main() -> int:
    if not os.path.isfile(os.path.join(HERE, "assets", "app.ico")):
        print("缺少 assets/app.ico，先运行 python make_icons.py")
        return 1

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "battery_viewer.spec"])

    exe = os.path.join(DIST, EXE_NAME)
    if not os.path.isfile(exe):
        print("打包失败：没有生成", exe)
        return 1
    size_mb = os.path.getsize(exe) / (1024 * 1024)
    print(f"\n生成：{exe}  （{size_mb:.1f} MB）")

    # 发布压缩包：exe + 说明 + 许可
    zpath = os.path.join(DIST, ZIP_NAME)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(exe, EXE_NAME)
        for name in ("README.md", "注意事项.md", "LICENSE"):
            p = os.path.join(HERE, name)
            if os.path.isfile(p):
                zf.write(p, name)
    print(f"发布包：{zpath}  （{os.path.getsize(zpath)/1024/1024:.1f} MB）")

    # 顺手清掉中间目录
    for junk in ("build",):
        shutil.rmtree(os.path.join(HERE, junk), ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
