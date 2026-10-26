# Validation

What has been measured, how, and what has not. The good-law check and the co-citation map are reading aids, not
a citator and not a source of doctrine. This file is meant to make it clear which parts are measured, which are
heuristics, and which are untested. Numbers come from the runs cited; the commands to reproduce them are given.

## Summary

| Claim | Evidence | Limitation |
|---|---|---|
| The good-law check flags overruled cases | Red within the overruling window for 35 of 40 overruled Supreme Court cases (37 of 40 red now); no control red (§1.2–1.3) | Supreme Court cases only, all overruled loudly; set B was a soft check, not a clean holdout |
| The quick check (newest 100) costs little accuracy | 34 of 40 red, 2 false greens vs 1 for the full check (§1.3) | Status now only, not the year it changed; same favourable test set |
| Co-citation areas recover known doctrinal groupings | F1 0.990 (sentence view) vs 0.913 (binary) on 32 shared benchmark names; switch decided by a rule fixed before the run (§2.3) | 16 hand groupings; the shared names are the stable ones; grades about 1 lineage in 8 |
| Areas are not artefacts of one run | Consensus over 20 runs; seed ranges for the two views don't overlap (§2.2) | No resolution plateau: the area level is a chosen cut. Not yet compared across bulk snapshots |
| Co-citation can hint at overruling | Successor fires at 37 of 41 overrulings… and at 11 of 20 controls (§2.5) | Demoted to a flag; never sets a colour |
| The US Code renumbering map is sound | Moved sections keep their citation partners (median cosine 0.21 vs 0.00 random), on held-out titles (§3) | The detector for missing moves is a review list only |
| Results can be reproduced | Rule versions, reading windows and bulk-load stamps are recorded (§4) | No clean-directory rebuild from a fixed snapshot yet |

