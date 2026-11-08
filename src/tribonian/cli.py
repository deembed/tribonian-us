"""tribonian -- one command for the whole pipeline.

    tribonian [--mode offline|full|complete|lite|api] <command> [arguments...]
    tribonian <command> --help

--mode overrides the data mode (config data_mode, CL_DATA_MODE) for this one command and
everything it runs.

Every command runs in the work folder (tconfig.WORK: TRIBONIAN_WORK, else the config file's
work_dir, else the checkout), so relative paths in arguments (xml/usc05.xml, cases/<file>.json)
are relative to it, as they were to the repo before.
"""
import errno, os, runpy, signal, subprocess, sys

# command -> (module, what it does). Order is the order of the help text.
COMMANDS = {
    "check":           ("check",           "setup and database health: is each piece there, does it answer"),
    "setup":           ("setup_guide",     "first install: tools, where the data goes, API token, database"),
    "usc":             ("usc2md",          "US Code: USLM XML -> section notes and chapter PDFs"),
    "usc-status":      ("usc_status",      "US Code release points: what is current, what changed"),
    "case":            ("case2md",         "cases: fetch, build, find, recap, api-search"),
    "map":             ("casemap",         "citation graph: get, map, stubs, citedby, goodlaw, coupling, semsearch"),
    "db":              ("casedb",          "bulk CSV -> local SQLite databases, full-text index"),
    "cocite":          ("cocite",          "co-citation clusters, lineages, areas, labels, vault, US Code co-citation"),
    "dockets":         ("dockets",         "View Dockets: the court tree and the index of fetched dockets (for the GUI)"),
    "history":         ("history",         "one federal case across its courts: a timeline of dockets, appeals and remands"),
    "scotus":          ("scotus",          "pending Supreme Court cases: fetch, build, recent"),
    "cfr":             ("cfr",             "Code of Federal Regulations: fetch titles from eCFR, list what is local"),
    "state":           ("statelaw",        "state law: fetch a state's code from its publisher (Colorado so far), list"),
    "build":           ("build",           "build every PDF and note that is out of date, in parallel"),
    "sync-vault":      ("sync_vault",      "mirror md/, md-cases/, md-cocite/, md-cfr/, md-state/ into the Obsidian vault"),
    "sync-remarkable": ("sync_remarkable", "upload changed PDFs to the reMarkable (rmapi)"),
    "goodlaw-eval":    ("goodlaw_eval",    "score the good-law rules on cases whose overruling is known"),
    "link-audit":      ("link_audit",      "wikilink connectivity across the US Code notes"),
    "serve":           ("serve",           "long-running JSON-lines process for the GUI (law text, fetches)"),
}
SCRIPTS = {"link_audit"}          # top-level scripts with no main(): run as __main__
# The work folder's subfolders: a checkout has them in git, an installed copy starts with none, and the scripts
# write into them without creating them. Not bulk/ or db/: whether db/ exists decides where opinions.sqlite goes
# by default (casedb.OPS_DB), so those are made only by the download (setup download).
WORK_DIRS = ("cases", "md", "md-cases", "md-cocite", "md-cfr", "md-state", "pdf", "build", "xml", "law", "state")


class QuietStream:
    """stdout/stderr that stop writing, silently, once the reader is gone: the work goes on (a build whose window
    was closed still finishes its PDF) instead of dying on a progress line. A closed pipe is BrokenPipeError on
    Linux and macOS, OSError EINVAL on Windows."""
    def __init__(self, stream):
        self._s, self._gone = stream, False

    def write(self, text):
        if self._gone:
            return len(text)
        try:
            return self._s.write(text)
        except OSError as e:
            if isinstance(e, BrokenPipeError) or e.errno in (errno.EPIPE, errno.EINVAL):
                self._gone = True
                return len(text)
            raise

    def flush(self):
        if not self._gone:
            try:
                self._s.flush()
            except OSError:
                self._gone = True

    def __getattr__(self, name):
        return getattr(self._s, name)


def usage(out=sys.stdout):
    from tribonian import tconfig
    print(__doc__.split("\n\n")[1].rstrip() + "\n", file=out)
    print("commands:", file=out)
    for name, (_, what) in COMMANDS.items():
        print("  %-16s %s" % (name, what), file=out)
    print("\nwork folder: %s" % tconfig.WORK, file=out)
    print("data mode:   %s (%s)" % (tconfig.data_mode(), tconfig.source("data_mode", "full")[1]), file=out)


def main(argv=None):
    if hasattr(signal, "SIGPIPE"):                 # `tribonian ... | head` ends quietly
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    argv = sys.argv[1:] if argv is None else argv
    # Windows writes a pipe in cp1252 and opens files in it by default: a § or ► stops a build. UTF-8 mode fixes
    # both; it can only be chosen at start-up, so a Python started without it runs the command again with it.
    # (The app sets PYTHONUTF8=1 on every command, so this is for the command line.)
    if os.name == "nt" and not sys.flags.utf8_mode:
        os.environ["PYTHONUTF8"] = "1"
        try:
            return subprocess.call([sys.executable, "-X", "utf8", "-m", "tribonian"] + argv)
        except KeyboardInterrupt:                   # Ctrl+C reaches the inner run too; it reports
            return 130
    for name in ("stdout", "stderr"):
        s = getattr(sys, name)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        setattr(sys, name, QuietStream(s))
    if argv[:1] == ["--version"]:
        from tribonian import setup_guide
        print("tribonian %s" % setup_guide.version())
        return 0
    while argv and (argv[0] == "--mode" or argv[0].startswith("--mode=")):
        if "=" in argv[0]:
            mode, argv = argv[0].split("=", 1)[1], argv[1:]
        elif len(argv) > 1:
            mode, argv = argv[1], argv[2:]
        else:
            sys.exit("tribonian: --mode needs a value: offline, full, complete, lite or api")
        os.environ["CL_DATA_MODE"] = mode          # child processes inherit it
        from tribonian import tconfig
        tconfig.data_mode()                         # an unknown mode stops here, with the choices
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        usage(sys.stderr)
        sys.exit("\ntribonian: no command %r" % name)
    module = COMMANDS[name][0]
    from tribonian import tconfig
    for d in WORK_DIRS:
        (tconfig.WORK / d).mkdir(parents=True, exist_ok=True)
    os.chdir(tconfig.WORK)
    # Child processes (build jobs, `case find --get`) run `python -m tribonian ...`: same interpreter,
    # same package (installed or not), same work folder.
    os.environ.update(TRIBONIAN_PY=sys.executable, TRIBONIAN_WORK=str(tconfig.WORK),
                      PYTHONPATH=os.pathsep.join(filter(None, [str(tconfig.PKG.parent), os.environ.get("PYTHONPATH")])))
    sys.argv = ["tribonian " + name] + rest       # argparse shows "usage: tribonian case ..."
    if module in SCRIPTS:
        runpy.run_module("tribonian." + module, run_name="__main__", alter_sys=True)
        return 0
    import importlib
    r = importlib.import_module("tribonian." + module).main()
    return r if isinstance(r, int) else 0


if __name__ == "__main__":
    sys.exit(main())
