# Installing tribonian-us

Four pieces, the same on every system. After them, `tribonian check` says what is there and what is missing.

1. **Python 3.10 or newer** and **git**: the pipeline (`tribonian` command).
2. **A TeX distribution** with XCharter, tcolorbox and ulem: the PDFs. Skip it for notes only (`MD_ONLY=1 tribonian build`).
3. **Poppler's `pdftotext`** (optional): Supreme Court filings, RECAP documents and some slip opinions.
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

The config file is `~/.config/tribonian-us/config.toml`. The token can also live in the GNOME keyring
(`secret-tool store --label='CourtListener API' service courtlistener`).

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

**The API token** (CourtListener, free account): put it in the config file,

```
courtlistener_token = "your token here"
```

or in the `COURTLISTENER_TOKEN` environment variable. [config.example.toml](config.example.toml) lists every setting.

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
- **Tested** on Linux daily; Windows and macOS by the test suite on every pull request (`.github/workflows/test.yml`).
