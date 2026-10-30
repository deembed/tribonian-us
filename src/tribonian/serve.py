"""serve.py -- tribonian as one long-running process the GUI talks to, so data reads that need the
scripts' code (law text, fetches, later the law browser) go through Python instead of the GUI opening
files itself. Started once, it keeps its imports and caches warm between requests.

    tribonian serve              # read requests on stdin until it closes

The GUI starts it as `python -m tribonian serve`, or as `<command_prefix> tribonian serve` when the
config file's [gui] command_prefix is set (e.g. "wsl" to run it, and the data, inside WSL).

Protocol: one JSON object per line each way, answered in order.
    request   {"id": 1, "op": "law.text", "key": "usc:5:552a"}
    reply     {"id": 1, "ok": {...}}   or   {"id": 1, "error": "why"}
stdout carries replies only: anything the code prints while handling a request goes to stderr.

Ops:
    ping                 -> {"protocol", "version", "work"}
    law.text  key        -> {"key", "local": true, "heading", "rows": [[depth, label, heading, text]], "fmt": "tex"}
                            or {"key", "local": false, "hint": what would store it, or why nothing can}
    law.fetch key        -> {"key", "fetched": bool, "why": reason when not}  (network; obeys robots.txt)
    quit                 -> {} and the process exits
Keys are the appendix keys the builds use: usc:5:552a, cfr:40:52.21, const:art-1:8, md-code:...
"""
import argparse, contextlib, json, sys

PROTOCOL = 1


def _ping(req):
    from tribonian import __version__, tconfig
    return {"protocol": PROTOCOL, "version": __version__, "work": str(tconfig.WORK)}


def _key(req):
    k = req.get("key")
    if not isinstance(k, str) or not k:
        raise ValueError("needs a \"key\" (e.g. usc:5:552a)")
    return k


def _law_text(req):
    k = _key(req)
    from tribonian import lawtext
    got = lawtext.text_of(k)
    if got is None:
        return {"key": k, "local": False, "hint": lawtext.fetch_hint(k)}
    heading, rows = got
    return {"key": k, "local": True, "heading": heading, "rows": [list(r) for r in rows], "fmt": "tex"}


def _law_fetch(req):
    k = _key(req)
    from tribonian import lawtext
    spec = lawtext.fetch_spec(k)
    if spec is None:
        return {"key": k, "fetched": False, "why": lawtext.fetch_hint(k) or "nothing fetches this key"}
    ok, why = lawtext.run_fetch(spec, want=[lawtext.want_of(k)] if lawtext.want_of(k) else None)
    return {"key": k, "fetched": ok, "why": why}


OPS = {"ping": _ping, "law.text": _law_text, "law.fetch": _law_fetch}


def handle(req):
    """One request -> one reply (never raises)."""
    rid = req.get("id") if isinstance(req, dict) else None
    if not isinstance(req, dict):
        return {"id": rid, "error": "a request is a JSON object"}
    op = req.get("op")
    if op not in OPS:
        return {"id": rid, "error": "no op %r (have: %s, quit)" % (op, ", ".join(OPS))}
    try:
        with contextlib.redirect_stdout(sys.stderr):        # stray prints must not break the protocol
            return {"id": rid, "ok": OPS[op](req)}
    except SystemExit as e:                                  # a helper that exits: report it, keep serving
        return {"id": rid, "error": str(e.code) if e.code not in (None, 0) else "stopped"}
    except Exception as e:
        return {"id": rid, "error": "%s: %s" % (type(e).__name__, e)}


def serve(inp, out):
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as e:
            reply = {"id": None, "error": "not JSON: %s" % e}
        else:
            if isinstance(req, dict) and req.get("op") == "quit":
                out.write(json.dumps({"id": req.get("id"), "ok": {}}) + "\n")
                out.flush()
                return
            reply = handle(req)
        out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        out.flush()


def main():
    argparse.ArgumentParser(prog="tribonian serve", description=__doc__.split("\n\n")[0],
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
