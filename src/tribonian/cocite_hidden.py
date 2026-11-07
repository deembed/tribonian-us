"""cocite_hidden.py -- hidden rulings: decisions that lean on a sub-cluster's cases but that few later courts cite.

A sub-cluster's members are there because later decisions cite them together. A hidden decision is
the other direction: it CITES the members heavily (bibliographic coupling to the sub-cluster), but is
itself cited little -- the decisions that applied a body of law and that nobody picked up. One from a
court of appeals or the Supreme Court that nobody cites is the interesting kind, so the court counts.

    score = coupling x obscurity x court x status x reviewed x record

  coupling   how much of the decision's reference list sits in the sub-cluster, rare references
             weighted up (idf = log(N / decisions citing it), as tribonian map coupling does):
                 sum of idf over the members it cites
                 / sqrt(sum of idf over everything it cites x sum of idf over all members)
             (a weighted Ochiai / cosine). The square root of the decision's own reference weight is
             what keeps string-cite compilations from topping the list: citing 300 cases hits many
             members by volume alone, and divides by a large number.
  obscurity  (1 - cohort rank) ^ power: the decision's place among every decision filed the same year in a
             court of the same level, by decisions citing it (mid-rank on ties). Citations come in years
             after filing, not at a steady rate, so a count only means something next to decisions of the
             same age: a 2025 decision cited by no one sits mid-cohort (most of its cohort is cited by no one
             yet), a 2016 circuit decision cited once sits near the bottom of its own.
  court      a weight per court level of the decision (court.scotus, court.circuit, ...)
  status     a weight per precedential status (status.Published, status.Unpublished, ...): an
             unpublished ruling nobody cites is less of a find, since many courts bar citing it
  reviewed   the reviewed weight when a later decision in the same case, from a higher court of the same
             system (or the same court: rehearing, en banc), cites it: later courts cite the decision on
             review instead, so its own count is low by design (State v. Curry, 199 Wash. App. 43, the #1
             "overlooked" decision until the Washington Supreme Court's Curry, 191 Wn.2d 475, was found)
  record     no_cite when the decision has no reporter citation on file in CourtListener, else 1.
             Later opinions cite a case by its reporter citation; with none on file, CourtListener
             can't link them to it, so "cited by 0" there mostly means "links missing", not "nobody
             cites it" (checked 2026-11: every Washington Supreme Court decision of 2017-10-12 had no
             citation on file and 0 citing decisions)

Every weight is a setting: defaults below (DEFAULTS), then the config file's [hidden] table, then
--set key=value on the command line (--set court.scotus=5 --set power=0.5). The settings used are
printed and stored with every result, so two runs can be compared.

Two lists. Overlooked: decisions old enough to have been cited (filed settle_years or more before the
bulk data), with the score above. Not yet visible: the younger ones, which co-citation cannot see at all
-- no later decision has had time to cite them beside anything -- while coupling sees them the day they
are filed (it reads what they cite). They are ranked by coupling x court x status, no obscurity: the
part of the law co-citation is blind to by construction, shown on its own.

Which decisions: those citing at least min_members of the sub-cluster's members, filed in the
sub-cluster's slice or (later = true) any time after it. Every row says which ("in slice" / "after"),
since a later decision is measured against a sub-cluster of an earlier slice. Members themselves are
never listed.

Nearest sub-clusters (of the sub-cluster): from the slice's citing decisions in the view's courts,
W(S, T) = decisions' co-citations between S's members and T's (sum over decisions of the members of S
times the members of T they cite: the binary pair support summed over S x T), shown as
W / sqrt(A_S x A_T), A = the sub-cluster's citations in the slice (sum of nodes.n). Binary counts, not the
view's sentence weights.

Quoted sources (of the sub-cluster): cases whose words reach the sub-cluster's decisions through its members --
"Member (quoting Source)" -- ranked by decisions carrying them x the share of their citations in the window
that come that way. Co-citation can't see these: courts cite the member, not the source (State v. Curry,
191 Wn.2d 475, carries United States v. Arlt, 41 F.3d 516 (9th Cir.), into Washington law). Members are left
out (they are already visible). Built once from the citation positions into cocite-cache/hidden_quotes.npz
(the first run takes a few minutes); every "(quoting X)", "(citing X)", "(cited in X)" whose X the source
text links, with "Id." resolved to the citation before it.

For each listed decision: its other sub-clusters (its coupling to each, so "fits better elsewhere"
shows), and how it uses the members -- the citing sentence from the position index, whether the
citation stands alone in its sentence or inside a string cite, and its signal (see, cf., but see ...).

    tribonian cocite hidden 2020-24:2.3 [--top 30] [--set power=0.5] [--json]
    tribonian cocite hidden --cite "550 U.S. 544"       # one decision: its profile against every sub-cluster
"""
import json, math, re, sys, time, zlib
from pathlib import Path

import numpy as np

from tribonian import cocite as C, tconfig

DEFAULTS = {
    "min_members": 2,          # cites at least this many of the sub-cluster's members
    "power": 1.0,              # obscurity = (1 - cohort rank) ^ power
    "settle_years": 3,         # younger than this (years before the bulk data): Not yet visible, no obscurity
    "later": True,             # also decisions filed after the slice
    "max_inbound": None,       # leave out decisions cited more than this (raw count); None = no cap
    "top": 30,                 # rows listed
    "neighbours": 5,           # nearest sub-clusters listed
    "usage": 30,               # decisions whose citing sentences are read (text pass; the slow part)
    "min_coupling": 0.05,      # a decision's other sub-clusters at or above this coupling are listed
    "quoted_top": 15,          # quoted sources listed
    "quoted_min": 2,           # carried into at least this many decisions
    "court": {"scotus": 3.0, "circuit": 2.0, "state-supreme": 1.5, "state-appellate": 1.0,
              "district": 0.7, "other": 0.3},
    "status": {"Published": 1.0, "Unpublished": 0.5, "Unknown": 0.8, "other": 0.3},
    "no_cite": 0.25,           # record weight when no reporter citation is on file (inbound count unreliable)
    "reviewed": 0.2,           # weight when a later decision in the same case, from a higher court, cites it
    "review_years": 6,         # how long after it that decision can come
}
PROFILE_VERSION = 5


def settings(overrides=()):
    """DEFAULTS <- config [hidden] <- --set key=value. Dotted keys reach into tables (court.scotus=4)."""
    P = json.loads(json.dumps(DEFAULTS))
    src = {k: "default" for k in P}

    def put(key, val, where):
        node, parts = P, key.split(".")
        for k in parts[:-1]:
            node = node.setdefault(k, {})
        node[parts[-1]] = val
        src[parts[0]] = where
    for k, v in (tconfig.FILE.get("hidden") or {}).items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                put("%s.%s" % (k, k2), v2, "config")
        else:
            put(k, v, "config")
    for kv in overrides or ():
        k, eq, v = kv.partition("=")
        if not eq:
            sys.exit("--set wants key=value, e.g. --set power=0.5")
        try:
            val = json.loads(v.lower() if v.lower() in ("true", "false", "null") else v)
        except ValueError:
            val = v
        top = k.split(".")[0]
        if top not in DEFAULTS:
            sys.exit("unknown setting %r; settings: %s" % (top, ", ".join(DEFAULTS)))
        put(k.strip(), val, "--set")
    if float(P["power"]) < 0:
        sys.exit("power must be >= 0 (more citations never make a decision more hidden)")
    bad = [k for k in P["court"] if k not in C.COURT_LEVELS]
    if bad:
        sys.exit("unknown court level in court.*: %s; levels: %s" % (", ".join(bad), ", ".join(C.COURT_LEVELS)))
    return P, src


# ================================================================ the citation cache

_CACHE = {}


def cache():
    """citing (sorted), cited, cited_sorted, by_cited, level and filing year per case; N = citing decisions."""
    if _CACHE:
        return _CACHE
    if not (C.CACHE / "cited_sorted.npy").exists():
        sys.exit("No citation cache at %s: run tribonian cocite prep" % C.CACHE)
    ci = np.load(C.CACHE / "citing.npy", mmap_mode="r")
    _CACHE.update(ci=ci, cd=np.load(C.CACHE / "cited.npy", mmap_mode="r"),
                  cds=np.load(C.CACHE / "cited_sorted.npy", mmap_mode="r"),
                  order=np.load(C.CACHE / "by_cited.npy", mmap_mode="r"),
                  level=np.load(C.CACHE / "level.npy", mmap_mode="r"),
                  year=np.load(C.CACHE / "ref_year.npy", mmap_mode="r"))
    n, step = 1, 8_000_000                              # distinct citing decisions, in chunks (ci is sorted)
    for i in range(0, len(ci), step):
        a = np.asarray(ci[i:i + step + 1])
        n += int(np.count_nonzero(a[1:] != a[:-1]))
    _CACHE["N"] = n
    return _CACHE


