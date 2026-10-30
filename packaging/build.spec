# PyInstaller spec for the desktop build.
#   pyinstaller packaging/build.spec --noconfirm

import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
APP_NAME = "COD Ping King"

datas = [
    (os.path.join(ROOT, "data", "seed_ranges.json"), "data"),
    (os.path.join(ROOT, "data", "seed_geo.json"), "data"),
    (os.path.join(ROOT, "data", "seed_community.json"), "data"),
    (os.path.join(ROOT, "cod_region_filter", "ui"), "cod_region_filter/ui"),
]
binaries = []
hiddenimports = ["webview.platforms.edgechromium", "pystray._win32"]

# pydivert carries the signed WinDivert driver next to its module.
for package in ("pydivert", "webview", "pystray", "PIL"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CODPingKing",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    uac_admin=True,
    version=os.path.join(SPECPATH, "version_info.txt"),
    icon=os.path.join(SPECPATH, "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
