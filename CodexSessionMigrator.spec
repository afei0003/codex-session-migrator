# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata


project_root = Path(SPECPATH)
datas = []
binaries = []
hiddenimports = ["gradio_app", *collect_submodules("csmigrator")]

for package in ("gradio", "gradio_client", "pystray", "PIL"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

for package in ("gradio", "gradio_client", "safehttpx", "groovy"):
    datas.extend(collect_data_files(package, include_py_files=True))

for package in ("gradio", "gradio_client", "safehttpx"):
    datas.extend(copy_metadata(package))


a = Analysis(
    [str(project_root / "desktop_launcher.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodexSessionMigrator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    a.zipfiles,
    strip=False,
    upx=False,
    name="CodexSessionMigrator",
)