def lookup(arr, keys, ids):
    """arr[ids] for a by-id array (level, year), 0 past its end."""
    ids = np.asarray(ids, np.int64)
    out = np.zeros(len(ids), arr.dtype)
    ok = (ids >= 0) & (ids < len(arr))
    out[ok] = np.asarray(arr[ids[ok]]) if ok.any() else out[ok]
    return out


def df_of(refs):
    """Decisions citing each case (cited_sorted is sorted: two binary searches each)."""
    cds = cache()["cds"]
    refs = np.asarray(refs, np.int64)
    return np.searchsorted(cds, refs, "right") - np.searchsorted(cds, refs, "left")


def citers_of(refs):
    """-> (citing decision, index into refs) for every edge into refs."""
    K = cache()
    refs = np.asarray(refs, np.int64)
    if not len(refs):
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    lo = np.searchsorted(K["cds"], refs, "left")
    hi = np.searchsorted(K["cds"], refs, "right")
    span = hi - lo
    which = np.repeat(np.arange(len(refs)), span)
    pos = np.repeat(lo - np.cumsum(np.r_[0, span[:-1]]), span) + np.arange(span.sum())
    edge = np.asarray(K["order"][pos]).astype(np.int64)
    srt = np.argsort(edge, kind="stable")             # read citing.npy in order, then put back
    dec = np.empty(len(edge), np.int64)
    dec[srt] = np.asarray(K["ci"][edge[srt]])
    return dec, which


def refs_of(decs):
    """-> (decision index, cited case) for every reference of these decisions (citing.npy is sorted)."""
    K = cache()
    decs = np.asarray(decs, np.int64)
    lo = np.searchsorted(K["ci"], decs, "left")
    hi = np.searchsorted(K["ci"], decs, "right")
    span = hi - lo
    owner = np.repeat(np.arange(len(decs)), span)
    pos = np.repeat(lo - np.cumsum(np.r_[0, span[:-1]]), span) + np.arange(span.sum())
    return owner, np.asarray(K["cd"][pos]).astype(np.int64)


# ================================================================ scoring

def slice_subs(db, s):
    """Every node of the slice: (refs sorted, their sub-cluster codes, their citing counts n)."""
    rows = np.array(db.execute("SELECT ref, sub, n FROM nodes WHERE slice=? ORDER BY ref", (s,)).fetchall(), np.int64)
    if not len(rows):
        sys.exit("slice %s has no nodes" % C.label(s))
    return rows[:, 0], rows[:, 1], rows[:, 2]


def sub_of(node_refs, node_sub, refs):
    i = np.minimum(np.searchsorted(node_refs, refs), len(node_refs) - 1)
    return np.where(node_refs[i] == refs, node_sub[i], 0)


def idf(refs):
    d = df_of(refs).astype(float)
    return np.where(d > 0, np.log(cache()["N"] / np.maximum(d, 1)), 0.0), d


def couplings(decs, node_refs, node_sub, sub_idf):
    """For each decision: its coupling to every sub-cluster it cites into.
    -> (decision index, sub code, coupling, members cited) arrays, and the decisions' own idf sums."""
    owner, r = refs_of(decs)
    w, _ = idf(r)
    own = np.bincount(owner, weights=w, minlength=len(decs))
    sb = sub_of(node_refs, node_sub, r)
    m = sb > 0
    key = owner[m] * 1_000_000_000 + sb[m]
    uk, inv = np.unique(key, return_inverse=True)
    num = np.bincount(inv, weights=w[m])
    cnt = np.bincount(inv)
    o, k = uk // 1_000_000_000, uk % 1_000_000_000
    den = np.sqrt(np.maximum(own[o], 1e-12) * np.array([sub_idf.get(int(x), 1e-12) for x in k]))
    return o, k, num / den, cnt, own


def siblings(con, d):
    """The decision and every cluster sharing a reporter citation with it: CourtListener holds some decisions
    twice (Babb v. Wilkie, 589 U.S. 399: one cluster cited 279 times, the other 0), and later opinions are
    linked to only one of them. -> (sibling ids, its reporters)."""
    cites = con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id = ?", (int(d),)).fetchall()
    ids = {int(d)}
    for v, r, pg in cites:
        ids.update(int(i) for (i,) in con.execute("SELECT cluster_id FROM cites WHERE volume=? AND reporter=? AND page=?",
                                                 (v, r, pg)))
    # and the same court, the same filing date, a party word in common: a copy with no citation on file has
    # nothing to share (Thurlby, Wash. 2015: cluster 3152368, no citation, cited by 0; 184 Wash. 2d 618, cited by 17)
    me = con.execute("SELECT c.date_filed, d.court_id, c.case_name, c.case_name_short, c.case_name_full, d.case_name "
                     "FROM clusters c LEFT JOIN dockets d ON d.id = c.docket_id WHERE c.id = ?", (int(d),)).fetchone()
    if me and me[0] and me[1]:
        mine = name_tokens(*me[2:])
        for r in con.execute("SELECT c.id, c.case_name, c.case_name_short, c.case_name_full, d.case_name FROM clusters c "
                             "JOIN dockets d ON d.id = c.docket_id WHERE c.date_filed = ? AND d.court_id = ? AND c.id != ?",
                             (me[0], me[1], int(d))):
            if mine & name_tokens(*r[1:]):
                ids.add(int(r[0]))
    return sorted(ids), {r for _, r, _ in cites}


# What the reporter says, over CourtListener's fields when they disagree: the Federal Appendix prints unpublished
# opinions whatever the status says, and F. Supp. / F.R.D. print district court opinions (D.D.C. decisions sit on
# cadc dockets, which made Forkwa, 297 F. Supp. 3d 90, a "circuit" decision).
UNPUBLISHED_REPORTERS = {"F. App'x"}
DISTRICT_REPORTERS = {"F. Supp.", "F. Supp. 2d", "F. Supp. 3d", "F.R.D."}


def reporters(decs, con):
    out = {}
    ids = [int(x) for x in decs]
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        for c, r in con.execute("SELECT cluster_id, reporter FROM cites WHERE cluster_id IN (%s)" % ",".join("?" * len(ch)), ch):
            out.setdefault(c, set()).add(r)
    return out


def apply_reporters(decs, con, P, lvl, cw, stn, sw):
    """Overrides court level and status from the reporters, in place; -> notes per decision index."""
    reps, notes = reporters(decs, con), {}
    for i, d in enumerate(int(x) for x in decs):
        r = reps.get(d, set())
        if r & UNPUBLISHED_REPORTERS and stn[i] == "Published":
            stn[i] = "Unpublished"
            sw[i] = float(P["status"].get("Unpublished", P["status"].get("other", 1.0)))
            notes.setdefault(i, []).append("unpublished: %s" % ", ".join(sorted(r & UNPUBLISHED_REPORTERS)))
        if r & DISTRICT_REPORTERS and lvl[i] != "district":
            notes.setdefault(i, []).append("district court: %s (listed as %s)" % (", ".join(sorted(r & DISTRICT_REPORTERS)), lvl[i]))
            lvl[i] = "district"
            cw[i] = float(P["court"].get("district", 1.0))
    return notes


def refine(i, d, con, P, data_year, cache_):
    """For one decision of the preliminary ranking: the exact inbound count (citers of the decision and its
    siblings, once each; never below the cheap count, so with power >= 0 it can only lower the score), its
    siblings, and the later decisions in the same case that reviewed it (reviewed_by)."""
    if d in cache_:
        return cache_[d]
    ids, reps = siblings(con, d)
    D, _ = citers_of(np.array(ids))
    D = np.setdiff1d(np.unique(D), ids)
    cache_[d] = (max(int(len(D)), int(df_of([d])[0])), [x for x in ids if x != d], reviewed_by(con, d, D, P))
    return cache_[d]


