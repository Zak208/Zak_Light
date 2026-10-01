# PyInstaller spec - run from the project root:  pyinstaller build/zak_light.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

datas = [
    (os.path.join(SRC, "web"), "web"),
    # No settings are bundled: the installer must never carry the personal settings of whoever builds it
]
binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.lifespan.off",
    "pystray._win32",
]

for pkg in ("webview", "dxcam", "pycaw", "hid", "comtypes"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(SRC, "app.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "IPython", "pytest",
              "yaml", "zstandard", "werkzeug", "flask", "jinja2", "setuptools", "pkg_resources",
              # never used here: TLS/websocket/validation extras that only ride along with our dependencies
              "cryptography", "websockets", "email_validator", "watchfiles", "orjson", "tzdata"],
    noarchive=False,
)

# OpenCV ships a 28 MB FFmpeg DLL for reading/writing video files; we only use resize/integral/cvtColor
def _is_video_io(entry):
    return "opencv_videoio_ffmpeg" in entry[0].lower()

a.binaries = [b for b in a.binaries if not _is_video_io(b)]
a.datas = [d for d in a.datas if not _is_video_io(d)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Zak_light",
    console=bool(os.environ.get("ZAK_CONSOLE")),  # ZAK_CONSOLE=1 for a debug build
    icon=os.path.join(SPECPATH, "zak_light.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Zak_light", upx=False)
