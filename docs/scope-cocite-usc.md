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

### 2. Extract: positions v3, statute sidecar
- Bump `VERSION` to 3. For each `-2` row that parses as a Code cite, write a sidecar row
  `(op, seq, key)` to `pos/<lo>-<hi>.npz` (`stat` arrays), linked to the existing row by `(op, seq)`.
  Proximity weights (sentence, string) then apply to statutes with no new code.
- `key`: int id of the **as-cited** (title, section) in `cocite-cache/usc_keys.json`. Section level only;
  subsections dropped in v1. "U.S.C.A." and "U.S.C.S." fold to the same key.
- `§§ 1981, 1983` -> one row per section. Ranges `§§ 1981-1983` -> endpoints only (letters in between make the
  interior unknowable without the map).
- Short forms: v1 counts them, does not resolve. (v2: resolve "§ N" to the title of the opinion's last full cite
  when the map says that section exists.)
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
