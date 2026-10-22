# Scope: US Code provisions in the co-citation build

Written 2026-10-14. Assumes the state in ARCHITECTURE.md §7.4 and `claude/positions-c-prototype.md`: positions done
(extractor v2), case notes read `appellate-sentence-m0.2`, `gold-common.json` is the benchmark.

---

## Question
Which Code provisions travel with which cases, and with each other, per five-year slice; how that changes;
with a provision keeping one identity when the Code renumbers it.

## Two problems, kept apart
- **Identity:** is "42 U.S.C. § 1973" cited in 2010 the same provision as "52 U.S.C. § 10301" cited in 2016?
  Co-citation needs this and nothing more.
- **Content:** what did § X say in year Y? Not needed for co-citation. Display only, fetched case by case from
  OLRC prior release points (USLM back to ~2013) or the annual historical archives. Out of scope here.

## What already exists (checked 2026-10-14)
- `cocite_positions.py` already meets statute citations: CourtListener wraps them in `<span class="citation ...">`
  with no link, and the extractor records them as `cited = -2` and drops the text. The occurrences, with
  sentence/paragraph positions, are found; only the identity is thrown away.
- `case2md.STATUTE_RE` and `statute_note()` ("15 U. S. C. § 1392" -> `15-usc-1392`, the md/ note name).
- **The current XML already holds most of the renumbering history** (needs the notes, as `--notes` reads them):
  - 1,924 `status="transferred"` stubs, each with a Codification note and a machine-readable
    `<ref href="/us/usc/t52/s10301">` ("Section 1973 was editorially reclassified as section 10301 of Title 52").
  - Titles 18 and 28 (1948 recodification): "Historical and Revision Notes: Based on title 18, U.S.C., 1940 ed., § 80"
    (959 in t18, 1,199 in t28).
  - Later positive-law titles (10, 31, 36, 40, 41, 46, 49, 51, 54, ...): "Revised Section | Source (U.S. Code)"
    tables per section (e.g. 773 in t49, 1,029 in t10).
  - Repealed sections with "Prior provisions" notes (repeal and re-enactment elsewhere).

---

## Steps

### 1. Measure first (no writes)
`./cocite.py usc-sample --n 2000`: random opinions, report
- share of `-2` spans that match `STATUTE_RE` (statutes vs other unlinked cites),
- statute mentions found by regex in the text but **outside** any span (CourtListener missed them),
- short forms ("§ 1983" alone, "id. § 552(b)") and popular-name cites ("Title VII", "§ 10(b) of the Exchange Act")
  per opinion, by decade.
This decides whether spans alone are enough for v1.

**Result (2026-10-14, 1,998 opinions, seed 1):** full Code cites 1,776 (1,489 in spans, 287 outside);
spans catch 83.8% overall, 78.3% in the 2020s -> v3 needs spans **plus** a regex pass outside them.
Bare "§ N" 3,188, of which 585 resolve from a full cite of the same section in the same opinion
(the rest mostly state codes and treatises). Popular-name cites with no Code cite within 150 characters: 331
(occurrences, inflated by repeats; about half non-federal or pre-Code in the examples).
Decision: **record every reference with a `how` code; resolve afterwards** (record once, compute later):
`how` = span | regex | short-resolved | short-unresolved | popular. Resolvable short forms add no binary pairs
(the decision already cites the section) but matter for the sentence-weighted view. Popular names are kept as
raw-name keys; a small hand table (Title VII, ADA Titles I-III, Title IX, Title VI, ...) maps them later, ranked by
the full run's counts. Regex miss to fix: `26 U.S.C.A. Int.Rev.Code, § 23` (section after "Int.Rev.Code").

### 2. Extract: positions v3, statute sidecar
- Bump `VERSION` to 3. For each `-2` row that parses as a Code cite, write a sidecar row
  `(op, seq, key)` to `pos/<lo>-<hi>.npz` (`stat` arrays), linked to the existing row by `(op, seq)`.
  Proximity weights (sentence, string) then apply to statutes with no new code.
