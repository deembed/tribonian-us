#!/bin/sh
# bootstrap.sh -- from a fresh clone to the app's Setup guide (Linux, macOS, WSL). Windows: bootstrap.ps1.
#
#     ./bootstrap.sh            # virtual environment, Python packages, build and open the app
#     ./bootstrap.sh --no-gui   # the same, but stop before Rust and the app (command line only)
#
# Needs no root. When a tool is missing it prints the install command and stops; run that, then run this again.
# Safe to rerun. An active virtual environment is used instead of .venv. The Setup guide takes over
# from there: where the data goes, the CourtListener API token, the database.
set -e
cd "$(dirname "$0")"

missing() {       # missing <what> <Linux command> <macOS command>
    echo "Missing: $1. Install it, then run ./bootstrap.sh again:" >&2
    if [ "$(uname -s)" = Darwin ]; then echo "    $3" >&2; else echo "    $2" >&2; fi
    exit 1
}

if [ -n "$VIRTUAL_ENV" ]; then
    VENV=$VIRTUAL_ENV
else
    VENV=$PWD/.venv
fi
PY=$VENV/bin/python

if [ ! -x "$PY" ]; then
    command -v python3 >/dev/null 2>&1 ||
        missing "Python 3.10 or newer" "sudo apt install python3 python3-venv" "brew install python"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
        missing "Python 3.10 or newer (found $(python3 --version))" "sudo apt install python3 python3-venv" "brew install python"
    echo "== virtual environment: $VENV"
    python3 -m venv "$VENV" ||
        missing "the venv module" "sudo apt install python3-venv" "brew install python"
fi

echo "== Python packages"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e .

# Before the database load, or the opinion text lands beside the main database.
mkdir -p xml bulk db

# So the app runs the same interpreter.
PYTHONPATH=src "$PY" -m tribonian setup set gui.python "$PY"

if [ "$1" = "--no-gui" ]; then
    echo "== done. Next: $VENV/bin/tribonian setup"
    exit 0
fi

command -v cc >/dev/null 2>&1 ||
    missing "a C compiler" "sudo apt install build-essential" "xcode-select --install"
command -v cargo >/dev/null 2>&1 ||
    missing "Rust" "see https://rustup.rs/ and then open a new terminal" "see https://rustup.rs/ and then open a new terminal"

echo "== building and opening the app; its Setup guide is on the start screen"
cd gui
exec cargo run --release
