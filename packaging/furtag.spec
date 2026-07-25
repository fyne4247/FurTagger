# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FurTag GUI.
# Build natively on each OS:
#   pyinstaller packaging/furtag.spec
#
# Do not cross-compile. Signed builds are required before validating
# keyring credential persistence across app updates.

import sys
from pathlib import Path

block_cipher = None

# When SPECPATH is packaging/, parent is project root.
try:
    project = Path(SPECPATH).resolve().parent
except NameError:
    project = Path(".").resolve()

datas = []
hiddenimports = [
    "PIL",
    "PIL.Image",
    "regex",
    "requests",
    "keyring",
    "keyring.backends",
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "platformdirs",
    "pymupdf",
    "fitz",
    "PySide6",
    "shiboken6",
]

a = Analysis(
    [str(project / "furtag_gui.py")],
    pathex=[str(project)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FurTag",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="FurTag",
)

# macOS .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="FurTag.app",
        icon=None,  # add FurTag.icns when available
        bundle_identifier="org.furtag.FurTag",
        info_plist={
            "CFBundleName": "FurTag",
            "CFBundleDisplayName": "FurTag",
            "CFBundleIdentifier": "org.furtag.FurTag",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
