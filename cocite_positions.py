#!/usr/bin/env python3
"""cocite_positions.py -- where every citation occurs (scope: docs/scope-cocite-next.md, section C).

    ./cocite.py positions                       # every opinion, sharded, resumable (all cores)
    ./cocite.py positions --show 690840         # one opinion's rows, with context, to check by eye
    ./cocite.py positions --sample 2000         # random opinions: rates and speed, nothing written
    ./cocite.py positions --status              # shards done / to do
    ./cocite.py positions --index               # positions by decision, for ./cocite.py build --weight

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



# ================================================================ the index: positions by decision, for the weightings
# One pass over the shards: resolve "Id." to the citation before it, mark string-cite groups (both
# need the raw sequence, so they are done before anything is dropped), map opinion -> decision,
# keep rows whose cited case is resolved, and sort by (decision year, decision) so a slice is one
# contiguous block. Within a decision the shards' order is kept: each opinion's rows stay
# together in reading order, so paragraph, sentence and string-cite groups are runs, and
# character offsets rise within an opinion (footnotes come after the body).

IDX_VERSION = 1
IDX_COLS = (("yr", np.int16), ("dec", np.int32), ("op", np.int32), ("cited", np.int32), ("para", np.int32),
            ("sent", np.int32), ("char", np.int32), ("sg", np.int32))


def idx_dir():
    import cocite
    return cocite.CACHE / "posidx"


def op_to_case():
    """opinion id -> decision (cluster) id, from the two indexes of the text table (as prep does)."""
    import cocite
    con = ops_db()
    a = cocite.fetch_array(con.execute("SELECT rowid, id FROM opinions INDEXED BY opinions_by_id"), 2)
    b = cocite.fetch_array(con.execute("SELECT rowid, coalesce(cluster_id, -1) FROM opinions INDEXED BY opinions_by_cluster"), 2)
    a = a[np.argsort(a[:, 0], kind="stable")]
    b = b[np.argsort(b[:, 0], kind="stable")]
    if len(a) != len(b) or not np.array_equal(a[:, 0], b[:, 0]):
        sys.exit("the two opinion indexes disagree; is opinions.sqlite complete?")
    m = np.full(int(a[:, 1].max()) + 1, -1, np.int32)
    m[a[:, 1]] = b[:, 1]
    return m


def idx_meta():
    import cocite, json
    return {"idx_version": IDX_VERSION, "pos_version": VERSION, "cache_stamp": (cocite.CACHE / "stamp.txt").read_text()}


def cmd_index(out=None, limit=None):
    import cocite, json
    t0 = time.time()
    d = Path(out) if out else idx_dir()
    files = sorted(pos_dir().glob("*-*.npz"))
    if limit:
        files = files[:limit]
    if not files:
        sys.exit("no position shards: run ./cocite.py positions first")
    ci = np.load(cocite.CACHE / "citing.npy", mmap_mode="r")
    yv = np.load(cocite.CACHE / "year.npy", mmap_mode="r")
    dec_year = np.zeros(int(ci.max()) + 1, np.int16)
    dec_year[np.asarray(ci)] = np.asarray(yv)
    op2dec = op_to_case()
    print("decision years and opinion -> decision map  (%.1f min)" % ((time.time() - t0) / 60), flush=True)
    parts = {c: [] for c, _ in IDX_COLS}
    n_in = n_id = n_id_ok = sg_base = 0
    for i, f in enumerate(files):
        with np.load(f) as z:
            if int(z["version"]) != VERSION:
                sys.exit("%s is extractor version %d, not %d: rerun ./cocite.py positions" % (f.name, int(z["version"]), VERSION))
            op, cited, para, sent, char, sep = (z[c] for c in ("op", "cited", "para", "sent", "char", "sep"))
        n = len(op)
        n_in += n
        cited = cited.copy()
        isid = cited == ID                         # "Id." -> the citation just before it, in the same opinion
        if isid.any():
            last = np.where(~isid, np.arange(n), -1)
            np.maximum.accumulate(last, out=last)
            ok = isid & (last >= 0)
            ok[ok] = op[last[ok]] == op[ok]
            cited[ok] = cited[last[ok]]
            n_id += int(isid.sum())
            n_id_ok += int((ok & (cited >= 0)).sum())
        new = np.ones(n, bool)                     # a string-cite group continues only across ';' or ','
        new[1:] = (op[1:] != op[:-1]) | ((sep[1:] != SEP[";"]) & (sep[1:] != SEP[","]))
        sg = (sg_base + np.cumsum(new) - 1).astype(np.int64)
        sg_base += int(new.sum())
        dec = np.where(op < len(op2dec), op2dec[np.minimum(op, len(op2dec) - 1)], -1)
        yr = np.where((dec >= 0) & (dec < len(dec_year)), dec_year[np.clip(dec, 0, len(dec_year) - 1)], 0)
        keep = (cited >= 0) & (yr > 0) & (cited != dec)
        for c, v in (("yr", yr), ("dec", dec), ("op", op), ("cited", cited), ("para", para), ("sent", sent),
                     ("char", char), ("sg", sg)):
            parts[c].append(v[keep])
        if (i + 1) % 20 == 0:
            print("  %d/%d shards read" % (i + 1, len(files)), flush=True)
    if sg_base >= 2 ** 31:
        sys.exit("too many string-cite groups for int32")
    cols = {c: np.concatenate(parts.pop(c)).astype(t) for c, t in IDX_COLS}     # one column at a time: peak RAM
    key = (cols["yr"].astype(np.int64) << 32) | cols["dec"].astype(np.int64)
    order = np.argsort(key, kind="stable")
    del key
    tmp = d.with_name(d.name + ".tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    for c, _t in IDX_COLS:
        np.save(tmp / ("%s.npy" % c), cols[c][order])
        del cols[c]
    np.save(tmp / "dec_year.npy", dec_year)
    m = idx_meta()
    m.update(rows=int(len(order)), rows_in=n_in, id_rows=n_id, id_resolved=n_id_ok, shards=len(files))
    (tmp / "meta.json").write_text(json.dumps(m, indent=1))
    if d.exists():                                 # replace the old index (rename, so readers never see half)
        old = d.with_name(d.name + ".old")
        if old.exists():
            for f in old.iterdir():
                f.unlink()
            old.rmdir()
        d.rename(old)
    tmp.rename(d)
    print("index: %s rows kept of %s (resolved, decision known); Id. resolved %s of %s  (%.1f min) -> %s" % (
        format(len(order), ","), format(n_in, ","), format(n_id_ok, ","), format(n_id, ","), (time.time() - t0) / 60, d))


_IDX = {}


def index():
    """The index, memory-mapped; exits with what to run when it is missing or stale."""
    if _IDX:
        return _IDX
    import json
    d = idx_dir()
    if not (d / "meta.json").exists():
        sys.exit("No position index: run ./cocite.py positions --index")
    m = json.loads((d / "meta.json").read_text())
    want = idx_meta()
    stale = [k for k in want if m.get(k) != want[k]]
    if stale:
        sys.exit("The position index is stale (%s): run ./cocite.py positions --index" % ", ".join(stale))
    for c, _t in IDX_COLS:
        _IDX[c] = np.load(d / ("%s.npy" % c), mmap_mode="r")
    _IDX["dec_year"] = np.load(d / "dec_year.npy", mmap_mode="r")
    return _IDX


# ================================================================ weightings: pair weight per decision from positions
# Binary co-citation is M'M = sum over decisions of m_d m_d': every decision adds 1 to every pair it
# cites. A weighting replaces that block with K_d(i, j) in [0, 1], and the pair's support becomes
# sum_d K_d(i, j). Nodes, population (--max-cites) and --min-support are unchanged, so with K = 1
# the build is the binary one (up to text links vs the citation map: 'decision' measures that).
#   decision     1 if the decision's text cites both anywhere (the control)
#   paragraph    1 if some paragraph cites both (footnotes are their own paragraphs)
#   sentence     1 if some sentence cites both
#   string       1 if both sit in one string cite (citations joined by ';' or ',')
#   kernel:<c>   max over the two cases' occurrences in one opinion of exp(-distance / c), distance in
#                characters; footnotes sit after the body, so a note and the body barely interact
# A decision's K is the max over its opinions (a majority and a dissent count once).

WEIGHT_NAMES = ("decision", "paragraph", "sentence", "string")


def parse_weight(text):
    """'sentence' -> 'sentence'; 'kernel:200' -> 'k200'; None / 'binary' -> None (the citation map)."""
    if not text or text == "binary":
        return None
    if text in WEIGHT_NAMES:
        return text
    m = re.fullmatch(r"(?:kernel:|k)(\d+)", text)
    if m and int(m.group(1)) > 0:
        return "k%d" % int(m.group(1))
    sys.exit("unknown --weight %s; use binary, %s or kernel:<chars>" % (text, ", ".join(WEIGHT_NAMES)))


def run_pairs(g):
    """Rows sorted so each group is a run (g = group id per row): index pairs (a, b), a < b, in one group."""
    n = len(g)
    if n < 2:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    starts = np.r_[0, np.flatnonzero(g[1:] != g[:-1]) + 1]
    ends = np.r_[starts[1:], n]
    end_of = np.repeat(ends, ends - starts)
    cnt = end_of - np.arange(n) - 1
    a = np.repeat(np.arange(n), cnt)
    first = np.repeat(np.cumsum(cnt) - cnt, cnt)
    b = a + 1 + (np.arange(len(a)) - first)
    return a, b


def weighted_pairs(weight, decs, refs, min_support, chunk_rows=4_000_000):
    """decs: the slice's population (sorted decision ids); refs: its nodes (sorted case ids).
    -> ea, eb (node indices, ea < eb), es (summed weight >= min_support), stats."""
    I = index()
    nn = len(refs)
    empty = np.zeros(0, np.int64)
    if not len(decs) or nn < 2:
        return empty, empty, np.zeros(0), {}
    yrs = np.asarray(I["dec_year"])[decs[decs < len(I["dec_year"])]]
    yrs = yrs[yrs > 0]
    if not len(yrs):
        return empty, empty, np.zeros(0), {}
    yr = I["yr"]
    lo, hi = int(np.searchsorted(yr, yrs.min(), "left")), int(np.searchsorted(yr, yrs.max(), "right"))
    dec = np.asarray(I["dec"][lo:hi])
    pi = np.minimum(np.searchsorted(decs, dec), len(decs) - 1)
    ok = decs[pi] == dec
    cited = np.asarray(I["cited"][lo:hi])
    ni = np.minimum(np.searchsorted(refs, cited), nn - 1)
    ok &= refs[ni] == cited
    rows = np.flatnonzero(ok)
    dec, node = dec[rows], ni[rows].astype(np.int64)
    kern = weight.startswith("k")
    if kern:
        lam = float(weight[1:])
        cut = int(5 * lam)                         # exp(-5) < 0.7%: farther pairs are dropped
        op, ch = np.asarray(I["op"][lo:hi])[rows], np.asarray(I["char"][lo:hi])[rows].astype(np.int64)
    elif weight == "decision":
        grp = dec.astype(np.int64)
    elif weight == "string":
        grp = np.asarray(I["sg"][lo:hi])[rows].astype(np.int64)
    else:
        col = "para" if weight == "paragraph" else "sent"
        op = np.asarray(I["op"][lo:hi])[rows].astype(np.int64)
        grp = (op << 32) | np.asarray(I[col][lo:hi])[rows].astype(np.int64)
    bits = max(1, int(nn).bit_length())
    pk_all, w_all, pending = [], [], 0
    stats = dict(decisions=len(decs), with_positions=int(len(np.unique(dec))), rows=len(rows))

    def merge():
        pk = np.concatenate(pk_all)
        wv = np.concatenate(w_all)
        u, inv = np.unique(pk, return_inverse=True)
        pk_all[:] = [u]
        w_all[:] = [np.bincount(inv, weights=wv)]

    # chunks end on a decision boundary
    bounds = [0]
    while bounds[-1] < len(rows):
        e = min(bounds[-1] + chunk_rows, len(rows))
        if e < len(rows):
            e = int(np.searchsorted(dec, dec[e - 1], "right"))
        bounds.append(e)
    for s0, s1 in zip(bounds, bounds[1:]):
        d_loc = np.unique(dec[s0:s1], return_inverse=True)[1].astype(np.int64)
        c = node[s0:s1]
        if kern:
            # pairs within one opinion, at most cut characters apart (rows are in reading order)
            ops_ = op[s0:s1]
            o = np.r_[0, np.cumsum(ops_[1:] != ops_[:-1])].astype(np.int64)   # opinion run: rises along rows
            key = (o << 32) | ch[s0:s1]                                       # so key is sorted
            end = np.searchsorted(key, key + cut, "right")
            cnt = end - np.arange(len(key)) - 1
            a = np.repeat(np.arange(len(key)), cnt)
            first = np.repeat(np.cumsum(cnt) - cnt, cnt)
            b = a + 1 + (np.arange(len(a)) - first)
            w = np.exp(-(key[b] - key[a]) / lam)
        else:
            g = grp[s0:s1]
            a, b = run_pairs(g)
            w = None
        keep = c[a] != c[b]
        a, b = a[keep], b[keep]
        i, j = np.minimum(c[a], c[b]), np.maximum(c[a], c[b])
        dk = (d_loc[a] << (2 * bits)) | (i << bits) | j
        if w is None:                              # K = 1: count each (decision, pair) once
            u = np.unique(dk)
            wv = np.ones(len(u))
        else:                                      # K = max over the decision's occurrence pairs
            w = w[keep]
            o2 = np.argsort(dk, kind="stable")
            dk, w = dk[o2], w[o2]
            st = np.r_[0, np.flatnonzero(dk[1:] != dk[:-1]) + 1] if len(dk) else np.zeros(0, np.int64)
            u = dk[st]
            wv = np.maximum.reduceat(w, st) if len(st) else np.zeros(0)
        pk_all.append(u & ((1 << (2 * bits)) - 1))
        w_all.append(wv)
        pending += len(u)
        if pending > 30_000_000:
            merge()
            pending = len(pk_all[0])
    if not pk_all:
        return empty, empty, np.zeros(0), stats
    merge()
    pk, w = pk_all[0], w_all[0]
    keep = w >= min_support
    pk, w = pk[keep], w[keep]
    print("  %s weights: %s of %s decisions have positions, %s rows, %s pairs >= %s" % (
        weight, format(stats["with_positions"], ","), format(stats["decisions"], ","), format(stats["rows"], ","),
        format(len(pk), ","), min_support), flush=True)
    return (pk >> bits).astype(np.int64), (pk & ((1 << bits) - 1)).astype(np.int64), w, stats



# ================================================================ evidence for a blind reading of proximity
# Does a low sentence share (decisions citing a pair in one sentence / decisions citing both) mark
# boilerplate (standard-of-review and procedure cases cited everywhere together), and a high share a
# doctrinal tie? A reader (a language model) classifies pairs from evidence alone; it never sees the
# share, the side, the controls or the hypothesis. Pairs come from one slice of the binary view:
# --n from the lowest-share fifth and --n from the highest, each case at most --per-case times per
# side, plus known-answer controls where they are pairs in the slice. Evidence per pair: names,
# CourtListener's parentheticals for each case, and --passages random decisions citing both, each
# with the text around each case's first citation in one of its opinions (merged when they overlap,
# which is the evidence itself). Items are shuffled and carry random ids; the key (side, share,
# support, control answer) goes to a separate file.

CONTROLS = [   # (cite A, cite B, expected); taken only when the pair is in the slice
    ("477 U.S. 317", "477 U.S. 242", "procedural"),      # Celotex / Anderson v. Liberty Lobby
    ("477 U.S. 242", "475 U.S. 574", "procedural"),      # Anderson / Matsushita
    ("477 U.S. 317", "475 U.S. 574", "procedural"),      # Celotex / Matsushita
    ("550 U.S. 544", "556 U.S. 662", "procedural"),      # Twombly / Iqbal
    ("443 U.S. 307", "397 U.S. 358", "procedural"),      # Jackson v. Virginia / Winship (sufficiency review)
    ("467 U.S. 837", "533 U.S. 218", "doctrinal"),       # Chevron / Mead
    ("373 U.S. 83", "405 U.S. 150", "doctrinal"),        # Brady / Giglio
    ("530 U.S. 466", "542 U.S. 296", "doctrinal"),       # Apprendi / Blakely
    ("384 U.S. 436", "451 U.S. 477", "doctrinal"),       # Miranda / Edwards v. Arizona
    ("476 U.S. 79", "514 U.S. 765", "doctrinal"),        # Batson / Purkett v. Elem
    ("466 U.S. 668", "474 U.S. 52", "doctrinal"),        # Strickland / Hill v. Lockhart
    ("392 U.S. 1", "517 U.S. 806", "doctrinal"),         # Terry / Whren
]

INSTRUCTIONS = """Each item is a pair of court decisions that later decisions often cite together.
Classify why they are cited together, from the evidence only:
  doctrinal   - together they state or develop one specific substantive rule or doctrine
  procedural  - a general standard (review, pleading, summary judgment, sufficiency, discretion)
                that courts recite across many subject areas
  incidental  - no real link; they appear in the same kinds of opinions for separate reasons
  unclear     - the evidence does not support a choice
