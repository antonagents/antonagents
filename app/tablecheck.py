"""`table` checks: deterministic rules over a CSV the agent wrote.

Built for jobs whose output is a register or ledger (invoices, payments, stock):
the verifier — not the agent — confirms the columns, that a key is unique, that
every row satisfies an arithmetic rule (e.g. `net + gst == total`), and that every
input file is accounted for. Rules are parsed with `ast` and evaluated over a tiny
whitelist (numbers, strings, column names, + - * /, comparisons, and/or/not) —
never `eval`.
"""
import ast
import csv
import fnmatch
import io
import re
from typing import Optional

TOLERANCE = 0.01          # money: equal within a cent
_NUM = re.compile(r"^[-(]?\s*[^\d\-(]{0,4}\s*[\d,]*\.?\d+\s*\)?$")


def to_number(v) -> Optional[float]:
    """'1,284.50' / '$1,284.50' / 'AUD 1284.5' / '(54.00)' → float; else None."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    if not s or not _NUM.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    digits = re.sub(r"[^\d.]", "", s)
    try:
        n = float(digits)
    except ValueError:
        return None
    return -n if neg else n


class RuleError(ValueError):
    pass


_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}


def parse_rule(rule: str) -> ast.Expression:
    """Parse and whitelist a rule; raises RuleError with a user-facing message."""
    try:
        tree = ast.parse(rule, mode="eval")
    except SyntaxError as e:
        raise RuleError(f"can't read the rule: {e.msg}")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.Name,
               ast.Constant, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
               ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise RuleError(f"'{type(node).__name__}' isn't allowed in a rule — use column names, "
                            "numbers, + - * /, comparisons, and/or/not")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str)):
            raise RuleError("only numbers and text are allowed as values")
    return tree


def rule_columns(tree: ast.Expression) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _val(node, row: dict):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        raw = row.get(node.id)
        n = to_number(raw)
        return n if n is not None else (str(raw).strip() if raw is not None else None)
    if isinstance(node, ast.UnaryOp):
        v = _val(node.operand, row)
        if isinstance(node.op, ast.Not):
            return not v
        if not isinstance(v, (int, float)):
            raise RuleError("not a number")
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _val(node.left, row), _val(node.right, row)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise RuleError("arithmetic on a value that isn't a number")
        return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.BoolOp):
        vals = [_val(v, row) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Compare):
        left = _val(node.left, row)
        for op, comp in zip(node.ops, node.comparators):
            right = _val(comp, row)
            if not _cmp(op, left, right):
                return False
            left = right
        return True
    raise RuleError(f"unsupported {type(node).__name__}")


def _cmp(op, a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        eq = abs(a - b) <= TOLERANCE
        return {ast.Eq: eq, ast.NotEq: not eq, ast.Lt: a < b and not eq, ast.LtE: a <= b or eq,
                ast.Gt: a > b and not eq, ast.GtE: a >= b or eq}[type(op)]
    a = "" if a is None else str(a).strip().lower()
    b = "" if b is None else str(b).strip().lower()
    if isinstance(op, ast.Eq):
        return a == b
    if isinstance(op, ast.NotEq):
        return a != b
    raise RuleError("only == and != compare text")


def evaluate(tree: ast.Expression, row: dict) -> bool:
    return bool(_val(tree.body, row))


def read_csv(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = [c.strip() for c in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
    return cols, rows


def check(text: str, cfg: dict, files: Optional[list[str]] = None) -> dict:
    """Evaluate a table check over CSV text. `files`: names in the covers_files dir."""
    cols, rows = read_csv(text)
    fails, notes = [], []
    missing = [c for c in cfg.get("columns") or [] if c not in cols]
    if missing:
        fails.append(f"missing column(s): {', '.join(missing)}")
    if cfg.get("min_rows") is not None and len(rows) < int(cfg["min_rows"]):
        fails.append(f"{len(rows)} rows, expected at least {cfg['min_rows']}")
    # `where` picks the rows the rules apply to (e.g. status == 'ok'), so duplicates and
    # exceptions can be listed in the register without breaking uniqueness or arithmetic
    where = parse_rule(cfg["where"]) if cfg.get("where") else None
    def applies(r):
        try:
            return where is None or evaluate(where, r)
        except RuleError:
            return True
    key = cfg.get("unique") or []
    if key and not missing:
        seen, dupes = set(), []
        for r in (r for r in rows if applies(r)):
            k = tuple(str(r.get(c, "")).strip().lower() for c in key)
            if k in seen:
                dupes.append(" / ".join(str(r.get(c, "")) for c in key))
            seen.add(k)
        if dupes:
            fails.append(f"duplicate {'+'.join(key)}: {', '.join(dupes[:5])}")
    if cfg.get("row_rule") and not missing:
        tree = parse_rule(cfg["row_rule"])
        bad = []
        for i, r in enumerate(rows, 2):            # row 1 is the header
            try:
                if not applies(r):
                    continue
                if not evaluate(tree, r):
                    bad.append(f"row {i}")
            except RuleError:
                bad.append(f"row {i} (not a number)")
        if bad:
            fails.append(f"'{cfg['row_rule']}' fails on {', '.join(bad[:6])}"
                         + (f" (+{len(bad) - 6} more)" if len(bad) > 6 else ""))
        notes.append(f"rule checked on {len(rows)} rows" + (f" where {cfg['where']}" if where else ""))
    cover = cfg.get("covers_files")
    if cover and files is not None:
        if cover.get("pattern"):        # e.g. *.pdf — ignore the agent's own notes/conversions
            files = [f for f in files if fnmatch.fnmatch(f.lower(), cover["pattern"].lower())]
        col = cover.get("column")
        listed = {str(r.get(col, "")).strip().split("/")[-1] for r in rows}
        absent = [f for f in files if f not in listed]
        if absent:
            fails.append(f"{len(absent)} file(s) not in the register: {', '.join(absent[:5])}")
        notes.append(f"{len(files)} file(s) in {cover.get('dir')}")
    observed = f"{len(rows)} rows" + ("; " + "; ".join(fails) if fails else "; all rules hold")
    expected = "; ".join(filter(None, [
        f"columns {', '.join(cfg['columns'])}" if cfg.get("columns") else "",
        f"unique {'+'.join(key)}" if key else "",
        f"{cfg['row_rule']} on every row" if cfg.get("row_rule") else "",
        f"(rules apply where {cfg['where']})" if cfg.get("where") else "",
        f"every file in {cover['dir']} listed" if cover else ""]))
    return {"passed": not fails, "observed": observed[:400], "expected": expected[:300],
            "detail": "; ".join(fails + notes)[:600]}
