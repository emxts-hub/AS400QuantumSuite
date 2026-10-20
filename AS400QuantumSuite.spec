# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('Image & Sound/warning.wav', 'Image & Sound'),
    ('Image & Sound/alert.wav', 'Image & Sound'),
    ('Image & Sound/up.wav', 'Image & Sound'),
    ('Image & Sound/ngani.wav', 'Image & Sound'),
    ('Image & Sound/logo.png', 'Image & Sound'),
    ('Image & Sound/monitor.png', 'Image & Sound'),
    ('Image & Sound/logs.png', 'Image & Sound'),
    ('Image & Sound/monthly.png', 'Image & Sound'),
    ('Image & Sound/backup.png', 'Image & Sound'),
    ('Image & Sound/settings.png', 'Image & Sound'),
    ('Image & Sound/info-logo.ico', 'Image & Sound'),
]
binaries = []
hiddenimports = []
tmp_ret = collect_all('PyQt6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['src\\main.py'],
    pathex=['src'],
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
    name='AS400QuantumSuite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['Image & Sound\\logo.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AS400QuantumSuite',
)
