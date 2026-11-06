"""setup_guide.py -- the Sync page's back end: what an install has, and the commands that fill it in.

    tribonian setup                         # the Sync page's rows as text: Tribonian, LaTeX, API key, bulk data
    tribonian setup --json [--remote]       # the same as JSON (the GUI reads this); --remote also asks
                                            #   CourtListener for the latest bulk set (one request per file)
    tribonian setup key set                 # read a token from standard input and store it (keystore.py)
    tribonian setup key skip | forget       # go without a token (offline) | remove the stored one
    tribonian setup test-token              # one live CourtListener request
    tribonian setup latex [install]         # is pdflatex there with the packages | install TinyTeX and the packages
    tribonian setup download [--only NAME ...] [--date YYYY-MM-DD]   # bulk files, restartable
    tribonian setup update                  # load the downloaded set beside the current database, check it, swap
    tribonian setup set KEY [VALUE]         # write one setting to the config file (no VALUE: read stdin)

The Sync page's start-up rule: the app opens on Sync until the required rows pass (Tribonian, LaTeX, and the API
key answered: a token stored or Skip chosen). The data mode is chosen here only on first setup and when the key
or the data row changes state, never over a mode the user picked later.

The statuses come from check.py's checks, so this can't disagree with tribonian check. bootstrap.sh and
bootstrap.ps1 (installs from a checkout) call `setup set` with only the standard library installed.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

from tribonian import tconfig

S3 = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com"
BULK_FILES = ["courts", "citations", "parentheticals", "citation-map", "opinion-clusters", "dockets", "opinions"]
# README stage 4, in GB. The downloads can be deleted after the load.
GB_DOWNLOADS, GB_MAIN, GB_OPINIONS = 60, 25, 110
SETTABLE = {"courtlistener_token", "bulk_dir", "opinions_db", "data_mode", "gui.python"}
RETRIES = (5, 15, 45, 120)            # seconds between attempts after a network error; then stop and show Resume


# ------------------------------------------------------------------ the config file

def quoted(value):
    """A TOML basic string: backslashes and quotes escaped, so a Windows path survives."""
    return json.dumps(value, ensure_ascii=False)


def set_text(text, key, value):
    """The config text with one setting replaced, added, or (value "") removed; every other line kept.
    "gui.python" is python in the [gui] table; other keys are top level."""
    table, _, name = key.rpartition(".")
    lines = text.splitlines()
    start = 0
    if table:
        head = [i for i, l in enumerate(lines) if l.strip() == "[%s]" % table]
        if head:
            start = head[0] + 1
        elif value:
            lines += ["", "[%s]" % table]
            start = len(lines)
    end = next((i for i in range(start, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pattern = re.compile(r"\s*%s\s*=" % re.escape(name))
    at = next((i for i in range(start, end) if pattern.match(lines[i])), None)
    new = "%s = %s" % (name, quoted(value))
    if at is not None:
        if value:
            lines[at] = new
        else:
            del lines[at]
    elif value:
        while end > start and not lines[end - 1].strip():      # before the blank lines that end the block
            end -= 1
        lines.insert(end, new)
    return "\n".join(lines) + "\n"


def write_setting(path, key, value):
    """Change one setting; the file is replaced whole, only if the result parses, and readable only by the
    owner since it may hold the API token."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    new = set_text(text, key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        tconfig.load(tmp)                                       # raises ValueError on a broken result
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def cmd_set(args):
    if args.key not in SETTABLE:
        sys.exit("tribonian setup set: %s is not settable here: %s" % (args.key, ", ".join(sorted(SETTABLE))))
    value = args.value if args.value is not None else sys.stdin.read()
    value = value.strip()
    if args.key == "courtlistener_token":                       # one code path for the token: keystore.py
        return cmd_key(argparse.Namespace(action="set", value=value))
    if args.key == "data_mode" and value and value not in tconfig.DATA_MODES:
        sys.exit("data_mode must be one of: " + ", ".join(tconfig.DATA_MODES))
    path = tconfig.config_path()
    try:
        write_setting(path, args.key, value)
    except ValueError as e:
        sys.exit("not saved: %s\nFix the config file first (see config.example.toml)." % e)
    print("%s %s in %s" % ("saved" if value else "removed", args.key, path))
    if args.key in ("bulk_dir", "opinions_db"):
        print("Restart the app so it reads the new location.")
    return 0


