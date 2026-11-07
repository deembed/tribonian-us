"""cocite_hidden: the settings chain and the usage summary (no database needed)."""
import pytest

np = pytest.importorskip("numpy")
from tribonian import cocite_hidden as H


def test_defaults_then_overrides():
    P, src = H.settings(["power=0.5", "court.scotus=5", "settle_years=5", "max_inbound=10", "later=false"])
    assert P["power"] == 0.5 and P["court"]["scotus"] == 5 and P["settle_years"] == 5 and P["max_inbound"] == 10
    assert P["later"] is False
    assert P["court"]["circuit"] == H.DEFAULTS["court"]["circuit"]      # the rest of the table is kept
    assert src["power"] == "--set" and src["min_members"] == "default"


def test_bad_settings_exit():
    with pytest.raises(SystemExit):
        H.settings(["nonsense=1"])
    with pytest.raises(SystemExit):
        H.settings(["court.supreme=2"])
    with pytest.raises(SystemExit):
        H.settings(["power=-1"])


def test_signals():
    assert H.norm_signal("See, e.g..") == "see, e.g."
    assert H.norm_signal("But see") == "but see"
    assert H.SIG_START.match("See also State v. X").group(1).lower() == "see also"
    assert H.SIG_INSIDE.search("(internal quotation marks omitted) (quoting In re Det. of Turay, ").group(1) == "quoting"


def test_usage_line():
    uses = [{"name": "A", "alone": True, "signal": "", "sentence": "x"}, {"name": "B", "alone": False, "signal": "but see", "sentence": "a much longer sentence"},
            {"name": "C", "alone": False, "signal": "", "sentence": "y"}]
    t = H.usage_line(uses)
    assert t.startswith("3 members cited: 1 alone in a sentence, 2 in string cites")
    assert "direct x2" in t and "against: B" in t
    assert H.best_use(uses)["name"] == "A"


def test_cohort_rank(monkeypatch):
    # cohort (year 2016, level 2): inbound 0, 0, 0, 1, 5 ; (2016, 3): 0
    keys = sorted([((2016 * 8 + 2) << 32) | x for x in (0, 0, 0, 1, 5)] + [((2016 * 8 + 3) << 32) | 0])
    monkeypatch.setitem(H._CACHE, "cohort", np.array(keys, np.int64))
    rank, n, less, same = H.cohort_rank([2016, 2016, 2016], [2, 2, 3], [0, 1, 0])
    assert list(n) == [5, 5, 1]
    assert less[0] == 0 and same[0] == 0.6 and rank[0] == 0.3          # three of five tied at 0: mid-rank 0.3
    assert less[1] == 0.6 and rank[1] == 0.7
    assert rank[2] == 0.5                                              # alone in its cohort