- `key`: int id of the **as-cited** (title, section) in `cocite-cache/usc_keys.json`. Section level only;
  subsections dropped in v1. "U.S.C.A." and "U.S.C.S." fold to the same key.
- `§§ 1981, 1983` -> one row per section. Ranges `§§ 1981-1983` -> endpoints only (letters in between make the
  interior unknowable without the map).
- Sidecar row: `(op, seq, how, key)`; regex-found and short-form rows get their own `seq` and positions, so
  they are also new rows in the positions arrays (sep/signal computed as for span rows).
- Short forms: resolved when the same section number is cited in full in the same opinion (title known);
  otherwise kept with title 0. Popular names: key = normalised raw name ("title vii", "section 10(b) of the
  securities exchange act").
- Cost: one more full positions run (same as the v2 run; resumable, all cores).

### 3. Identity map: `./cocite.py usc-map`
- Reads `xml/*.xml` (the four note sources above) plus OLRC's editorial reclassification tables
  (t7, t25, t34, t52, t54, ...; downloaded once into `data/usc-reclass/`, small, tracked).
- Edges: `(old_key, new_key, year, source)`. `year` = when the move took effect.
- Canonical id = the newest number in the chain (union-find over edges).
- **Time-aware resolution**: an old number maps to its successor for citing decisions of any year (courts keep
  citing the old number after a move), *unless* the number was later reused for something else; then only
  decisions filed before the reuse year map. Reuse is detected as a key that is both an edge source and a live
  section in the current XML.
- Output: `cocite-cache/usc_map.json` (edges, canonical ids, stamp of the XML release point).
- Unmapped cites keep their as-cited key: correct within an era, discontinuous across a move.

### 4. Check the map against the data: `./cocite.py usc-map --check`
- **Mapped pairs:** for A -> B, citing counts per slice and partner-case Jaccard across the move. Low Jaccard =
  wrong edge or reuse.
- **Missing moves:** an unmapped A whose citations end in the slice B's start, with partner Jaccard >= 0.2 (the
  lineage threshold), is a candidate renumbering. Printed with evidence, never auto-applied.

### 5. Tier 1: statute profile on the shared clusters (no rebuild)
Like scope B tier 1. For each sub-cluster / area and slice: provisions cited by its citing decisions, share and
lift against all appellate decisions. Hub provisions (28 U.S.C. § 1291, § 1331, § 2254 boilerplate) show up as
low-lift; skip anything cited by more than `--max-df` decisions, as `coupling` does.

Where it shows:
- `subdoc` and area/lineage notes: "Provisions this sub-cluster's citers cite" (top 10, with lift).
- **US Code section notes (`md/`)**: a new "Cited with" section: top co-cited cases per slice and the areas the
  section sits in, linked to case notes. This is the bridge between the US Code and case vaults. Markdown only;
  the US Code PDFs stay as they are unless asked.
- GUI case page: provisions the case's sub-cluster draws on.

### 6. Tier 2: statutes as nodes (only if tier 1 isn't enough)
`./cocite.py build --statutes`: canonical provisions become extra columns of M; Leiden, lineages, areas
unchanged. A separate results database, like the weighting views. Keep it only if `gold-common` F1 does not drop.

---

## Falsification
- **VRA move:** before mapping, 42 § 1973 (to 2014) and 52 § 10301 (from 2014) are two nodes with high partner
  Jaccard (Gingles, Shelby County, Brnovich); after mapping, one. If the partners differ, extraction is wrong.
- **Blind recovery:** hold out the OLRC reclassification tables, build the map from XML stubs minus Title 34/52/54,
  and see how many of those moves step 4 finds from the data alone. Report recall and false candidates.
- **Known pairs (tier 1):** 42 § 1983 with Monell / Harlow / Pearson; 28 § 2254(d) with Williams v. Taylor /
  Harrington v. Richter; 5 § 706 with State Farm / Overton Park. Each should be in the top provisions of its
  sub-cluster with lift well above 1.
- **1948 recodification:** 18 U.S.C. 1940 ed. § 80 -> 18 § 1001 should carry the false-statements cases across
  1945-49 / 1950-54.

