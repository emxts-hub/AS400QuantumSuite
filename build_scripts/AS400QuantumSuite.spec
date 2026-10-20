# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['../src/main.py'],
    pathex=['../src'],
    binaries=[],
    datas=[
        ('../Image & Sound/warning.wav', 'Image & Sound'),
        ('../Image & Sound/alert.wav', 'Image & Sound'),
        ('../Image & Sound/up.wav', 'Image & Sound'),
        ('../Image & Sound/ngani.wav', 'Image & Sound'),
        ('../Image & Sound/logo.png', 'Image & Sound'),
        ('../Image & Sound/monitor.png', 'Image & Sound'),
        ('../Image & Sound/logs.png', 'Image & Sound'),
        ('../Image & Sound/monthly.png', 'Image & Sound'),
        ('../Image & Sound/backup.png', 'Image & Sound'),
        ('../Image & Sound/settings.png', 'Image & Sound'),
        ('../Image & Sound/info-logo.ico', 'Image & Sound'),
    ],
    hiddenimports=[],
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
    name='AS400QuantumSuite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['../Image & Sound/logo.ico'],
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
