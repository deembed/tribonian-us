# Scope: two next steps for `cocite.py`

Written 2026-10-01. Assumes the state in ARCHITECTURE.md §7.4: two views (all courts, appellate),
sub-clusters, lineages, labels, areas (CPM + shrinkage + consensus), case-note and PDF boxes,
the vault (`md-cocite/`), cluster-driven crawl (`casemap.py map --by cluster`).

---

## A. Good-law signals from cluster change (`cocite.py shift`)

### Question
Does a case's position in the co-citation structure change in a recognisable way when it is
overruled, abrogated or displaced? If so, flag it, name the likely successor, and feed that to the
text-based good-law check (`casemap.py goodlaw`), which stays the judge.

### Why only a flag
Co-citation sees *displacement*, not its reason. Bowles/Thompson (overruled), Twombly/Conley
(a phrase retired) and Mead/Chevron (narrowed, co-cited as complements) can look alike. Only the
text says "overruled".

### Worked example (from the data)
Thompson v. INS, 375 U.S. 384: in "Timeliness of appeals" (lineage 20244) 1980-99; split off as
lineage 25684 in 2000-04; absorbed into "Jurisdictional time limits" (with Kontrick, Eberhart,
Bowles) in 2005-09; gone after. 20 of its 48 citing decisions in 2005-09 also cite Bowles (42%).
Bowles (2007) abolished Thompson's unique-circumstances exception.

### Signals (per case A, per slice transition s -> s+5)
1. **Successor partner.** A partner B, decided after A, with
   - share = decisions citing A and B / decisions citing A >= `--share` (try 0.25),
   - B's citing count growing much faster than A's (ratio of growth >= `--growth`, try 3).
2. **Lineage break.** A's sub-cluster lineage ends or splits at s+5 (`links`, `lineages.last_slice`),
   and A's new home is a sub-cluster B anchors or sits in.
3. **Citation collapse.** A drops below the slice threshold, or its citing count falls more than
   its cohort's (cases of the same decade and citing level). Raw decline alone is normal aging.
4. **Core to edge.** A's rank by weight inside its unit falls from the top 10% to the bottom half,
   or its home share falls under 0.5.

Output per case: one row per transition with which signals fire, and the best successor B.

### Validation (falsification first)
- Positives: the 40 Supreme Court cases with known overruling years in `goodlaw_eval.py`.
- Negatives: its 20 never-overruled cases, plus extended-not-overruled pairs (Chevron/Mead,
  Twombly/Iqbal) as hard negatives.
- Score each signal and simple combinations: hit = fires in the overruling's slice or the next.
  Report detection, false-alarm rate, and **lead time** (does it fire before the overruling,
  during erosion? check Chevron before Loper Bright, Roe before Dobbs).
- Expect misses for cases cited under ~5 times per slice (never nodes) and for overrulings in
  the partial newest slice.

### Commands
```
./cocite.py shift --cite "375 U.S. 384" --courts appellate     # one case, all transitions
./cocite.py shift --eval --courts appellate                     # the goodlaw_eval set, scored
```
Pure reads of the built databases plus the cache; no rebuild.

### Wiring, only if the eval earns it
- Good-law box: a line "Role changed 2005-09: likely successor Bowles (42% of citers cite both)"
  beside green/yellow/red; never changes the colour.
- `goodlaw` reading set: put decisions citing both A and B first.

### Size
One new command (~200 lines) and an eval harness reusing `goodlaw_eval.py`'s case list.

---

## B. Court-specific views

### Question
What does a given court (Fourth Circuit, D. Md., Maryland appellate courts, the D.C. Circuit)
draw on, and how does that differ from everyone else? Later: binding vs persuasive authority.

### Two tiers (do the cheap one first)
1. **Profile on the shared clusters (cheap, no reclustering).** Keep the all-courts/appellate
   clusters. For court X's citing decisions only, count which clusters/sub-clusters/areas they
   cite and compare with all appellate courts (share, and lift = X's share / everyone's).
   "The Fourth Circuit over-draws on area 5 (qualified immunity) 1.8x." Reuses the
   `citing_breakdown` logic with a court filter.
2. **Reclustered view (expensive, only for big courts).** `build --courts ca4` clusters only
   ca4's citing decisions. Shows a court's own groupings, but each view is a full build, and
   small courts are too sparse (few nodes clear `--min-cited`).

### What prep needs
- `prep` stores court *level* per decision (`level.npy`), not the court. Add `court.npy`
  (an int code per decision, from `dockets.court_id`) plus a code table. One prep rerun
  (the long step), no change to existing builds.
- `--courts` grows to accept court ids and groups: `--courts ca4`, `--courts "md,mdctspecapp"`,
  `--courts circuit:4` (the circuit plus its districts).

### Binding vs persuasive (later, needs a hand table)
- For a citing court X and a cited case from court Y: binding if Y = SCOTUS, or Y is X's own
  circuit (for X a district or the circuit itself), or Y is X's state supreme court (for X a state
  court); otherwise persuasive.
- The bulk `courts` table has no parent links, so this needs a static map: 94 districts ->
  circuit (by state), state appellate -> state supreme. About 150 lines of data; CourtListener's
  court ids are stable.
- Output: in a case note, "Cited by ca4 as binding / persuasive" splits; in a court profile,
  how much of X's citing is to binding authority, by area.

### Commands
```
./cocite.py prep                                    # once, adds court.npy
./cocite.py court ca4 --courts appellate            # tier 1: profile vs all appellate
./cocite.py court mdd --vs ca4                      # a district against its circuit
./cocite.py build --courts ca4                      # tier 2, only if tier 1 isn't enough
```

### Size and cost
Tier 1: one prep rerun on your machine, then ~150 lines; per-court profiles are seconds.
Tier 2: one full build per court view (hours each). Binding/persuasive: the static map plus
~100 lines.

### Related idea, same machinery
"What each judge draws on": the same tier-1 profile with citing decisions filtered by author
(opinions' author field) instead of court. Needs an author code per decision in prep too.
