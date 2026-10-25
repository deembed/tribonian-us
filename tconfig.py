"""tconfig.py -- tribonian-us settings: environment variable, else the config file, else the default.

The config file (optional) is:
    $TRIBONIAN_CONFIG                                if set, else
    $XDG_CONFIG_HOME/tribonian-us/config.toml        (XDG_CONFIG_HOME defaults to ~/.config; also under WSL)
    %APPDATA%\tribonian-us\config.toml               (native Windows)

It is TOML. Top-level keys are shared by the scripts and the GUI; the [gui] table is the GUI's own.
See config.example.toml. Paths may start with ~; a relative path is relative to the repo.

    key                  variable               default
    bulk_dir             CL_BULK                <repo>/bulk
    main_db              CL_DB                  <bulk_dir>/courtlistener.sqlite
    opinions_db          CL_OPS_DB              <repo>/db/opinions.sqlite (if db/ exists), else beside main_db
    search_db            CL_SEARCH_DB           beside main_db
    cocite_db            CL_COCITE_DB           beside main_db (cocite.sqlite; views are cocite-<view>.sqlite)
    api_limits           CL_LIMITS              "5,50,125"  (per minute, hour, day)
    courtlistener_token  COURTLISTENER_TOKEN    secret-tool lookup service courtlistener

Standard library only. Python 3.11+ reads the file with tomllib; on 3.10 a small reader handles the
subset this file needs ([tables], key = "string" | 'string' | number | true/false, # comments).
"""
import os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# key -> environment variable
ENV = {"bulk_dir": "CL_BULK", "main_db": "CL_DB", "opinions_db": "CL_OPS_DB", "search_db": "CL_SEARCH_DB",
       "cocite_db": "CL_COCITE_DB", "api_limits": "CL_LIMITS", "courtlistener_token": "COURTLISTENER_TOKEN"}
PATH_KEYS = {"bulk_dir", "main_db", "opinions_db", "search_db", "cocite_db"}


def config_path():
    """The config file that would be read (it may not exist)."""
    if os.environ.get("TRIBONIAN_CONFIG"):
        return Path(os.environ["TRIBONIAN_CONFIG"]).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):          # native Windows (WSL is posix)
        base = os.environ["APPDATA"]
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "tribonian-us" / "config.toml"


_VALUE = re.compile(r'''\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^#]*?)\s*(?:#.*)?$''')


def _value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        return bytes(raw[1:-1], "utf-8").decode("unicode_escape")
    if raw.startswith("'"):
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        return float(raw)


def parse(text):
    """TOML subset (see the module docstring), for Python 3.10."""
    out, table = {}, None
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.fullmatch(r"\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?", s)
        if m:
            table = out.setdefault(m.group(1), {})
            continue
        k, eq, rest = s.partition("=")
        m = _VALUE.match(rest)
        if not eq or not k.strip() or not m or not m.group(1):
            raise ValueError("line %d: not key = value: %s" % (n, line))
        (table if table is not None else out)[k.strip().strip('"')] = _value(m.group(1))
    return out


def load(path=None):
    """(the file's settings as a dict, its path or None if there is none). A broken file raises ValueError."""
    p = Path(path) if path else config_path()
    if not p.is_file():
        return {}, None
    text = p.read_text(encoding="utf-8")
    try:
        import tomllib
        try:
            return tomllib.loads(text), p
        except tomllib.TOMLDecodeError as e:
            raise ValueError("%s: %s" % (p, e))
    except ImportError:
        try:
            return parse(text), p
        except ValueError as e:
            raise ValueError("%s: %s" % (p, e))


try:
    FILE, FILE_PATH = load()
    FILE_ERROR = None
except ValueError as e:          # reported by check.py; the scripts carry on with variables and defaults
    FILE, FILE_PATH, FILE_ERROR = {}, config_path(), str(e)
    print("tribonian-us: ignoring the config file: %s" % e, file=sys.stderr)


def _path(v):
    p = Path(str(v)).expanduser()
    return p if p.is_absolute() else HERE / p


def get(key, default=None):
    """The value in effect for key: its environment variable, else the config file, else default."""
    v, _ = source(key, default)
    return v


def source(key, default=None):
    """(value, where it came from: 'env CL_DB' | 'config' | 'default')."""
    var = ENV.get(key)
    if var and os.environ.get(var):
        v, src = os.environ[var], "env " + var
    elif key in FILE:
        v, src = FILE[key], "config"
    else:
        return default, "default"
    return (_path(v) if key in PATH_KEYS else v), src


def gui():
    """The [gui] table."""
    g = FILE.get("gui")
    return g if isinstance(g, dict) else {}
