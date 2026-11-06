# Installing tribonian-us

## The app (no terminal)

1. Download the file for your system from the [latest release](https://github.com/deembed/tribonian-us/releases/latest):
   `...-windows-x64.zip`, `...-macos-universal.zip` (Apple silicon and Intel) or `...-linux-x64.tar.gz`.
2. Unzip it and open **Tribonian** (the approval steps below come first, once).
3. The app opens on its **Sync** page. Press **Set up everything**: it installs the Python pipeline (with
   [uv](https://docs.astral.sh/uv/), which brings its own Python) and LaTeX (TinyTeX, for your user only; no
   administrator rights), then stops at the CourtListener API key.
4. Paste a key (a free [CourtListener](https://www.courtlistener.com/) account, then your profile's API page), or press
   **Skip** to start offline. You can add a key, or the bulk data, on the Sync page any time.

The app opens on Sync until those rows pass, and after that on its start screen; Sync stays one click away there.
Everything goes into your user folders: the app's own files in `%LOCALAPPDATA%\tribonian-us` (Windows) or
`~/.local/share/tribonian-us` (macOS, Linux), settings in the [config file](#notes). The API key goes into the
system's key store (Windows Credential Manager, macOS Keychain, Linux Secret Service).

**Bulk data** (optional, large) is under **Add bulk data** on the Sync page: CourtListener's quarterly snapshot, about
60 GB to download and 135 GB loaded. Downloads pick up where they stopped, after a crash too; an update is built
beside the current database and swapped in only when its health check passes.

### Windows: "Windows protected your PC"

The app isn't signed, so SmartScreen stops it the first time. Click **More info**, then **Run anyway**.

<!-- screenshot: docs/img/install-windows-smartscreen.png (the SmartScreen box with "More info" and "Run anyway") -->

### macOS: "Tribonian can't be opened"

The app isn't notarized by Apple, so macOS refuses the first double-click. Open **System Settings > Privacy & Security**,
scroll to the message about Tribonian, and click **Open Anyway**; confirm, and enter your password if asked. (On macOS
14 and older, Control-click the app and choose **Open** instead.)

<!-- screenshot: docs/img/install-macos-open-anyway.png (Privacy & Security with "Open Anyway") -->

### Linux

Unpack it and run `./tribonian` (or double-click it in a file manager that runs programs). On Linux Mint the key store
is GNOME Keyring, already running.

## From source

`git clone`, then `./bootstrap.sh` (Windows: `bootstrap.ps1`). It tells you which of the pieces below to install if
any is missing, then builds and opens the app on its Sync page. The app sees the checkout and uses it rather than
installing a copy.

By hand: four pieces, the same on every system. After them, `tribonian check` says what is there and what is missing.

1. **Python 3.10 or newer** and **git**: the pipeline (`tribonian` command).
2. **A TeX distribution** with XCharter, tcolorbox and ulem: the PDFs. Skip it for notes only (`MD_ONLY=1 tribonian build`).
3. **Poppler's `pdftotext`** (optional): Supreme Court filings, RECAP documents and some slip opinions. (Supreme Court filings also use pdfplumber, a Python package the install pulls in, to find footnotes.)
4. **Rust** (optional): the desktop browser in `gui/`.

Then [the same steps everywhere](#then-on-every-system). The [README](README.md#getting-started) takes it from
there (US Code, CourtListener token, local database, co-citation maps).

## Windows

Run these in PowerShell. `winget` comes with Windows 10 and 11.

```
winget install Python.Python.3.12 Git.Git MiKTeX.MiKTeX oschwartz10612.Poppler
```

Close PowerShell and open a new one, so the new programs are on the PATH. Then install the LaTeX packages
(MiKTeX can also fetch them on first use, but a build running many PDFs at once should not wait on that):

```
miktex packages install xcharter tcolorbox ulem
```

If `miktex` is not found, use MiKTeX Console > Packages, search for each, and install it.

For the desktop browser: install Rust from <https://rustup.rs/> (its installer offers the Visual Studio C++ build
tools Rust needs on Windows; accept them).

In the steps below, Windows differs in two places:

- Activate the virtual environment with `.\.venv\Scripts\Activate.ps1`. If PowerShell refuses to run it, run
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
- The config file is `%APPDATA%\tribonian-us\config.toml` (in PowerShell: `$env:APPDATA\tribonian-us\config.toml`).
  Write Windows paths in it in single quotes, `bulk_dir = 'D:\courtlistener'`, or with forward slashes,
  `"D:/courtlistener"`: inside double quotes a backslash starts an escape, and the file is rejected.

## macOS

With Homebrew (<https://brew.sh/>):

```
brew install python git poppler
brew install --cask mactex-no-gui
```

Open a new terminal afterwards. MacTeX is the full TeX Live, so XCharter, tcolorbox and ulem are already in it.
With the smaller BasicTeX instead, add them: `sudo tlmgr install xcharter tcolorbox ulem`.

For the desktop browser: `xcode-select --install` (the C compiler Rust needs), then Rust from <https://rustup.rs/>.

The config file is `~/.config/tribonian-us/config.toml`.

## Linux (Debian, Ubuntu, Mint)

```
sudo apt install python3 python3-venv git texlive-latex-extra texlive-fonts-extra texlive-plain-generic poppler-utils lbzip2 libsecret-tools
```

For the desktop browser: Rust from <https://rustup.rs/>, plus `sudo apt install build-essential`.

The config file is `~/.config/tribonian-us/config.toml`. A token stored earlier with
`secret-tool store --label='CourtListener API' service courtlistener` is still read.

## Then, on every system

```
git clone https://github.com/deembed/tribonian-us.git
cd tribonian-us
python3 -m venv .venv
```

(On Windows the command is `python`, not `python3`: `python -m venv .venv`.)

Activate the virtual environment: `source .venv/bin/activate` (macOS, Linux) or `.\.venv\Scripts\Activate.ps1`
(Windows). Then:

```
pip install -e ".[test]"
python -m pytest -q
tribonian check
```

`tribonian check` lists each tool and database with a fix for anything missing. On a new install it reports the
API token and the databases as missing: those come next, in the README's [Getting started](README.md#getting-started)
from stage 2.

**The API token** (CourtListener, free account): paste it on the app's Sync page, or

```
tribonian setup key set
```

and paste it there (it reads standard input, so the token stays out of your shell history). It goes into the system's
key store; where there is none it falls back to the config file, readable only by you, and says so. The
`COURTLISTENER_TOKEN` environment variable overrides both. [config.example.toml](config.example.toml) lists every setting.

**The desktop browser**, once Rust is installed:

```
cd gui
cargo run --release
```

It finds the scripts in the checkout above it. On Windows it runs `python`, elsewhere `python3`; to use the
virtual environment's interpreter, set `python` in the config file's `[gui]` table to its full path
(`.venv\Scripts\python.exe` or `.venv/bin/python`).

## Notes

- **Large data.** The CourtListener bulk files and databases run to hundreds of gigabytes. Put `bulk_dir` and
  `opinions_db` on a large disk (an SSD for `opinions.sqlite`); see the README's [Configuration](README.md#configuration).
- **reMarkable sync** (`tribonian sync-remarkable`) needs `rmapi`; see the README's [Obsidian and reMarkable](README.md#7-obsidian-and-remarkable).
- **Tested** on Linux daily; Windows and macOS by the test suite (`.github/workflows/test.yml`), run by hand while the repository is private.