# ------------------------------------------------------------------ data mode

def data_level():
    """What local data is installed: "full" (graph + opinion text), "lite" (graph only) or None."""
    from tribonian import casedb, check
    if not casedb.DB.exists() or Path(str(casedb.DB) + ".loading").exists():
        return None
    try:
        con = check.ro(casedb.DB)
        names = {r[0] for r in con.execute("SELECT name FROM loaded")}
    except Exception:
        return None
    if not {"citation-map", "opinion-clusters"} <= names:
        return None
    return "full" if casedb.OPS_DB.exists() else "lite"


def row_state():
    from tribonian import keystore
    return bool(keystore.lookup()[0]), data_level()


def pick_mode(key, data):
    """scope-sync.md's table: skipped + none -> offline; key + none -> api; skipped + data -> offline; key + data -> full
    (lite when the opinion text isn't local)."""
    if not key:
        return "offline"
    return data or "api"


def after_change(before, say=print):
    """Choose the mode on first setup, or when the key or data row changed state; otherwise leave the user's pick."""
    after = row_state()
    if "data_mode" in tconfig.FILE and after == before:
        return
    mode = pick_mode(*after)
    write_setting(tconfig.config_path(), "data_mode", mode)
    tconfig.FILE["data_mode"] = mode
    say("data mode: %s" % mode)


# ------------------------------------------------------------------ API key

def cmd_key(args):
    from tribonian import keystore
    before = row_state()
    if args.action == "set":
        value = args.value if getattr(args, "value", None) is not None else sys.stdin.read()
        try:
            print(keystore.store(value))                          # never prints the token
        except ValueError:
            sys.exit("no token given (paste it on standard input)")
    elif args.action == "skip":
        print(keystore.skip())
    else:
        keystore.forget()
        print("removed the stored token")
    after_change(before)
    return 0


def cmd_test_token(args):
    from tribonian import check
    check.RESULTS.clear()
    check.run_api(True)
    live = next(r for r in check.RESULTS if r.key == "api:live")
    print("%s: %s" % (live.status, live.detail))
    return 0 if live.status == "ok" else 1


# ------------------------------------------------------------------ LaTeX

TINYTEX = {"posix": "https://yihui.org/tinytex/install-bin-unix.sh",
           "nt": "https://yihui.org/tinytex/install-bin-windows.bat"}


def latex_status():
    """(status, text): ok when pdflatex is there with XCharter, tcolorbox and ulem (check.py's test)."""
    from tribonian import check
    check.RESULTS.clear()
    check.run_tools()
    c = next(r for r in check.RESULTS if r.key == "tool:pdflatex")
    where = shutil.which("pdflatex")
    text = c.detail + (" (%s)" % where if where and c.status == "ok" else "")
    return ("ok" if c.status == "ok" else "fail"), text


def smoke_tex():
    """A one-line document with the real preamble, so every package the builders load is tested."""
    from tribonian import render
    pre = re.sub(r"__[A-Z]+__", "", render.PREAMBLE.replace("__FONTPKG__", "XCharter"))
    return pre + "\n\\begin{document}\n\\sout{Tribonian} \\begin{tcolorbox}test\\end{tcolorbox}\n\\end{document}\n"


def missing_file(log):
    m = re.search(r"File `([^']+)' not found", log) or re.search(r"Font \S+=(\S+) at .* not loadable: Metric \(TFM\) file", log)
    return m.group(1) if m else None


def compile_smoke(say=print, tries=25):
    """Compile smoke_tex(); with TinyTeX's tlmgr, install each missing package and try again. True on a PDF."""
    tlmgr = shutil.which("tlmgr")
    tinytex = bool(tlmgr) and "tinytex" in tlmgr.lower()
    with tempfile.TemporaryDirectory() as d:
        Path(d, "t.tex").write_text(smoke_tex(), encoding="utf-8")
        for _ in range(tries):
            r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "t.tex"], cwd=d,
                               capture_output=True, text=True, errors="replace")
            if r.returncode == 0 and Path(d, "t.pdf").exists():
                return True
            log = Path(d, "t.log").read_text(errors="replace") if Path(d, "t.log").exists() else r.stdout
            f = missing_file(log)
            if not f or not tinytex:
                say("pdflatex failed%s" % (": missing %s" % f if f else "") + ("" if tinytex else
                    " -- install it with your TeX distribution's package manager"))
                return False
            found = subprocess.run([tlmgr, "search", "--global", "--file", "/" + f], capture_output=True, text=True).stdout
            pkg = next((l.rstrip(":") for l in found.splitlines() if l.endswith(":") and not l.startswith(" ")), None)
            if not pkg:
                say("no TeX Live package provides %s" % f)
                return False
            say("installing LaTeX package %s (for %s)" % (pkg, f))
            subprocess.run([tlmgr, "install", pkg], check=False)
    return False


