"""Groups of close labels for the differentiation pass, from a merged import file and one view.

A label's lineage: evidence_lineage for round-2 labels, else the view's label_map. Pairs of labels attached in the
view are close when they are sibling sub-clusters under one parent cluster (with some overlap), share fingerprint
cases (Jaccard >= 0.15), or share name words (>= 0.5). Connected groups larger than CAP are split by community
detection on the pair strengths; dropped singletons rejoin their strongest neighbour's group when it has room.

usage: python diff_groups.py WORK_DIR IMPORT.jsonl VIEW OUT.json [--cap 8]
"""
import collections, itertools, json, re, sqlite3, sys
from pathlib import Path

import igraph as ig

work, imp, view, out = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
CAP = int(sys.argv[sys.argv.index("--cap") + 1]) if "--cap" in sys.argv else 8
db = sqlite3.connect("file:%s?mode=ro" % (work / "db" / ("cocite-%s.sqlite" % view)), uri=True)
labs = {}
for line in imp.open(encoding="utf-8"):
    x = json.loads(line)
    if x["level"] != "area":
        labs[x["key"]] = x
lmap = {k: l for l, k in db.execute("SELECT lineage, key FROM label_map")}
lin = {}
for k, x in labs.items():
    if x.get("round") == 2 and x.get("evidence_lineage"):
        lin[k] = x["evidence_lineage"]
    elif x.get("round2_kind") == "drifted-auto-kept":
        lin[k] = x["evidence_lineage"]
    elif k in lmap:
        lin[k] = lmap[k]
level = dict(db.execute("SELECT lineage, level FROM lineages"))
lin = {k: l for k, l in lin.items() if l in level}
votes = {}
for sl, pl, n in db.execute("SELECT s.lineage, c.lineage, count(*) FROM subs s JOIN clusters c "
                            "ON c.slice = s.slice AND c.cl = s.sub / 1000 WHERE s.sub % 1000 > 0 GROUP BY 1, 2"):
    if n > votes.get(sl, (None, 0))[1]:
        votes[sl] = (pl, n)
parent = {k: votes.get(l, (None,))[0] for k, l in lin.items()}
STOP = {"appeals", "review", "state", "federal", "court", "courts", "cases", "civil", "criminal", "after", "under", "and",
        "the", "of", "for", "from", "with", "rule", "law"}


def toks(s):
    return {w for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in STOP}


keys = sorted(lin)
fp = {k: set(labs[k]["fingerprint"]) for k in keys}
nt = {k: toks(labs[k]["name"]) for k in keys}
pairs = []
for a, b in itertools.combinations(keys, 2):
    if labs[a]["level"] != labs[b]["level"] and not (parent.get(b) == lin[a] or parent.get(a) == lin[b]):
        continue
    j = len(fp[a] & fp[b]) / max(1, len(fp[a] | fp[b]))
    nj = len(nt[a] & nt[b]) / max(1, len(nt[a] | nt[b]))
    sib = parent.get(a) is not None and parent.get(a) == parent.get(b)
    fam = parent.get(b) == lin[a] or parent.get(a) == lin[b]          # a cluster and its own sub-cluster
    if j >= 0.15 or nj >= 0.5 or (sib and (j >= 0.05 or nj >= 0.25)) or (fam and (j >= 0.3 or nj >= 0.4)):
        pairs.append((a, b, round(j, 2), round(nj, 2), bool(sib)))
ix = {k: i for i, k in enumerate(keys)}
G = ig.Graph(n=len(keys), edges=[(ix[a], ix[b]) for a, b, *_ in pairs])
G.es["weight"] = [max(p[2], 0.8 * p[3]) + (0.15 if p[4] else 0) for p in pairs]
groups = []


def split(vs, depth=0):
    if len(vs) <= CAP or depth > 6:
        if len(vs) > 1:
            groups.append(vs)
        return
    parts = [[vs[i] for i in c] for c in G.subgraph(vs).community_multilevel(weights="weight", resolution=1.0 + 0.7 * depth)]
    if len(parts) == 1:
        groups.append(vs)
        return
    for p in parts:
        split(p, depth + 1)


for comp in G.connected_components():
    if len(comp) > 1:
        split(list(comp))
placed = {v for g in groups for v in g}
for v in range(len(keys)):
    if v in placed or not G.degree(v):
        continue
    for w_, (a, b) in sorted(((G.es[e]["weight"], G.es[e].tuple) for e in G.incident(v)), reverse=True):
        o = b if a == v else a
        g = next((g for g in groups if o in g), None)
        if g is not None and len(g) < CAP + 2:
            g.append(v)
            placed.add(v)
            break
res = []
for i, g in enumerate(sorted(groups, key=len, reverse=True), 1):
    ks = [keys[v] for v in g]
    res.append({"group": i, "keys": ks, "pairs": [p for p in pairs if p[0] in ks and p[1] in ks]})
out.write_text(json.dumps(res, indent=1), encoding="utf-8")
cut = sum(1 for a, b, *_ in pairs if not any(a in g["keys"] and b in g["keys"] for g in res))
print("%d labels attached in %s; %d close pairs; %d groups holding %d labels %s; %d pairs fall between groups" % (
    len(keys), view, len(pairs), len(res), sum(len(g["keys"]) for g in res),
    sorted(collections.Counter(len(g["keys"]) for g in res).items()), cut))
