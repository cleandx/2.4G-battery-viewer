# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、带自己的图标。

用法：
    python build_exe.py          # 推荐（会顺带打 zip、检查产物）
    python -m PyInstaller battery_viewer.spec --noconfirm
"""
import os

HERE = os.path.abspath(SPECPATH)   # noqa: F821  (PyInstaller 注入)

a = Analysis(
    [os.path.join(HERE, "my_battery.py")],
    pathex=[HERE],
    binaries=[],
    # assets 里有键盘/鼠标产品图、程序图标，全部原样带进 exe
    datas=[(os.path.join(HERE, "assets"), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 调试工具不进发布包
        "app", "make_icons", "tkinter.test", "test", "unittest",
        "pydoc_data", "email.mime",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)   # noqa: F821

exe = EXE(   # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="2.4G设备电量",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                  # 不弹黑色命令行窗口
    disable_windowed_traceback=True,
    icon=os.path.join(HERE, "assets", "app.ico"),
    version=os.path.join(HERE, "version_info.txt"),
)
