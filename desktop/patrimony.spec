# -*- mode: python ; coding: utf-8 -*-
# Patrimony Desktop — spec PyInstaller (onefile, sans console).
# Build : pyinstaller --clean --noconfirm desktop/patrimony.spec  (depuis la racine du dépôt)

a = Analysis(
    ['launcher.py'],
    pathex=['..'],
    binaries=[],
    datas=[
        ('../public', 'public'),   # UI servie par StaticFiles
        ('../VERSION', '.'),       # version lue par src/app.py
    ],
    hiddenimports=[
        # uvicorn importe ses implémentations dynamiquement
        'uvicorn.logging',
        'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
        'uvicorn.protocols', 'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan', 'uvicorn.lifespan.on',
        # fenêtre native (pywebview : plateforme choisie au runtime)
        'webview',
        'webview.platforms.winforms', 'webview.platforms.edgechromium',
        'webview.platforms.gtk', 'webview.platforms.cocoa',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Patrimony',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='patrimony.ico',
    disable_windowed_traceback=False,
)
