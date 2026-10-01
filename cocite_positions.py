#!/usr/bin/env python3
"""cocite_positions.py -- where every citation occurs (scope: docs/scope-cocite-next.md, section C).

    ./cocite.py positions                       # every opinion, sharded, resumable (all cores)
    ./cocite.py positions --show 690840         # one opinion's rows, with context, to check by eye
    ./cocite.py positions --sample 2000         # random opinions: rates and speed, nothing written
    ./cocite.py positions --status              # shards done / to do

The expensive text pass runs once and stores positions, not pairs or distances: any proximity
weight (same sentence, same paragraph, string cite, a distance kernel) is then a cheap
computation over these rows, and changing the metric never rereads the text.

One row per citation occurrence (bulk/cocite-cache/pos/<lo>-<hi>.npz, one file per rowid range):
    op      citing opinion id (aggregate to the decision through cocite-cache later)
    cited   cited CLUSTER id, from href="/opinion/<cluster>/..."; negative = unresolved:
            -1 ambiguous (multiple-matches), -2 no link (statutes too), -3 "Id." (resolve from
            the previous row when a metric wants it)
    seq     order in the opinion
    para    paragraph index    \\  over the whole opinion, footnotes placed after the body,
    sent    sentence index      >  so footnote numbers never collide with body numbers
    char    character offset   /   (in the normalised text, as casemap.marked_text makes it)
    fn      footnote ordinal (1, 2, ... in order of appearance; 0 = body)
    sep     what lies between this citation and the previous one in the paragraph:
            0 first in paragraph, 1 ';' (string cite), 2 ',' (short gap: history, lists),
            3 prose in the same sentence, 4 a sentence break
    signal  introducing signal, index into SIGNALS
Per opinion (ops, int32 x 8): op, linked (1 = html_with_citations read, 0 = skipped, -1 = extractor failed), paragraphs,
sentences, characters, footnotes, rows, links seen.
Footnote marks (m_op, m_fn, m_char, m_sent): where each footnote is called in the body, so a
metric can place a note's citations at their mark.

Facts the extractor relies on (checked against db/opinions.sqlite, 2026-10-01):
  - href="/opinion/<N>/" is the cited cluster, data-id the cited opinion; they differ for about
    half of links (newer data), and the href number read as an opinion id is another case.
  - Parallel cites to one case are one occurrence ("443 U.S. 307, 319, 99 S.Ct. 2781";
    California "[184 Cal.Rptr. 622]"; New York parentheses). The case's own caption cite is dropped.
  - <pre> text alternates </pre><span citation>..</span><pre class="inline">; those tags are not
    paragraph breaks (casemap.marked_text treats them as one).
  - Footnotes: Harvard <footnote label>/<footnotemark>; other HTML <div class="footnote" id=..>
    with <a class="footnote" href="#.."> marks. <pre> text: notes are not separable (fn = 0).
  - Only html_with_citations is read (99.8% of opinions); the rest are counted in ops, linked = 0.

After changing the extractor, bump VERSION: shards with another version are redone.
"""
import argparse, html, os, random, re, sqlite3, sys, time, zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

import casedb

VERSION = 2                                        # 2: control characters in the source text stripped
SHARD = 50_000                                     # rowids per shard file

NO_LINK, MULTIPLE, ID = -2, -1, -3
SEP = {"start": 0, ";": 1, ",": 2, "prose": 3, "sentence": 4}
SIGNALS = ["(none)", "see", "see also", "cf.", "but see", "but cf.", "compare", "accord",
           "see generally", "see, e.g.,", "contra", "quoting", "citing", "cited in", "e.g.,"]
SIG_CODE = {s: i for i, s in enumerate(SIGNALS)}
SIGNAL_RE = re.compile(r"\b(see,? e\. ?g\.,|see also|see generally|see|but see|but cf\.|cf\.|accord|"
                       r"compare|contra|quoting|citing|cited in|e\. ?g\.,)(?=[\s,]|$)", re.I)
