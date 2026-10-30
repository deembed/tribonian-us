"""check.py -- setup and database health check: is each piece there, and does it answer a query?

    tribonian check              # every check, grouped, then which data mode this install supports
    tribonian check --api        # also make one live CourtListener request (counts against the daily limit)
    tribonian check --json       # the same as JSON (the GUI's Check setup page reads this)
    tribonian check --paths      # only the files the scripts use, as JSON; the GUI reads this at start

Data modes (what the local data supports; the best one that passes is reported):
    api    nothing local: every case, citer and text comes from the CourtListener API
    lite   the small bulk tables loaded (courts, citations, citation map, parentheticals,
           clusters, opmap); opinion text still comes from the API
    full   lite + the opinion text database (opinions.sqlite)

Each probe looks up one known case, Motor Vehicle Mfrs. Ass'n v. State Farm (463 U.S. 29,
CourtListener cluster 110991), through an index, so a check is quick even on a hard disk.
A table that loaded but lacks that case (a --limit sample load) is a warning, not a failure.
Reads only: every database is opened read-only; nothing is written.
"""
import argparse, glob, importlib, json, os, re, shutil, sqlite3, subprocess, sys, time, zlib
from pathlib import Path

from tribonian import casedb                      # paths only (tconfig: CL_* variable, config file, default); stdlib
from tribonian import tconfig

HERE = tconfig.WORK                          # the work folder
# cocite.py's location, repeated here so the check runs before numpy/igraph are installed
COCITE_BASE = tconfig.get("cocite_db", casedb.DB.with_name("cocite.sqlite"))
COCITE_CACHE = COCITE_BASE.with_name("cocite-cache")
COCITE_SCHEMA = "4"                # cocite.py SCHEMA_VERSION; the GUI reads only this one

PROBE = {"cluster": 110991, "cite": ("463", "U.S.", "29"), "name": "State Farm"}

# check key -> modes that need it. Keys not listed are optional: they warn, never block a mode.
LITE_TABLES = ["db:courts", "db:citations", "db:citation-map", "db:parentheticals", "db:opinion-clusters", "db:opmap"]
# Tools that only make output (pdflatex for PDFs, rmapi for the tablet) block no mode: notes-only works without them.
NEEDS = {"api": ["tool:python", "api:token"]}
NEEDS["lite"] = NEEDS["api"] + ["tool:analysis", "db:file"] + LITE_TABLES
NEEDS["full"] = NEEDS["lite"] + ["ops:text"]
MODES = ["api", "lite", "full"]


class Check:
    def __init__(self, key, group, label):
        self.key, self.group, self.label = key, group, label
        self.status, self.detail, self.fix, self.ms = "skip", "", "", 0

    def ok(self, detail):
        self.status, self.detail = "ok", detail

    def warn(self, detail, fix=""):
        self.status, self.detail, self.fix = "warn", detail, fix

    def fail(self, detail, fix=""):
        self.status, self.detail, self.fix = "fail", detail, fix

    def skip(self, detail):
        self.status, self.detail = "skip", detail

    def as_dict(self):
        return {k: getattr(self, k) for k in ("key", "group", "label", "status", "detail", "fix", "ms")}


RESULTS = []


def check(key, group, label):
    """Decorator: run fn(c) timed; an exception becomes a failure with its message."""
    def wrap(fn):
        c = Check(key, group, label)
        t0 = time.time()
        try:
            fn(c)
        except Exception as e:                     # a check must never stop the rest
            c.fail("%s: %s" % (type(e).__name__, e))
        c.ms = int((time.time() - t0) * 1000)
        RESULTS.append(c)
        return c
    return wrap


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)


def gb(p):
    return "%.1f GB" % (Path(p).stat().st_size / 1e9)


def n(x):
    return format(x, ",")


# ------------------------------------------------------------------ configuration

