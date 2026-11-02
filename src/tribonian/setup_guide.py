"""setup_guide.py -- the tricky part of a first install, as steps: tools, where the data goes, API token, database.

    tribonian setup                    # each step: done or action required, and what to do next
    tribonian setup --json             # the same as JSON (the GUI's Setup guide reads this)
    tribonian setup set KEY [VALUE]    # write one setting to the config file; no VALUE: read it from stdin, so a
                                       #   token stays out of the process list. An empty value removes the setting.
    tribonian setup test-token         # one live CourtListener request
    tribonian setup download [--date YYYY-MM-DD]   # the seven bulk files, newest complete set, resumable

The status comes from check.py's checks, so this can't disagree with tribonian check. bootstrap.sh and bootstrap.ps1
get a fresh clone as far as running this (and call `setup set` with only the standard library installed).
"""
import argparse, json, os, re, shutil, sys, tempfile, urllib.error, urllib.request
from pathlib import Path

from tribonian import tconfig

S3 = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com"
BULK_FILES = ["courts", "citations", "parentheticals", "citation-map", "opinion-clusters", "dockets", "opinions"]
# README stage 4, in GB. The downloads can be deleted after the load.
GB_DOWNLOADS, GB_MAIN, GB_OPINIONS = 60, 25, 110
SETTABLE = {"courtlistener_token", "bulk_dir", "opinions_db", "data_mode", "gui.python"}
# bootstrap's fix, for a machine whose Python packages are missing
BOOTSTRAP = ".\\bootstrap.ps1" if os.name == "nt" else "./bootstrap.sh"


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
    if args.key == "data_mode" and value and value not in tconfig.DATA_MODES:
        sys.exit("data_mode must be one of: " + ", ".join(tconfig.DATA_MODES))
    path = tconfig.config_path()
    try:
        write_setting(path, args.key, value)
    except ValueError as e:
        sys.exit("not saved: %s\nFix the config file first (see config.example.toml)." % e)
    print("%s %s in %s" % ("saved" if value else "removed", args.key, path))     # never the value: it may be the token
    if args.key in ("bulk_dir", "opinions_db"):
        print("Restart the app so it reads the new location.")
    return 0


# ------------------------------------------------------------------ bulk downloads

def listing(name, base=S3):
    """{date: size} of one bulk file's quarterly sets."""
    url = "%s/?list-type=2&prefix=bulk-data/%s-" % (base, name)
    with urllib.request.urlopen(url, timeout=60) as r:
        xml = r.read().decode("utf-8")
    out = {}
    for key, size in re.findall(r"<Key>bulk-data/([^<]+)</Key>.*?<Size>(\d+)</Size>", xml, re.S):
        m = re.fullmatch(re.escape(name) + r"-(\d{4}-\d\d-\d\d)\.csv\.bz2", key)
        if m:
            out[m.group(1)] = int(size)
    return out


def newest_set(sets):
    """The newest date every file has: one set must not mix quarters. sets: {name: {date: size}}."""
    common = set.intersection(*(set(s) for s in sets.values())) if sets else set()
    return max(common) if common else None


