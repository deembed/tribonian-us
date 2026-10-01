# LawFormatting — architecture and design intent

For a developer opening the scripts for the first time. It covers what each system does, how data flows between them, and why things are built the way they are. For command usage, see `README.md` in the repo. Written 2026-09-30 against the working tree, which includes uncommitted changes.

---

## 1. What the project is for

The project turns federal law into **dense PDFs you can read and annotate on a reMarkable Paper Pro**, plus an **Obsidian mirror** of the same material, linked as notes. There are two corpora:

- **US Code**: USLM XML from uscode.house.gov, handled by `usc2md.py`.
- **Case law and district-court filings**: CourtListener API data plus its quarterly bulk dumps, handled by `case2md.py`, `casemap.py` and `casedb.py`.

### Constraints that shape everything

| Constraint | Consequence in the code |
|---|---|
| The reMarkable only follows links **inside** one PDF. Cross-document links (GoToR) silently jump to the same page number in the *current* document, and search covers only the open document (see `claude/remarkable-pdf-navigation-findings.md`). | The unit of document is the unit of navigation. PDFs are built per chapter, per case, or per docket, with a PDF outline (bookmarks) for navigation. Cross-document links are never emitted. |
| Screen is 1620×2160 px at 229 PPI | The page size is hard-coded in the LaTeX preamble (`509.34pt × 679.13pt`), with 24pt margins, 10pt type and microtype. |
| Deep statutory nesting, (a)(1)(A)(i)(I) | Every level is its own flat box, with the enclosing frames *drawn* rather than nested (see §3.3). |
| CourtListener API limits (free tier: 5/min, 50/h, 125/day) | Every request is cached under `cases/` and self-paced. Commands stop cleanly at the daily limit and resume when rerun. Anything the bulk data can answer is answered locally. |
| No language model anywhere | Structure, good-law status and citation analysis all come from markup, text layout, regular expressions and counting. Every result can be traced back to evidence. |
| The same content goes to two outputs | Each parser builds one intermediate tree, and two renderers (Markdown and TeX) walk it. |

---

## 2. System map

```
                 uscode.house.gov                 CourtListener
                   xml/*.xml              API (rate-limited)        bulk CSV.bz2 (quarterly)
                       │                     │         │                     │
                       ▼                     ▼         ▼                     ▼
                  usc2md.py          case2md.py fetch   case2md.py       casedb.py load
               (USLM → Node tree)    (opinions →         recap fetch      ├─ bulk/courtlistener.sqlite
                       │              cases/*.json)     (dockets →        │   courts, cites, edges,
                       │                     │           cases/recap/)    │   parens, clusters, dockets
                       │                     │               │            ├─ db/opinions.sqlite (text)
                       │                     │               │            └─ bulk/search.sqlite (FTS5)
                       │                     │               │                     │
                       │                     │               │        casemap.py get / map / stubs
                       │                     │               │        (cases/*.json from the DB,
                       │                     │               │         stubs, cited-by, good-law)
                       │                     ▼               ▼                     │
                       │               case2md.py build / recap build ◄───────────┘
                       │              (JSON → El tree → Part tree)
                       ▼                     ▼
               md/ + build/*.tex     md-cases/ + build/*.tex
                       └───────┬─────────────┘
                               ▼
                         build_all.sh  (parallel, incremental, pdflatex ×2)
                               ▼
                             pdf/
                   ┌───────────┴────────────┐
             sync_vault.sh             sync_remarkable.py
          (rsync --delete into      (rmapi via rmfakecloud,
           Obsidian 08-Reference)     never deletes on tablet)
```

### Files, largest first