# Each setting: its CL_* variable, else the config file (tconfig.py), else the default.
# (label, config key, value in effect)
SETTINGS = [
    ("Work folder", "work_dir", tconfig.WORK),
    ("Bulk folder", "bulk_dir", casedb.BULK),
    ("Main database", "main_db", casedb.DB),
    ("Opinion text", "opinions_db", casedb.OPS_DB),
    ("Search index", "search_db", casedb.SEARCH_DB),
    ("Co-citation base", "cocite_db", COCITE_BASE),
    ("API rate limits", "api_limits", tconfig.get("api_limits", "5,50,125")),
]
GUI_TOML = (tconfig.REPO or HERE) / "gui" / "gui.toml"          # the GUI's old config file, still read (overrides [gui])
KNOWN = set(tconfig.ENV) | {"gui"}
GUI_KEYS = {"default_view", "obsidian_vault", "obsidian_cases_dir", "python"}


def where(key):
    src = tconfig.source(key)[1]
    return {"config": "from %s" % tconfig.FILE_PATH, "default": "default"}.get(src, "from " + src[4:])


def on_wsl():
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:                      # native Windows has no os.uname
        return False


def run_config():
    @check("cfg:file", "Configuration", "Config file")
    def _(c):
        p = tconfig.config_path()
        if tconfig.FILE_ERROR:
            return c.fail(tconfig.FILE_ERROR, "fix the file (see config.example.toml); until then it is ignored")
        if not tconfig.FILE_PATH:
            return c.ok("none (optional); would be %s   (template: config.example.toml)" % p)
        unknown = sorted(set(tconfig.FILE) - KNOWN) + ["gui." + k for k in sorted(set(tconfig.gui()) - GUI_KEYS)]
        msg = "%s: %d settings%s" % (p, len(tconfig.FILE), ", [gui] %d" % len(tconfig.gui()) if tconfig.gui() else "")
        if "courtlistener_token" in tconfig.FILE and os.name == "posix" and p.stat().st_mode & 0o077:
            return c.warn(msg + "; holds the API token but other users can read it", "chmod 600 %s" % p)
        if unknown:
            return c.warn(msg + "; unknown keys (typos?): " + ", ".join(unknown), "see config.example.toml")
        c.ok(msg)

    for label, key, value in SETTINGS:
        @check("cfg:" + key, "Configuration", label)
        def _(c, key=key, value=value):
            c.ok("%s  (%s)" % (value, where(key)))

    @check("cfg:gui", "Configuration", "Old GUI config (gui/gui.toml)")
    def _(c):
        if not GUI_TOML.exists():
            return c.ok("none (good: the GUI's settings go in the config file's [gui] table)")
        try:
            cfg = tconfig.load(GUI_TOML)[0]
        except ValueError as e:
            return c.fail(str(e), "fix or delete gui/gui.toml")
        over = [k for k in ("bulk_dir", "main_db", "opinions_db", "search_db") if cfg.get(k)]
        msg = "%s: %d settings, read after the config file's [gui] and overriding it" % (GUI_TOML, len(cfg))
        if over:
            msg += "; overrides these paths for the GUI only: " + ", ".join(over)
        c.warn(msg, "move its settings into %s ([gui] table; paths at the top level), then delete gui/gui.toml"
               % tconfig.config_path())

    if on_wsl():
        @check("cfg:wsl", "Configuration", "WSL")
        def _(c):
            slow = [label for label, key, value in SETTINGS if key in tconfig.PATH_KEYS and str(value).startswith("/mnt/")
                    and len(str(value).split("/")) > 2 and len(str(value).split("/")[2]) == 1]
            if slow:
                return c.warn("on a Windows drive: %s. Access through /mnt/<letter> is slow, and SQLite locking there "
                              "is unreliable" % ", ".join(slow), "keep the databases under your WSL home (~/...)")
            c.ok("databases are on the Linux file system")

    @check("cfg:rmapi", "Configuration", "rmapi config")
    def _(c):
        p = Path(os.environ.get("RMAPI_CONFIG") or os.path.expanduser("~/.config/rmapi/rmapi.conf"))
        host = os.environ.get("RMAPI_HOST")
        where_ = "rmfakecloud at %s" % host if host else "reMarkable cloud (RMAPI_HOST not set)"
        c.ok("%s; %s" % (p, where_)) if p.exists() else c.skip("not paired (%s); only sync_remarkable.py needs it" % p)


