"""cocite_hidden: the settings chain and the usage summary (no database needed)."""
import pytest

np = pytest.importorskip("numpy")
from tribonian import cocite_hidden as H


def test_defaults_then_overrides():
    P, src = H.settings(["power=0.5", "court.scotus=5", "per_year=false", "max_inbound=10"])
    assert P["power"] == 0.5 and P["court"]["scotus"] == 5 and P["per_year"] is False and P["max_inbound"] == 10
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
