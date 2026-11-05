"""rules.Decision: first rule that holds wins, in registration order; after=/before= place a rule."""
import pytest

from tribonian import rules, trace


def make():
    d = rules.Decision("demo", default="none")
    d.rule("a")(lambda c: c.x > 10)
    d.rule("b")(lambda c: c.x > 5)
    return d


def test_first_rule_that_holds_wins_else_default():
    d = make()
    assert d.decide(rules.ctx(x=11)) == "a"
    assert d.decide(rules.ctx(x=6)) == "b"
    assert d.decide(rules.ctx(x=1)) == "none"


def test_after_and_before_place_a_rule():
    d = make()
    d.rule("c", before="a")(lambda c: c.x > 0)
    d.rule("z", after="a")(lambda c: False)
    assert d.order() == ["c", "a", "z", "b"]
    assert d.decide(rules.ctx(x=11)) == "c"


def test_a_name_registers_once():
    d = make()
    with pytest.raises(ValueError):
        d.rule("a")(lambda c: True)


def test_decide_records_the_outcome_in_the_trace():
    d = make()
    with trace.collect() as (counts, hits):
        d.decide(rules.ctx(x=11), 3, "line")
    assert counts[("demo", "a")] == 1 and hits[0][:3] == ("demo", "a", 3)


def test_paragraph_rules_keep_their_order():
    from tribonian import case2md as c
    assert c.PARAGRAPH.order() == [
        "break:after-blank-line-or-block", "join:double-spaced-pair", "break:short-last-line",
        "break:enumerated-start", "break:bullet-start", "break:indent-past-continuation",
        "break:back-to-margin-after-item", "break:indent-after-sentence"]