For each item give: id, class, confidence (high/medium/low), and a reason that quotes the passage
or parenthetical it relies on."""


def cmd_prox_evidence(args):
    import cocite, json, random as rnd_mod
    import casemap
    slice_ = int(args.slice) - int(args.slice) % cocite.WIDTH
    con = cocite.main_db()
    db = cocite.need_db()
    ci_all, cd_all, y_all, _, lvl = cocite.load_cache(view=db)
    codes, tag = cocite.parse_courts(args.courts)
    ci, cd, y = np.asarray(ci_all), np.asarray(cd_all), np.asarray(y_all)
    sel = (y >= slice_) & (y < slice_ + cocite.WIDTH)
    if codes:
        sel &= np.isin(np.asarray(lvl)[ci], codes)
    ci, cd = ci[sel], cd[sel]
    p = dict(cocite.DEFAULTS)
    refs, n, N, skipped, ea, eb, es, mc, _ = cocite.slice_pairs(ci, cd, p)
    rows_u = np.unique(ci)                             # the population slice_pairs used: decisions citing 1..max_cites nodes
    on_node = np.isin(cd, refs)
    per_row = np.bincount(np.searchsorted(rows_u, ci[on_node]), minlength=len(rows_u))
    pop = rows_u[(per_row >= 1) & (per_row <= p["max_cites"])]
    pa, pb, pw = weighted_pairs("sentence", pop.astype(np.int64), refs.astype(np.int64), 1e-12)[:3]
    bits = max(1, len(refs).bit_length())
    kp = (pa << bits) | pb
    o = np.argsort(kp)
    kp, pw = kp[o], pw[o]
    kb_ = (ea << bits) | eb
    j = np.minimum(np.searchsorted(kp, kb_), max(len(kp) - 1, 0))
    prox = np.where(kp[j] == kb_, pw[j], 0.0) if len(kp) else np.zeros(len(ea))
    share = prox / np.maximum(es, 1)
    ok = es >= args.min_support
    idx = np.flatnonzero(ok)
    print("%s %s: %s pairs with %d+ decisions citing both; sentence share median %.2f" % (
        tag, cocite.label(slice_), format(len(idx), ","), args.min_support, float(np.median(share[idx]))))
    order = idx[np.argsort(share[idx], kind="stable")]
    fifth = max(1, len(order) // 5)
    rng = rnd_mod.Random(args.seed)

    def pick(pool, side):
        pool = list(pool)
        rng.shuffle(pool)
        used, out = {}, []
        for k in pool:
            a, b = int(ea[k]), int(eb[k])
            if used.get(a, 0) >= args.per_case or used.get(b, 0) >= args.per_case:
                continue
            used[a] = used.get(a, 0) + 1
            used[b] = used.get(b, 0) + 1
            out.append((k, side))
            if len(out) >= args.n:
                break
        return out
    chosen = pick(order[:fifth], "low") + pick(order[-fifth:], "high")
    pos = {int(r): i for i, r in enumerate(refs)}
    pair_k = {(int(a), int(b)): k for k, (a, b) in enumerate(zip(ea.tolist(), eb.tolist()))}
    taken = {k for k, _ in chosen}
    controls = []
    for x, z, want in CONTROLS:
        ra, rb = casemap.db_lookup(con, x), casemap.db_lookup(con, z)
        if ra in pos and rb in pos:
            a, b = sorted((pos[ra], pos[rb]))
            k = pair_k.get((a, b))
            if k is not None and k not in taken:
                controls.append((k, "control", want))
    print("pairs: %d low share, %d high share, %d controls (of %d listed)" % (
        sum(1 for _, s in chosen if s == "low"), sum(1 for _, s in chosen if s == "high"), len(controls), len(CONTROLS)))
    items = [(k, s, None) for k, s in chosen] + controls

    # passages: decisions of the slice citing both, from the position index
    I = index()
    yr = I["yr"]
    lo, hi = int(np.searchsorted(yr, slice_, "left")), int(np.searchsorted(yr, slice_ + cocite.WIDTH - 1, "right"))
    dec = np.asarray(I["dec"][lo:hi])
    cited = np.asarray(I["cited"][lo:hi])
    want = np.unique(refs[np.r_[ea[[k for k, _, _ in items]], eb[[k for k, _, _ in items]]]])
    m = np.isin(cited, want) & np.isin(dec, pop)
    dec, cited = dec[m], cited[m]
    op, ch = np.asarray(I["op"][lo:hi])[m], np.asarray(I["char"][lo:hi])[m]
    by_case = {}
    for i, r in enumerate(cited.tolist()):
        by_case.setdefault(r, []).append(i)
    ops_con = ops_db()
    import case2md
    con.execute("ATTACH DATABASE ? AS ops", ("file:%s?mode=ro" % casedb.OPS_DB,))
    text_cache = {}

    def opinion_text(o):
        if o not in text_cache:
            r = ops_con.execute("SELECT cluster_id, text FROM opinions WHERE id = ?", (o,)).fetchone()
            text_cache[o] = extract(o, zlib.decompress(r[1]).decode("utf-8", "replace"), r[0])[1]["text"] if r and r[1] else ""
        return text_cache[o]

    def window(t, c, w=args.window):
        a, b = max(0, c - w), min(len(t), c + w)
        return a, b

    def snippet(t, a, b):
        s = t[a:b].replace("\n", " ¶ ")
        return ("…" if a > 0 else "") + s.strip() + ("…" if b < len(t) else "")

    def parens(ref):
        pd = case2md.parens_data(con, ref) or {}
        return [g["text"][:300] for g in (pd.get("groups") or [])[:3]]

    out, key = [], {}
    for k, side, ctl in items:
        A, B = int(refs[ea[k]]), int(refs[eb[k]])
        ia, ib = by_case.get(A, []), by_case.get(B, [])
        da = {}                                          # (decision, opinion) -> first char of A, of B
        for i in ia:
            e = da.setdefault((int(dec[i]), int(op[i])), [None, None])
            if e[0] is None:
                e[0] = int(ch[i])
        for i in ib:
            e = da.get((int(dec[i]), int(op[i])))
            if e is not None and e[1] is None:
                e[1] = int(ch[i])
        both = [(d, o, e[0], e[1]) for (d, o), e in da.items() if e[0] is not None and e[1] is not None]
        seen, picks = set(), []
        rng.shuffle(both)
        for d, o, x, z in both:                          # one opinion per decision
            if d in seen:
                continue
            seen.add(d)
            picks.append((o, x, z))
            if len(picks) >= args.passages:
                break
        passages = []
        for o, x, z in picks:
            t = opinion_text(o)
            (a1, b1), (a2, b2) = window(t, x), window(t, z)
            if a2 <= b1 and a1 <= b2:
                passages.append([snippet(t, min(a1, a2), max(b1, b2))])
            else:
                passages.append([snippet(t, a1, b1), snippet(t, a2, b2)])
        iid = "%04x" % rng.randrange(16 ** 4)
        while iid in key:
            iid = "%04x" % rng.randrange(16 ** 4)
        cases = [{"name": cocite.name(db, A), "parentheticals": parens(A)},
                 {"name": cocite.name(db, B), "parentheticals": parens(B)}]
        if rng.random() < 0.5:
            cases.reverse()
        out.append({"id": iid, "cases": cases, "passages": passages})
        key[iid] = {"side": side, "expected": ctl, "share": round(float(share[k]), 3), "support": int(es[k]),
                    "sentence": round(float(prox[k]), 1), "refs": [A, B]}
    rng.shuffle(out)
    cocite.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stem = "proximity-%s-%d" % (cocite.COCITE_DB.stem, slice_)
    path = cocite.EVIDENCE_DIR / (stem + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"instructions": INSTRUCTIONS, "items": len(out)}, ensure_ascii=False) + "\n")
        for it in out:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    (cocite.EVIDENCE_DIR / (stem + ".key.json")).write_text(json.dumps(key, indent=1))
    print("%d items -> %s (key: %s.key.json; do not show the key to the reader)" % (len(out), path, stem))


def add_evidence_parser(sub, view):
    p = sub.add_parser("prox-evidence", parents=[view],
                       help="blind evidence file: pairs by sentence share, for a reader to classify (cocite_positions.py)")
    p.add_argument("--slice", default="2010", help="slice start year (default %(default)s)")
    p.add_argument("--n", type=int, default=100, help="pairs from each end of the share range (default %(default)s)")
    p.add_argument("--min-support", dest="min_support", type=int, default=30,
                   help="pairs need this many decisions citing both (default %(default)s)")
    p.add_argument("--per-case", dest="per_case", type=int, default=3, help="a case appears at most this often per side")
    p.add_argument("--passages", type=int, default=4, help="decisions quoted per pair (default %(default)s)")
    p.add_argument("--window", type=int, default=220, help="characters either side of a citation (default %(default)s)")
    p.add_argument("--seed", type=int, default=1)
    import cocite
    p.set_defaults(func=sys.modules["__main__"].cmd_prox_evidence if hasattr(sys.modules["__main__"], "cmd_prox_evidence")
                   else cmd_prox_evidence)


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
    if args.index:
        return cmd_index(args.out, args.limit)
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
    p.add_argument("--index", action="store_true",
                   help="sort the shards by decision into cocite-cache/posidx/, for build --weight (after a run or a prep)")
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