# ------------------------------------------------------------------ tools

def by_os(linux, mac, windows):
    """A fix hint for this system (INSTALL.md has the full steps)."""
    return windows if os.name == "nt" else mac if sys.platform == "darwin" else linux


def run_tools():
    def modules(c, names, what):
        missing = []
        for m in names:
            try:
                importlib.import_module(m)
            except ImportError:
                missing.append(m)
        if missing:
            c.fail("missing: %s (%s)" % (", ".join(missing), what),
                   "pip install -r requirements.txt   (or: apt install %s)" % " ".join("python3-" + m for m in missing))
        else:
            c.ok("Python %d.%d; %s" % (sys.version_info[:2] + (" ".join(names),)))

    @check("tool:python", "Tools", "Python: notes and PDFs")
    def _(c):
        modules(c, ["lxml"], "US Code and case notes")

    @check("tool:analysis", "Tools", "Python: clustering")
    def _(c):
        modules(c, ["numpy", "scipy", "igraph"], "co-citation clusters")

    @check("tool:pdflatex", "Tools", "pdflatex + packages")
    def _(c):
        if not shutil.which("pdflatex"):
            return c.fail("pdflatex not found", by_os(
                "install TeX Live (e.g. apt install texlive-latex-extra texlive-fonts-extra texlive-plain-generic)",
                "install MacTeX (brew install --cask mactex-no-gui), then open a new terminal",
                "install MiKTeX (winget install MiKTeX.MiKTeX), then open a new terminal"))
        if not shutil.which("kpsewhich"):
            return c.warn("pdflatex found; kpsewhich missing, packages not checked")
        missing = [s for s in ("XCharter.sty", "tcolorbox.sty", "ulem.sty")
                   if not subprocess.run(["kpsewhich", s], capture_output=True, text=True).stdout.strip()]
        if missing:
            c.fail("missing LaTeX packages: " + ", ".join(missing),
                   by_os("XCharter: texlive-fonts-extra; tcolorbox: texlive-latex-extra; ulem: texlive-plain-generic",
                         "sudo tlmgr install xcharter tcolorbox ulem",
                         "miktex packages install xcharter tcolorbox ulem   (or MiKTeX Console > Packages)"))
        else:
            c.ok("XCharter, tcolorbox, ulem")

    @check("tool:pdftotext", "Tools", "pdftotext")
    def _(c):
        c.ok(shutil.which("pdftotext")) if shutil.which("pdftotext") else c.warn("not found (used by tests and some slip opinions)",
                                                                         by_os("apt install poppler-utils", "brew install poppler", "winget install oschwartz10612.Poppler, then open a new terminal"))

    @check("tool:rmapi", "Tools", "rmapi (reMarkable sync)")
    def _(c):
        p = os.environ.get("RMAPI") or shutil.which("rmapi") or os.path.expanduser("~/.local/bin/rmapi")
        c.ok(p) if Path(p).is_file() else c.skip("not installed; only sync_remarkable.py needs it")


# ------------------------------------------------------------------ CourtListener API

