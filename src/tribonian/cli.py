"""tribonian -- one command for the whole pipeline.

    tribonian <command> [arguments...]
    tribonian <command> --help

Every command runs in the work folder (tconfig.WORK: TRIBONIAN_WORK, else the config file's
work_dir, else the checkout), so relative paths in arguments (xml/usc05.xml, cases/<file>.json)
are relative to it, as they were to the repo before.
"""
import os, runpy, signal, sys

# command -> (module, what it does). Order is the order of the help text.
COMMANDS = {
    "check":           ("check",           "setup and database health: is each piece there, does it answer"),
    "usc":             ("usc2md",          "US Code: USLM XML -> section notes and chapter PDFs"),
    "usc-status":      ("usc_status",      "US Code release points: what is current, what changed"),
    "case":            ("case2md",         "cases: fetch, build, find, recap, api-search"),
    "map":             ("casemap",         "citation graph: get, map, stubs, citedby, goodlaw, coupling, semsearch"),
    "db":              ("casedb",          "bulk CSV -> local SQLite databases, full-text index"),
    "cocite":          ("cocite",          "co-citation clusters, lineages, areas, labels, vault, US Code co-citation"),
    "dockets":         ("dockets",         "View Dockets: the court tree and the index of fetched dockets (for the GUI)"),
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


def usage(out=sys.stdout):
    from tribonian import tconfig
    print(__doc__.split("\n\n")[1].rstrip() + "\n", file=out)
    print("commands:", file=out)
    for name, (_, what) in COMMANDS.items():
        print("  %-16s %s" % (name, what), file=out)
    print("\nwork folder: %s" % tconfig.WORK, file=out)


def main(argv=None):
    if hasattr(signal, "SIGPIPE"):                 # `tribonian ... | head` ends quietly
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        usage(sys.stderr)
        sys.exit("\ntribonian: no command %r" % name)
    module = COMMANDS[name][0]
    from tribonian import tconfig
    tconfig.WORK.mkdir(parents=True, exist_ok=True)
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
