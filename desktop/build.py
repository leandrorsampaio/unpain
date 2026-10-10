"""Build the UnPAIN desktop app and its installer for the OS this runs on.

    python desktop/build.py              app + self-test + installer
    python desktop/build.py --app-only   app + self-test, no installer

Needs requirements.txt and desktop/requirements-build.txt installed in the running Python,
and a Python that has tkinter. On Windows it also needs Inno Setup 6 (iscc).

Output in dist/, under names that do not change between versions, so that the website can link
to releases/latest/download/<name> (the version is inside each package):
    macOS     UnPAIN-macos-arm64.dmg, UnPAIN-macos-x86_64.dmg
    Windows   UnPAIN-windows-x64-setup.exe
    Linux     UnPAIN-linux-x86_64.tar.gz and UnPAIN-linux-amd64.deb

None of them is signed: macOS and Windows show a warning on first open (README explains
the one-time step). The built app is self-tested before it is packaged: a bundle that is
missing a data file starts fine and fails later, on someone's statement.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
FROZEN_DIR = DIST / "pyinstaller"


def run(command, **kwargs):
    print("+ " + " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def versions():
    """(display, numeric, debian) from the nearest release tag.

    v1.5.0 -> 1.5.0 everywhere. Three commits past it, the display version says so
    (1.5.0-3-gabc1234) while the numeric one, which macOS and Windows insist on, stays
    1.5.0; Debian gets 1.5.0+3.gabc1234 so that it still sorts after the release.
    """
    try:
        described = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=ROOT,
                                   capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        described = ""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(\d+)-g([0-9a-f]+))?(-dirty)?$", described)
    if not match:
        return (described or "dev"), "0.0.0", "0.0.0"
    numeric = "%s.%s.%s" % match.group(1, 2, 3)
    debian = numeric + ("+%s.g%s" % (match.group(4), match.group(5)) if match.group(4) else "")
    return described.lstrip("v"), numeric, debian


def build_app(display, numeric):
    info = BUILD / "desktop" / "build_info.json"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps({"version": display}), encoding="utf-8")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", FROZEN_DIR, "--workpath", BUILD / "pyinstaller", ROOT / "desktop" / "unpain.spec"],
        env=dict(os.environ, UNPAIN_BUILD_VERSION=numeric))


def executable():
    if sys.platform == "darwin":
        return FROZEN_DIR / "UnPAIN.app" / "Contents" / "MacOS" / "UnPAIN"
    return FROZEN_DIR / "UnPAIN" / ("UnPAIN.exe" if os.name == "nt" else "UnPAIN")


def self_test():
    report = BUILD / "self-test.json"
    report.unlink(missing_ok=True)
    started = time.monotonic()
    result = subprocess.run([str(executable()), "--self-test", str(report)], timeout=600)
    print("self-test finished in %.1fs" % (time.monotonic() - started))
    if report.exists():
        print(report.read_text(encoding="utf-8"))
    log = report.with_suffix(".log")
    if result.returncode != 0:
        if log.exists():
            print("---- self-test log ----\n" + log.read_text(encoding="utf-8", errors="replace"))
        sys.exit("self-test FAILED (exit code %d)" % result.returncode)


def package_macos():
    app = FROZEN_DIR / "UnPAIN.app"
    run(["codesign", "--verify", "--deep", "--strict", app])     # ad-hoc, but intact
    staging = BUILD / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    run(["ditto", app, staging / "UnPAIN.app"])
    (staging / "Applications").symlink_to("/Applications")
    output = DIST / ("UnPAIN-macos-%s.dmg" % platform.machine())
    for attempt in range(3):            # hdiutil is known to fail now and then with "resource busy"
        try:
            run(["hdiutil", "create", "-volname", "UnPAIN", "-srcfolder", staging,
                 "-ov", "-format", "UDZO", output])
            break
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(5)
    return [output]


def _iscc():
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base and Path(base, "Inno Setup 6", "ISCC.exe").exists():
            return str(Path(base, "Inno Setup 6", "ISCC.exe"))
    sys.exit("Inno Setup 6 not found. Install it from https://jrsoftware.org/isinfo.php "
             "or with: choco install innosetup")


def package_windows(display, numeric):
    from PIL import Image
    icon = BUILD / "desktop" / "unpain.ico"
    Image.open(ROOT / "desktop" / "icon.png").save(
        icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    name = "UnPAIN-windows-x64-setup"
    run([_iscc(), "/Q",
         "/DAppVersion=%s" % numeric, "/DDisplayVersion=%s" % display,
         "/DSourceDir=%s" % (FROZEN_DIR / "UnPAIN"), "/DOutputDir=%s" % DIST,
         "/DOutputName=%s" % name, "/DIconFile=%s" % icon,
         ROOT / "desktop" / "windows" / "unpain.iss"])
    return [DIST / (name + ".exe")]


def package_linux(debian):
    outputs = []
    tarball = DIST / "UnPAIN-linux-x86_64.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(FROZEN_DIR / "UnPAIN", arcname="UnPAIN")
    outputs.append(tarball)

    if not shutil.which("dpkg-deb"):
        print("dpkg-deb not found: skipping the .deb package")
        return outputs
    from PIL import Image
    tree = BUILD / "deb"
    shutil.rmtree(tree, ignore_errors=True)
    shutil.copytree(FROZEN_DIR / "UnPAIN", tree / "opt" / "unpain", symlinks=True)
    applications = tree / "usr" / "share" / "applications"
    applications.mkdir(parents=True)
    shutil.copy(ROOT / "desktop" / "linux" / "unpain.desktop", applications / "unpain.desktop")
    for size in (64, 128, 256, 512):
        icons = tree / "usr" / "share" / "icons" / "hicolor" / ("%dx%d" % (size, size)) / "apps"
        icons.mkdir(parents=True)
        Image.open(ROOT / "desktop" / "icon.png").resize((size, size), Image.LANCZOS).save(icons / "unpain.png")
    (tree / "usr" / "bin").mkdir(parents=True)
    (tree / "usr" / "bin" / "unpain").symlink_to("/opt/unpain/UnPAIN")
    (tree / "DEBIAN").mkdir()
    (tree / "DEBIAN" / "control").write_text(
        "Package: unpain\n"
        "Version: %s\n"
        "Section: misc\n"
        "Priority: optional\n"
        "Architecture: amd64\n"
        # PyInstaller bundles Python, Tk and their libraries, but not glibc, X11 and the font
        # stack Tk draws with; the build runs on Ubuntu 22.04, hence glibc 2.35.
        "Depends: libc6 (>= 2.35), libx11-6, libxft2, libfontconfig1, libfreetype6\n"
        "Recommends: xdg-utils\n"
        "Maintainer: UnPAIN <249798330+LeandroSampaioSolenis@users.noreply.github.com>\n"
        "Homepage: https://github.com/leandrorsampaio/unpain\n"
        "Description: Household expense accountability for two people, fully offline\n"
        " UnPAIN keeps every transaction on this computer. Your data lives in ~/UnPAIN.\n" % debian,
        encoding="utf-8")
    package = DIST / "UnPAIN-linux-amd64.deb"
    run(["dpkg-deb", "--build", "--root-owner-group", tree, package])
    outputs.append(package)
    return outputs


def main():
    display, numeric, debian = versions()
    print("UnPAIN desktop build %s (numeric %s) on %s/%s" % (display, numeric, sys.platform, platform.machine()))
    DIST.mkdir(exist_ok=True)
    build_app(display, numeric)
    self_test()
    if "--app-only" in sys.argv:
        print("built %s" % executable())
        return
    if sys.platform == "darwin":
        outputs = package_macos()
    elif os.name == "nt":
        outputs = package_windows(display, numeric)
    else:
        outputs = package_linux(debian)
    for output in outputs:
        print("built %s (%.1f MB)" % (output, output.stat().st_size / 1e6))


if __name__ == "__main__":
    main()
