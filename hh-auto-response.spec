# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

icon_path = 'icon.ico' if sys.platform == 'win32' else 'icon.png'

playwright_pkg = None
for sp in sys.path:
    candidate = Path(sp) / 'playwright'
    if candidate.is_dir():
        playwright_pkg = candidate
        break

datas_list = [
    ('config.example.yaml', '.'),
    ('.env.example', '.'),
    ('icon.ico', '.'),
    ('icon.png', '.'),
]

if playwright_pkg:
    driver_dir = playwright_pkg / 'driver'
    if driver_dir.is_dir():
        datas_list.append((str(driver_dir), 'playwright/driver'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas_list,
    hiddenimports=[
        'hh_auto',
        'hh_auto.app_config',
        'hh_auto.browser',
        'hh_auto.cover_letter',
        'hh_auto.filters',
        'hh_auto.gui',
        'hh_auto.nim_client',
        'hh_auto.profile',
        'hh_auto.runner',
        'hh_auto.storage',
        'playwright',
        'playwright.sync_api',
        'playwright._impl',
        'playwright._impl._driver',
        'greenlet',
        'pyee',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['hh_auto/_runtime_hook.py'],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
        'pytest',
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyi_runtime_deps = []

a.datas += pyi_runtime_deps

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='hh-auto-response',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    upx=True,
    upx_exclude=[],
    name='hh-auto-response',
)