# Review: the same case, later, in a court above it in the same system (or the same court: rehearing, en banc).
RANK = {"district": 1, "state-appellate": 1, "circuit": 2, "state-supreme": 2, "scotus": 3, "other": 0}
SYSTEM = {"district": "federal", "circuit": "federal", "state-appellate": "state", "state-supreme": "state", "scotus": "both"}
NAME_SKIP = {"state", "states", "united", "people", "commonwealth", "city", "county", "department", "dept", "of", "the",
             "and", "inc", "corp", "co", "llc", "ltd", "et", "al", "jr", "sr", "in", "re", "matter", "ex", "rel", "parte",
             "personal", "restraint", "detention", "estate", "petition", "v", "vs", "appellant", "respondent", "petitioner",
             "appellee", "plaintiff", "defendant", "commissioner", "secretary", "attorney", "general", "office", "board",
             "school", "district", "corporation", "company", "national", "american", "association"}


def name_tokens(*names):
    from tribonian import casemap
    out = set()
    for n in names:
        for w in re.findall(r"[a-z][a-z'-]{2,}", (n or "").lower()):
            if w not in NAME_SKIP and w not in casemap.STATES:
                out.add(w)
    return out


def case_rows(con, ids):
    """id -> (names, court id, year) from clusters and dockets."""
    out = {}
    ids = [int(x) for x in ids]
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        for r in con.execute("SELECT c.id, c.case_name, c.case_name_short, c.case_name_full, d.case_name, d.court_id, "
                             "substr(c.date_filed, 1, 4) FROM clusters c LEFT JOIN dockets d ON d.id = c.docket_id "
                             "WHERE c.id IN (%s)" % ",".join("?" * len(ch)), ch):
            out[r[0]] = (r[1:5], r[5] or "", int(r[6]) if r[6] and r[6].isdigit() else 0)
    return out


def level_name(code):
    return C.COURT_LEVELS[code - 1] if 1 <= code <= len(C.COURT_LEVELS) else "other"


def reviewed_by(con, d, D, P):
    """Later decisions in the same case from a higher court of the same system (or the same court) citing d:
    [{"cluster", "name", "cite", "court", "year"}]. Same case = a party word in common, beyond the generic ones
    (State, United States, City, ...)."""
    me = case_rows(con, [d]).get(int(d))
    if not me or not len(D):
        return []
    my_lv = level_name(int(lookup(cache()["level"], None, [d])[0]))
    yrs = lookup(cache()["year"], None, D).astype(int)
    lv = lookup(cache()["level"], None, D).astype(int)
    span = int(P["review_years"])
    pick = [int(x) for x, y, l in zip(D.tolist(), yrs.tolist(), lv.tolist())
            if me[2] <= y <= me[2] + span and RANK.get(level_name(l), 0) >= RANK.get(my_lv, 0)
            and (SYSTEM.get(level_name(l)) in (SYSTEM.get(my_lv), "both"))]
    if not pick:
        return []
    mine = name_tokens(*me[0])
    out = []
    rows = case_rows(con, pick)
    for x in pick:
        r = rows.get(x)
        if not r:
            continue
        higher = RANK.get(level_name(int(lookup(cache()["level"], None, [x])[0])), 0) > RANK.get(my_lv, 0)
        if not higher and r[1] != me[1]:                 # same level: only the same court (rehearing, en banc)
            continue
        if mine & name_tokens(*r[0]):
            nm, cite, y, court = C.case_label(con, x)
            out.append({"cluster": x, "name": nm, "cite": cite, "court": court, "year": y})
    return sorted(out, key=lambda z: z["year"])


def exact_top(score, N, fix):
    """The N best indices of score (> 0), where fix(indices) may lower the scores it is given: fix the best pool,
    re-rank, and widen the pool until no unfixed score left outside could still beat the N-th. -> (best, pool)."""
    order = np.argsort(-score, kind="stable")
    order = order[score[order] > 0]
    done = set()
    pool = min(len(order), max(4 * N, 50))
    while len(order):
        todo = [i for i in order[:pool].tolist() if i not in done]
        if todo:
            fix(todo)
            done.update(todo)
        top_in = np.sort(score[order[:pool]])[::-1]
        nth = top_in[min(N, len(top_in)) - 1]
        if pool >= len(order) or score[order[pool]] <= nth:
            break
        pool = min(len(order), pool * 2)
    best = order[:pool][np.argsort(-score[order[:pool]], kind="stable")][:N] if len(order) else order
    return best[score[best] > 0], pool


def cohort():
    """Every citing decision's inbound count, keyed by (filing year, court level), sorted: one int64 per decision,
    key << 32 | inbound. Cached in cocite-cache/hidden_cohort.npz with the cache's stamp (rebuilt after a prep)."""
    if "cohort" in _CACHE:
        return _CACHE["cohort"]
    K = cache()
    stamp = (C.CACHE / "stamp.txt").read_text()
    path = C.CACHE / "hidden_cohort.npz"
    if path.exists():
        with np.load(path) as z:
            if str(z["stamp"]) == stamp:
                _CACHE["cohort"] = z["keys"]
                return _CACHE["cohort"]
    t0 = time.time()
    top = int(max(int(K["ci"][-1]), int(K["cds"][-1]))) + 1
    counts = np.zeros(top, np.int32)                     # decisions citing each case, from the cited column
    ci, cd, step = K["ci"], K["cd"], 8_000_000
    decs = []
    for i in range(0, len(cd), step):
        counts += np.bincount(np.asarray(cd[i:i + step]), minlength=top).astype(np.int32)
        a = np.asarray(ci[i:i + step])
        decs.append(np.unique(a))
    decs = np.unique(np.concatenate(decs)).astype(np.int64)
    yr = lookup(K["year"], None, decs).astype(np.int64)
    lv = lookup(K["level"], None, decs).astype(np.int64)
    keys = np.sort((yr * 8 + lv) << 32 | counts[decs].astype(np.int64))
    np.savez(path, keys=keys, stamp=np.array(stamp))
    print("hidden: cohort of %s citing decisions by filing year and court level  (%.0f s) -> %s" % (
        format(len(keys), ","), time.time() - t0, path.name), file=sys.stderr)
    _CACHE["cohort"] = keys
    return keys


def cohort_rank(year, level_code, inbound):
    """Mid-rank of each decision's inbound count in its (year, level) cohort, in [0, 1], and the cohort's size."""
    S = cohort()
    g = (np.asarray(year, np.int64) * 8 + np.asarray(level_code, np.int64)) << 32
    x = g | np.asarray(inbound, np.int64)
    a, b = np.searchsorted(S, g, "left"), np.searchsorted(S, g + (1 << 32), "left")
    lo, hi = np.searchsorted(S, x, "left"), np.searchsorted(S, x, "right")
    n = np.maximum(b - a, 1)
    return ((lo - a) + (hi - lo) / 2.0) / n, b - a, (lo - a) / n, (hi - lo) / n


LEVEL_CODE = {n: i + 1 for i, n in enumerate(C.COURT_LEVELS)}


def obscurity(decs, P, levels, inbound=None):
    """-> inbound, filing year, obscurity = (1 - cohort rank) ^ power, cohort rank, cohort size."""
    inbound = df_of(decs) if inbound is None else np.asarray(inbound)
    yr = lookup(cache()["year"], None, decs).astype(int)
    rank, size, less, same = cohort_rank(yr, [LEVEL_CODE.get(l, 6) for l in levels], inbound)
    return inbound, yr, (1.0 - rank) ** float(P["power"]), np.c_[rank, less, same], size


def court_weight(decs, P):
    lv = lookup(cache()["level"], None, decs).astype(int)
    names = [C.COURT_LEVELS[c - 1] if 1 <= c <= len(C.COURT_LEVELS) else "other" for c in lv]
    return names, np.array([float(P["court"].get(n, 1.0)) for n in names])


def status_weight(decs, P, con):
    """CourtListener's precedential status of each decision, and its weight (status.<name>, else status.other)."""
    st = {}
    ids = [int(x) for x in decs]
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        st.update(con.execute("SELECT id, precedential_status FROM clusters WHERE id IN (%s)" % ",".join("?" * len(ch)), ch))
    names = [st.get(d) or "Unknown" for d in ids]
    W = P["status"]
    return names, np.array([float(W.get(n, W.get("other", 1.0))) for n in names])


def record_weight(decs, P, con):
    """Whether each decision has a reporter citation on file (else its inbound count is unreliable), and the weight."""
    has = set()
    ids = [int(x) for x in decs]
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        has.update(r for (r,) in con.execute("SELECT DISTINCT cluster_id FROM cites WHERE cluster_id IN (%s)" % ",".join("?" * len(ch)), ch))
    ok = np.array([d in has for d in ids])
    return ok, np.where(ok, 1.0, float(P["no_cite"]))