Contents: [Summary](#summary) · [1. Good-law check](#1-good-law-check) · [2. Co-citation map](#2-co-citation-map) ·
[3. US Code renumbering](#3-us-code-renumbering) · [4. Provenance and reproducibility](#4-provenance-and-reproducibility) ·
[5. Not yet measured](#5-not-yet-measured)

---

## 1. Good-law check

**What it is.** No language model. It uses rules over the words later courts write about a case, next to its
citation or in sentences that name it (`casemap.classify`, rules R9, `GOODLAW_VERSION` 5). The output is red,
yellow or green, plus the evidence: year, court, the word that matched, and the snippet. See the README for the
words that count and the reading set.

**Exclusions built into the rules.** These are the known ways to get a false red:
- hypotheticals ("unless Roe is overruled")
- a party's argument ("defendant argues that Apodaca has been overruled")
- the case doing the overruling ("Brown … (overruling Plessy)")
- negated or quoted softer words
- another case with the same name (`other_case`: "Aguilar v. Texas" is not "Aguilar v. Felton")

"On other grounds" is yellow, not red, because the case still stands for other propositions.

### 1.1 Test set (`goodlaw_eval.py`)
- 40 Supreme Court cases whose overruling year is known, in two sets of 20 (A and B), plus 20 controls that were
  never overruled.
- A **hit** is red from one year before the overruling to five years after it.
- Rules are scored against cached reading windows, so a ruleset can be compared without re-reading text.

**How independent it is:**
- Set A tuned the rules.
- Set B was consulted **once** during tuning (the same-name rule came from it), so it is a soft check, **not a
  clean holdout**.
- The controls were watched throughout.

### 1.2 Results (full check, R9; reproduced after the 2026-10-04 audit)

| | Overruled: hit | Controls: green / yellow / red |
|---|---|---|
| Set A (tuning) | 17 of 20 | 8 / 2 / 0 |
| Set B (soft check) | 18 of 20 | 6 / 4 / 0 |
| First ruleset, for comparison | 13 and 16 of 20 | 3 controls red |

Reported separately:
- **False negatives (the dangerous error):** overruled cases left green. Example: Saucier v. Katz. Pearson v.
  Callahan made its two-step order optional without using the word "overruled", so no rule can see it.
- **False positives:** no control was red. Some controls are yellow, from softer words ("questioned", "limited to
  its facts") that are true of them.

```
tribonian goodlaw-eval collect     # read each test case's citing opinions once (slow)
tribonian goodlaw-eval score       # score every ruleset on set A and set B (fast, from the cache)
tribonian goodlaw-eval show "410 U.S. 113" R9   # one case under one ruleset, with its evidence
```

### 1.3 Quick check (newest N citing opinions only)

The quick check was chosen from a measured curve, not set by hand. It was measured on the same 60 cases with the
same rules, varying only the reading set (2026-10-07).

| Reading set | Overruled: red / yellow / green | Controls: green / yellow / red |
|---|---|---|
| full | 37 / 2 / 1 | 14 / 6 / 0 |
| newest 25 | 23 / 5 / 12 | 20 / 0 / 0 |
| newest 50 | 31 / 3 / 6 | 20 / 0 / 0 |
| **newest 100 (default)** | **34 / 4 / 2** | **19 / 1 / 0** |
| newest 200 | 36 / 2 / 2 | 19 / 1 / 0 |
| newest 500 | 37 / 2 / 1 | 18 / 2 / 0 |

- **Why 100:** it is the knee of the curve. At 50, four more cases go false green (Olmstead, Union Gas, Michigan
  v. Jackson, Swain).
- **False greens at N = 100–300:** Saucier (also green in the full check) and Williamson County (yellow in the
  full check; overruled by Knick, 2019).
- **What it reports:** status now, never the year the status changed. Every result says so: "(quick)", with the
  window of years read.

### 1.4 Limits of this evidence
- **The test set is favourable.** All 40 overrulings are famous, heavily cited Supreme Court cases, overruled
  loudly by the Supreme Court. Circuit, state and district cases, and cases overruled by their own court, are
  **not measured**.
- **Statutory supersession** usually leaves no citation trail, so a citation-based check cannot see it.
- **The year the status changed:** the full check estimates it; the quick check does not. Partial abrogation is
  flagged only as "on other grounds" or softer words. The check does not say *which* proposition was affected.
  Read the evidence snippets.

---

## 2. Co-citation map

**The question it answers** is which cases later decisions cite together, and how that changes over time. That
is citation behaviour. Whether it matches doctrine is tested, not assumed (§2.3).

### 2.1 Construction choices aimed at known failure modes

| Failure mode | What the code does |
|---|---|
| One decision with several opinions counted several times | The unit is the citing **decision** (cluster), not the opinion |
| Long string citations creating ties | Each decision counts once per pair. The note view weights pairs by **proximity**: `--mix 0.2` = 0.2 × "both cited in the decision" + 0.8 × "cited in the same sentence" |
| Heavily cited hub cases dominating | Partners are ranked by cosine; lift is stored next to it |
| District courts flooding recent slices | The note view counts appellate citing courts only (the citing side) |
| Sub-clusters that are noise | A split is kept only if its modularity beats **every** degree-preserving rewired copy of the cluster, and their mean by `--sub-z` (4) standard deviations (§2.4) |

Why proximity weighting helps: a blind reading of 212 pairs by two independent readers found that pairs never
cited in the same sentence are mostly *incidental*. They are not boilerplate. Controls: 12 of 12 right; agreement
between readers: 87%.

### 2.2 Areas (lineages pooled into broad subjects)
- **Method:** CPM on association-strength weights, with shrinkage, then consensus clustering (Lancichinetti and
  Fortunato) over 20 runs: `--method cpm --resolution 0.5 --shrink 10 --consensus 20`. The final partition is
  the connected components of ties kept by at least τ = 0.5 of the runs, so it is deterministic.
- **Chosen by grid** (`areas --grid`) against the benchmark (§2.3).
- **Seed sensitivity:** the best cells, over seeds 0–4: sentence `--mix 0.2` ranged 0.881–0.926 F1. Binary
  ranged 0.791–0.849. The two ranges do not overlap.
- **Resolution sensitivity:** there is **no resolution plateau**. The structure is nested, so the area level is
  a **chosen cut**, not a natural one.
- **Methods rejected:**
  - Plain modularity produced one giant area of 2,699 lineages.
  - CPM without shrinkage over-weighted small lineages.
  - Using CPM in the later consensus rounds dropped coverage from 83% to 44%.

### 2.3 Benchmark: does the graph recover independently known groupings?
`data/cocite_gold.json`: 16 hand-made groupings of doctrines that belong together, scored by pairwise F1 over the
lineages they name.

**Switching the case notes to the sentence-weighted view** (2026-10-02) was decided by a rule fixed before the
run: switch only if sentence consensus, at binary's own setting, matched binary's precision (0.953) and beat its
recall (0.765). It passed: precision 1.000, recall 0.841, F1 0.914 on 65 names.

**Re-scored after the 2026-10-04 audit**, which flagged that the first figures were best-of-grid and graded only
benchmark lineages. Both views were scored on the 32 names that attach in every view (10 groups):

| View | F1 | Precision | Recall | On its own names |
|---|---|---|---|---|
| binary | 0.913 | 1.000 | 0.840 | 0.865 on 61 |
| sentence `--mix 0.2` (notes) | **0.990** | 1.000 | 0.980 | 0.937 on 54 |

Read these with three caveats (also in ARCHITECTURE §7.4):
1. **Intersecting selects the easy names.** The lineages that attach everywhere are the stable ones. 0.990 means
   near-perfect on those names, not general accuracy.
2. **The finer view leaves more units unlabelled.** The sentence view attaches fewer labels: 384 of 609
   lineages, against binary's 403. The cost is unlabelled units, not wrong ones.
3. **The benchmark grades a small fraction.** Areas that hold benchmark lineages hold 22 lineages at the median,
   and 87–89% of those are outside the benchmark. The benchmark grades about one lineage in eight inside the areas
   it touches.

**Known pairs, as spot checks:** `tribonian cocite together` on pairs with known history: Twombly/Iqbal, the
summary-judgment trilogy, and Chevron/State Farm/Loper Bright.

### 2.4 Stability
- **Over runs:** areas are consensus partitions (§2.2), so seed noise is absorbed and measured.
- **Sub-cluster splits:** the null test was tightened on 2026-10-04. A standard deviation from 5 null draws has
  about 35% relative error, so the max-of-nulls condition now carries the test. Views built before that date use
  the older test until rebuilt with `tribonian cocite build --force`.
- **Over time:** lineages link clusters in adjacent slices by mutual best Jaccard ≥ 0.2. Merges and splits are
  recorded, not hidden.
- **Over rebuilds:** each label stores a fingerprint (its 30 heaviest cases). After a rebuild, `labels check`
  reports each label as stable (Jaccard ≥ 0.7), drifted, displaced or orphaned.

### 2.5 Using co-citation as a good-law signal: tested and demoted
`cocite shift --eval` on the good-law test cases:
- The successor signal fires at the overruling for 37 of 41 cases and names the overruling decision for 35 of 41.
- It also fires for 11 of 20 cases that were never overruled (extension, not overruling).
- No signal leads an overruling by more than chance.

So the signal is shown only as a flag, the "likely successor" line, and never sets a colour.

### 2.6 Labels
A language model writes area and lineage names from script-built evidence: members, time spans, and the
parentheticals courts wrote. The clustering itself uses no model. Labels are shown as labels, never as a reason.

---

## 3. US Code renumbering

The map of old section numbers to new ones is built from the Code's own notes: transferred sections, "Based on"
notes, and revision tables. It was then checked against citation data.

**Known moves, checked with a held-out set of titles** (`usc-check --holdout 34,52,54`):
- 103 of the 3,367 dated moves have 20+ citing decisions on both sides.
- Partner cosine for those moves: median 0.21. For random targets: 0.00.
- The lowest scores are splits (one section into 30). These are correct edges; each new section inherits only
  part of the old one's partners.

**Detecting moves the map lacks:**
- Recall was 0 of 5 at cosine ≥ 0.5, and 5 of 5 at 0.2.
- At 0.2 it produced 622 candidates. Most are related provisions, not renumberings.

Decision: the map is trusted as built from the notes. The detector produces a review list and never adds edges.

---

## 4. Provenance and reproducibility

- **Good-law results** record their rule version (`GOODLAW_VERSION`; cached results are redone when it changes),
  the mode (full or quick), and what was read ("newest 100 of N, filed a–b").
- **Dormancy** ("grey") is measured against the bulk-data date, not the wall clock.
- **Co-citation views** record the bulk load they were built from (`meta.stamp`) and each slice's parameters
  (`slices.params`). Read paths check the cache against the bulk-load stamp, so an old view is not silently
  combined with a new dump.
- **`tribonian check`** probes each piece of the setup: is it there, and does it return a query result.
- **Tests:** `tests/` (smoke, configuration, health check, semantic search, US Code cited-by).
- **Not yet done:** a clean-directory rebuild from one fixed snapshot, with outputs compared. See §5.

---

## 5. Not yet measured

In rough order of consequence:

1. **Good-law check below the Supreme Court:** circuit, state and district cases, especially those overruled by
   their own court, or abrogated implicitly with no "overruled" wording.
2. **A clean good-law holdout:** set B is soft (§1.1). It needs a new, larger set, labelled before anyone looks
   at the rules' output.
3. **Which proposition was affected** by a partial overruling. The check flags it; it does not classify it.
4. **A benchmark large enough to claim doctrine:** 16 groupings grade about one lineage in eight in the areas
   they touch (§2.3). This is enough to choose between methods, not to claim that the clusters are doctrine.
5. **Stability across bulk snapshots:** label fingerprints are built for this, but no two CourtListener dumps
   have been compared yet.
6. **Draft-to-cases search** (Sub-Cluster Search): planned as a leave-one-out test on real briefs. Remove a cited
   case, measure whether it comes back in the top 10 or 25, and compare with simply ranking the most-cited cases.
   Not yet run.
7. **Reproducing outputs from a clean directory** for one fixed snapshot (§4).

---

## Where the claims above came from

| Claim | Source in the repo |
|---|---|
| Good-law rules, exclusions, test set | `casemap.py` (`RULES`, `classify`), `goodlaw_eval.py`, ARCHITECTURE §7.3 |
| Quick-check curve | measurement of 2026-10-07; `casemap.GOODLAW_QUICK` |
| Clustering, nulls, consensus, benchmark | `cocite.py`, `data/cocite_gold.json`, ARCHITECTURE §7.4, `build/cocite/grid-*.txt` (local) |
| Audit of these claims | `docs/audit-2026-10-04.md` §3 |
| US Code renumbering checks | `docs/scope-cocite-usc.md` |
