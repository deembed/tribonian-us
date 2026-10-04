# lawgui

Read-only desktop browser over what the LawFormatting scripts produce: co-citation areas, lineages,
slices, clusters and sub-clusters (`cocite*.sqlite`), and the built case notes (`cases/_index.json`,
`md-cases/`, `pdf/`). It never writes to any of them. Scope: `claude/scope-gui.md` in the Project.

```
cd gui
cp gui.toml.example gui.toml     # then edit the paths
cargo run --release
```

- Start screen: View Clusters, View Cases. Search for Cases is v1; Sub-Cluster Search is TBD.
- Back/forward: the arrow buttons or Alt+Left / Alt+Right. The breadcrumb shows the last few pages.
- The app opens a view only if its `meta.schema` matches `db::SCHEMA` (cocite.py `SCHEMA_VERSION`);
  other views are listed greyed out. After a schema bump in cocite.py, update `db::SCHEMA` and the queries.
- Lineage pages need the `subs(lineage)` / `clusters(lineage)` indexes: `./cocite.py index --courts ...`
  once per view built before 2026-10-04 (a new build includes them).
- Area pages carry a history graph (`src/graph.rs`), drawn like `git log --graph`: one lane per lineage,
  time running down (or up, with "Newest at top"). Click a dot to open its lineage.
- Test aid: `LAWGUI_DELAY_MS=800 cargo run` slows every page load, to see the loading placeholders.
- Case tables sort by any column heading (click again to flip). The Good law column reads
  `cases/citedby/<ref>/profile.json`, so it is filled only for cases `casemap.py goodlaw` has run on.
- Hollow markers on the history graph are links to lineages outside the area; click one to go there.
- Search for Cases (`src/search.rs`, its own worker thread): case names (most cited first), a citation
  such as `463 U.S. 29`, or Parentheticals (what later courts said a case stands for, ranked by how many
  parentheticals describe it). Uses `search.sqlite` and `courtlistener.sqlite` from `bulk_dir` unless
  `search_db` / `main_db` say otherwise. Tests: `cargo test` (query building, citation detection).
- Build on the case page runs `casemap.py get --cluster N` then `case2md.py build cases/<file>` in `repo_dir`
  (interpreter: `python` in gui.toml), streaming output into a log panel at the bottom. A build only counts
  as done when case2md prints its `wrote pdf/...` line, as in build_all.sh. Afterwards the app rereads the
  case files and reloads the page. One build at a time.
- The case page shows at once, with no build: details from the main database (docket, dates, judges,
  CourtListener's citing count, every citation, headmatter, syllabus/summary when present), the cached case
  profile (citations per year, successor) when a build wrote one, how later courts describe the case (cached,
  or read live from the parentheticals), and the note's front matter. Good law says "not calculated" until a
  build computes it (it reads up to ~2,000 citing opinions, so it is slow). `opinions_db` in gui.toml
  (default repo_dir/db/opinions.sqlite) maps a case to its opinions for the live parentheticals.
- Builds queue: the status line at the bottom shows "Building k of n" and what is queued, then "Done" (with any
  failures). A build added after "Done" starts a new run with fresh counts. "Output" opens the log.