def fetch(url, dest, size, say=print):
    """Download to dest, resuming a .part; renamed only when the size matches, so casedb never sees a partial file."""
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have < size:
        req = urllib.request.Request(url, headers={"Range": "bytes=%d-" % have} if have else {})
        with urllib.request.urlopen(req, timeout=120) as r:
            if have and r.status != 206:                         # server ignored the range: start over
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
                        say("  %s: %d%%" % (dest.name, have * 100 // size))
    got = part.stat().st_size
    if got != size:
        raise OSError("%s: got %d of %d bytes; run it again to resume" % (dest.name, got, size))
    part.replace(dest)


def download(bulk, date=None, base=S3, names=BULK_FILES, say=print):
    """Fetch one quarterly set into bulk; files already complete are skipped."""
    sets = {n: listing(n, base) for n in names}
    date = date or newest_set(sets)
    lacking = [n for n in names if date not in sets[n]]
    if not date or lacking:
        raise OSError("no set for %s has all the files (missing: %s)" % (date or "any date", ", ".join(lacking or names)))
    bulk = Path(bulk)
    bulk.mkdir(parents=True, exist_ok=True)
    todo = []
    for n in names:
        dest = bulk / ("%s-%s.csv.bz2" % (n, date))
        if dest.exists() and dest.stat().st_size == sets[n][date]:
            say("have %s" % dest.name)
        else:
            todo.append((n, dest, sets[n][date]))
    need = sum(s - (d.with_name(d.name + ".part").stat().st_size if d.with_name(d.name + ".part").exists() else 0)
               for _, d, s in todo)
    free = shutil.disk_usage(bulk).free
    if need > free:
        raise OSError("%.0f GB to download, %.0f GB free in %s: set bulk_dir to a larger disk" % (need / 1e9, free / 1e9, bulk))
    for n, dest, size in sorted(todo, key=lambda t: t[2]):       # smallest first: early failures are cheap
        say("downloading %s (%.1f GB)" % (dest.name, size / 1e9))
        fetch("%s/bulk-data/%s" % (base, dest.name), dest, size, say)
    return date


def cmd_download(args):
    from tribonian import casedb
    if not casedb.OPS_DB.exists() and "opinions_db" not in tconfig.FILE:
        (tconfig.WORK / "db").mkdir(parents=True, exist_ok=True)  # or the opinion text lands beside the main database
    try:
        date = download(casedb.BULK, args.date)
    except (OSError, urllib.error.URLError) as e:
        sys.exit("download stopped: %s\nRun it again to resume, or download by hand (README stage 4)." % e)
    print("set %s is complete in %s; next: tribonian db load" % (date, casedb.BULK))
    return 0


# ------------------------------------------------------------------ the steps

def gather():
    """check.py's results by key: tools, API (no live request), database, opinion text, derived."""
    from tribonian import check
    check.RESULTS.clear()
    check.run_config()
    check.run_tools()
    check.run_api(False)
    check.run_db()
    check.run_ops()
    check.run_derived()
    return {r.key: r for r in check.RESULTS}


def ready(checks, key):
    return key in checks and checks[key].status in ("ok", "warn")


def line(c):
    return {"status": c.status, "text": "%s: %s" % (c.label, c.detail), "fix": c.fix if c.status in ("warn", "fail") else ""}


def run(label, *commands, tip=""):
    return {"kind": "run", "label": label, "commands": [list(c) for c in commands], "tip": tip}


def space_lines(bulk, ops):
    """(lines, enough?): free space against what the database will take, per disk."""
    def device(p):
        p = Path(p).resolve()
        while not p.exists() and p != p.parent:
            p = p.parent
        return p
    need = {}
    for p, gb in ((bulk, GB_DOWNLOADS + GB_MAIN), (ops, GB_OPINIONS)):
        at = device(p)
        key = at.stat().st_dev
        need[key] = (at, need.get(key, (at, 0))[1] + gb)
    out, ok = [], True
    for at, gb in need.values():
        free = shutil.disk_usage(at).free / 1e9
        enough = free >= gb
        ok = ok and enough
        out.append({"status": "ok" if enough else "fail", "fix": "",
                    "text": "%s: %.0f GB free, the database needs about %d GB there%s" % (
                        at, free, gb, "" if enough else ". Choose a larger disk below")})
    return out, ok


def steps(checks):
    from tribonian import casedb
    api_only = tconfig.FILE.get("data_mode") == "api"
    tables = ["db:" + n for n in ("courts", "citations", "citation-map", "parentheticals", "opinion-clusters", "dockets", "opmap")]
    db_keys = ["db:file"] + tables + ["ops:text"]
    db_done = all(ready(checks, k) for k in db_keys)

    # 1. Python packages
    keys = ["tool:python"] + ([] if api_only else ["tool:analysis"])
    tools = {"id": "tools", "title": "Python packages", "done": all(ready(checks, k) for k in keys),
             "why": "The scripts behind this app. PDFs also need TeX (optional for notes only).",
             "lines": [line(checks[k]) for k in ("tool:python", "tool:analysis", "tool:pdflatex") if k in checks],
             "actions": []}
    if not tools["done"]:
        tools["actions"].append({"kind": "copy", "label": "In a terminal, in the checkout, then restart:", "text": BOOTSTRAP})

    # 2. Where the data goes
    lines, roomy = space_lines(casedb.BULK, casedb.OPS_DB)
    config = {"id": "config", "title": "Where the data goes",
              "why": "The local database is large. The defaults are inside the work folder (%s); put the opinion text on the "
                     "fastest disk you have." % tconfig.WORK,
              "done": not tconfig.FILE_ERROR and (api_only or db_done or roomy),
              "lines": ([{"status": "fail", "text": tconfig.FILE_ERROR, "fix": "fix the file (see config.example.toml); until then it is ignored"}]
                        if tconfig.FILE_ERROR else []) + ([] if api_only or db_done else lines),
              "actions": []}
    for key, label, hint, value in (("bulk_dir", "Bulk folder", "the downloads and the main database", casedb.BULK),
                                    ("opinions_db", "Opinion text file", "opinions.sqlite, read hard: use an SSD", casedb.OPS_DB)):
        src = tconfig.source(key)[1]
        if src not in ("config", "default"):
            config["lines"].append({"status": "warn", "text": "%s is set by %s, which overrides the config file" % (key, src[4:]), "fix": ""})
        config["actions"].append({"kind": "input", "key": key, "label": label, "hint": hint, "secret": False,
                                  "value": str(tconfig.FILE.get(key, "")), "placeholder": str(value)})

    # 3. API token
    token = {"id": "token", "title": "CourtListener API token", "done": ready(checks, "api:token"),
             "why": "A free account. Fetching cases and dockets, and anything newer than the database, goes through it.",
             "lines": [line(checks["api:token"])] if ready(checks, "api:token") else [],
             "actions": [{"kind": "link", "label": "Make an account, then copy the token from your profile's API page",
                          "url": "https://www.courtlistener.com/"},
                         {"kind": "input", "key": "courtlistener_token", "label": "API token", "secret": True, "value": "",
                          "placeholder": "paste the token", "hint": "saved in %s, readable only by you" % tconfig.config_path()}]}
    if token["done"]:
        token["actions"].append(run("Test the token", ["setup", "test-token"], tip="one live request; counts against the day's limit"))

    # 4. Database
    database = {"id": "database", "title": "Local database (CourtListener bulk data)", "done": api_only or db_done,
                "why": "The good-law check, case profiles, search and every co-citation step read this, not the API. "
                       "About %d GB to download and %d GB loaded." % (GB_DOWNLOADS, GB_MAIN + GB_OPINIONS),
                "lines": [line(checks[k]) for k in db_keys if k in checks and checks[k].status != "skip" and not db_done],
                "actions": []}
    if api_only:
        database["lines"] = [{"status": "ok", "text": "Skipped: data_mode is api, so everything comes from CourtListener's API "
                              "(limited: 125 requests a day on the free tier)", "fix": ""}]
        database["actions"].append({"kind": "set", "key": "data_mode", "value": "", "label": "Use a local database after all"})
    else:
        have = {n for n in BULK_FILES if any(re.fullmatch(re.escape(n) + r"-\d{4}-\d\d-\d\d\.csv\.bz2", p.name)
                                             for p in casedb.BULK.glob(n + "-*"))} if casedb.BULK.is_dir() else set()
        unloaded = [n for n in BULK_FILES if not ready(checks, "ops:text" if n == "opinions" else "db:" + n)]
        if [n for n in unloaded if n not in have]:
            database["actions"].append(run("Download and load", ["setup", "download"], ["db", "load"],
                                           tip="the newest quarterly set into the bulk folder, then the load. Both resume if interrupted."))
        elif unloaded or not db_done:
            database["actions"].append(run("Load", ["db", "load"], tip="every file in the bulk folder, smallest first; a finished table is skipped"))
        if db_done:
            for key, command in (("idx:search", "index"), ("db:courtindex", "courtindex")):
                if key in checks and checks[key].status == "warn":
                    database["lines"].append(line(checks[key]))
                    database["actions"].append(run("Build " + checks[key].label, ["db", command]))
        else:
            database["actions"].append({"kind": "set", "key": "data_mode", "value": "api", "label": "Skip it: use the API only"})
    return [tools, config, token, database]


def report():
    s = steps(gather())
    return {"actions_required": sum(not x["done"] for x in s), "config_file": str(tconfig.config_path()), "steps": s}


def cmd_status(args):
    r = report()
    if args.json:
        print(json.dumps(r, indent=1))
        return 0
    for i, s in enumerate(r["steps"], 1):
        print("%d. [%s] %s" % (i, "done" if s["done"] else "ACTION REQUIRED", s["title"]))
        if s["done"]:
            continue
        print("   " + s["why"])
        for l in s["lines"]:
            print("   %-4s %s" % (l["status"], l["text"]))
            if l["fix"]:
                print("        fix: " + l["fix"])
        for a in s["actions"]:
            if a["kind"] == "run":
                print("   run: " + " && ".join("tribonian " + " ".join(c) for c in a["commands"]))
            elif a["kind"] == "copy":
                print("   %s %s" % (a["label"], a["text"]))
            elif a["kind"] == "link":
                print("   %s: %s" % (a["label"], a["url"]))
            elif a["kind"] == "input":
                print("   set: tribonian setup set %s%s   (%s)" % (a["key"], "" if a["secret"] else " PATH", a["hint"]))
            elif a["kind"] == "set":
                print("   %s: tribonian setup set %s %s" % (a["label"], a["key"], a["value"] or '""'))
    print("\nActions required: %d. Everything else (US Code, citation maps, first cases): README, Getting started." % r["actions_required"])
    return 0


def cmd_test_token(args):
    from tribonian import check
    check.RESULTS.clear()
    check.run_api(True)
    live = next(r for r in check.RESULTS if r.key == "api:live")
    print("%s: %s" % (live.status, live.detail))
    return 0 if live.status == "ok" else 1


def main():
    ap = argparse.ArgumentParser(prog="tribonian setup", description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("set", help="write one setting to the config file")
    p.add_argument("key")
    p.add_argument("value", nargs="?")
    sub.add_parser("test-token", help="one live CourtListener request")
    p = sub.add_parser("download", help="the seven bulk files, newest complete set, resumable")
    p.add_argument("--date", help="YYYY-MM-DD: this set instead of the newest")
    args = ap.parse_args()
    return {"set": cmd_set, "test-token": cmd_test_token, "download": cmd_download}.get(args.cmd, cmd_status)(args)


if __name__ == "__main__":
    sys.exit(main())
