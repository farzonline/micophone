# PyInstaller build for Micophone.exe. Run build.bat (it also runs the self-checks first).

# Qt pieces the app never uses. Dropping them takes the exe from ~40 MB to about half.
DROP = (
    "opengl32sw.dll",                     # software OpenGL; the window is plain widgets
    "qt6pdf.dll", "qpdf.dll",             # PDF image plugin
    "libcrypto-3-x64.dll", "libssl-3-x64.dll", "\tls\\",  # Qt's TLS: only a local socket is used
    "\translations\\",                   # Qt's own UI translations
    "\imageformats\\",                   # icons are drawn in code; PNG is built into Qt
)


def keep(entry):
    return not any(d in entry[0].lower() for d in DROP)


a = Analysis(["micophone_gui.py"], excludes=["numpy", "tkinter"])
a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Micophone", console=False, upx=False,
          icon="assets/micophone.ico", version="version_info.txt")
