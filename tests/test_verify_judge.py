"""Judge checks: the score comes from evidence the engine can find, never from the model."""
from app.verify import grade

CONTENT = """## D. "Do not build" list — pains already well served
1. **White-label client reporting** — saturated. Buyers choose AgencyAnalytics or DashThis.
"""


def _answer(evidence, met=True, criterion="a"):
    return {"criteria": [{"criterion": criterion, "met": met, "evidence": evidence}]}


def test_quote_retyped_with_other_punctuation_counts():
    score, out = grade(["a"], _answer("D. “Do not build” list - pains already well served"), CONTENT)
    assert score == 1.0 and out[0]["met"]


def test_invented_quote_is_rejected():
    score, out = grade(["a"], _answer("Do not build list of pains nobody serves"), CONTENT)
    assert score == 0.0 and not out[0]["met"]
    assert "couldn't point to it" in out[0]["why"]
    assert out[0]["unverified_quote"] == "Do not build list of pains nobody serves"


def test_word_fragment_and_too_short_quotes_are_rejected():
    assert grade(["a"], _answer("eady well served"), CONTENT)[0] == 0.0
    assert grade(["a"], _answer("saturated."), CONTENT)[0] == 0.0


def test_unmet_and_missing_criteria_score_zero():
    assert grade(["a"], _answer("", met=False), CONTENT)[0] == 0.0
    assert grade(["a", "b"], None, CONTENT)[0] == 0.0


def test_answers_match_by_text_then_position():
    crit = ["Names two tools", "Says it is saturated"]
    ans = {"criteria": [
        {"criterion": "says it is SATURATED", "met": True, "evidence": "client reporting saturated"},
        {"criterion": "something else", "met": True, "evidence": "choose AgencyAnalytics or DashThis"},
    ]}
    score, out = grade(crit, ans, CONTENT)
    assert [c["met"] for c in out] == [True, True] and score == 1.0