ID_RE = re.compile(r"^(?:(?:see also|see|cf\.|accord|but see)\s+)?(?:id\.|ibid\.?)", re.I)
# casemap.sentences' abbreviations; the test here is stricter (the word must touch the period),
# else "§ 924(c). Count IV" reads "c" as an initial and never breaks
NO_BREAK = {"v", "co", "inc", "corp", "ltd", "no", "nos", "stat", "cong", "sess", "rep", "fed", "reg", "ed",
            "supp", "cir", "app", "cf", "ibid", "id", "op", "cit", "ass", "assn", "mfrs", "mut", "ins", "auto",
            "dept", "e", "g", "i", "u", "s", "c", "f", "d", "pp", "p", "n", "nn", "ch", "pt", "art", "sec",
            "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sept", "sep", "oct", "nov", "dec", "etc", "al", "et"}
BREAK_RE = re.compile('[.?!]["”’)\\]]*\\s+(?=[A-Z“"‘(\\[])')
PARALLEL_GAP = re.compile(r"(?:[\s,\[\]()–-]|\d){0,24}")
CTRL_RE = re.compile(r"[\x00-\x08]")
CITE_RE = re.compile(r'<span class="citation([^"]*)"[^>]*>(.*?)</span>', re.S)
HREF_RE = re.compile(r'href="/opinion/(\d+)/')
HARVARD_FN = re.compile(r'<footnote\b[^>]*?(?:label="([^"]*)")?[^>]*>(.*?)</footnote>', re.S)
HTML_FN = re.compile(r'<div class="footnote" id="([^"]+)"[^>]*>(.*?)</div>', re.S)
HARVARD_MARK = re.compile(r'<footnotemark\b[^>]*>(.*?)</footnotemark>', re.S)
HTML_MARK = re.compile(r'<a class="footnote" href="#([^"]+)"[^>]*>.*?</a>', re.S)

COLS = ("op", "cited", "seq", "para", "sent", "char", "fn", "sep", "signal")
DTYPES = (np.int32, np.int32, np.int32, np.int32, np.int32, np.int32, np.int16, np.int8, np.int8)
MARK_COLS = ("m_op", "m_fn", "m_char", "m_sent")


def pos_dir(out=None):
    import cocite
    return Path(out) if out else cocite.CACHE / "pos"


# ---------------------------------------------------------------- the extractor

def clean(htm):
    """casemap.marked_text's normalisation, generalised: \x01k\x02 citation tokens and \x03k\x03
    footnote-mark tokens survive. -> paragraphs."""
    htm = re.sub(r"\n[ \t]*\n", "\x06", htm.replace("\r", ""))
    htm = re.sub(r'<pre class="inline"[^>]*>', "", htm)
    htm = re.sub(r"<(p|div|blockquote|h\d|footnote|li|tr|pre)\b[^>]*>", "\x06", htm)
    text = html.unescape(re.sub(r"<[^>]+>", "", htm))
    text = re.sub(r"(\w)-\n[ \t]*(?=[a-z])", r"\1", text)
    text = re.sub(r"\*+\d+", " ", text)
    paras = [re.sub(r"\s+([,.;:)])", r"\1", re.sub(r"\s+", " ", p)).strip() for p in text.split("\x06")]
    return [p for p in paras if p]


def streams(htm):
    """-> body html, [footnote html]; marks in the body become \x03ordinal\x03."""
    fns, ids = [], {}
    if "<footnote" in htm:
        def take(m):
            ids[(m.group(1) or "").strip()] = len(fns) + 1
            fns.append(m.group(2))
            return " "
        htm = HARVARD_FN.sub(take, htm)
        htm = HARVARD_MARK.sub(lambda m: "\x03%d\x03" % ids.get(re.sub(r"<[^>]+>|\s", "", m.group(1)), 0), htm)
    elif 'class="footnote"' in htm:
        def take(m):
            ids[m.group(1)] = len(fns) + 1
            fns.append(re.sub(r'<a class="footnote"[^>]*>.*?</a>', " ", m.group(2), flags=re.S))
            return " "
        htm = HTML_FN.sub(take, htm)
        htm = HTML_MARK.sub(lambda m: " " if m.group(1).endswith("_ref") else "\x03%d\x03" % ids.get(m.group(1), 0), htm)
    return htm, fns