| File | Lines | Role |
|---|---|---|
| `case2md.py` | ~2,400 | Cases: API client, opinion parser (Harvard HTML), layout parser (RECAP and slip opinions), MD/TeX renderers, `find`, case profile box |
| `casemap.py` | ~1,200 | Citation graph: cases from the local DB, stubs, cited-by analysis, sentence lineage, good-law check |
| `usc2md.py` | ~1,150 | US Code: USLM → Node tree → MD notes and TeX. **Owns the shared LaTeX preamble and TeX escaping** |
| `cocite.py` | ~550 | Co-citation clusters per five-year slice, soft membership, lineage across slices |
| `casedb.py` | ~400 | Bulk CSV → SQLite loader, full-text index |
| `goodlaw_eval.py` | ~375 | Test harness for the good-law rules against cases whose overruling is known |
| `build_all.sh` | ~240 | Orchestrator: finds out-of-date jobs, runs them in parallel, keeps a manifest |
| `sync_remarkable.py`, `sync_vault.sh`, `link_audit.py` | small | Delivery and audits |

`case2md.py` imports `PREAMBLE`, `RULES` and `tex_chars` from `usc2md.py`, so both corpora look the same on the tablet. `casemap.py` imports `case2md` (as `c`) and `casedb`.

---

## 3. Shared rendering model

### 3.1 One tree, two renderers

- **US Code:** `build()` makes a `Node` per USLM level. Each text field is stored twice, as an `(md, tex)` pair, because escaping differs and footnote handling differs.
- **Cases:** everything is parsed into a small DOM (`El`, from `parse_html`). An opinion becomes a **flat block list** of `("p" | "q" | "h", El)`. `tree_of()` then folds it into a **`Part` tree** using the heading levels. `render_md` and `render_tex` walk that tree. The RECAP and slip-opinion parsers deliberately produce *the same opinion dict* as the Harvard parser, so all three share one renderer.
- **Inline rendering:** `inline(node, mode, ctx, marks)` converts inline markup. It handles page numbers, footnote marks, emphasis, links (case links become `[[wikilinks]]` when a note exists, statute citations link to US Code notes), leader rows, and so on. `ctx` (`Ctx`) carries rendering state: current reporter page, current Part path, link tables.

### 3.2 Escaping

`usc2md.tex_chars` is the single path to pdflatex. It covers `TEX_ESCAPES`, fractions, combining accents mapped to TeX accents, and a whitelist of glyphs T1 can set. Anything unmapped is counted in `UNMAPPED` and reported after the build (`unmapped_note`). Scan specks are dropped and anything else prints as a boxed "?", so nothing is passed through silently. Odd Unicode spaces (U+202F, U+2009, U+2007) are folded to a no-break space once, at parse time (`strip_ns`), so none can reach pdflatex, headings or filenames.

### 3.3 Why the boxes aren't nested (the load-bearing LaTeX decision)

Nested `tcolorbox`es can't break across pages, so a tall nested subsection overflows the page. Instead, **every item is its own top-level breakable box** (`lvl`). The frames of its enclosing groups are *drawn by each item*: side lines at fixed offsets (`\sides{d}`), a top rule on a group's first item and a bottom rule on its last (`\hrulestep`). The result looks nested but can break anywhere. `STYLE=rails` draws only the left-hand lines. `lines before break=3` prevents pages holding one stray line.

### 3.4 Running header without `\markright`

Marks are lost inside breakable tcolorboxes. Instead, each label calls `\recordpath{path}`, which writes `\pathmark{page}{path}` to the `.aux` at shipout, when `\thepage` is real. The header for page N shows the last path recorded on or before page N−1. This is why every build runs pdflatex twice. The footer uses `\@abspage@last` for "page N of M".

### 3.5 Footnotes

Inserts can't escape a split box, so footnote texts are collected during rendering. They are flushed as `\footnotetext[n]{…}` **after the box closes**, with marks numbered document-wide (`FnSink`). hyperref's footnote links are turned off because marks and texts are paired by hand.

### 3.6 Navigation = the PDF outline

The reMarkable's table of contents is the PDF outline. Cases emit `\pdfbookmark` entries like `V-A · *46 · first words`: Part path, reporter page, then the first words of the text. This is the main way to jump around a long opinion on the tablet.

---

## 4. US Code: `usc2md.py`

