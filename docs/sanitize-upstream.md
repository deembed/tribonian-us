# Sanitize upstream: one step that corrects CourtListener's data before anything reads it

Status: proposal, 2026-11-10. One item is done (orders-list entries); the rest are catalogued here with the
evidence found so far.

## Why

The bulk data has systematic faults, and each consumer has been patching the ones it meets:

- the case build joins OCR line-break hyphens and drops scan specks (`case2md.join_lines`, `soft_hyphen`, `clean`;
  cd9382a on `dehyphen-opinions`);
- `cocite prep` drops Supreme Court orders-list entries (`cocite_orders.py`, 2026-11-10);
- `cocite hidden` merges decisions CourtListener holds twice (Babb v. Wilkie, State v. Garza);
- the label rewrites of 2026-11-10 found parentheticals attached to the wrong case and worked around them by hand.

A fault fixed in one consumer stays in the others. For example, the case notes, `cocite hidden`, quote lineage and
the label evidence all read the same parentheticals. A single **sanitize** step, run after `tribonian db load` and
before `cocite prep` and the case builds, would fix each fault once.

## Principles

1. **Never edit the bulk tables.** Write corrections to side tables (`db/sanitize.sqlite`), each row with its rule and
   reason, so a correction can be audited, reverted, or recomputed after the next bulk load.
2. **Every rule is versioned,** and the sanitize stamp joins the cache stamp, the way `" | orders v1"` does now, so
   views built before a rule changed count as stale.
3. **Measure before filtering.** Each rule ships with a count on the full data and a sample of what it catches,
   including false positives (the orders classifier: 421,990 of 499,538 Supreme Court clusters, checked by sample).
4. **Text fixes live in one module** (`textfix.py`), used by the case build, citation positions and the orders
   classifier alike. Only the PDF layout rules stay in `case2md`.

## Catalogue

### 1. Orders-list entries as cited cases (done; to move into sanitize)

CourtListener holds one cluster per orders-list page entry. "…, cert. denied, 469 U.S. 873" linked the citing
decision to that entry as well as to the decision under review, and one entry covers every case on its page
(459 U.S. 1188 is the denial for Horton (Ga.) and for Blair and Trimble (Mo.)). Fixed in `cocite_orders.py`:
490,949 of 72.3 million edges dropped. **Move:** the classifier becomes a sanitize rule (`drop_node`, reason
"orders-list entry"), so case notes' cited-by counts and coupling use it too.

### 2. Parentheticals attached to the wrong case (open; the most damaging)

`parens.described` points at the wrong case. Found while writing labels; each agent saw the parenthetical contradict
the case's known holding:

| Case shown | What the parenthetical says | What the case held / is |
|---|---|---|
| Whorton v. Bockting (2007) | made Crawford retroactive | Crawford is *not* retroactive on collateral review |
| Odom (Tenn.) | torture aggravator | a different Odom (the suppression-review standard was wanted) |
| Kiobel (2013) | arbitration | Alien Tort Statute extraterritoriality |
| Griffiths (1973) | § 1983 | alienage and bar admission |
| Hilton (1987) | screening petitions | (another case) |
| Executive Jet, Penn Central, Ash (1973), Penry, Indiana v. Edwards, Hendricks, Mickens, Pellegrini, Becker | various | parentheticals inconsistent with the holdings |

Probable cause: CourtListener resolved the citation that precedes a parenthetical to the wrong cluster, most likely
where two cases share a short name ("Odom", "Becker") or where a pin cite or *id.* was resolved loosely.

**Detection, cheapest first:**
1. *Short-name collisions*: parentheticals whose described cluster shares its short name with another cluster cited
   in the same describing opinion. A count is the first measurement.
2. *Cite check*: in the describing opinion's text, find the citation immediately before the parenthetical, and check
   that its reporter cite is one of the described cluster's cites (`cites` table). A mismatch means a wrong link.
   The citation positions index (`cocite positions`) already holds where each citation sits.
3. *Content check*: compare the parenthetical with the described case's own text (syllabus, or the semantic index
   `semsearch` uses) and flag the outliers. This is the costliest; use it only on what 1 and 2 leave uncertain.

**Fix:** `paren_flag(paren_id, rule, detail)`. Consumers skip flagged parentheticals, or relink them when the cite
check names the right cluster.

### 3. Citations resolved to the wrong reporter or case (open)

- Modern Louisiana Fifth Circuit cites resolve to 1920s "La. App." volumes. Giliberti (1925), Copenhaver (1925)
  and Canovsky (1927) are leading cases of a 2005-2019 errors-patent sub-cluster.
- "Ex Parte Young 1967" anchors a 2015-19 Texas habeas lineage.
- "Salisbury, 179 F.3d 197" is In re Coastal Plains. "Rose 2001, 252 F.3d 676" carries another Rose's
  parenthetical.

**Detection:** year sanity, when a cited case is decades older than every other case its citers cite alongside it in
that slice; and reporter-series ranges, when a volume number falls outside the series' years. **Fix:** `cite_fix`
rows or `drop_edge`.

### 4. One decision held twice (partly handled in `cocite_hidden`)

Babb v. Wilkie (589 U.S. 399) is one cluster cited 279 times and another cited 0. State v. Garza (2003) appears as
150 Wash. 2d 360 and 77 P.3d 347. **Move** the merge rule (same short name, court and filing date; or a shared
reporter cite) into sanitize as `same_as(cluster, canonical)`.

### 5. Missing or generic case names (open)

Cases appear only as "Com. 1999", "Commonwealth 19xx", "United States of America 2004", "Matter", or "cluster 716614".
The label agents identified many from their citations, by hand. **Fix:** `name_fix` from `case_name_full`, the
docket, or the parties in the opinion's caption.

### 6. Wrong filing years (open)

`date_filed` is sometimes not the decision date: Booker shows 2004 (decided Jan. 12, 2005), Phillips v. AWH 2006
(2005), Verburgt 1998 (1997), Sebby 2018 (2017). Slices, citing years and labels inherit the error.
**Detection:** compare with `other_dates`, the SCDB id for Supreme Court cases, and the year in the reporter cite.
**Fix:** `date_fix(cluster, date, source)`.

### 7. OCR in opinion text (handled at render time; to move into `textfix.py`)

- Line-break hyphens: "Certio-rari", "Cer- tiorari", "prece-dential" (`case2md.soft_hyphen`, `cocite_orders.classify`).
- Soft hyphens (U+00AD) left by the PDF (`case2md.join_lines`).
- Scan specks read as bullets at a paragraph's end (`case2md.clean`).
- Digit/letter confusions in court names: "Sup. Ct. 111." for "Ill.".
- Garbled or truncated parentheticals ("Mergens, 496 U.S. 226 -10- defraud…").

**Fix:** one `textfix.py`, applied by every reader of opinion text. Whether to store sanitized text or apply the
functions on read is open: on read keeps the raw text the source of truth, at a small cost each time.

## Order of work

1. Measure item 2 (short-name collisions, then the cite check), because it is the most damaging and the cheapest to
   detect.
2. Create `db/sanitize.sqlite` and `tribonian sanitize`, moving item 1 into it, so the structure exists.
3. Items 4 and 6 (merges and dates), which change counts and slices.
4. Items 3 and 5.
5. Item 7: consolidate the text fixes.

Every rule then needs a rebuild of the co-citation views (`cocite prep`, `build`, `areas`, `labels check`), which
took about 25 minutes on 2026-11-10.