def sentence_starts(p, spans):
    """Offsets in paragraph p where sentences start; never inside a citation."""
    out = [0]
    for m in BREAK_RE.finditer(p):
        if any(a <= m.start() < b for a, b in spans):
            continue
        word = re.findall(r"([A-Za-z]+)$", p[out[-1]:m.start()] or " ")
        if p[m.start()] == "." and word and (word[-1].lower() in NO_BREAK or len(word[-1]) == 1):
            continue
        out.append(m.end())
    return out


def gap_kind(gap):
    depth, flat = 0, []
    for ch in gap:                                  # punctuation outside parentheses only
        depth += ch == "("
        if depth == 0:
            flat.append(ch)
        depth -= ch == ")" and depth > 0
    flat = "".join(flat)
    if ";" in flat:
        return SEP[";"]
    if "," in flat and len(re.findall(r"[A-Za-z]{4,}", flat)) <= 8:
        return SEP[","]
    return SEP["prose"]


def extract(op, htm, own=None):
    """-> rows [(op, cited, seq, para, sent, char, fn, sep, signal)], stats, marks [(fn, char, sent)]"""
    cites = []
    # the source text can itself hold \x00-\x08; they would read as our tokens (IndexError, v1)
    htm = CTRL_RE.sub(" ", htm)

    def tok(m):
        cls, inner = m.group(1), m.group(2)
        h = HREF_RE.search(inner)
        anchor = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", inner))).strip()
        cid = int(h.group(1)) if h else (MULTIPLE if "multiple" in cls else ID if ID_RE.match(anchor) else NO_LINK)
        cites.append((cid, anchor))
        return "\x01%d\x02" % (len(cites) - 1)
    body, fns = streams(CITE_RE.sub(tok, htm))

    rows, marks, flat = [], [], []
    text_len = para_no = sent_no = selfs = 0
    last = None                                      # (cited, para, sent) of the previous occurrence
    for fn, stream in enumerate([body] + fns):
        for p in clean(stream):
            out, spans, items, pos, at = [], [], [], 0, 0
            for m in re.finditer("\x01(\\d+)\x02|\x03(\\d+)\x03", p):
                out.append(p[pos:m.start()])
                at += m.start() - pos
                if m.group(1) is not None:
                    cid, anchor = cites[int(m.group(1))]
                    out.append(anchor)
                    spans.append((at, at + len(anchor)))
                    items.append((True, cid, at, at + len(anchor)))
                    at += len(anchor)
                else:
                    items.append((False, int(m.group(2)), at, at))
                pos = m.end()
            out.append(p[pos:])
            txt = "".join(out)
            starts = sentence_starts(txt, spans)
            prev_end = 0
            for is_cite, val, a, b in items:
                s_idx = sent_no + int(np.searchsorted(starts, a, side="right")) - 1
                if not is_cite:
                    marks.append((val, text_len + a, s_idx))
                    continue
                if val == own:                       # the case's own caption citation
                    selfs += 1
                    continue
                gap = txt[prev_end:a]
                if last and val >= 0 and last[0] == val and last[1] == para_no and PARALLEL_GAP.fullmatch(gap):
                    prev_end = b                     # a parallel cite: same occurrence
                    continue
                if last is None or last[1] != para_no:
                    sep = SEP["start"]
                elif last[2] != s_idx:
                    sep = SEP["sentence"]
                else:
                    sep = gap_kind(gap)
                sig = 0
                for sm in SIGNAL_RE.finditer(gap[-160:]):
                    key = re.sub(r"\s+", " ", sm.group(1).lower()).replace("e. g.", "e.g.").replace("see e.g.", "see, e.g.")
                    sig = SIG_CODE.get(key, 0)
                rows.append((op, val, len(rows), para_no, s_idx, text_len + a, fn, sep, sig))
                last = (val, para_no, s_idx)
                prev_end = b
            flat.append(txt)
            text_len += len(txt) + 1
            sent_no += len(starts)
            para_no += 1
    stats = dict(paras=para_no, sents=sent_no, chars=text_len, fns=len(fns), links=len(cites), self=selfs,
                 rows=len(rows), resolved=sum(1 for r in rows if r[1] >= 0), text="\n".join(flat))
    return rows, stats, marks