1. **Parse:** lxml. `strip_ns` removes the default namespace and normalizes text (NFC, spaces).
2. **Units and containers:** a *unit* (one note) is a `section` or `courtRule`. A *container* (one PDF) is a `chapter`, `courtRules` or `compiledAct` (an act printed in a title's appendix). The Federal Rules sets are recognized from the `<courtRules>` heading (`RULE_SETS`). Supplemental rules restart at Rule 1, so they get distinct slugs.
3. **Identifiers drive everything:** `/us/usc/t5/s552/a/1/A` becomes components `['a','1','A']`, label `(a)(1)(A)`, slug `5-usc-552`, citation `5 U.S.C. § 552`, and the aliases Obsidian resolves.
4. **Node model:** each level has `heading`, `chapeau` (lead-in before children), `content`, `continuation` (text after the children), `more` paragraphs and a `repealed` flag. Repealed sections are struck through.
5. **Markdown:** one note per section in `md/tNN/chNN/`. YAML front matter: citation, identifier, release point, aliases. Body: flat paragraphs, each carrying its full citation path. A References list holds deduplicated `[[wikilinks]]` for US Code cross-references only (`usc_ref`). Public Laws and Statutes at Large stay plain text, and a section never links to itself.
6. **TeX:** `render_tex(docs, …)` takes *a list of sections*, so any selection can become one PDF: a chapter, a whole title, or a hand-picked list. That is the builder for the planned "working-set document" (see the navigation findings doc).
7. **Options:** `--credits` (source credits) and `--notes` (editorial and statutory notes, including quoted text and tables).

---

## 5. Cases: `case2md.py`

### 5.1 Getting the data

- **`call()` / `throttle()`:** the only door to the API. Limits come from `CL_LIMITS`. Request times persist in `cases/_api_log.json` (a sliding 24-hour window) and `cases/_api_history.tsv` (every request). At the daily limit the command exits cleanly, and because everything is cached, rerunning resumes.
- **`fetch`:** cluster → opinions → saved as `cases/<cite>_<name>.json` with `{cluster, docket, opinions}`. `casemap.db_case()` builds **the same JSON shape from the local database**, so `build` never needs to know where the data came from.
- **`cases/_index.json`:** cluster id → slug, name, citation, `full` file. It is edited under an `fcntl` lock (`index_lock`) and written atomically (temp file plus rename), so parallel builds are safe. It is the source for wikilinks: a citation becomes `[[slug]]` only if the index has a note for it.

### 5.2 The three kinds of source text

The central design question is **where the structure comes from**:

**A. Harvard-format HTML: structure is given.** `html_with_citations` for Caselaw Access Project cases carries the Harvard tags: `<opinion type>`, `<author>`, `<judges>`, `<footnote label>`, `<footnotemark>`, star-pagination spans and paragraph ids like `b258-4`. `read_opinion()` mostly just follows the tags. (The bulk data's `xml_harvard` column is the same markup without the citation links, so `casedb` keeps `html_with_citations`.) The added logic:

- **Headings** are paragraphs holding only `II`, `A` or `3` (`heading_candidate`). `assign_levels()` infers the hierarchy from the sequence (Roman numeral → capital letter → number, continuing whichever counter matches next), and `tree_of()` nests the Parts.
- **OCR-damaged headings** ("r-H h-I" for III) go through `repair_headings()`. It tries three things in order:
  1. Adopt the `<h2>` headings from CourtListener's *other* digitization of the same case, if the counts line up.
  2. Fill gaps by counting: I, II, ?, IV means the gap is III.
  3. Otherwise, warn and leave it.
- **Consistency checks:** a byline paragraph that starts lowercase ("delivered the opinion of the Court.") is recognized, and a footnote with no mark in the text produces a warning.

**B. Slip opinions: structure from layout, plus the judges' own markers.** These are recent cases that exist only as pdftotext output. `read_slip()` reuses the layout parser (C). The caption ends at the first `NAME, Circuit Judge:` line (`SLIP_AUTHOR_RE`). Each later `…, concurring:` starts a separate opinion, and "Before:" becomes the byline. CourtListener's citation links are harvested from the `<pre>` HTML and re-applied (`slip_links`, `relink`).

**C. RECAP filings: structure from layout alone.** This is plain `pdftotext -layout` text (or OCR for scans). The pipeline:

| Stage | Function | How it decides |
|---|---|---|
| Pages | `layout_pages` | Split on form feeds. `HEADER_RE` ("Case 1:07-cv… Page 3 of 7") gives the page label and is removed. A lone number at the top or foot is a page number and is removed. The common left margin is subtracted. |
| Footnotes | `split_footnotes` | The footnote area starts at a line holding **only the next expected footnote number** (it tolerates one skipped number). A footnote continuing from the previous page is detected when the last footnote hasn't finished its sentence and a wider-set block follows two or more blank lines. |
| Caption | `split_caption` | After the VERSUS / "v." line, the first indented all-caps line set off by blank lines is the document title. |
| Blocks | `recap_blocks` | The right margin is the **median line end** per page, because justified lines throw off the maximum. Line spacing is detected as double or single. Rules: a short last line ends a paragraph; in double-spaced text, a single-spaced indented run is a block quote; a lone `II.` or a centred title-case line is a heading; a rule, `/s/` or a line ending in "JUDGE" starts the signature. |
| Table-of-contents rows | `split_leaders`, `leader_entry` | *Added 2026-09-30.* Dot-leader lines reach the right margin, so the short-line rule never ended a table-of-contents or table-of-authorities entry, and the whole table merged into one paragraph. Any line matching `LEADER_RE` now ends its entry. The dots become a real TeX leader, with page numbers right-aligned, and ` … pages` in Markdown. |
| Footnote marks | `place_marks` | Each footnote's number is searched for moving forward from the previous mark, and no later than the page the footnote is printed on. `FNMARK` excludes digits that follow dot leaders (fixed 2026-09-30). |

Output per docket: one PDF for the whole docket, one note per document, and one docket note that lists every entry, including unpurchased ones. Each document note gets a **Cites** section (the RECAP `cites` ids resolved through the local DB) and a **Statutes cited** section (`STATUTE_RE` counts, linked to US Code notes).

**Known limit:** scanned filings (for example Wilner ECF 12) arrive as unreliable OCR with no form feeds. The layout rules have nothing to work with, and the page headers and the court's "USDC SDNY" stamp end up mixed into the text. This is still open: skip these documents, or re-OCR the filed PDF.

### 5.3 Above the opinion

- **Headmatter** (yellow box): parties, docket, dates and counsel from the published report, parsed from `clusters.headmatter`.
- **Case profile** (blue box): citing cases per year as a bar chart, the last five years compared with the five before (rising / steady / falling), and the good-law status (see §7.3). This needs the local DB. The result is cached in `cases/citedby/<cluster>/profile.json` and recomputed when the bulk data's load stamp changes.
- **Editorial callouts:** the reporter's summary and syllabus become collapsed Obsidian callouts labelled "not part of the opinion".

### 5.4 Where notes live

`note_dir()`: **an existing note's location wins**, so moving a note inside `md-cases/` files it permanently. RECAP document notes follow their docket note. New notes are placed by `FOLDERS` rules on `nature_of_suit` or `cause` (FOIA and Privacy Act cases → `FOIA/`). `pdf/` mirrors the note folders.

---

## 6. Local database: `casedb.py`

- **Input:** `bulk/<name>-YYYY-MM-DD.csv.bz2`, newest date wins. These are PostgreSQL dumps: `csv.reader(doublequote=False, escapechar="\\")`. Plain `csv.DictReader` misparses them.
- **Streaming:** `rows_of` pipes through `lbzip2` or `pbzip2` when available, using every core.
- **`TABLES`** is the whole schema. Each entry holds a CREATE body, a row function that picks columns, and the indexes to build after loading. Loading happens once, in bulk, with durability pragmas off, and indexes are created afterwards. The `loaded` table records which file each table came from, so finished tables are skipped.
- **Two databases, deliberately:** the graph and metadata (`bulk/courtlistener.sqlite`, about 25 GB) are separate from the opinion text (`db/opinions.sqlite`, compressed with zlib, and on the fast disk). The opinions table keeps **only the first non-empty text field**, in the order `html_with_citations`, `xml_harvard`, `html_columbia`, `html_lawbox`, `html_anon_2020`, `html`, `plain_text`.
- **`search.sqlite`:** FTS5 over case names and parentheticals, used by `case2md.py find`. It is stamped with the source tables' load times and rebuilt when they change.
- **`columns`:** a stand-in row (`_Seen`) records which columns each row function reads, so the list of columns kept or dropped is always accurate.
- **Not in the bulk data:** RECAP filings. They are API-only.

---

## 7. Citation graph: `casemap.py`

### 7.1 Building the note web

- **`get`:** one case from the DB → `cases/*.json` plus its note, with no API.
- **`map --depth N`:** walks outward along `edges` (citing → cited). The first `--text K` steps get full text and the outer ring becomes **stubs**: notes that list what they cite and wait to be filled.
- **`stubs`:** fills stubs, most-cited first, from the DB. With `--api`, it falls back to CourtListener. `readable()` rejects text that `build` can't parse, and that text is parked in `cases/unparsed/`.

### 7.2 Cited-by analysis (`citedby`)

The question is **who cites this case, and what do they use it for**:

- **Counts:** exact counts from `edges` and `parens`, broken down by court level (`level_of` maps `courts.jurisdiction`) and by decade.
- **Text-derived statistics:** pinpoint pages, quoted sentences and citation signals, read from the citing opinions. Above `--sample-over`, a **simple random sample** is read, sized with Cochran's formula and a finite-population correction (`sample_size`) for ±5% at 95%. Reported percentages carry that margin (`moe`).
- **Citation windows:** `marked_text` turns HTML into one paragraph per line, turns citation links into anchors, rejoins hyphenated words and drops star-page markers so quotations still match. Plain-text opinions get their citations marked by regex (`_cite_marks`). Parallel-reporter pinpoints (`103 S.Ct. 2856, 2866`) are mapped back to the case (`parallel_patterns`).
- **Which sentences get restated:** `credit_sentences` credits a sentence in the cited case when a citing text shares a run of consecutive words with it, and some 5-word window of that run occurs in *only that sentence*. Phrases the opinion itself repeats need a longer run and go to the earliest occurrence.
- **Lineage:** follows a sentence's wording back through the cited cases. It extends only to cases that contain the wording, so the search follows the phrase instead of spreading across the whole graph.

### 7.3 Good-law check (`goodlaw`)

- **Reading set:** every citing opinion from a court that could overrule the case (up to 1,500), plus the 500 newest from any court.
- **Classification (`classify`):** looks at the citation's own sentence and at sentences that name the case by its short name (`case_names`: "Chevron", "Roe"). Results are **red** (overruled, abrogated, subsequent history like "overruled by"), **yellow** (softer words, "on other grounds"), or **green**.
- **Exclusions:** hypotheticals ("unless Roe is overruled"), parties' arguments, the case doing the overruling, negated or quoted softer words, and same-name different cases (`other_case`: "Aguilar v. Texas" is not "Aguilar v. Felton").
- **Test harness (`goodlaw_eval.py`):** 40 Supreme Court cases whose overruling year is known (a tuning set and a check set) plus 20 that were never overruled. A hit means red from one year before to five years after the overruling. Each ruleset in `RULESETS` (the live one in `casemap.RULES` is R9) is scored against cached reading windows, so tuning is fast. **After changing the rules, bump `GOODLAW_VERSION` and rerun `score`.**

### 7.4 Co-citation clusters (`cocite.py`)

The question is **which cases travel together, and how that changes over time**. Added 2026-10-01. The pipeline itself uses no language model; names and descriptions of its results are written by one, from script-built evidence, and kept in a tracked file.

- **Unit:** two cases are co-cited when one later *decision* (cluster, not opinion) cites both. Citing decisions are sliced by filing year into five-year slices (2005-09, ...).
- **Two views:** `bulk/cocite.sqlite` (all courts) and `bulk/cocite-appellate.sqlite` (`--courts appellate`; filters the *citing* side only). Case notes read the appellate view, because district courts flood the 2020s.
- **`prep`:** one pass over the databases into `bulk/cocite-cache/*.npy`: decision -> cited case, with year and court level.
- **Per slice (`build`):** nodes = cases cited by at least `max(--min-cited, --min-cited-frac x decisions)`; pair support from a blockwise sparse product MᵀM; Leiden (modularity) on raw support. Clusters over `--split-over` are split into **sub-clusters**, kept only when their modularity beats degree-preserving rewired nulls by `--sub-z` standard deviations.
- **Soft membership and partners:** a case's pair weight is split by the other end's cluster, so a case can sit in several; partners by cosine, with lift stored.
- **Lineage:** clusters and sub-clusters in adjacent slices are matched by Jaccard; a mutual best match at 0.2 or more continues the lineage. Merges and splits are `links` rows.
- **Labels (`evidence`, `labels import|match|check`):** evidence per lineage (members, spans, the parentheticals courts wrote) -> a language model writes a name and description -> `cocite_labels.json` (git-tracked; it can't be rebuilt). Each label stores a fingerprint (its 30 heaviest cases) and re-attaches after a rebuild at Jaccard 0.3 or more; `label_status` marks it stable (0.7+), drifted, displaced or orphaned. Siblings are labelled together (`evidence --family-of`) so descriptions say how they differ.
- **Areas (`areas`):** sub-cluster lineages pooled over all slices into broad, stable areas. Settled 2026-10-01: CPM on association-strength weights with shrinkage, `--method cpm --shrink 10 --resolution 0.5 --consensus 20` (consensus clustering, Lancichinetti and Fortunato). Chosen with `--grid` against `cocite_gold.json` (16 hand groupings, 78 labelled lineages, scored by pairwise F1): F1 0.80, precision 0.91, recall 0.71, 82.7% of lineages in areas of 5+. Plain modularity left a giant area of 2,699 lineages; CPM without shrinkage inflated small lineages' ties. There is no resolution plateau, so the structure is nested and **the area level is a chosen cut**, not a natural one. Many areas are jurisdiction x subject (state appellate courts cite their own state); doctrines the Supreme Court governs form cross-state areas. Area labels: `build/cocite/labels-areas.jsonl` (110 of the 120 heaviest), imported into the same labels file.
- **Case notes:** `case2md.py` calls `cocite.case_md()` (Markdown) and `cocite.case_tex()` (PDF, a violet "not part of the record" box) for a co-citation section near the top: the case's sub-cluster and area; who cites it (in the PDF, one 100% bar per slice coloured by citing-cluster lineage, plus ranked citing sub-clusters); where it sits (cluster, sub-cluster, area); partners; sub-cluster descriptions. After `vault`, names in the Markdown link to the vault notes (via `md-cocite/.names.json`).
- **How later courts describe it:** `case2md.parens_data()` reads CourtListener's parentheticals for the case's opinions, in its similarity groups, largest first, with each group's best-scored wording and who wrote it; cached in `profile.json`. Markdown section after the co-citation one; a pale green PDF box. No language model.
- **Vault (`vault`):** `md-cocite/`: `Co-citation map.md` (index MOC), `Areas/` (one note per area), `Lineages/` (one per lineage reaching `--min-size` cases: summary, core cases, a timeline of who joined and left each slice, merges and splits, where leavers went). Generator-owned; `sync_vault.sh` mirrors it to `08-Reference/Co-citation`. Fixed headings (`Summary`, `Core cases`, `Timeline`, ...) so a personal MOC can transclude them; starter in `templates/cocite-personal-moc.md`.
- **Crawl (`casemap.py map --by cluster`, the default unless `--depth` is given):** notes for the cases in the root's sub-cluster lineage in the newest full slice, plus cases that joined it since (`--scope all`: every lineage it has been in), heaviest first, up to `--cluster-max`. `--depth N` keeps the old citation walk.
- **Falsification checks:** `together` on pairs with known history (Twombly/Iqbal, the summary-judgment trilogy, Chevron/State Farm/Loper Bright).
- **Not yet:** good-law signals from cluster change and court-specific views, scoped in `docs/scope-cocite-next.md`; citation proximity; judge profiles.

---

## 8. Build orchestration: `build_all.sh`

- **Job discovery:** `xml/*.xml` gives one job per chapter (chapter lists are cached in `build/chapters/` until the XML changes). `cases/*.json` gives case jobs and `cases/recap/*.json` gives docket jobs.
- **Incremental builds:** `build/built.tsv` maps each job to its PDF. A job builds when it has no record, its PDF is missing, or its source is newer. Failures are never recorded, so the next run retries them. `FORCE=1` after changing a generator.
- **Parallel runs:** each job gets its own scratch folder (`mktemp -d "$SCRATCH/job.XXXXXX"`), so parallel `.tex`, `.aux` and `.md` files never collide. Manifest lines are short enough that appends from parallel workers are atomic.
- **Success signal:** `case2md.py` prints `wrote pdf/…` only when pdflatex succeeded. The script parses that line, and a missing line is a failure.
- **Failures:** saved to `build/failures.txt` and `build/failed_jobs.txt` (rerunnable as a list file).

---

## 9. Delivery

- **`sync_vault.sh`:** `rsync --delete` of `md/` → `08-Reference/USC` and `md-cases/` → `08-Reference/Cases`. These vault folders are **generator-owned**: anything not in the source is deleted. Personal notes live elsewhere in the vault and link in.
- **`sync_remarkable.py`:** uploads through rmapi and rmfakecloud. A PDF is classified as a case if a note with the same name exists in `md-cases/`; otherwise, a name starting with a title number means US Code. **It never deletes on the tablet**, so pen annotations are safe. `build/rm_manifest.json` holds SHA-256 hashes, so only changed PDFs are uploaded.

---

## 10. Conventions and invariants

1. **Two outputs from one parse.** Never add a feature to only one renderer without deciding what the other shows.
2. **The same JSON shape from every source:** API, local DB and RECAP all produce `{cluster, docket, opinions}` or `{docket, documents}`.
3. **No cross-document links in PDFs.** Internal `GoTo` links and bookmarks only.
4. **Every derived judgment carries its evidence:** good-law status lists the opinions behind it, and warnings name the ECF number and the heading.
5. **Resumable and idempotent:** caches everywhere, atomic writes, locks for shared files, and a clean stop at API limits.
6. **Generated output is disposable:** only scripts are tracked in git. `.tex`, `.md`, `.pdf` and caches are all ignored and rebuilt.
7. **Front matter is uniform:** `title`, `aliases`, `citation`, `source`, `retrieved`, `synthetic: false`. Aliases are what make `[[5 U.S.C. § 552]]` resolve in Obsidian.

## 11. Gotchas

- **`FONT=XCharter`** needs the `xcharter` TeX package. Builds in the Cowork VM fail on it; use `FONT=charter` there for test builds only.
- **Deletes are blocked in the project folder** (Cowork). Set `SCRATCH` outside the repo for test builds.
- **`bulk/` is a symlink** to the CourtListener bulk-data folder (on a separate drive). Cowork sessions need that folder connected separately.
- **Reading the bulk CSVs by hand:** always pass `escapechar="\\", doublequote=False`.
- **`*.prevault` and `*.bak` files** are old versions kept by hand; nothing reads them.

## 12. Suggested reading order

1. `README.md`, for the commands.
2. `usc2md.py`: `PREAMBLE` (the box and header machinery), then `build()` → `render_md()` → `render_tex()`.
3. `case2md.py`, in this order:
   1. The module docstring.
   2. `read_opinion` → `repair_headings` / `assign_levels` → `tree_of` → `render_tex`.
   3. The RECAP section, from `layout_pages` to `recap_blocks`.
   4. `recap_build`.
4. `casedb.py`: `TABLES`.
5. `casemap.py`: `db_case`, then `cmd_citedby` and `goodlaw`.
6. `build_all.sh`: `build_case`, `run_job` and the manifest logic.
