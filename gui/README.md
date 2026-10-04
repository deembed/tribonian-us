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
