"""cocite_hidden.py -- hidden rulings: decisions that lean on a sub-cluster's cases but that few later courts cite.

A sub-cluster's members are there because later decisions cite them together. A hidden decision is
the other direction: it CITES the members heavily (bibliographic coupling to the sub-cluster), but is
itself cited little -- the decisions that applied a body of law and that nobody picked up. One from a
court of appeals or the Supreme Court that nobody cites is the interesting kind, so the court counts.

    score = coupling x obscurity x court x status x record

  coupling   how much of the decision's reference list sits in the sub-cluster, rare references
             weighted up (idf = log(N / decisions citing it), as tribonian map coupling does):
                 sum of idf over the members it cites
                 / sqrt(sum of idf over everything it cites x sum of idf over all members)
             (a weighted Ochiai / cosine). The square root of the decision's own reference weight is
             what keeps string-cite compilations from topping the list: citing 300 cases hits many
             members by volume alone, and divides by a large number.
  obscurity  1 / (1 + inbound) ^ power, inbound = decisions citing it, per year since it was filed
             (per_year = true: a 2023 decision has had less time to be cited than a 1995 one)
  court      a weight per court level of the decision (court.scotus, court.circuit, ...)
  status     a weight per precedential status (status.Published, status.Unpublished, ...): an
             unpublished ruling nobody cites is less of a find, since many courts bar citing it
  record     no_cite when the decision has no reporter citation on file in CourtListener, else 1.
             Later opinions cite a case by its reporter citation; with none on file, CourtListener
             can't link them to it, so "cited by 0" there mostly means "links missing", not "nobody
             cites it" (checked 2026-11: every Washington Supreme Court decision of 2017-10-12 had no
             citation on file and 0 citing decisions)

Every weight is a setting: defaults below (DEFAULTS), then the config file's [hidden] table, then
--set key=value on the command line (--set court.scotus=5 --set power=0.5). The settings used are
printed and stored with every result, so two runs can be compared.

Which decisions: those citing at least min_members of the sub-cluster's members, filed in the
sub-cluster's slice or (later = true) any time after it. Every row says which ("in slice" / "after"),
since a later decision is measured against a sub-cluster of an earlier slice. Members themselves are
never listed.

Nearest sub-clusters (of the sub-cluster): from the slice's citing decisions in the view's courts,
W(S, T) = decisions' co-citations between S's members and T's (sum over decisions of the members of S
times the members of T they cite: the binary pair support summed over S x T), shown as
W / sqrt(A_S x A_T), A = the sub-cluster's citations in the slice (sum of nodes.n). Binary counts, not the
view's sentence weights.

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
    "power": 1.0,              # obscurity = 1 / (1 + inbound) ^ power
    "per_year": True,          # inbound counted per year since filing
    "later": True,             # also decisions filed after the slice
    "max_inbound": None,       # leave out decisions cited more than this (raw count); None = no cap
    "top": 30,                 # rows listed
    "neighbours": 5,           # nearest sub-clusters listed
    "usage": 30,               # decisions whose citing sentences are read (text pass; the slow part)
    "min_coupling": 0.05,      # a decision's other sub-clusters at or above this coupling are listed
    "court": {"scotus": 3.0, "circuit": 2.0, "state-supreme": 1.5, "state-appellate": 1.0,
              "district": 0.7, "other": 0.3},
    "status": {"Published": 1.0, "Unpublished": 0.5, "Unknown": 0.8, "other": 0.3},
    "no_cite": 0.25,           # record weight when no reporter citation is on file (inbound count unreliable)
}
PROFILE_VERSION = 2


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
    """The exact inbound count (citers of the decision and its siblings, once each), for one decision of the
    preliminary ranking. It is never below the cheap count, so (power >= 0) it can only lower the score."""
    if d in cache_:
        return cache_[d]
    ids, reps = siblings(con, d)
    D, _ = citers_of(np.array(ids))
    inbound = int(len(np.setdiff1d(np.unique(D), ids)))
    cache_[d] = (max(inbound, int(df_of([d])[0])), [x for x in ids if x != d])
    return cache_[d]


def obscurity(decs, P, data_year, inbound=None):
    inbound = df_of(decs) if inbound is None else np.asarray(inbound)
    yr = lookup(cache()["year"], None, decs).astype(int)
    rate = inbound / np.maximum(1, data_year - yr + 1) if P["per_year"] else inbound.astype(float)
    return inbound, yr, (1.0 + rate) ** (-float(P["power"])), rate


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
    inbound, cyr, obs, rate = obscurity(cand, P, res["data_year"])
    lvl, cw = court_weight(cand, P)
    con = con or C.main_db()
    stn, sw = status_weight(cand, P, con)
    hasc, rw = record_weight(cand, P, con)
    rnotes = apply_reporters(cand, con, P, lvl, cw, stn, sw)
    score = cS * obs * cw * sw * rw
    # Preliminary scores use the cheap inbound count; refine() makes it exact for a pool of the best, and can only
    # lower a score, so the pool grows until no decision left outside it could still beat the N-th: the list is exact.
    sibs, unpub_by, ref_cache = {}, {}, {}
    N = int(P["top"])
    order = np.argsort(-score, kind="stable")
    pool = min(len(order), max(4 * N, 50))
    while True:
        for i in order[:pool].tolist():
            if i in sibs:
                continue
            n_in, sib = refine(i, int(cand[i]), con, P, res["data_year"], ref_cache)
            sibs[i] = sib
            inbound[i] = n_in
            _, _, ob, rt = obscurity(cand[i:i + 1], P, res["data_year"], [n_in])
            obs[i], rate[i] = ob[0], rt[0]
            score[i] = cS[i] * obs[i] * cw[i] * sw[i] * rw[i]
        if P.get("max_inbound") is not None:
            score[inbound > int(P["max_inbound"])] = -1
        top_in = np.sort(score[order[:pool]])[::-1]
        nth = top_in[min(N, len(top_in)) - 1] if len(top_in) else 0
        if pool >= len(order) or score[order[pool]] <= nth:
            break
        pool = min(len(order), pool * 2)
    res["refined"] = int(pool)
    best = order[:pool][np.argsort(-score[order[:pool]], kind="stable")][:N]
    best = best[score[best] > 0]
    group = {}                                            # one decision held as several clusters: one row
    for i in best.tolist():
        group.setdefault(min([int(cand[i])] + sibs.get(i, [])), []).append(i)
    keep = set(v[0] for v in group.values())
    dup_of = {v[0]: [int(cand[x]) for x in v[1:]] for v in group.values()}
    best = np.array([i for i in best.tolist() if i in keep], np.int64)
    others = {}                                           # decision index -> [(sub, coupling, members)]
    sel = np.isin(o, best)
    for a, b, c, n in zip(o[sel].tolist(), k[sel].tolist(), cpl[sel].tolist(), mc[sel].tolist()):
        others.setdefault(a, []).append((b, c, n))
    names, seen = {}, {}
    rank = 0
    for i in best.tolist():
        d = int(cand[i])
        nm, cite, y, court = C.case_label(con, d)
        dup = (nm.lower(), court, int(cyr[i]), round(float(score[i]), 6))   # CourtListener holds some decisions twice
        if dup in seen:
            seen[dup].setdefault("duplicates", []).append(d)
            continue
        rank += 1
        extra = dup_of.get(i, []) + [x for x in sibs.get(i, []) if x not in dup_of.get(i, [])]
        subs = sorted(others.get(i, []), key=lambda x: -x[1])
        best_sub = subs[0][0] if subs else j
        alt = [x for x in subs if x[0] != j]
        res["results"].append({
            "rank": rank, "cluster": d, "name": nm, "cite": cite, "year": int(cyr[i]) or y, "court": court,
            "level": lvl[i], "when": "in slice" if cyr[i] <= s + C.WIDTH - 1 else "after",
            "score": round(float(score[i]), 5), "coupling": round(float(cS[i]), 4), "obscurity": round(float(obs[i]), 4),
            "court_weight": float(cw[i]), "status": stn[i], "status_weight": float(sw[i]),
            "has_cite": bool(hasc[i]), "record_weight": float(rw[i]), "inbound": int(inbound[i]), "inbound_per_year": round(float(rate[i]), 3),
            "members_cited": int(nS[i]), "fits_best_here": best_sub == j,
            "duplicates": extra, "notes": rnotes.get(i, []),
            "next": [{"sub": b, "code": "%s:%s" % (C.label(s), C.sub_label(b)), "coupling": round(c, 4), "members": n,
                      "name": names.setdefault(b, C.unit_name(db, s, b, "sub")[0])}
                     for b, c, n in alt if c >= float(P["min_coupling"])][:5]
                    or [{"sub": b, "code": "%s:%s" % (C.label(s), C.sub_label(b)), "coupling": round(c, 4), "members": n,
                         "name": names.setdefault(b, C.unit_name(db, s, b, "sub")[0])} for b, c, n in alt[:1]]})
        seen[dup] = res["results"][-1]
    if int(P["usage"]):
        mset = set(members.tolist())
        add_usage(res["results"][:int(P["usage"])], mset, con)
    res["seconds"] = round(time.time() - t0, 1)
    return res


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
SIG_AFTER = re.compile(r"^[^()]{0,120}?\((quoting|citing|cited in)\b", re.I)     # A (quoting B): A's use
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
            sig = SIG_INSIDE.search(bef) or SIG_START.match(clause)
            sig2 = None if sig else SIG_AFTER.match(aft)
            if m not in lab:
                lab[m] = C.case_label(con, m)[0]
            sent = re.sub(r"\s+", " ", (bef + aft)).strip()[:500]
            if lab[m] in seen:                     # one case held twice in CourtListener: one use
                continue
            seen.add(lab[m])
            uses.append({"cluster": m, "name": lab[m], "alone": int(sg_size.get(sgs[i], 1)) == 1,
                         "signal": norm_signal(sig.group(1) if sig else (sig2.group(1) if sig2 else "")),
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
    inbound, cyr, obs, rate = obscurity(np.array([ref]), P, data_year_of(db))
    lvl, cw = court_weight(np.array([ref]), P)
    con = con or C.main_db()
    stn, sw = status_weight([ref], P, con)
    hasc, rw = record_weight([ref], P, con)
    n_in, sib = refine(0, int(ref), con, P, data_year_of(db), {})
    inbound, cyr, obs, rate = obscurity(np.array([ref]), P, data_year_of(db), [n_in])
    rnotes = apply_reporters([ref], con, P, lvl, cw, stn, sw)
    mult = float(obs[0] * cw[0] * sw[0] * rw[0])
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
           "inbound": int(inbound[0]), "inbound_per_year": round(float(rate[0]), 3), "obscurity": round(float(obs[0]), 4),
           "level": lvl[0], "court_weight": float(cw[0]), "status": stn[0], "status_weight": float(sw[0]),
           "has_cite": bool(hasc[0]), "record_weight": float(rw[0]), "also_held_as": sib,
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

def facts_line(p):
    return "Filed %s; %s court (x%g); %s (x%g)%s%s; cited by %d decision%s (%.2f a year) -> obscurity %.3f" % (
        p["year"], p["level"], p["court_weight"], p["status"], p["status_weight"],
        "" if p["has_cite"] else "; no reporter citation on file, so the count is unreliable (x%g)" % p["record_weight"],
        "".join("; %s" % n for n in p.get("notes", [])) + ("; also held as cluster %s" % ", ".join(map(str, p["also_held_as"]))
                                                           if p.get("also_held_as") else ""),
        p["inbound"], "" if p["inbound"] == 1 else "s", p["inbound_per_year"], p["obscurity"])


METHOD = ("Whether this decision is a hidden ruling: how much it leans on each co-citation sub-cluster's cases "
          "(coupling: the share of its references, rare ones weighted up, that are the sub-cluster's members), against how "
          "little later decisions cite it. Score = coupling x obscurity x court x status x record; every weight is a setting "
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
            L.append("- **%s** (%s; %s): %s" % (esc(u["name"]), u["sub"], how, esc(u["sentence"])))
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
            out.append("{\\leftskip=1.2em\\footnotesize\\noindent\\textbf{%s} {\\scriptsize(%s; %s)}: %s\\par}\\vspace{3pt}" % (
                E(u["name"]), E(u["sub"]), E(how), E(u["sentence"])))
    return "\n".join(out) + "\n"


def settings_line(P, src=None):
    c = ", ".join("%s %g" % (k, v) for k, v in P["court"].items())
    st = ", ".join("%s %g" % (k, v) for k, v in P["status"].items())
    t = ("score = coupling x obscurity x court x status x record;  obscurity = 1/(1+inbound%s)^%g;  court: %s;  status: %s;  "
         "no citation on file %g;  min_members %d") % (
        " per year" if P["per_year"] else "", float(P["power"]), c, st, float(P["no_cite"]), int(P["min_members"]))
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
    print("Hidden decisions")
    for r in res["results"]:
        nxt = r["next"][0] if r["next"] else None
        print("%3d. %s, %s (%s %s)  [%s]" % (r["rank"], r["name"][:70], r["cite"] or "cluster %d" % r["cluster"],
                                          r["court"], r["year"], r["when"]))
        print("     score %.4f = coupling %.3f x obscurity %.3f x court %g (%s) x status %g (%s)%s   cited by %d (%.2f/yr)%s, %d members%s" % (
            r["score"], r["coupling"], r["obscurity"], r["court_weight"], r["level"], r["status_weight"], r["status"],
            "" if r["has_cite"] else " x record %g (no citation on file)" % r["record_weight"],
            r["inbound"], r["inbound_per_year"], "" if r["has_cite"] else "?", r["members_cited"],
            ("   (also held as cluster %s)" % ", ".join(map(str, r["duplicates"]))) if r.get("duplicates") else ""))
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
    print()
    print("(%s s)" % res.get("seconds"))


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
            print("  [%s] %s%s: \"%s\"" % (u["sub"], u["name"], (" (%s)" % u["signal"]) if u["signal"] else "",
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
                   help="override a weight: power=0.5, court.scotus=5, per_year=false, later=false, "
                        "min_members=3, max_inbound=10 (defaults, then config [hidden], then these)")
    p.add_argument("--top", type=int, help="rows listed (setting top)")
    p.add_argument("--no-usage", action="store_true", help="skip the citing sentences (no text pass)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(func=cmd_hidden)
