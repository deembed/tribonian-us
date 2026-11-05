"""rules.py -- parsing decisions as ordered lists of named rules.

A Decision is one place where the parsers choose between outcomes (does this line start a paragraph? is this
line a footnote's label?). Its rules are small functions, each with a name, tried in the order they are
registered; the first that holds decides, and its name is the outcome (trace.py records it). A rule may assume
the ones before it did not hold: the order is part of the rules, and it is written down once, here in the
registration order, so a rule can be moved or added without editing a long `or` chain.

    PARAGRAPH = Decision("paragraph", default="join:continuation")

    @PARAGRAPH.rule("break:short-last-line")
    def _(c):
        return c.short(c.cur["last"])

    if PARAGRAPH.decide(ctx).startswith("break"): ...

`after=` / `before=` place a rule next to another one instead of at the end. The numbers the rules compare
against are in PARAMS, so they sit in one place too.
"""

from dataclasses import dataclass
from types import SimpleNamespace

from tribonian import trace


@dataclass
class Params:
    # paragraphs (case2md.recap_blocks)
    para_indent: int = 2            # columns past a paragraph's continuation lines that start a new one
    item_dedent: int = 2            # columns back toward the margin that end a bulleted item
    short_line: int = 12            # a line ending this many columns before the margin is a paragraph's last
    # footnotes from the PDF's type (extract.py)
    small: float = 0.92             # below this share of the body size: a footnote's type, or a mark
    raise_pt: float = 1.0           # points a mark's baseline sits above the character before it
    agree: float = 0.7              # share of characters pdftotext and pdfplumber must agree on to read a page


PARAMS = Params()


class Decision:
    def __init__(self, name, default=None, keep=None):
        """default: the outcome when no rule holds. keep(outcome) -> bool: list the decision's line in the
        trace's hits (else only counted); default: always."""
        self.name, self.default, self.keep = name, default, keep
        self.rules = []                         # [(name, test)], in the order they are tried

    def rule(self, name, after=None, before=None):
        """Decorator: register test(ctx) -> bool under name, at the end or next to another rule."""
        def add(test):
            names = [n for n, _ in self.rules]
            if name in names:
                raise ValueError("%s: rule %s is registered twice" % (self.name, name))
            at = len(self.rules)
            if after is not None:
                at = names.index(after) + 1
            elif before is not None:
                at = names.index(before)
            self.rules.insert(at, (name, test))
            return test
        return add

    def order(self):
        return [n for n, _ in self.rules]

    def decide(self, ctx, page=None, text="", before=""):
        """-> the name of the first rule that holds for ctx (else the default), recorded in the trace."""
        out = next((n for n, test in self.rules if test(ctx)), self.default)
        if out is not None:
            trace.hit(self.name, out, page, text, before, keep=self.keep(out) if self.keep else True)
        return out


def ctx(**kw):
    """A decision's context: the facts its rules read, by name."""
    return SimpleNamespace(**kw)