def cmd_latex(args):
    if args.action != "install":
        status, text = latex_status()
        print("%s: %s" % (status, text))
        return 0 if status == "ok" else 1
    if not shutil.which("pdflatex"):
        url = TINYTEX["nt" if os.name == "nt" else "posix"]
        print("installing TinyTeX (per user, no administrator rights) from %s" % url)
        with tempfile.TemporaryDirectory() as d:
            script = Path(d, url.rsplit("/", 1)[1])
            with urllib.request.urlopen(url, timeout=120) as r:
                script.write_bytes(r.read())
            cmd = ["cmd", "/c", str(script)] if os.name == "nt" else ["sh", str(script)]
            if subprocess.run(cmd).returncode:
                sys.exit("the TinyTeX installer failed; see its output above")
        tconfig.add_tinytex_to_path()
        if not shutil.which("pdflatex"):
            sys.exit("TinyTeX installed, but pdflatex isn't in %s; restart the app" % tconfig.tinytex_bin())
    print("checking the packages with a test document")
    ok = compile_smoke()
    print("LaTeX: %s" % ("ready" if ok else "not ready"))
    return 0 if ok else 1


# ------------------------------------------------------------------ bulk downloads

def listing(name, base=S3):
    """{date: (size, etag)} of one bulk file's quarterly sets (S3's list API)."""
    url = "%s/?list-type=2&prefix=bulk-data/%s-" % (base, name)
    with urllib.request.urlopen(url, timeout=60) as r:
        xml = r.read().decode("utf-8")
    out = {}
    for item in re.findall(r"<Contents>(.*?)</Contents>", xml, re.S):
        key = re.search(r"<Key>bulk-data/([^<]+)</Key>", item)
        size = re.search(r"<Size>(\d+)</Size>", item)
        etag = re.search(r"<ETag>([^<]*)</ETag>", item)
        m = key and re.fullmatch(re.escape(name) + r"-(\d{4}-\d\d-\d\d)\.csv\.bz2", key.group(1))
        if m and size:
            out[m.group(1)] = (int(size.group(1)), etag.group(1).replace("&quot;", '"') if etag else "")
    return out


def newest_set(sets):
    """The newest date every file has: one set must not mix quarters. sets: {name: {date: ...}}."""
    common = set.intersection(*(set(s) for s in sets.values())) if sets else set()
    return max(common) if common else None


def bz2_ok(path):
    """The whole bzip2 stream decompresses (a cut or corrupt download fails here, not halfway through a load)."""
    tool = next((t for t in ("lbzip2", "pbzip2", "bzip2") if shutil.which(t)), None)
    if tool:
        return subprocess.run([tool, "-t", str(path)], capture_output=True).returncode == 0
    import bz2
    try:
        with bz2.open(path, "rb") as f:
            while f.read(1 << 24):
                pass
        return True
    except (OSError, EOFError, ValueError):
        return False


