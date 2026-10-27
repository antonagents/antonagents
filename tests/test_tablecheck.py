"""`table` checks: the verifier, not the agent, decides whether a register adds up."""
import pytest

from app import tablecheck as t

REGISTER = """invoice_number,supplier,source_file,net,gst,total,status
INV-4471,Acme Supplies,digital-invoice.pdf,"1,167.73",116.77,"$1,284.50",ok
INV-4502,Acme Supplies,scanned-invoice.pdf,540.00,54.00,594.00,ok
INV-4519,Brightline,brightline.pdf,200.00,20.00,230.00,exception
"""
CFG = {"columns": ["invoice_number", "net", "gst", "total"], "unique": ["invoice_number"],
       "row_rule": "net + gst == total", "where": "status == 'ok'",
       "covers_files": {"dir": "uploads/inbox", "column": "source_file"}}
FILES = ["digital-invoice.pdf", "scanned-invoice.pdf", "brightline.pdf"]


def test_good_register_passes_and_exceptions_are_excluded_by_where():
    r = t.check(REGISTER, CFG, FILES)
    assert r["passed"], r


def test_arithmetic_error_fails_without_where():
    r = t.check(REGISTER, {**CFG, "where": None}, FILES)
    assert not r["passed"] and "row 4" in r["observed"]


def test_duplicate_invoice_fails():
    dup = REGISTER + "INV-4502,Acme Supplies,again.pdf,540.00,54.00,594.00,ok\n"
    r = t.check(dup, CFG, FILES + ["again.pdf"])
    assert not r["passed"] and "duplicate" in r["observed"]


def test_duplicate_marked_as_such_is_listed_but_not_counted():
    dup = REGISTER + "INV-4502,Acme Supplies,again.pdf,540.00,54.00,594.00,duplicate\n"
    assert t.check(dup, CFG, FILES + ["again.pdf"])["passed"]


def test_unlisted_file_fails():
    r = t.check(REGISTER, CFG, FILES + ["lost-invoice.pdf"])
    assert not r["passed"] and "lost-invoice.pdf" in r["observed"]


def test_missing_column_fails():
    r = t.check(REGISTER, {**CFG, "columns": ["invoice_number", "abn"]}, FILES)
    assert not r["passed"] and "abn" in r["observed"]


@pytest.mark.parametrize("raw,num", [("1,284.50", 1284.5), ("$1,284.50", 1284.5), ("AUD 594", 594.0),
                                     ("(54.00)", -54.0), ("-3", -3.0), ("2026-10-20", None),
                                     ("INV-4471", None), ("", None)])
def test_to_number(raw, num):
    assert t.to_number(raw) == num


@pytest.mark.parametrize("rule", ["__import__('os').system('id')", "net.real", "net[0]",
                                  "(lambda: 1)()", "open('x')"])
def test_rules_cannot_run_code(rule):
    with pytest.raises(t.RuleError):
        t.parse_rule(rule)


def test_cent_tolerance():
    tree = t.parse_rule("net + gst == total")
    assert t.evaluate(tree, {"net": "1167.73", "gst": "116.77", "total": "1284.505"})
    assert not t.evaluate(tree, {"net": "1167.73", "gst": "116.77", "total": "1284.60"})


def test_covers_files_pattern_ignores_other_files():
    cfg = {**CFG, "covers_files": {"dir": "uploads", "column": "source_file", "pattern": "*.pdf"}}
    assert t.check(REGISTER, cfg, FILES + ["digital-invoice.md", "notes.txt"])["passed"]
    assert not t.check(REGISTER, {**cfg, "covers_files": {"dir": "uploads", "column": "source_file"}},
                       FILES + ["digital-invoice.md"])["passed"]
