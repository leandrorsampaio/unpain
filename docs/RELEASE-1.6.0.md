# UnPAIN 1.6.0 — the desktop release

**Download it. Install it. Double-click it. No terminal.**

UnPAIN used to start with `./start.sh`, which was fine for the partner who likes terminals and a
wall for the one who does not. 1.6 is an app: an installer for macOS, Windows, and Linux, with
Python inside. Nothing about the ledger changes. Same files, same math, same guarantees, and your
data still never leaves your computer.

---

## Why this release exists

A household tool that only one of the two people can start is half a household tool. The math was
never the obstacle. The obstacle was "open a terminal, create a virtual environment, install the
requirements". 1.6 removes that step and leaves everything else alone.

---

## Install

Get the installer from the [website](https://leandrorsampaio.github.io/unpain/#download). The top
button picks the right file for your computer. You can also take it from the
[release page](https://github.com/leandrorsampaio/unpain/releases/latest).

| Computer | File |
|---|---|
| Mac with Apple silicon (M1 and newer) | `UnPAIN-macos-arm64.dmg` |
| Mac with Intel | `UnPAIN-macos-x86_64.dmg` |
| Windows 10 or 11 (64-bit) | `UnPAIN-windows-x64-setup.exe` |
| Linux: Debian, Ubuntu | `UnPAIN-linux-amd64.deb` |
| Linux: other | `UnPAIN-linux-x86_64.tar.gz` |

**The installers are not signed.** Signing costs money every year, and UnPAIN is free. So macOS and
Windows warn you the first time. You do this step one time only:

- **macOS:** open the app, click **Done** on the warning, then go to **System Settings → Privacy &
  Security** and click **Open Anyway**.
- **Windows:** on "Windows protected your PC", click **More info**, then **Run anyway**. The
  installer does not need an administrator account.
- **Linux:** no warning.

---

## What you get

- **A small window and your browser.** When you open UnPAIN, a window shows that it runs, its
  address, and where your data is. Your browser opens the app. To stop UnPAIN, close the window or
  click **Quit**. If you open UnPAIN again while it runs, it only opens the browser again.
- **Your data in a folder you can see.** The data lives in `UnPAIN` in your home folder:
  `~/UnPAIN` on macOS and Linux, `C:\Users\<you>\UnPAIN` on Windows. It is not in Documents,
  because macOS and Windows can sync Documents to iCloud or OneDrive. An update or an uninstall
  never touches it. To update, install the new version over the old one.
- **A window in your language.** The window follows the language you chose in Settings (English or
  German).
- **A guard against other web pages.** The app runs beside your normal browser all day, so it
  refuses requests that other sites' pages make to it. A page in another tab cannot upload a file,
  start a restore or a backup, or read your data.

---

## Already running from source?

Nothing changes for you. `./start.sh`, LAN mode, and the CLI work as before, and the new guard is
off unless the desktop app turns it on.

To move to the app: in your current UnPAIN, go to **Settings → Data** and download a backup.
Install the app, complete the setup wizard, then restore the backup in **Settings → Data**.

---

## Fixed

- **Running from source on Windows could not work.** `pip install -r requirements.txt` failed,
  because the lockfile listed `uvloop` (no Windows support) for every platform. The lockfile is now
  compiled for all platforms at once.
- **The test workflow was red on `main` since 1.5.0.** A fresh clone has no `config.json`, and the
  first check in `run-tests.sh` asked for one. It no longer does, and CI is green again.
- **A failed exchange-rate download now says what to do**, instead of showing a bare
  `<urlopen error ...>`.

An external code review of the desktop app found 13 problems before release, all fixed and covered
by tests. The three that mattered most: the built app could not verify HTTPS certificates, so the
first foreign-currency import would have failed; a quick quit-and-reopen on macOS moved the app to
another address and lost your theme and year selection; and on Linux the window title was a
4-pixel font.

---

## What this release does not claim

- **The installers are unsigned** (see above). That is a cost decision, not an oversight.
- **Every installer passes an automated self-test on its own operating system** before it is
  packaged: page, setup wizard, bank formats, window toolkit, certificates, and the guard. The
  macOS app was also tested by hand. The Windows and Linux apps have so far been tested only by
  that self-test, not by a person.
- **PDF statement import needs `pdftotext`**, which the app does not include. On Linux, install
  `poppler-utils`. On macOS and Windows, use the CSV export of your bank.
- **The app answers only on this computer.** For access from other devices on your home network,
  run from source with `./start.sh --lan`.
- **There is no automatic update.** Install the new version over the old one.

---

*UnPAIN is self-hosted, offline, and free for noncommercial use under
[PolyForm Noncommercial 1.0.0](../LICENSE). No accounts, no telemetry, no cloud.*