def data_year_of(db):
    return C.newest_full_slice(db)


def sub_idf_sums(node_refs, node_sub):
    w, _ = idf(node_refs)
    out = {}
    for k, v in zip(node_sub.tolist(), w.tolist()):
        if k > 0:
            out[k] = out.get(k, 0.0) + v
    return out


def neighbours(db, s, j, node_refs, node_sub, node_n, view_codes, top):
    """Nearest sub-clusters of S: binary co-citation between S's members and T's, from the slice's decisions."""
    members = node_refs[node_sub == j]
    D, _ = citers_of(members)
    yr = lookup(cache()["year"], None, D)
    keep = (yr >= s) & (yr <= s + C.WIDTH - 1)
    if view_codes:
        keep &= np.isin(lookup(cache()["level"], None, D), view_codes)
    decs, nS = np.unique(D[keep], return_counts=True)
    owner, r = refs_of(decs)
    sb = sub_of(node_refs, node_sub, r)
    m = (sb > 0) & (sb != j)
    key = sb[m]
    W = {}
    for k, v in zip(key.tolist(), nS[owner[m]].tolist()):
        W[k] = W.get(k, 0) + v
    A = {}
    for k, v in zip(node_sub.tolist(), node_n.tolist()):
        if k > 0:
            A[k] = A.get(k, 0) + v
    out = []
    for k, w in W.items():
        out.append({"sub": k, "code": "%s:%s" % (C.label(s), C.sub_label(k)), "pairs": int(w),
                    "weight": w / math.sqrt(A[j] * A[k]) if A.get(j) and A.get(k) else 0.0})
    out.sort(key=lambda x: -x["weight"])
    tot = sum(W.values()) or 1
    for x in out:
        x["share"] = x["pairs"] / tot
    for x in out[:top]:
        x["name"] = C.unit_name(db, s, x["sub"], "sub")[0] or x["code"]
    return out[:top], len(decs)


def view_codes_of(view):
    tag = (view or "").split("-")[0]
    if tag in C.COURT_SETS or tag in C.COURT_LEVELS:
        return C.parse_courts(tag)[0]
    return None


