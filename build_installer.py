# -*- coding: utf-8 -*-
"""build_installer.py —— 用 Inno Setup 把 dist 里的 exe 打成安装包（Setup.exe）。

用法：
    python build_installer.py

前提：
    1. dist/2.4G设备电量.exe 已经用 build_exe.py 打好（单文件程序本体）
    2. 装了 Inno Setup 6（winget install JRSoftware.InnoSetup）

产物：
    dist/2.4G设备电量-Setup-v{版本}.exe
    —— 别人拿到后双击：下一步 → 选安装位置 → 安装 → 开始菜单/桌面快捷方式，
       控制面板里可卸载。
    桌面「电量管理安装包」文件夹也会自动归档一份（按版本号区分，历史版本保留）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER_DIR = os.path.join(HERE, "installer")
ISS = os.path.join(INSTALLER_DIR, "installer.iss")
DIST = os.path.join(HERE, "dist")

# 版本号跟 build_exe.py 保持同一份
sys.path.insert(0, HERE)
from build_exe import EXE_NAME, VERSION  # noqa: E402


def find_iscc() -> str:
    """找 Inno Setup 编译器 ISCC.exe。"""
    cands = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    which = shutil.which("ISCC") or shutil.which("iscc")
    if which:
        return which
    return ""


def main() -> int:
    app_exe = os.path.join(DIST, EXE_NAME)
    if not os.path.isfile(app_exe):
        print(f"缺少 {app_exe}：先运行 python build_exe.py")
        return 1
    if not os.path.isfile(os.path.join(INSTALLER_DIR, "ChineseSimplified.isl")):
        print("缺少 installer/ChineseSimplified.isl（安装向导的简体中文语言包）")
        return 1

    iscc = find_iscc()
    if not iscc:
        print("找不到 Inno Setup 的 ISCC.exe，先装："
              "winget install JRSoftware.InnoSetup")
        return 1

    # 版本号注入 .iss（模板里是 __VERSION__ 占位符）
    with open(ISS, "r", encoding="utf-8") as f:
        tpl = f.read()
    iss_out = os.path.join(INSTALLER_DIR, "_build.iss")
    with open(iss_out, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(tpl.replace("__VERSION__", VERSION))

    print("+", iscc, os.path.basename(iss_out))
    r = subprocess.run([iscc, iss_out], cwd=INSTALLER_DIR)
    if r.returncode != 0:
        print("Inno Setup 编译失败")
        return 1

    setup = os.path.join(DIST, f"2.4G设备电量-Setup-v{VERSION}.exe")
    if not os.path.isfile(setup):
        print("没有生成", setup)
        return 1
    print(f"\n安装包：{setup}  （{os.path.getsize(setup) / 1024 / 1024:.1f} MB）")

    # 归档到桌面「电量管理安装包」（按版本号区分，历史版本保留）
    try:
        import win_shell
        archive_dir = os.path.join(win_shell.desktop_dir(), "电量管理安装包")
        os.makedirs(archive_dir, exist_ok=True)
        shutil.copy2(setup, os.path.join(archive_dir, os.path.basename(setup)))
        print(f"已归档：{os.path.join(archive_dir, os.path.basename(setup))}")
    except Exception as e:
        print(f"归档到桌面文件夹失败（不影响安装包本身）：{e}")

    print("把这个文件发给别人，双击就能：选位置 → 安装 → 开始菜单/桌面快捷方式。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