## Known gaps (measured in step 1, not fixed in v1)
- Popular-name and act-section cites (needs OLRC Popular Names + classification tables).
- Short forms ("§ 1983" without a title).
- Before 1926: Revised Statutes and Statutes at Large only. Out of scope.
- Moves before the current XML's notes reach (pre-1940 editions).
- State statutes.

## Commands
```
./cocite.py usc-sample --n 2000                         # step 1: rates, nothing written
./cocite.py positions                                   # step 2: v3 run, statute sidecar
./cocite.py usc-map                                     # step 3: xml/ + data/usc-reclass/ -> usc_map.json
./cocite.py usc-map --check --courts appellate          # step 4
./cocite.py usc-profile --courts appellate --weight sentence --mix 0.2   # step 5, feeds subdoc/vault/md
./usc2md.py --all                                       # rebuild section notes with "Cited with"
./cocite.py build --courts appellate --statutes         # step 6, only if needed
```

## Size
- Step 1: ~150 lines. Step 2: ~100 lines plus the positions rerun. Step 3: ~250 lines (four XML note parsers, one
  HTML table reader). Step 4: ~150. Step 5: ~200 plus the note sections in `usc2md.py`, `cocite_subdoc.py`, the
  vault and the GUI. Step 6: ~100 plus a build per view.

---

## Results so far (2026-10-15)

- **Step 2 (`usc-extract`, version 2):** full run done, 216 shards. Appellate: 2,610,091 decision x section rows,
  69,010 sections. Version 2 added I.R.C. citations, default titles ("references are to ..." notes; Tax Court and
  Board of Tax Appeals -> Title 26) and "Sec." short forms. In the Tax Court shard, unresolved short forms fell
  from 324k to 59k.
- **Step 3 (`usc-map`, version 2):** 13.8k distinct edges (stubs, "Based on ... 1940 ed.", revision tables); 718 old
  numbers are live again today. Version 2 reads range stubs (`s11601...11610`, 144 of them, e.g. ICARA 42 § 11601 ->
  22 § 9001) paragraph by paragraph, and dates a stub only from the moving act's date or the target title's
  reclassification year, not from the act's history.
- **Step 4 (`usc-check --holdout 34,52,54`, appellate):**
  - Known moves: 103 of 3,367 dated edges have 20+ citing decisions on both sides (most moved sections are rarely
    cited, so validation covers the ones that matter). Partner cosine: median 0.21 (10% 0.06, 90% 0.38) vs random
    target 0.00 (90% 0.03-0.06). The lowest scores are **splits**: 28 § 41 (1940 ed.) -> 30 sections, 18 § 408 ->
    §§ 10, 2311, 2313. Each target inherits only a fragment of the old section's partners. These are not wrong edges.
  - Candidate moves: at cosine >= 0.5, recall on the held-out titles was 0 of 5. The threshold sat above 90% of real
    moves, so the default is now 0.2. At 0.2: recall 5 of 5, but 622 candidates, of which only 17 are in the map.
    The rest are mostly **related provisions**, not renumberings: the capital habeas chapter (28 § 2263 vs
    §§ 2254/2255), the IDEA sections, Black Lung. A few are substantive relocations by amendment, e.g.
    8 § 1253(h) -> § 1231(b)(3) (IIRIRA, 1996), at subsection level. This is the same lesson as `cocite shift`:
    co-citation sees displacement, not its reason.
  - **Decision:** the map is trusted as built from the notes. The detector is a review list, never a source of edges.
- **Node rule (decided 2026-10-15): separate nodes, linked over time.** A citation keeps the number it was cited
  by; nothing is rewritten to a current number. Node id = the number, plus an era when the number was later reused
  for another provision (`49:1651@-1983` vs `49:1651`). Map edges become lineage links between nodes
  (`UscMap.links()`, `successors()`), as cluster lineages link clusters across slices. A split (28 § 41, 1940 ed.,
  into 30 sections) is one node with 30 links; no fan-out threshold is needed (an earlier "collapse to current
  number, keep the old one past 3 targets" rule was dropped). A slice containing a move has two nodes for one
  provision; they share partners and should co-cluster (a visible handoff). Moves by amendment stay out unless
  added by hand.