def run_api(live):
    tok = {}

    @check("api:token", "CourtListener API", "API token")
    def _(c):
        t = str(tconfig.get("courtlistener_token") or "").strip()
        src = where("courtlistener_token")[5:] if t else ""
        if not t and shutil.which("secret-tool"):
            t = subprocess.run(["secret-tool", "lookup", "service", "courtlistener"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
            src = "secret-tool (service courtlistener)"
        if not t:
            return c.fail("no token", "courtlistener_token in %s (chmod 600), or COURTLISTENER_TOKEN, or "
                          "secret-tool store --label='CourtListener API' service courtlistener" % tconfig.config_path())
        tok["t"] = t
        c.ok("from %s (%d characters)" % (src, len(t)))

    @check("api:live", "CourtListener API", "Live request")
    def _(c):
        if not live:
            return c.skip("not run; tribonian check --api makes one request")
        if "t" not in tok:
            return c.skip("no token")
        from tribonian import case2md
        r = case2md.call("%s/clusters/%d/?fields=id,case_name" % (case2md.API, PROBE["cluster"]), tok["t"])
        if r.get("id") != PROBE["cluster"]:
            return c.fail("unexpected answer: %s" % str(r)[:120])
        c.ok("cluster %d: %s; %d requests left today by the local log" % (r["id"], r.get("case_name", "")[:50], case2md.budget_left()))


# ------------------------------------------------------------------ main database

def run_db():
    con, ids = {}, {}

    @check("db:file", "Main database", "courtlistener.sqlite")
    def _(c):
        if not casedb.DB.exists():
            return c.fail("not found: %s" % casedb.DB, "download the bulk files into %s, then tribonian db load" % casedb.BULK)
        con["c"] = ro(casedb.DB)
        lock = Path(str(casedb.DB) + ".loading")
        if lock.exists():
            return c.warn("%s (%s); a load is running: %s" % (casedb.DB, gb(casedb.DB), lock.read_text().strip()))
        c.ok("%s (%s)" % (casedb.DB, gb(casedb.DB)))

    def loaded(name):
        try:
            return con["c"].execute("SELECT file, rows FROM loaded WHERE name=?", (name,)).fetchone()
        except sqlite3.Error:
            return None

    def table_check(name, probe):
        @check("db:" + name, "Main database", name)
        def _(c):
            if "c" not in con:
                return c.skip("no database")
            row = loaded(name)
            if not row:
                return c.fail("not loaded", LOAD_FIX.get(name, "tribonian db load %s" % name))
            msg = probe(con["c"])
            head = "%s rows from %s" % (n(row[1]), row[0])
            if msg is None:
                c.warn(head + "; probe case %d not found (a --limit sample?)" % PROBE["cluster"], "tribonian db load --force %s" % name)
            else:
                c.ok(head + "; " + msg)

    # opmap is not a bulk file: casedb.py builds it from the opinions file's indexes
    LOAD_FIX = {"opmap": "tribonian db opmap   (built from opinions.sqlite, which has to be loaded once)"}

    def p_courts(db):
        r = db.execute("SELECT full_name FROM courts WHERE id='scotus'").fetchone()
        return r and "scotus = %s" % r[0]

    def p_cites(db):
        r = db.execute("SELECT cluster_id FROM cites WHERE volume=? AND reporter=? AND page=?", PROBE["cite"]).fetchall()
        return ("463 U.S. 29 -> cluster %d" % PROBE["cluster"]) if (PROBE["cluster"],) in r else None

    def p_clusters(db):
        r = db.execute("SELECT case_name FROM clusters WHERE id=?", (PROBE["cluster"],)).fetchone()
        return r and r[0][:60]

    def p_opmap(db):
        r = [x[0] for x in db.execute("SELECT id FROM opmap WHERE cluster_id=?", (PROBE["cluster"],))]
        if not r:
            return None
        ids["ops"] = r
        stale = casedb.op_table(db) != "opmap"
        return "opinions %s%s" % (r, "  (STALE: built from older opinions; tribonian db opmap)" if stale else "")

    def p_edges(db):
        if "ops" not in ids:
            return "not probed (needs opmap)"
        q = ",".join("?" * len(ids["ops"]))
        k = db.execute("SELECT count(DISTINCT citing) FROM edges WHERE cited IN (%s)" % q, ids["ops"]).fetchone()[0]
        return k and "cited by %s opinions" % n(k)

    def p_parens(db):
        if "ops" not in ids:
            return "not probed (needs opmap)"
        q = ",".join("?" * len(ids["ops"]))
        k = db.execute("SELECT count(*) FROM parens WHERE described IN (%s)" % q, ids["ops"]).fetchone()[0]
        return k and "%s parentheticals" % n(k)

    def p_dockets(db):
        r = db.execute("SELECT d.docket_number FROM clusters c JOIN dockets d ON d.id = c.docket_id WHERE c.id=?",
                       (PROBE["cluster"],)).fetchone()
        return r and "docket %s" % r[0]

    # opmap first: edges and parentheticals are keyed by opinion id
    for name, probe in (("courts", p_courts), ("citations", p_cites), ("opinion-clusters", p_clusters),
                        ("opmap", p_opmap), ("citation-map", p_edges), ("parentheticals", p_parens),
                        ("dockets", p_dockets)):
        table_check(name, probe)

    @check("db:courtindex", "Main database", "dockets by court")
    def _(c):
        if "c" not in con or not loaded("dockets"):
            return c.skip("no dockets table")
        if not casedb.has_court_index(con["c"]):
            return c.warn("not built: View Dockets lists a court's opinions from CourtListener instead (not offline)",
                          "tribonian db courtindex")
        k = con["c"].execute("SELECT count(*) FROM (SELECT 1 FROM dockets WHERE court_id='scotus' LIMIT 100000)").fetchone()[0]
        c.ok("built; scotus has %s%s dockets" % (n(k), "+" if k >= 100000 else ""))
    @check("db:dateindex", "Main database", "cases by date")
    def _(c):
        if "c" not in con or not loaded("opinion-clusters"):
            return c.skip("no clusters table")
        from tribonian import scotus
        if not scotus.has_date_index(con["c"]):
            return c.warn("not built: cites missing from the citations table (most since 2018) stay red instead of "
                          "being found by case name, court and date", "tribonian db courtindex")
        if not scotus.has_index(con["c"], "dockets_by_number"):
            return c.warn("dockets by number not built: Westlaw cites are not matched to their RECAP dockets",
                          "tribonian db courtindex")
        c.ok("built (cases by date, dockets by number)")
    # report in load order
    order = ["db:file"] + ["db:" + x for x in casedb.ORDER if x != "opinions"] + ["db:opmap", "db:courtindex", "db:dateindex"]
    block = sorted([r for r in RESULTS if r.group == "Main database"], key=lambda r: order.index(r.key))
    RESULTS[:] = [r for r in RESULTS if r.group != "Main database"] + block
    return con.get("c")


# ------------------------------------------------------------------ opinion text

def run_ops():
    @check("ops:text", "Opinion text", "opinions.sqlite")
    def _(c):
        if not casedb.OPS_DB.exists():
            return c.fail("not found: %s%s" % (casedb.OPS_DB, " (dangling link)" if casedb.OPS_DB.is_symlink() else ""),
                          "tribonian db load opinions   (or set CL_OPS_DB; not needed in lite or api mode)")
        db = ro(casedb.OPS_DB)
        r = db.execute("SELECT id, field, text FROM opinions INDEXED BY opinions_by_cluster WHERE cluster_id=? AND text IS NOT NULL LIMIT 1",
                       (PROBE["cluster"],)).fetchone()
        if not r:
            return c.warn("%s (%s); probe case not found (a --limit sample?)" % (casedb.OPS_DB, gb(casedb.OPS_DB)))
        t = zlib.decompress(r[2]).decode("utf-8")
        found = "arbitrary and capricious" in t
        msg = "%s (%s); opinion %d: %s characters of %s%s" % (casedb.OPS_DB, gb(casedb.OPS_DB), r[0], n(len(t)), r[1],
                                                              "" if found else ", expected phrase missing")
        c.ok(msg) if found else c.warn(msg)


# ------------------------------------------------------------------ derived data (built by the scripts)

def run_derived():
    @check("idx:search", "Built from the data", "Search index (search.sqlite)")
    def _(c):
        if not casedb.SEARCH_DB.exists():
            return c.warn("not built: %s (Search for Cases needs it)" % casedb.SEARCH_DB, "tribonian db index")
        db = ro(casedb.SEARCH_DB)
        r = [x[0] for x in db.execute("SELECT cluster_id FROM names WHERE names MATCH ? LIMIT 50", ('"%s"' % PROBE["name"],))]
        stamp = db.execute("SELECT stamp FROM meta").fetchone()
        # the stamp records when its source tables were loaded; show just the dates
        built = "built from tables loaded " + ", ".join(sorted(set(re.findall(r"\d{4}-\d\d-\d\d", stamp[0] or "")))) if stamp else "no stamp"
        msg = "%s (%s, %s); \"%s\": %d+ hits" % (casedb.SEARCH_DB, gb(casedb.SEARCH_DB), built,
                                                PROBE["name"], len(r))
        c.ok(msg) if r else c.warn(msg + ", none", "tribonian db index")

    @check("cocite:views", "Built from the data", "Co-citation views (cocite*.sqlite)")
    def _(c):
        files = sorted(glob.glob(str(COCITE_BASE.with_name("cocite*.sqlite"))))
        if not files:
            return c.warn("none in %s (clusters, areas, sub-clusters need one)" % COCITE_BASE.parent,
                          "tribonian cocite prep && tribonian cocite build --courts appellate   (README stage 5)")
        parts, bad = [], 0
        for f in files:
            db = ro(f)
            try:
                schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                k = db.execute("SELECT count(*) FROM slices").fetchone()[0]
                hit = db.execute("SELECT slice, cl FROM nodes WHERE ref=? ORDER BY slice DESC LIMIT 1", (PROBE["cluster"],)).fetchone()
                old = (schema[0] if schema else "1") != COCITE_SCHEMA
                parts.append("%s: schema %s%s, %d slices%s" % (
                    Path(f).stem, schema[0] if schema else "?", " (older; the GUI skips it)" if old else "", k,
                    ", probe in cluster %d (%d)" % (hit[1], hit[0]) if hit else ""))
            except sqlite3.Error as e:
                bad += 1
                parts.append("%s: %s" % (Path(f).stem, e))
        (c.warn if bad else c.ok)("; ".join(parts))

    @check("cocite:labels", "Built from the data", "Cluster labels")
    def _(c):
        p = tconfig.data_file("cocite_labels.json")
        if not p.exists():
            return c.warn("no %s; clusters show without names" % p)
        c.ok("%s: %s entries" % (p, n(len(json.loads(p.read_text(encoding="utf-8"))))))

    @check("cocite:positions", "Built from the data", "Citing-sentence index (posidx)")
    def _(c):
        d = COCITE_CACHE / "posidx"
        if not d.is_dir() or not any(d.iterdir()):
            return c.warn("not built (only the sentence-weighted views need it; full mode)", "tribonian cocite positions --index")
        c.ok("%s: %d files" % (d, sum(1 for _ in d.iterdir())))


# ------------------------------------------------------------------ US Code

def run_usc():
    @check("usc:xml", "US Code", "Title XML files")
    def _(c):
        files = sorted((HERE / "xml").glob("usc*.xml"))
        if not files:
            return c.warn("none in xml/", "download the USLM XML zip from uscode.house.gov into xml/ (README stage 2)")
        from lxml import etree
        small = HERE / "xml" / "usc01.xml"       # Title 1: small, and always has sections
        small = small if small.exists() else files[0]
        k = sum(1 for _ in etree.parse(str(small)).iter("{*}section"))
        msg = "%d titles; %s parses: %d sections" % (len(files), small.name, k)
        c.ok(msg) if k else c.warn(msg)

    @check("usc:cocite", "US Code", "US Code citation data")
    def _(c):
        d = COCITE_CACHE / "usc"
        have = {"usc_map.json": "usc-map", "citedby.sqlite": "usc-citedby"}
        if not d.is_dir():
            return c.warn("not built: %s (Cited with / Cited by in section notes; full mode)" % d,
                          "tribonian cocite usc-extract, usc-map, usc-profile, usc-citedby   (README stage 5)")
        missing = [cmd for f, cmd in have.items() if not (d / f).exists()]
        profiles = sorted(p.name for p in d.glob("profile-*.sqlite"))
        msg = "%s; profiles: %s" % (d, ", ".join(profiles) or "none")
        if (d / "citedby.sqlite").exists():
            k = ro(d / "citedby.sqlite").execute("SELECT count(*) FROM total").fetchone()[0]
            msg += "; citedby: %s sections" % n(k)
        if missing or not profiles:
            c.warn(msg, "tribonian cocite " + ", ".join(missing + ([] if profiles else ["usc-profile"])))
        else:
            c.ok(msg)


# ------------------------------------------------------------------ output

def modes():
    by = {r.key: r.status for r in RESULTS}
    LABEL = {r.key: r.label for r in RESULTS}
    out = {}
    for m in MODES:
        blocking = [LABEL[k] for k in NEEDS[m] if by.get(k) not in ("ok", "warn")]
        out[m] = {"ok": not blocking, "missing": blocking}
    best = ([m for m in MODES if out[m]["ok"]] or [None])[-1]
    return best, out


MARK = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "--  "}


def paths():
    """The files the scripts use (CL_* variables or defaults). The GUI takes its paths from here."""
    return {"repo_dir": str(tconfig.REPO) if tconfig.REPO else None, "work_dir": str(HERE), "labels_file": str(tconfig.data_file("cocite_labels.json")),
            "main_db": str(casedb.DB), "opinions_db": str(casedb.OPS_DB),
            "search_db": str(casedb.SEARCH_DB), "cocite_dir": str(COCITE_BASE.parent),
            "config_file": str(tconfig.FILE_PATH) if tconfig.FILE_PATH else None, "config_error": tconfig.FILE_ERROR,
            "gui": tconfig.gui(), "data_mode": safe_mode()}


def mode_mismatch(mode, best, ms):
    """The mode set and the data installed disagree: a sentence saying how, else "". The mode is a label for
    full / lite / api (the scripts use whatever local data exists); only offline changes what they do."""
    order = ["api", "lite", "full"]
    mode = "full" if mode == "complete" else mode          # complete: the full install, plus gap-filling requests
    if mode == "offline" or mode not in order:
        return ""
    if not ms.get(mode, {}).get("ok"):
        return "%s is set, but this install lacks %s; the scripts fall back on the API where they can" % (
            mode, ", ".join(ms.get(mode, {}).get("missing") or ["what it needs"]))
    if best and order.index(best) > order.index(mode):
        return ("%s is set, but this install has the data for %s; the scripts still use the local data "
                "(the mode doesn't make them ignore it)" % (mode, best))
    return ""


def safe_mode():
    """The data mode setting, or "full" when it is not one of the four (the full check reports that)."""
    try:
        return tconfig.data_mode()
    except SystemExit:
        return "full"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", action="store_true", help="make one live CourtListener request")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--paths", action="store_true", help="print only the paths the scripts use, as JSON (no checks)")
    args = ap.parse_args()
    if args.paths:
        print(json.dumps(paths()))
        return
    run_config()
    run_tools()
    run_api(args.api)
    run_db()
    run_ops()
    run_derived()
    run_usc()
    best, ms = modes()
    if args.json:
        print(json.dumps({"best": best, "modes": ms, "needs": NEEDS, "paths": paths(),
                          "setting": {"mode": tconfig.data_mode(), "from": tconfig.source("data_mode", "full")[1],
                                      "note": mode_mismatch(tconfig.data_mode(), best, ms)},
                          "checks": [r.as_dict() for r in RESULTS]}, indent=1))
    else:
        group = None
        for r in RESULTS:
            if r.group != group:
                group = r.group
                print("\n" + group)
            print("  %s  %-34s %s" % (MARK[r.status], r.label, r.detail))
            if r.fix and r.status in ("warn", "fail"):
                print("  %s  %-34s fix: %s" % ("    ", "", r.fix))
        print("\nData mode set: %s (%s); the best this install supports: %s" % (
            tconfig.data_mode(), tconfig.source("data_mode", "full")[1], best or "none (fix the Tools / API items above)"))
        note = mode_mismatch(tconfig.data_mode(), best, ms)
        if note:
            print("  note: " + note)
        for m in MODES:
            print("  %-4s %s" % (m, "ready" if ms[m]["ok"] else "needs " + ", ".join(ms[m]["missing"])))
    sys.exit(0 if best else 1)


if __name__ == "__main__":
    main()
