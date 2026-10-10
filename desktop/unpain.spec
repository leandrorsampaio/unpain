# PyInstaller spec for the UnPAIN desktop app. Run it through desktop/build.py, which sets the
# version and self-tests the result; running PyInstaller on it directly also works.
#
# What goes in: the launcher, app/ and pipeline/ (found by import analysis), and the
# non-Python files they read relative to their own location: app/static, examples/ (the
# setup wizard's seed files), pipeline/formats (the bank-format catalogue) and the PDF
# extractor manifests in scripts/. The self-test checks each of them in the built app,
# because a missing data file fails quietly: no formats means every import says "unknown".
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
VERSION = os.environ.get("UNPAIN_BUILD_VERSION", "0.0.0")
ICON = str(ROOT / "desktop" / "icon.png")       # PyInstaller converts it (needs Pillow)

manifests = sorted((ROOT / "scripts").glob("*/extractor.json"))
datas = [
    (str(ROOT / "app" / "static"), "app/static"),
    (str(ROOT / "examples"), "examples"),
    (str(ROOT / "pipeline" / "formats"), "pipeline/formats"),
    (str(ROOT / "desktop" / "icon.png"), "desktop"),
] + [(str(path), "scripts/%s" % path.parent.name) for path in manifests]
BUILD_INFO = ROOT / "build" / "desktop" / "build_info.json"      # written by build.py
if BUILD_INFO.exists():
    datas.append((str(BUILD_INFO), "desktop"))

hiddenimports = [
    "app.server",
    # uvicorn imports these by name from its config strings, which import analysis cannot see.
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.lifespan.on",
    "uvicorn.logging",
] + ["scripts.%s.extract" % path.parent.name for path in manifests]   # loaded via importlib

a = Analysis(
    [str(ROOT / "desktop" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    # uvicorn[standard] extras the launcher never selects (it pins loop/http/ws), and test tools.
    excludes=["uvloop", "httptools", "watchfiles", "websockets", "wsproto", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UnPAIN",
    console=False,          # a window app: no terminal opens next to it
    icon=ICON,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="UnPAIN", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="UnPAIN.app",
        icon=ICON,
        bundle_identifier="io.github.leandrorsampaio.unpain",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "UnPAIN",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            # The window sits behind the browser all day. App Nap would then throttle the
            # process, and with it the server the browser is talking to.
            "NSAppSleepDisabled": True,
        },
    )
