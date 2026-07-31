# PyInstaller spec for the LLM64 proxy launcher. Build from this
# directory with:
#
#   pyinstaller llm64.spec
#
# and the result is dist/llm64-proxy (llm64-proxy.exe on Windows), one
# self-contained file. It must be built ON the platform it targets -
# PyInstaller does not cross-compile. See PACKAGING.md.
#
# datas mirror the checkout layout under src/ because respath.py
# resolves bundled files as <_MEIPASS>/src/<name>.

a = Analysis(
    ['llm64_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/default_cards', 'src/default_cards'),
        ('src/adventure_rules.json', 'src'),
        ('src/sid_overrides.json', 'src'),
        ('workflows', 'workflows'),
        ('config.toml.example', '.'),
    ],
    hiddenimports=['toml'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name='llm64-proxy',
    debug=False,
    strip=False,
    upx=False,
    # windowed: the launcher UI is the console. On Windows console=True
    # would flash a second cmd window behind it; logs go to the UI pane
    # and <data_dir>/proxy.log either way.
    console=False,
)