# ---------------------------------------------------------------- shards

def ops_db():
    if not casedb.OPS_DB.exists():
        sys.exit("No opinion database at %s" % casedb.OPS_DB)
    return sqlite3.connect("file:%s?mode=ro" % casedb.OPS_DB, uri=True)


def shard_path(d, lo, hi):
    return d / ("%08d-%08d.npz" % (lo, hi))


def shard_done(path):
    if not path.exists():
        return False
    try:
        with np.load(path) as z:
            return int(z["version"]) == VERSION
    except Exception:
        return False


def run_shard(lo, hi, path):
    con = ops_db()
    rows, ops, marks = [], [], []
    for oid, own, field, blob in con.execute(
            "SELECT id, cluster_id, field, text FROM opinions WHERE rowid BETWEEN ? AND ?", (lo, hi)):
        if field != "html_with_citations" or not blob:
            ops.append((oid, 0, 0, 0, 0, 0, 0, 0))
            continue
        try:
            r, st, mk = extract(oid, zlib.decompress(blob).decode("utf-8", "replace"), own)
        except Exception as e:                      # one bad opinion must not cost the shard
            print("  opinion %d skipped: %s: %s" % (oid, type(e).__name__, e), file=sys.stderr, flush=True)
            ops.append((oid, -1, 0, 0, 0, 0, 0, 0))   # linked = -1: extractor failed
            continue
        rows.extend(r)
        marks.extend((oid,) + m for m in mk)
        ops.append((oid, 1, st["paras"], st["sents"], st["chars"], st["fns"], st["rows"], st["links"]))
    arr = np.array(rows, dtype=np.int64).reshape(-1, len(COLS))
    mk = np.array(marks, dtype=np.int64).reshape(-1, len(MARK_COLS))
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez(tmp, version=np.int32(VERSION), ops=np.array(ops, dtype=np.int32).reshape(-1, 8),
             **{c: arr[:, i].astype(d) for i, (c, d) in enumerate(zip(COLS, DTYPES))},
             **{c: mk[:, i].astype(np.int32 if c != "m_fn" else np.int16) for i, c in enumerate(MARK_COLS)})
    tmp.replace(path)                                # atomic: a shard file is complete or absent
    return len(ops), len(arr)


def plan(d, shard):
    top = ops_db().execute("SELECT max(rowid) FROM opinions").fetchone()[0] or 0
    return [(lo, min(lo + shard - 1, top), shard_path(d, lo, min(lo + shard - 1, top)))
            for lo in range(1, top + 1, shard)]


def load(d=None, cols=COLS):
    """All shards concatenated: {column: array}. For the proximity weightings."""
    files = sorted(pos_dir(d).glob("*-*.npz"))
    parts = {c: [] for c in cols}
    for f in files:
        with np.load(f) as z:
            if int(z["version"]) != VERSION:
                continue
            for c in cols:
                parts[c].append(z[c])
    return {c: (np.concatenate(v) if v else np.zeros(0)) for c, v in parts.items()}


# ---------------------------------------------------------------- commands

def cmd_show(oid):
    con = ops_db()
    r = con.execute("SELECT id, cluster_id, field, text FROM opinions WHERE id = ?", (oid,)).fetchone()
    if not r or not r[3]:
        sys.exit("no text for opinion %d" % oid)
    rows, st, marks = extract(r[0], zlib.decompress(r[3]).decode("utf-8", "replace"), r[1])
    text = st.pop("text")
    print("opinion %d (case %d), %s: %s" % (r[0], r[1], r[2], st))
    print("%-4s %-9s %4s %4s %6s %3s %-8s %-10s  context" % ("seq", "cited", "para", "sent", "char", "fn", "sep", "signal"))
    inv = {v: k for k, v in SEP.items()}
    for x in rows:
        ctx = text[max(0, x[5] - 50):x[5]].replace("\n", "¶") + "⟦" + text[x[5]:x[5] + 40].replace("\n", "¶")
        print("%-4d %-9d %4d %4d %6d %3d %-8s %-10s  %s" % (x[2], x[1], x[3], x[4], x[5], x[6], inv[x[7]], SIGNALS[x[8]], ctx))
    if marks:
        print("footnote marks (fn: char, sentence):", ", ".join("%d: %d, %d" % m for m in marks))