def _get(url, part, size, etag, say):
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have == size:
        return
    headers = {}
    if have:
        headers["Range"] = "bytes=%d-" % have
        if etag:
            headers["If-Range"] = etag                           # changed on the server: a 200 with the whole file
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        if have and r.status != 206:                             # range ignored or file changed: start over
            have = 0
        step, shown = max(size // 20, 1), have
        with open(part, "ab" if have else "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                if have - shown >= step:
                    shown = have
                    say("  %s: %d%%" % (part.name[:-5], have * 100 // size))
    if have < size:                                              # a cut connection can end the stream quietly
        raise OSError("connection closed at %d of %d bytes" % (have, size))


def fetch(url, dest, size, etag="", say=print, delays=RETRIES):
    """Download to dest through <dest>.part, resuming with a Range request. The size and ETag recorded at the start
    sit in <dest>.part.json; if either changes, the file starts over. Network errors are retried after each delay,
    then raised. A finished file is checked (size, bzip2 stream) before it gets its real name."""
    part = dest.with_name(dest.name + ".part")
    meta = dest.with_name(dest.name + ".part.json")
    want = {"size": size, "etag": etag}
    try:
        was = json.loads(meta.read_text())
    except (OSError, ValueError):
        was = None
    if part.exists() and was is not None and was != want:
        say("  %s changed on the server: starting over" % dest.name)
        part.unlink()
    meta.write_text(json.dumps(want))
    for i in range(len(delays) + 1):
        try:
            _get(url, part, size, etag, say)
            break
        except OSError as e:                                      # URLError, timeouts and resets are all OSError
            if i == len(delays):
                raise OSError("%s: %s" % (dest.name, e))
            say("  %s: %s; trying again in %d s" % (dest.name, e, delays[i]))
            time.sleep(delays[i])
    got = part.stat().st_size
    if got != size:
        raise OSError("%s: got %d of %d bytes" % (dest.name, got, size))
    say("  %s: testing the bzip2 stream" % dest.name)
    if not bz2_ok(part):
        part.unlink()
        meta.unlink(missing_ok=True)
        raise OSError("%s: the download is corrupt and was deleted; download it again" % dest.name)
    part.replace(dest)
    meta.unlink(missing_ok=True)


def download(bulk, date=None, base=S3, names=BULK_FILES, say=print, only=None, delays=RETRIES):
    """Fetch one quarterly set (or the files in only) into bulk; files already complete are skipped."""
    sets = {n: listing(n, base) for n in names}
    date = date or newest_set(sets)
    lacking = [n for n in names if date not in sets[n]]
    if not date or lacking:
        raise OSError("no set for %s has all the files (missing: %s)" % (date or "any date", ", ".join(lacking or names)))
    bulk = Path(bulk)
    bulk.mkdir(parents=True, exist_ok=True)
    todo = []
    for n in (only or names):
        dest = bulk / ("%s-%s.csv.bz2" % (n, date))
        size, etag = sets[n][date]
        if dest.exists() and dest.stat().st_size == size:
            say("have %s" % dest.name)
        else:
            todo.append((dest, size, etag))
    part = lambda d: d.with_name(d.name + ".part")
    need = sum(s - (part(d).stat().st_size if part(d).exists() else 0) for d, s, _ in todo)
    free = shutil.disk_usage(bulk).free
    if need > free:
        raise OSError("%.0f GB to download, %.0f GB free in %s: choose a larger disk for the bulk folder" % (need / 1e9, free / 1e9, bulk))
    for dest, size, etag in sorted(todo, key=lambda t: t[1]):     # smallest first: early failures are cheap
        say("downloading %s (%.1f GB)" % (dest.name, size / 1e9))
        fetch("%s/bulk-data/%s" % (base, dest.name), dest, size, etag, say, delays)
    return date


def cmd_download(args):
    from tribonian import casedb
    if not casedb.OPS_DB.exists() and "opinions_db" not in tconfig.FILE:
        (tconfig.WORK / "db").mkdir(parents=True, exist_ok=True)  # or the opinion text lands beside the main database
    try:
        date = download(casedb.BULK, args.date, only=args.only)
    except OSError as e:
        sys.exit("download stopped: %s\nResume picks up where it stopped." % e)
    print("set %s is in %s; next: tribonian setup update" % (date, casedb.BULK))
    return 0


# ------------------------------------------------------------------ local files and versions

DATED = re.compile(r"(.+)-(\d{4}-\d\d-\d\d)\.csv\.bz2$")


def local_files(bulk):
    """{name: {"complete": [dates], "part": (date, bytes, size or None)}} from what is on disk."""
    out = {n: {"complete": [], "part": None} for n in BULK_FILES}
    if not Path(bulk).is_dir():
        return out
    for p in Path(bulk).iterdir():
        name = p.name
        partial = name.endswith(".csv.bz2.part")
        m = DATED.match(name[:-5] if partial else name)
        if not m or m.group(1) not in out:
            continue
        if partial:
            try:
                size = json.loads(p.with_name(p.name + ".json").read_text())["size"]
            except (OSError, ValueError, KeyError):
                size = None
            out[m.group(1)]["part"] = (m.group(2), p.stat().st_size, size)
        else:
            out[m.group(1)]["complete"].append(m.group(2))
    for v in out.values():
        v["complete"].sort()
    return out


def loaded_dates(db):
    """{name: date} of the set each table in the database was loaded from."""
    from tribonian import check
    if not Path(db).exists():
        return {}
    try:
        rows = check.ro(db).execute("SELECT name, file FROM loaded").fetchall()
    except Exception:
        return {}
    out = {}
    for name, f in rows:
        m = DATED.match(f or "")
        if m:
            out[name] = m.group(2)
    return out


def complete_set(files):
    """The newest date every bulk file is downloaded for, or None."""
    sets = [set(v["complete"]) for v in files.values()]
    common = set.intersection(*sets) if sets else set()
    return max(common) if common else None


# ------------------------------------------------------------------ load beside the current database, check, swap

def staged():
    """[(live path, staged path)] for the three files a load writes. Each staged file sits in next/ beside its live one,
    so the swap is a rename on the same disk."""
    from tribonian import casedb
    return [(p, p.parent / "next" / p.name) for p in (casedb.DB, casedb.OPS_DB, casedb.SEARCH_DB)]


def finish_swap(say=print):
    """Rename every staged file over its live one. Marked by next/SWAPPING, so a crash part way through is finished
    on the next run rather than leaving the live files from two sets."""
    pairs = staged()
    marker = pairs[0][1].parent / "SWAPPING"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("swapping\n")
    lock = Path(str(pairs[0][0]) + ".loading")                    # readers wait while the files change
    lock.write_text("tribonian setup update: swapping, pid %d\n" % os.getpid())
    try:
        for live, nxt in pairs:
            if nxt.exists():
                live.parent.mkdir(parents=True, exist_ok=True)
                os.replace(nxt, live)
                say("  %s -> %s" % (nxt, live))
    finally:
        lock.unlink(missing_ok=True)
    marker.unlink()
    for _, nxt in pairs:
        try:
            nxt.parent.rmdir()
        except OSError:
            pass


def space_needed():
    """{device: (path, GB needed, GB free)}: the new databases beside the old ones."""
    from tribonian import casedb
    def disk(p):
        p = Path(p).resolve()
        while not p.exists() and p != p.parent:
            p = p.parent
        return p
    need = {}
    for live, gb in ((casedb.DB, GB_MAIN), (casedb.OPS_DB, GB_OPINIONS)):
        if live.exists():
            gb = live.stat().st_size / 1e9 * 1.1                   # the new set is about the old one's size, a little more
        at = disk(live.parent)
        key = at.stat().st_dev
        need[key] = (at, need.get(key, (at, 0))[1] + gb)
    return {k: (at, gb, shutil.disk_usage(at).free / 1e9) for k, (at, gb) in need.items()}


def stale_derived():
    """Data built from the bulk set that an update leaves behind (the search index and court indexes are rebuilt by
    the update itself; match ledger entries are dated by bulk set already)."""
    from tribonian import check
    out = []
    views = sorted(check.COCITE_BASE.parent.glob("cocite*.sqlite"))
    if views:
        out.append({"what": "co-citation clusters", "files": [str(v) for v in views],
                    "rebuild": "tribonian cocite prep && tribonian cocite build --courts appellate"})
    pos = check.COCITE_CACHE / "posidx"
    if pos.is_dir():
        out.append({"what": "citing-sentence index", "files": [str(pos)], "rebuild": "tribonian cocite positions --index"})
    return out


def STALE_FILE():
    from tribonian import casedb
    return casedb.DB.with_name("stale-derived.json")


def stale_after_update():
    """What the last update made stale, minus what has been rebuilt since (a file newer than the swap)."""
    p = STALE_FILE()
    if not p.exists():
        return []
    try:
        rec = json.loads(p.read_text())
    except ValueError:
        return []
    swapped = p.stat().st_mtime
    left = [s for s in rec["stale"] if any(Path(f).exists() and Path(f).stat().st_mtime < swapped for f in s["files"])]
    if not left:
        p.unlink(missing_ok=True)
    return left


def cmd_update(args):
    from tribonian import casedb
    pairs = staged()
    if (pairs[0][1].parent / "SWAPPING").exists():
        print("finishing an interrupted swap")
        finish_swap()
    files = local_files(casedb.BULK)
    date = complete_set(files)
    if not date:
        sys.exit("no complete set in %s: download first (tribonian setup download)" % casedb.BULK)
    loaded = loaded_dates(casedb.DB)
    if loaded and all(loaded.get(n) == date for n in BULK_FILES):
        print("set %s is already loaded" % date)
        return 0
    short = [(at, gb, free) for at, gb, free in space_needed().values() if free < gb]
    if short:
        sys.exit("\n".join("%s: %.0f GB free, the new database needs about %.0f GB there (beside the current one)"
                           % s for s in short))
    before = row_state()
    env = dict(os.environ, CL_DB=str(pairs[0][1]), CL_OPS_DB=str(pairs[1][1]), CL_SEARCH_DB=str(pairs[2][1]))
    for _, nxt in pairs:
        nxt.parent.mkdir(parents=True, exist_ok=True)
    tri = [sys.executable, "-m", "tribonian"]
    print("loading set %s beside the current database (into next/)" % date)
    for step in (["db", "load"], ["db", "index"]):
        if subprocess.run(tri + step, env=env).returncode:
            sys.exit("`tribonian %s` failed; the current database is unchanged. Run update again to retry." % " ".join(step))
    r = subprocess.run(tri + ["check", "--json"], env=env, capture_output=True, text=True)
    try:
        checks = json.loads(r.stdout)["checks"]
    except (ValueError, KeyError):
        sys.exit("the health check gave no answer; the current database is unchanged\n" + r.stderr[-2000:])
    bad = [c for c in checks if (c["key"].startswith("db:") or c["key"] in ("ops:text", "idx:search")) and c["status"] == "fail"]
    if bad:
        sys.exit("health check failed on the new database; the current one is unchanged:\n" +
                 "\n".join("  %s: %s" % (c["label"], c["detail"]) for c in bad))
    print("health check passed; swapping")
    finish_swap()
    stale = stale_derived()
    if stale:
        STALE_FILE().write_text(json.dumps({"set": date, "stale": stale}, indent=1))
    for s in stale:
        print("built from the previous set: %s (%s). Rebuild: %s" % (s["what"], ", ".join(s["files"]), s["rebuild"]))
    after_change(before)
    return 0


# ------------------------------------------------------------------ the Sync page

def version():
    """The installed package's version; from a checkout run with PYTHONPATH=src, pyproject.toml's."""
    if tconfig.REPO:
        m = re.search(r'^version\s*=\s*"([^"]+)"', (tconfig.REPO / "pyproject.toml").read_text(), re.M)
        if m:
            return m.group(1)
    try:
        from importlib.metadata import version as v
        return v("tribonian-us")
    except Exception:
        return "unknown"


def bulk_rows(remote, base=S3):
    from tribonian import casedb
    files = local_files(casedb.BULK)
    loaded = loaded_dates(casedb.DB)
    latest, error = {}, ""
    if remote:
        try:
            sets = {n: listing(n, base) for n in BULK_FILES}
            date = newest_set(sets)
            latest = {n: (date, sets[n][date][0]) for n in BULK_FILES} if date else {}
        except OSError as e:
            error = "couldn't reach CourtListener's bulk listing: %s" % e
    rows = []
    for n in BULK_FILES:
        f = files[n]
        have = f["complete"][-1] if f["complete"] else None
        new = latest.get(n, (None, None))
        if f["part"]:
            d, got, size = f["part"]
            status = "partial %s%s" % (d, ", %d%%" % (got * 100 // size) if size else "")
        elif loaded.get(n) and (not new[0] or loaded[n] >= new[0]):
            status = "loaded"
        elif new[0] and (not have or have < new[0]):
            status = "update available" if have or loaded.get(n) else "not downloaded"
        elif have and loaded.get(n) != have:
            status = "downloaded, not loaded"
        else:
            status = "not downloaded" if not have else "loaded"
        rows.append({"name": n, "downloaded": have, "loaded": loaded.get(n), "latest": new[0],
                     "size": new[1], "status": status})
    space = [{"path": str(at), "need_gb": round(gb), "free_gb": round(free)} for at, gb, free in space_needed().values()]
    return {"files": rows, "complete_set": complete_set(files), "error": error, "bulk_dir": str(casedb.BULK),
            "opinions_db": str(casedb.OPS_DB), "space": space, "stale": stale_after_update(),
            "swap_pending": (staged()[0][1].parent / "SWAPPING").exists()}


def report(remote=False):
    from tribonian import keystore
    tex, tex_text = latex_status()
    token, src = keystore.lookup()
    key = "ok" if token else ("skipped" if keystore.skipped() else "fail")
    key_text = {"ok": "stored (%s)" % src, "skipped": "skipped: CourtListener requests are off until a key is added",
                "fail": "not set"}[key]
    level = data_level()
    rows = [{"id": "tribonian", "required": True, "status": "ok", "text": "tribonian %s, Python %d.%d" % ((version(),) + sys.version_info[:2])},
            {"id": "latex", "required": True, "status": tex, "text": tex_text},
            {"id": "key", "required": True, "status": key, "text": key_text},
            {"id": "data", "required": False, "status": "ok" if level else "none",
             "text": {"full": "full: the graph and opinion text", "lite": "lite: the graph, no opinion text"}.get(level, "none installed")}]
    mode_set = "data_mode" in tconfig.FILE or bool(os.environ.get("CL_DATA_MODE"))
    unavailable = ""
    if key == "skipped" and not level:
        unavailable = ("Offline with no local data: US Code, C.F.R., state law and Supreme Court dockets work; court opinions "
                       "need either a CourtListener key or the bulk data.")
    return {"version": version(), "config_file": str(tconfig.config_path()),
            "required_ok": all(r["status"] in ("ok", "skipped") for r in rows if r["required"]),
            "rows": rows, "has_key": bool(token), "data": level,
            "mode": {"value": tconfig.data_mode(), "set": mode_set}, "unavailable": unavailable,
            "bulk": bulk_rows(remote)}


def cmd_status(args):
    r = report(args.remote)
    if args.json:
        print(json.dumps(r, indent=1))
        return 0
    for row in r["rows"]:
        print("%-9s %-8s %s%s" % (row["id"], row["status"], row["text"], "" if row["required"] else "   (optional)"))
    print("\ndata mode: %s%s" % (r["mode"]["value"], "" if r["mode"]["set"] else " (not set yet)"))
    if r["unavailable"]:
        print(r["unavailable"])
    b = r["bulk"]
    print("\nbulk data (%s):" % b["bulk_dir"])
    for f in b["files"]:
        print("  %-17s %-24s downloaded %-10s loaded %-10s%s" % (f["name"], f["status"], f["downloaded"] or "-", f["loaded"] or "-",
                                                                " latest %s (%.1f GB)" % (f["latest"], f["size"] / 1e9) if f["latest"] else ""))
    if b["error"]:
        print("  " + b["error"])
    for s in b["stale"]:
        print("  stale: %s -- rebuild: %s" % (s["what"], s["rebuild"]))
    if not r["required_ok"]:
        print("\nTo do: " + "; ".join({"latex": "tribonian setup latex install", "key": "tribonian setup key set (or key skip)"}[x["id"]]
                                     for x in r["rows"] if x["required"] and x["status"] == "fail"))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="tribonian setup", description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--remote", action="store_true", help="also ask CourtListener for the latest bulk set")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("set", help="write one setting to the config file")
    p.add_argument("key")
    p.add_argument("value", nargs="?")
    p = sub.add_parser("key", help="the CourtListener API token: set (stdin), skip, forget")
    p.add_argument("action", choices=["set", "skip", "forget"])
    sub.add_parser("test-token", help="one live CourtListener request")
    p = sub.add_parser("latex", help="is LaTeX ready; install: TinyTeX and the packages")
    p.add_argument("action", nargs="?", choices=["status", "install"], default="status")
    p = sub.add_parser("download", help="bulk files, newest complete set, restartable")
    p.add_argument("--date", help="YYYY-MM-DD: this set instead of the newest")
    p.add_argument("--only", nargs="+", choices=BULK_FILES, help="just these files")
    sub.add_parser("update", help="load the downloaded set beside the current database, check it, swap")
    args = ap.parse_args()
    return {"set": cmd_set, "key": cmd_key, "test-token": cmd_test_token, "latex": cmd_latex,
            "download": cmd_download, "update": cmd_update}.get(args.cmd, cmd_status)(args)


if __name__ == "__main__":
    sys.exit(main())
