"""Print rule-history evidence (from rule_evidence.py) compactly, one lineage at a time.

usage: python show_rule_evidence.py EVIDENCE.jsonl [EVIDENCE2.jsonl ...] -- KEY_OR_NAME_PATTERN [...]
       python show_rule_evidence.py EVIDENCE.jsonl ... --keys-file KEYS.txt --index N   (the Nth key, 1-based)
"""
import json, sys

argv = sys.argv[1:]
files, pats = [], []
if "--" in argv:
    i = argv.index("--")
    files, pats = argv[:i], argv[i + 1:]
elif "--keys-file" in argv:
    files = argv[:argv.index("--keys-file")]
    keys = [k.strip() for k in open(argv[argv.index("--keys-file") + 1]) if k.strip()]
    pats = [keys[int(argv[argv.index("--index") + 1]) - 1]]
else:
    files = [argv[0]]
    pats = argv[1:]


def c(x):
    """'Name, cite (year)' -> 'Name year'."""
    return x.split(",")[0][:60] + " " + x.rsplit("(", 1)[-1].rstrip(")")


for f in files:
    for line in open(f, encoding="utf-8"):
        r = json.loads(line)
        if not any(p == r["key"] or (len(p) > 12 and p.lower() in r["current"]["name"].lower()) for p in pats):
            continue
        print("=" * 90)
        print("KEY %s  level %s  lineage %s  view %s  span %s" % (r["key"], r["level"], r["lineage"], r["view"], r["span"]))
        if r.get("parent_cluster"):
            print("PARENT CLUSTER:", r["parent_cluster"].get("label"))
        print("CURRENT NAME:", r["current"]["name"])
        print("CURRENT DESC:", r["current"]["description"])
        print("HEAVIEST:", "; ".join(r["heaviest"][:12]))
        print("TIMELINE (slice: size, anchor | leading | +entered | -left | merges/splits | newly decided):")
        for t in r["timeline"]:
            print(" %s size %s anchor %s%s" % (t["unit"], t["size"], c(t["anchor"]), "  (gap before)" if t.get("gap_before") else ""))
            print("    lead: " + "; ".join(c(x) for x in t["leading"][:6]))
            if t.get("entered"):
                print("    +in : " + "; ".join(c(x) for x in t["entered"][:6]))
            if t.get("left"):
                print("    -out: " + "; ".join(c(x) for x in t["left"][:4]))
            for k in ("merged_in_from", "split_off_to"):
                if t.get(k):
                    print("    %s: %s" % (k, "; ".join("%s %s" % (x["unit"], x["label"] or "(unlabelled)") for x in t[k])))
        print("HOLDINGS (parentheticals describing each case, best first):")
        for case, ps in r["holdings"].items():
            print("  %s | %s" % (c(case), " || ".join(p[:230] for p in ps[:2])))
        print("CHANGE PARENTHETICALS (wording suggesting a rule made, changed or reread):")
        for case, ps in r["change_parentheticals"].items():
            print("  %s | %s" % (c(case), " || ".join(p[:230] for p in ps[:2])))
