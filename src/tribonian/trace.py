"""trace.py -- which named rule made each parsing decision, so the rules can be counted across a corpus.

The parsers (case2md.recap_blocks, case2md.split_footnotes, case2md.place_marks, extract.split_page) call
hit(decision, rule, ...) where they decide something. Off by default: a hit is then one attribute check.
`tribonian scotus trace` turns it on, reads every brief, and totals the rules (build/trace/).

Rule names are "<what>:<why>", e.g. "break:short-last-line" or "join:continuation" for a paragraph decision.
"""

import contextlib
from collections import Counter

ON = False
COUNTS = Counter()                  # (decision, rule) -> times it decided
HITS = []                           # (decision, rule, page, text, before) for the decisions worth reading
KEEP = set()                        # decisions whose hits are kept (the rest are counted only)


def hit(decision, rule, page=None, text="", before="", keep=True):
    """Record one decision. keep=False: count it only (a join on every line is too many to list)."""
    if not ON:
        return
    COUNTS[(decision, rule)] += 1
    if keep and (not KEEP or decision in KEEP):
        HITS.append((decision, rule, page, (text or "")[:110], (before or "")[-70:]))


@contextlib.contextmanager
def collect(keep=None):
    """Turn tracing on for a block: -> (counts, hits) filled when the block ends; the state before is restored."""
    global ON, COUNTS, HITS, KEEP
    saved = ON, COUNTS, HITS, KEEP
    ON, COUNTS, HITS, KEEP = True, Counter(), [], set(keep or ())
    out = (COUNTS, HITS)
    try:
        yield out
    finally:
        ON, COUNTS, HITS, KEEP = saved