def cmd_sample(n, seed=1):
    con = ops_db()
    top = con.execute("SELECT max(rowid) FROM opinions").fetchone()[0]
    rnd = random.Random(seed)
    tot = dict(opinions=0, linked=0, rows=0, resolved=0, ambiguous=0, nolink=0, id=0, self=0, footnote_rows=0)
    seps, sigs, t = np.zeros(len(SEP), int), np.zeros(len(SIGNALS), int), 0.0
    for rid in rnd.sample(range(1, top + 1), n):
        r = con.execute("SELECT id, cluster_id, field, text FROM opinions WHERE rowid = ?", (rid,)).fetchone()
        if not r:
            continue
        tot["opinions"] += 1
        if r[2] != "html_with_citations" or not r[3]:
            continue
        tot["linked"] += 1
        t0 = time.perf_counter()
        rows, st, _ = extract(r[0], zlib.decompress(r[3]).decode("utf-8", "replace"), r[1])
        t += time.perf_counter() - t0
        tot["rows"] += len(rows)
        tot["self"] += st["self"]
        for x in rows:
            tot["resolved"] += x[1] >= 0
            tot["ambiguous"] += x[1] == MULTIPLE
            tot["nolink"] += x[1] == NO_LINK
            tot["id"] += x[1] == ID
            tot["footnote_rows"] += x[6] > 0
            seps[x[7]] += 1
            sigs[x[8]] += 1
    print(tot)
    print("%.2f ms per opinion (decompress + extract)" % (1000 * t / max(1, tot["linked"])))
    print("sep:", dict(zip(SEP, seps.tolist())))
    print("signal:", {s: int(c) for s, c in zip(SIGNALS, sigs) if c})


def cmd_positions(args):
    if args.show:
        return cmd_show(args.show)
    if args.sample:
        return cmd_sample(args.sample)
    d = pos_dir(args.out)
    d.mkdir(parents=True, exist_ok=True)
    jobs = plan(d, args.shard_size)
    todo = [j for j in jobs if not shard_done(j[2])]
    print("%d shards of %s rowids in %s: %d done, %d to do (extractor version %d)"
          % (len(jobs), format(args.shard_size, ","), d, len(jobs) - len(todo), len(todo), VERSION), flush=True)
    if args.status or not todo:
        return
    if args.limit:
        todo = todo[:args.limit]
    workers = args.workers or os.cpu_count() or 1
    t0, n_ops, n_rows = time.time(), 0, 0
    with ProcessPoolExecutor(workers) as pool:
        futs = {pool.submit(run_shard, lo, hi, path): path for lo, hi, path in todo}
        for i, f in enumerate(as_completed(futs), 1):
            o, r = f.result()
            n_ops += o
            n_rows += r
            el = time.time() - t0
            print("  %d/%d %s: %s opinions, %s rows  (%.0f opinions/s, %.1f h left)"
                  % (i, len(todo), futs[f].name, format(o, ","), format(r, ","), n_ops / el,
                     el / i * (len(todo) - i) / 3600), flush=True)
    print("done: %s opinions, %s rows in %.1f h" % (format(n_ops, ","), format(n_rows, ","), (time.time() - t0) / 3600))


def add_parser(sub):
    p = sub.add_parser("positions", help="record where every citation occurs (cocite_positions.py)")
    p.add_argument("--show", type=int, metavar="OPINION_ID", help="one opinion's rows with context")
    p.add_argument("--sample", type=int, metavar="N", help="N random opinions: rates and speed; writes nothing")
    p.add_argument("--status", action="store_true", help="shards done and to do")
    p.add_argument("--workers", type=int, help="processes (default: all cores)")
    p.add_argument("--shard-size", dest="shard_size", type=int, default=SHARD)
    p.add_argument("--limit", type=int, help="run at most this many shards (a trial run)")
    p.add_argument("--out", help="shard folder (default: cocite-cache/pos)")
    p.set_defaults(func=cmd_positions)
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    add_parser(sp)
    a = ap.parse_args(["positions"] + sys.argv[1:])
    a.func(a)