def hidden(db, view, s, j, P, con=None):
    t0 = time.time()
    node_refs, node_sub, node_n = slice_subs(db, s)
    members = node_refs[node_sub == j]
    sidf = sub_idf_sums(node_refs, node_sub)
    D, which = citers_of(members)
    yr = lookup(cache()["year"], None, D)
    hi_year = 9999 if P["later"] else s + C.WIDTH - 1
    keep = (yr >= s) & (yr <= hi_year) & ~np.isin(D, members)
    D, which = D[keep], which[keep]
    pair = np.unique(D * 100_000 + which)                 # distinct (decision, member)
    dd, cnt = np.unique(pair // 100_000, return_counts=True)
    cand = dd[cnt >= int(P["min_members"])]
    res = {"slice": s, "slice_label": C.label(s), "sub": j, "code": "%s:%s" % (C.label(s), C.sub_label(j)),
           "name": C.unit_name(db, s, j, "sub")[0], "view": view, "members": int(len(members)),
           "settings": P, "candidates": int(len(cand)), "data_year": data_year_of(db),
           "window": "filed %d-%s" % (s, "now" if P["later"] else s + C.WIDTH - 1), "results": []}
    nb, ndec = neighbours(db, s, j, node_refs, node_sub, node_n, view_codes_of(view), int(P["neighbours"]))
    res["neighbours"], res["neighbour_decisions"] = nb, ndec
    if not len(cand):
        return res
    o, k, cpl, mc, own = couplings(cand, node_refs, node_sub, sidf)
    here = k == j
    cS = np.zeros(len(cand))
    cS[o[here]] = cpl[here]
    nS = np.zeros(len(cand), np.int64)
    nS[o[here]] = mc[here]
    lvl, cw = court_weight(cand, P)
    con = con or C.main_db()
    stn, sw = status_weight(cand, P, con)
    hasc, rw = record_weight(cand, P, con)
    rnotes = apply_reporters(cand, con, P, lvl, cw, stn, sw)
    inbound, cyr, obs, crank, csize = obscurity(cand, P, lvl)
    young = cyr > res["data_year"] - int(P["settle_years"])
    res["settled_before"] = res["data_year"] - int(P["settle_years"]) + 1
    old_score = np.where(young, -1.0, cS * obs * cw * sw * rw)
    new_score = np.where(young, cS * cw * sw, -1.0)
    N = int(P["top"])
    # Preliminary scores use the cheap inbound count and no review check; refine() makes both exact for a pool of
    # the best, and can only lower a score, so the pool grows until nothing outside it could still beat the N-th:
    # both lists are exact.
    sibs, revd, ref_cache = {}, {}, {}
    rvw = np.ones(len(cand))

    def fix_old(todo):
        for i in todo:
            inbound[i], sibs[i], revd[i] = refine(i, int(cand[i]), con, P, res["data_year"], ref_cache)
            if revd[i]:
                rvw[i] = float(P["reviewed"])
        ix = np.array(todo)
        _, _, ob, rk, sz = obscurity(cand[ix], P, [lvl[i] for i in todo], inbound[ix])
        obs[ix], crank[ix], csize[ix] = ob, rk, sz
        old_score[ix] = cS[ix] * obs[ix] * cw[ix] * sw[ix] * rvw[ix] * rw[ix]
        if P.get("max_inbound") is not None:
            old_score[ix[inbound[ix] > int(P["max_inbound"])]] = -1

    def fix_new(todo):
        for i in todo:
            inbound[i], sibs[i], revd[i] = refine(i, int(cand[i]), con, P, res["data_year"], ref_cache)
            if revd[i]:
                rvw[i] = float(P["reviewed"])
        ix = np.array(todo)
        new_score[ix] = cS[ix] * cw[ix] * sw[ix] * rvw[ix]

    best_old, pool_old = exact_top(old_score, N, fix_old)
    best_new, pool_new = exact_top(new_score, N, fix_new)
    res["refined"] = int(pool_old + pool_new)
    want = np.r_[best_old, best_new]
    others = {}                                           # decision index -> [(sub, coupling, members)]
    sel = np.isin(o, want)
    for a, b, c, n in zip(o[sel].tolist(), k[sel].tolist(), cpl[sel].tolist(), mc[sel].tolist()):
        others.setdefault(a, []).append((b, c, n))
    names = {}

    def rows(best, score, listname):
        group = {}                                        # one decision held as several clusters: one row
        for i in best.tolist():
            group.setdefault(min([int(cand[i])] + sibs.get(i, [])), []).append(i)
        firsts = {v[0]: [int(cand[x]) for x in v[1:]] for v in group.values()}
        out, seen = [], {}
        for i in best.tolist():
            if i not in firsts:
                continue
            d = int(cand[i])
            nm, cite, y, court = C.case_label(con, d)
            dup = (nm.lower(), court, int(cyr[i]), round(float(score[i]), 6))   # same name, court, year, score
            if dup in seen:
                seen[dup]["duplicates"].append(d)
                continue
            subs = sorted(others.get(i, []), key=lambda x: -x[1])
            alt = [x for x in subs if x[0] != j]
            nxt = [x for x in alt if x[1] >= float(P["min_coupling"])][:5] or alt[:1]
            row = {"rank": len(out) + 1, "list": listname, "cluster": d, "name": nm, "cite": cite, "year": int(cyr[i]) or y,
                   "court": court, "level": lvl[i], "when": "in slice" if cyr[i] <= s + C.WIDTH - 1 else "after",
                   "score": round(float(score[i]), 5), "coupling": round(float(cS[i]), 4),
                   "obscurity": round(float(obs[i]), 4) if listname == "overlooked" else None,
                   "cohort_rank": round(float(crank[i, 0]), 4), "cohort_less": round(float(crank[i, 1]), 4),
                   "cohort_same": round(float(crank[i, 2]), 4), "cohort_size": int(csize[i]),
                   "court_weight": float(cw[i]), "status": stn[i], "status_weight": float(sw[i]),
                   "has_cite": bool(hasc[i]), "record_weight": float(rw[i]) if listname == "overlooked" else None,
                   "reviewed_by": revd.get(i, []), "reviewed_weight": float(rvw[i]),
                   "inbound": int(inbound[i]), "members_cited": int(nS[i]),
                   "fits_best_here": not subs or subs[0][0] == j,
                   "duplicates": firsts[i] + [x for x in sibs.get(i, []) if x not in firsts[i]], "notes": rnotes.get(i, []),
                   "next": [{"sub": b, "code": "%s:%s" % (C.label(s), C.sub_label(b)), "coupling": round(c, 4), "members": n,
                             "name": names.setdefault(b, C.unit_name(db, s, b, "sub")[0])} for b, c, n in nxt]}
            out.append(row)
            seen[dup] = row
        return out
    res["results"] = rows(best_old, old_score, "overlooked")
    res["new"] = rows(best_new, new_score, "not yet visible")
    res["young_candidates"] = int(young.sum())
    if P.get("quoted_top"):
        res["quoted"] = quoted_sources(db, s, j, members, P, s, hi_year, con, node_refs, node_sub)
    if int(P["usage"]):
        mset = set(members.tolist())
        add_usage(res["results"][:int(P["usage"])] + res["new"][:int(P["usage"])], mset, con)
    res["seconds"] = round(time.time() - t0, 1)
    return res


# ================================================================ quoted sources

QUOTE_VERSION = 1
QUOTE_KINDS = ("quoting", "citing", "cited in")


def quotes_meta():
    from tribonian import cocite_positions as CP
    return "%d|%d|%s" % (QUOTE_VERSION, CP.VERSION, (C.CACHE / "stamp.txt").read_text())


def quotes(build=False):
    """Every "A (quoting B)" in the citation positions: arrays dec, a, b, kind (0 quoting, 1 citing, 2 cited in),
    op, char (of B). From the position shards (cocite-cache/pos), each opinion's citations in order: a row with
    one of those signals is B, the row before it in the same opinion is A ("Id." resolved first, as the position
    index does). None when not built and build is False."""
    if "quotes" in _CACHE:
        return _CACHE["quotes"]
    from tribonian import cocite_positions as CP
    path = C.CACHE / "hidden_quotes.npz"
    if path.exists():
        z = np.load(path)
        if str(z["meta"]) == quotes_meta():
            _CACHE["quotes"] = {k: z[k] for k in ("dec", "a", "b", "kind", "op", "char")}
            return _CACHE["quotes"]
    if not build:
        return None
    files = sorted(CP.pos_dir().glob("*-*.npz"))
    if not files:
        return None
    t0 = time.time()
    I = CP.index()                                       # opinion -> decision, from the position index's own columns
    op2dec = np.full(int(I["op"].max()) + 1, -1, np.int32)
    step = 10_000_000
    for i in range(0, len(I["op"]), step):
        op2dec[np.asarray(I["op"][i:i + step])] = np.asarray(I["dec"][i:i + step])
    codes = [CP.SIG_CODE[k] for k in QUOTE_KINDS]
    parts = {k: [] for k in ("dec", "a", "b", "kind", "op", "char")}
    for n, f in enumerate(files, 1):
        with np.load(f) as z:
            op, cited, sig, char = z["op"], z["cited"].copy(), z["signal"], z["char"]
        isid = cited == CP.ID                            # "Id." -> the citation just before it, same opinion
        if isid.any():
            last = np.where(~isid, np.arange(len(op)), -1)
            np.maximum.accumulate(last, out=last)
            ok = isid & (last >= 0)
            ok[ok] = op[last[ok]] == op[ok]
            cited[ok] = cited[last[ok]]
        q = np.flatnonzero(np.isin(sig, codes))
        q = q[q > 0]
        q = q[(op[q - 1] == op[q]) & (cited[q] >= 0) & (cited[q - 1] >= 0) & (cited[q] != cited[q - 1])]
        dec = np.where(op[q] < len(op2dec), op2dec[np.minimum(op[q], len(op2dec) - 1)], -1)
        keep = dec >= 0
        q, dec = q[keep], dec[keep]
        kind = np.searchsorted(np.array(codes), sig[q])
        for k, v in (("dec", dec), ("a", cited[q - 1]), ("b", cited[q]), ("kind", kind), ("op", op[q]), ("char", char[q])):
            parts[k].append(np.asarray(v))
        if n % 40 == 0:
            print("  quotes: %d/%d shards" % (n, len(files)), file=sys.stderr, flush=True)
    out = {k: np.concatenate(v).astype(np.int8 if k == "kind" else np.int32) for k, v in parts.items()}
    o = np.argsort(out["a"], kind="stable")              # by A: "what a member passes on" is a binary search
    out = {k: v[o] for k, v in out.items()}
    np.savez(path, meta=np.array(quotes_meta()), **out)
    print("hidden: %s quotations through citations (A quoting/citing B)  (%.0f s) -> %s" % (
        format(len(out["a"]), ","), time.time() - t0, path.name), file=sys.stderr)
    _CACHE["quotes"] = out
    return out


def quoted_sources(db, s, j, members, P, lo_year, hi_year, con, node_refs, node_sub):
    """Cases whose words reach the window's decisions through the sub-cluster's members (members excluded)."""
    Q = quotes(build=True)
    if Q is None:
        return None
    a = Q["a"]
    lo, hi = np.searchsorted(a, members, "left"), np.searchsorted(a, members, "right")
    idx = np.concatenate([np.arange(x, y) for x, y in zip(lo.tolist(), hi.tolist())]) if len(members) else np.zeros(0, np.int64)
    if not len(idx):
        return []
    dec, A, B = Q["dec"][idx].astype(np.int64), Q["a"][idx].astype(np.int64), Q["b"][idx].astype(np.int64)
    yr = lookup(cache()["year"], None, dec)
    keep = (yr >= lo_year) & (yr <= hi_year) & ~np.isin(B, members)
    idx, dec, A, B, yr = idx[keep], dec[keep], A[keep], B[keep], yr[keep]
    if not len(idx):
        return []
    key = np.unique(B * 10**8 + dec)                    # distinct (source, decision)
    srcs, carried = np.unique(key // 10**8, return_counts=True)
    ok = carried >= int(P["quoted_min"])
    srcs, carried = srcs[ok], carried[ok]
    if not len(srcs):
        return []
    D, w = citers_of(srcs)                              # every decision in the window citing the source at all
    dy = lookup(cache()["year"], None, D)
    m = (dy >= lo_year) & (dy <= hi_year)
    allc = np.bincount(w[m], minlength=len(srcs))
    share = carried / np.maximum(allc, carried)
    score = carried * share
    top = np.argsort(-score, kind="stable")[:int(P["quoted_top"])]
    out = []
    for i in top.tolist():
        b = int(srcs[i])
        sel = B == b
        carriers, cname = {}, {}
        for x in A[sel].tolist():                       # by name: a member held twice is one carrier
            nm_x = cname.setdefault(x, C.case_label(con, x)[0])
            carriers[nm_x] = carriers.get(nm_x, 0) + 1
        kinds = np.bincount(Q["kind"][idx[sel]], minlength=3)
        nm, cite, y, court = C.case_label(con, b)
        sb = int(sub_of(node_refs, node_sub, np.array([b]))[0])
        first = idx[sel][np.argmin(yr[sel])]
        out.append({"cluster": b, "name": nm, "cite": cite, "year": y, "court": court,
                    "decisions": int(carried[i]), "cited_in_window": int(allc[i]), "share": round(float(share[i]), 3),
                    "score": round(float(score[i]), 2), "first_year": int(yr[sel].min()),
                    "kinds": {k: int(n) for k, n in zip(QUOTE_KINDS, kinds.tolist()) if n},
                    "in_sub": ("%s:%s" % (C.label(s), C.sub_label(sb))) if sb > 0 else None,
                    "through": [{"name": x, "n": n} for x, n in sorted(carriers.items(), key=lambda kv: -kv[1])[:4]],
                    "example": {"decision": int(Q["dec"][first]), "op": int(Q["op"][first]), "char": int(Q["char"][first])}})
    for x in out[:int(P["usage"])]:                     # one sentence each, from the decision that carried it first
        tx = opinion_text(x["example"]["op"])
        if tx:
            bef, aft = sentence_at(tx, x["example"]["char"])
            x["example"]["sentence"] = re.sub(r"\s+", " ", bef + aft).strip()[:500]
            x["example"]["name"] = C.case_label(con, x["example"]["decision"])[0]
    return out


# ================================================================ how the members are used

_TEXT = {}


def unpack(blob):
    """Stored opinion text, whichever codec wrote it (zlib; zstd once textcodec is in)."""
    try:
        from tribonian import textcodec
        return textcodec.unpack(blob)
    except ImportError:
        return zlib.decompress(blob).decode("utf-8", "replace")


def opinion_text(o):
    from tribonian import cocite_positions as CP
    if o not in _TEXT:
        if "ops" not in _TEXT:
            _TEXT["ops"] = CP.ops_db()
        r = _TEXT["ops"].execute("SELECT cluster_id, text FROM opinions WHERE id = ?", (o,)).fetchone()
        _TEXT[o] = CP.extract(o, unpack(r[1]), r[0])[1]["text"] if r and r[1] else ""
    return _TEXT[o]


SIGNALS = r"see,? e\. ?g\.[.,]?|see also|see generally|but see|but cf\.|cf\.|see|accord|compare|contra|e\. ?g\.[.,]?"
SIG_START = re.compile(r"^\W*(?:\d+\s+)?(%s)(?=[\s,]|$)" % SIGNALS, re.I)   # at the start of the cite's own clause
SIG_AFTER = re.compile(r"^(?:[^()]|\([^()]{0,40}\)){0,200}?\((quoting|citing|cited in)\s+([^()]*?)(?=,\s*\d|\s+\d+\s|\)|$)", re.I)  # A (quoting B): A passes on B
SIG_INSIDE = re.compile(r"\((quoting|citing|cited in)\s+[^()]{0,120}$", re.I)                   # A (quoting B): B's use


def sentence_at(t, c):
    """The citation's sentence. A citation sentence ("State v. X, 1 Wn.2d 2 (1990).") supports the sentence
    before it, so when the cite opens its sentence the one before is taken in too."""
    from tribonian import casemap
    before = t[max(0, c - 900):c]
    parts = re.split(casemap.SENTENCE_END, before)
    b = parts[-1]
    if len(re.sub(r"[^A-Za-z]", "", SIG_START.sub("", b))) < 25 and len(parts) > 1:   # only a signal / a name
        b = parts[-2] + ". " + b
    _, a = casemap.sentence_window(before, t[c:c + 600])
    a = re.split(r"\s¶\s?\d*", a)[0]                   # a paragraph number starts the next sentence
    b = re.split(r"¶\s?\d*\s", b)[-1]
    return b[-400:], a


def add_usage(rows, members, con):
    """Each row gets 'uses': one entry per member it cites -- sentence, alone or in a string cite, signal --
    and 'usage': a one-line summary. Needs tribonian cocite positions --index; without it, a note why not."""
    from tribonian import cocite_positions as CP
    try:
        I = CP.index()
    except SystemExit as e:
        for r in rows:
            r["usage"] = "(no citing sentences: %s)" % e
        return
    yr, dec = I["yr"], I["dec"]
    lab = {}
    for row in rows:
        d, y = row["cluster"], row["year"]
        try:
            y = int(y)
        except (TypeError, ValueError):
            row["usage"] = "(no filing year)"
            continue
        lo, hi = int(np.searchsorted(yr, y, "left")), int(np.searchsorted(yr, y, "right"))
        dd = np.asarray(dec[lo:hi])
        a, b = lo + int(np.searchsorted(dd, d, "left")), lo + int(np.searchsorted(dd, d, "right"))
        cited = np.asarray(I["cited"][a:b])
        if not len(cited):
            row["usage"] = "(not in the position index)"
            continue
        ops, chars, sgs = np.asarray(I["op"][a:b]), np.asarray(I["char"][a:b]), np.asarray(I["sg"][a:b])
        sg_size = dict(zip(*np.unique(sgs, return_counts=True)))
        uses, seen = [], set()
        for i in np.flatnonzero(np.isin(cited, list(members))).tolist():
            m = int(cited[i])
            if m in seen:                      # first occurrence of each member
                continue
            seen.add(m)
            t = opinion_text(int(ops[i]))
            if not t:
                continue
            bef, aft = sentence_at(t, int(chars[i]))
            clause = re.split(r";|\)\s*,\s*(?=[A-Z])", bef)[-1]   # its own place in a string cite
            inside = SIG_INSIDE.search(bef)                 # the member is the B of "A (quoting B)"
            sig = None if inside else SIG_START.match(clause)
            via = SIG_AFTER.match(aft)                      # the member is the A: its words are B's
            if m not in lab:
                lab[m] = C.case_label(con, m)[0]
            sent = re.sub(r"\s+", " ", (bef + aft)).strip()[:500]
            if lab[m].lower() in seen:             # one case held twice in CourtListener: one use
                continue
            seen.add(lab[m].lower())
            uses.append({"cluster": m, "name": lab[m], "alone": int(sg_size.get(sgs[i], 1)) == 1,
                         "signal": ("quoted inside another's cite" if inside.group(1).lower() == "quoting" else
                                    "cited inside another's cite") if inside else norm_signal(sig.group(1) if sig else ""),
                         "via": {"kind": via.group(1).lower(), "name": re.sub(r"\s+", " ", via.group(2)).strip(" ,")} if via else None,
                         "sentence": sent})
        row["uses"] = uses
        row["usage"] = usage_line(uses)


def best_use(uses):
    """The use that says most: alone in its sentence first, then the most words outside citations."""
    def prose(u):
        return len(re.sub(r"\d+\s+[A-Z][\w.\s]{0,15}\d+|\([^()]*\d{4}\)", "", u["sentence"]).split())
    return max(uses, key=lambda u: (u["alone"], prose(u))) if uses else None


def norm_signal(s):
    s = re.sub(r"\s+", " ", s.lower()).strip(" .,")
    s = re.sub(r"e\. ?g", "e.g.", s)
    return "see, e.g." if s.startswith("see") and "e.g" in s else s


def usage_line(uses):
    if not uses:
        return "(members cited, but no citing sentence found)"
    alone = sum(1 for u in uses if u["alone"])
    sig = {}
    for u in uses:
        sig[u["signal"] or "direct"] = sig.get(u["signal"] or "direct", 0) + 1     # no signal: cited as stating it
    adverse = [u["name"] for u in uses if u["signal"] in ("but see", "but cf.", "contra")]
    t = "%d member%s cited: %d alone in a sentence, %d in string cites; %s" % (
        len(uses), "s" if len(uses) != 1 else "", alone, len(uses) - alone,
        ", ".join("%s x%d" % kv for kv in sorted(sig.items(), key=lambda kv: -kv[1])))
    if adverse:
        t += "; against: " + ", ".join(adverse)
    vias = ["%s (%s %s)" % (u["name"], u["via"]["kind"], u["via"]["name"]) for u in uses if u.get("via")]
    if vias:
        t += "; passing on others' words: " + "; ".join(vias)
    return t


# ================================================================ one decision (case notes)

def profile(ref, db=None, view=C.NOTE_VIEW, P=None, con=None, year=None):
    """One decision against every sub-cluster of the newest full slice (or the slice it was filed in, when
    that one is built and complete): its couplings, obscurity, court weight, score per sub-cluster, and how it
    uses the members of the best ones. -> dict, or None when the view or the cache is missing."""
    db = db or C.open_view(view)
    if db is None or not (C.CACHE / "cited_sorted.npy").exists():
        return None
    P = P or settings()[0]
    full = C.full_slice_of(db, year)
    if full is None:
        return None
    y = int(lookup(cache()["year"], None, [ref])[0])
    own = y - y % C.WIDTH if y else None
    s = own if own is not None and own <= full and db.execute("SELECT 1 FROM slices WHERE slice=?", (own,)).fetchone() else full
    node_refs, node_sub, node_n = slice_subs(db, s)
    sidf = sub_idf_sums(node_refs, node_sub)
    o, k, cpl, mc, ownw = couplings(np.array([ref]), node_refs, node_sub, sidf)
    lvl, cw = court_weight(np.array([ref]), P)
    con = con or C.main_db()
    stn, sw = status_weight([ref], P, con)
    hasc, rw = record_weight([ref], P, con)
    n_in, sib, rv = refine(0, int(ref), con, P, data_year_of(db), {})
    rnotes = apply_reporters([ref], con, P, lvl, cw, stn, sw)
    inbound, cyr, obs, crank, csize = obscurity(np.array([ref]), P, lvl, [n_in])
    dy = data_year_of(db)
    young = bool(y and y > dy - int(P["settle_years"]))
    rvw = float(P["reviewed"]) if rv else 1.0
    mult = float(cw[0] * sw[0] * rvw) if young else float(obs[0] * cw[0] * sw[0] * rvw * rw[0])
    is_node = sub_of(node_refs, node_sub, np.array([ref]))[0]
    order = np.argsort(-cpl)
    subs = []
    for i in order.tolist():
        if mc[i] < int(P["min_members"]) or (subs and cpl[i] < float(P["min_coupling"])):
            continue
        subs.append({"sub": int(k[i]), "code": "%s:%s" % (C.label(s), C.sub_label(int(k[i]))),
                     "name": C.unit_name(db, s, int(k[i]), "sub")[0], "coupling": round(float(cpl[i]), 4),
                     "members": int(mc[i]), "score": round(float(cpl[i]) * mult, 5)})
    out = {"version": PROFILE_VERSION, "cluster": ref, "view": view, "slice": s, "slice_label": C.label(s),
           "slice_is": "filed in it" if s == own else "newest full slice", "year": y,
           "list": "not yet visible" if young else "overlooked", "data_year": dy, "settle_years": int(P["settle_years"]),
           "inbound": int(inbound[0]), "obscurity": None if young else round(float(obs[0]), 4),
           "cohort_rank": round(float(crank[0, 0]), 4), "cohort_less": round(float(crank[0, 1]), 4),
           "cohort_same": round(float(crank[0, 2]), 4), "cohort_size": int(csize[0]),
           "level": lvl[0], "court_weight": float(cw[0]), "status": stn[0], "status_weight": float(sw[0]),
           "has_cite": bool(hasc[0]), "record_weight": None if young else float(rw[0]), "also_held_as": sib,
           "reviewed_by": rv, "reviewed_weight": rvw,
           "notes": rnotes.get(0, []), "node_in": int(is_node) if is_node > 0 else None,
           "settings": P, "subs": subs[:8]}
    if subs:
        top = set(x["sub"] for x in subs[:3])
        members = set(node_refs[np.isin(node_sub, list(top))].tolist())
        row = {"cluster": ref, "year": y}
        add_usage([row], members, con)
        sub_by_ref = dict(zip(node_refs.tolist(), node_sub.tolist()))
        for u in row.get("uses", []):
            u["sub"] = "%s:%s" % (C.label(s), C.sub_label(sub_by_ref.get(u["cluster"], 0)))
        out["uses"], out["usage"] = row.get("uses", []), row.get("usage")
    return out


# ================================================================ output

def reviewed_text(r):
    rv = r.get("reviewed_by") or []
    if not rv:
        return ""
    return "; reviewed by %s (x%g)" % ("; ".join("%s, %s (%s %s)" % (x["name"], x["cite"] or "cluster %d" % x["cluster"],
                                                                  x["court"], x["year"]) for x in rv[:3]), r["reviewed_weight"])


def cohort_text(r):
    """'cited by 1 decision; of the 20,524 circuit decisions of 2015, 52% are cited less, 11% as often'.
    The rank obscurity uses is the middle of that tie: less + same / 2."""
    return "cited by %d decision%s; of the %s %s decisions of %s, %s are cited less, %s as often" % (
        r["inbound"], "" if r["inbound"] == 1 else "s", format(r["cohort_size"], ","), r["level"], r["year"],
        pct(r["cohort_less"]), pct(r["cohort_same"]))


def pct(x):
    return "%.0f%%" % (100 * x) if x >= 0.01 or x == 0 else "%.1f%%" % (100 * x)


def facts_line(p):
    young = p.get("list") == "not yet visible"
    return "%s list. Filed %s; %s court (x%g); %s (x%g)%s%s; %s%s" % (
        "Not yet visible" if young else "Overlooked", p["year"], p["level"], p["court_weight"], p["status"], p["status_weight"],
        "" if p["has_cite"] or young else "; no reporter citation on file, so the count is unreliable (x%g)" % p["record_weight"],
        "".join("; %s" % n for n in p.get("notes", [])) + ("; also held as cluster %s" % ", ".join(map(str, p["also_held_as"]))
                                                           if p.get("also_held_as") else ""),
        cohort_text(p) + reviewed_text(p),
        (". Not yet visible: filed within %d years of the bulk data (%s), too new to be co-cited, so scored without "
         "obscurity" % (p["settle_years"], p["data_year"])) if young else " -> obscurity %.3f" % p["obscurity"])


METHOD = ("Whether this decision is a hidden ruling: how much it leans on each co-citation sub-cluster's cases "
          "(coupling: the share of its references, rare ones weighted up, that are the sub-cluster's members), against how "
          "little later decisions cite it, next to decisions of its own year and court level. Score = coupling x obscurity x "
          "court x status x reviewed x record; a decision too new to have been cited is scored coupling x court x status instead "
          "(Not yet visible: the part co-citation cannot see yet). Every weight is a setting "
          "(the config file's [hidden] table, or set on the command line for one run).")


def section_md(p, esc=lambda t: t):
    """'## Hidden ruling check' for a case note: every sub-cluster it leans on, its score in each, and its sentences."""
    if not p or not p.get("subs"):
        return []
    L = ["## Hidden ruling check", "", "*%s Not part of the record.*" % METHOD, "",
         "- " + esc(facts_line(p)),
         "- Against the sub-clusters of %s (%s)%s" % (p["slice_label"], p["slice_is"],
             "; it is itself a member of %s:%s" % (p["slice_label"], C.sub_label(p["node_in"])) if p.get("node_in") else ""),
         "- Settings: " + esc(settings_line(p["settings"])), "", "| Sub-cluster | Coupling | Members cited | Score |", "|---|---|---|---|"]
    for x in p["subs"]:
        L.append("| %s %s | %.3f | %d | %.4f |" % (x["code"], esc(x["name"] or ""), x["coupling"], x["members"], x["score"]))
    L.append("")
    if p.get("usage"):
        L += ["**How it uses them:** " + esc(p["usage"]), ""]
        for u in p.get("uses", []):
            how = ", ".join(t for t in (u["signal"], "alone in its sentence" if u["alone"] else "in a string cite") if t)
            if u.get("via"):
                how += "; passes on the words of %s (%s)" % (u["via"]["name"], u["via"]["kind"])
            L.append("- **%s** (%s; %s): %s" % (esc(u["name"]), u["sub"], esc(how), esc(u["sentence"])))
        L.append("")
    return L


def section_tex(p, esc):
    """The same as a closing part of the case PDF."""
    if not p or not p.get("subs"):
        return ""
    E = lambda t: esc(t or "", "tex")
    out = ["\\clearpage\\recordcover{none}\\recordstart{Hidden ruling check}\\noindent\\tocback\\tocadd{tocstage}{Hidden ruling check}"
           "\\pdfbookmark[0]{Hidden ruling check}{hiddenruling}{\\Large\\bfseries Hidden ruling check\\par}\\vspace{4pt}",
           "{\\scriptsize\\itshape %s Not part of the record.\\par}\\vspace{4pt}" % E(METHOD),
           "{\\small\\noindent %s.\\par\\noindent Against the sub-clusters of %s (%s)%s.\\par}\\vspace{2pt}" % (
               E(facts_line(p)), E(p["slice_label"]), E(p["slice_is"]),
               E("; it is itself a member of %s:%s" % (p["slice_label"], C.sub_label(p["node_in"]))) if p.get("node_in") else ""),
           "{\\scriptsize\\noindent %s\\par}\\vspace{4pt}" % E(settings_line(p["settings"])),
           "\\noindent{\\small\\begin{tabular}{@{}l p{0.5\\linewidth} r r r@{}}\\textbf{Sub-cluster} & & \\textbf{Coupling} & "
           "\\textbf{Members} & \\textbf{Score}\\\\"]
    for x in p["subs"]:
        out.append("%s & %s & %.3f & %d & %.4f\\\\" % (E(x["code"]), E(x["name"] or ""), x["coupling"], x["members"], x["score"]))
    out.append("\\end{tabular}\\par}\\vspace{6pt}")
    if p.get("usage"):
        out.append("{\\small\\noindent\\textbf{How it uses them:} %s\\par}\\vspace{3pt}" % E(p["usage"]))
        for u in p.get("uses", []):
            how = ", ".join(t for t in (u["signal"], "alone in its sentence" if u["alone"] else "in a string cite") if t)
            if u.get("via"):
                how += "; passes on the words of %s (%s)" % (u["via"]["name"], u["via"]["kind"])
            out.append("{\\leftskip=1.2em\\footnotesize\\noindent\\textbf{%s} {\\scriptsize(%s; %s)}: %s\\par}\\vspace{3pt}" % (
                E(u["name"]), E(u["sub"]), E(how), E(u["sentence"])))
    return "\n".join(out) + "\n"


def settings_line(P, src=None):
    c = ", ".join("%s %g" % (k, v) for k, v in P["court"].items())
    st = ", ".join("%s %g" % (k, v) for k, v in P["status"].items())
    t = ("score = coupling x obscurity x court x status x reviewed x record;  obscurity = (1 - rank in its year and court level)^%g;  "
         "court: %s;  status: %s;  reviewed %g (within %d years);  no citation on file %g;  min_members %d;  settle_years %d") % (
        float(P["power"]), c, st, float(P["reviewed"]), int(P["review_years"]), float(P["no_cite"]), int(P["min_members"]),
        int(P["settle_years"]))
    if P.get("max_inbound") is not None:
        t += ";  max_inbound %d" % int(P["max_inbound"])
    if src:
        ch = sorted(k for k, v in src.items() if v != "default")
        if ch:
            t += "   [changed: %s]" % ", ".join("%s (%s)" % (k, src[k]) for k in ch)
    return t


def print_table(res, src=None):
    print("%s  %s  (%d members, view %s)" % (res["code"], res["name"] or "", res["members"], res["view"]))
    print(settings_line(res["settings"], src))
    print("decisions: %s, citing >= %d members  -- %s (\"after\" rows are measured against this %s sub-cluster)" % (
        format(res["candidates"], ","), int(res["settings"]["min_members"]), res["window"], res["slice_label"]))
    print()
    print("Nearest sub-clusters  (binary co-citation from %s decisions of %s; weight = pairs / sqrt(citations x citations))" % (
        format(res["neighbour_decisions"], ","), res["slice_label"]))
    for x in res["neighbours"]:
        print("  %-12s %.4f  %5.1f%% of pairs  %s" % (x["code"], x["weight"], 100 * x["share"], x["name"] or ""))
    print()
    print("Overlooked  (filed before %d: had time to be cited; ranked by the full score)" % res["settled_before"])
    print_rows(res["results"])
    print()
    print("Not yet visible  (filed %d or later: too new for co-citation to see; coupling x court x status, no obscurity; "
          "%s such decisions)" % (res["settled_before"], format(res["young_candidates"], ",")))
    print_rows(res["new"])
    print()
    q = res.get("quoted")
    if q is None:
        print("Quoted sources: no citation positions (tribonian cocite positions, then --index)")
    else:
        print("Quoted sources  (words reaching %s's decisions through its members: \"member (quoting X)\"; members left out; "
              "share = of the decisions citing X in the window, those that do it this way)" % res["code"])
        for n, x in enumerate(q, 1):
            print("%3d. %s, %s (%s %s)%s" % (n, x["name"][:70], x["cite"] or "cluster %d" % x["cluster"], x["court"], x["year"],
                                            "  [node of %s]" % x["in_sub"] if x["in_sub"] else "  [in no sub-cluster]"))
            print("     carried into %d decisions (from %d), %s of its %d citations in the window; %s" % (
                x["decisions"], x["first_year"], pct(x["share"]), x["cited_in_window"],
                ", ".join("%s %d" % kv for kv in x["kinds"].items())))
            print("     through: %s" % "; ".join("%s x%d" % (t["name"][:40], t["n"]) for t in x["through"]))
            if x["example"].get("sentence"):
                print("       %s: \"%s\"" % (x["example"]["name"][:40], x["example"]["sentence"][:240]))
    print()
    print("(%s s)" % res.get("seconds"))


def print_rows(rows):
    for r in rows:
        nxt = r["next"][0] if r["next"] else None
        print("%3d. %s, %s (%s %s)  [%s]" % (r["rank"], r["name"][:70], r["cite"] or "cluster %d" % r["cluster"],
                                          r["court"], r["year"], r["when"]))
        parts = ["coupling %.3f" % r["coupling"]]
        if r["obscurity"] is not None:
            parts.append("obscurity %.3f" % r["obscurity"])
        parts += ["court %g (%s)" % (r["court_weight"], r["level"]), "status %g (%s)" % (r["status_weight"], r["status"])]
        if r.get("reviewed_by"):
            parts.append("reviewed %g" % r["reviewed_weight"])
        if r["record_weight"] is not None and not r["has_cite"]:
            parts.append("record %g (no citation on file)" % r["record_weight"])
        print("     score %.4f = %s" % (r["score"], " x ".join(parts)))
        print("     %s%s%s; %d members%s" % (cohort_text(r), "" if r["has_cite"] else " (?)", reviewed_text(r), r["members_cited"],
              ("; also held as cluster %s" % ", ".join(map(str, r["duplicates"]))) if r.get("duplicates") else ""))
        if r.get("notes"):
            print("     note: %s" % "; ".join(r["notes"]))
        if nxt:
            print("     next: %s %.3f%s%s" % (nxt["code"], nxt["coupling"], ("  " + nxt["name"]) if nxt["name"] else "",
                                          "" if r["fits_best_here"] else "   ** fits better there"))
        if r.get("usage"):
            print("     uses: %s" % r["usage"])
            u = best_use(r.get("uses", []))
            if u:
                print("       %s: \"%s\"" % (u["name"][:40], u["sentence"][:220]))


def print_profile(p):
    print(facts_line(p))
    print("against %s (%s)%s" % (p["slice_label"], p["slice_is"],
                                 ("; itself a member of %s:%s" % (p["slice_label"], C.sub_label(p["node_in"]))) if p["node_in"] else ""))
    print(settings_line(p["settings"]))
    for x in p["subs"]:
        print("  %-12s coupling %.3f (%d members)  score %.4f  %s" % (x["code"], x["coupling"], x["members"], x["score"], x["name"] or ""))
    if p.get("usage"):
        print("uses: %s" % p["usage"])
        for u in p.get("uses", [])[:12]:
            print("  [%s] %s%s%s: \"%s\"" % (u["sub"], u["name"], (" (%s)" % u["signal"]) if u["signal"] else "",
                                           (" [passes on %s, %s]" % (u["via"]["name"], u["via"]["kind"])) if u.get("via") else "",
                                           u["sentence"][:200]))


def out_path(res):
    d = tconfig.WORK / "build" / "cocite"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("hidden-%s-%s.json" % (res["view"], res["code"].replace(":", "_")))


def cmd_hidden(args):
    from tribonian import cocite_subdoc as SD
    P, src = settings(args.set)
    if args.top:
        P["top"] = args.top
    if args.no_usage:
        P["usage"] = 0
    if args.no_quoted:
        P["quoted_top"] = 0
    db, view = SD.open_db(args)
    if args.cite or args.cluster:
        ref = args.cluster or C.resolve(args.cite)
        p = profile(ref, db, view, P)
        if p is None:
            sys.exit("no view or citation cache")
        print(json.dumps(p, indent=1) if args.json else "", end="")
        if not args.json:
            print_profile(p)
        return
    if not args.which:
        sys.exit("name a sub-cluster (e.g. 2020-24:2.3) or a case (--cite)")
    s, j = SD.resolve(db, args.which)
    res = hidden(db, view, s, j, P)
    path = out_path(res)
    path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        print_table(res, src)
        print("-> %s" % path)


def add_parser(sub, view):
    p = sub.add_parser("hidden", parents=[view], help="hidden rulings: decisions leaning on a sub-cluster's cases "
                                                      "that few courts cite (cocite_hidden.py)")
    p.add_argument("which", nargs="?", help="slice:sub-cluster, e.g. 2020-24:2.3")
    p.add_argument("--cite", help="one decision instead: its profile against every sub-cluster")
    p.add_argument("--cluster", type=int)
    p.add_argument("--view", help="results file stem after cocite-, e.g. appellate-sentence-m0.2 (default, the "
                                  "case-note view)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override a weight: power=0.5, court.scotus=5, settle_years=5, later=false, "
                        "min_members=3, max_inbound=10 (defaults, then config [hidden], then these)")
    p.add_argument("--top", type=int, help="rows listed (setting top)")
    p.add_argument("--no-usage", action="store_true", help="skip the citing sentences (no text pass)")
    p.add_argument("--no-quoted", action="store_true", help="skip the Quoted sources list")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(func=cmd_hidden)
