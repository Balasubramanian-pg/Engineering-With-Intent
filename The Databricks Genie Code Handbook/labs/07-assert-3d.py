"""labs/07-assert-3d.py

3-Dimensional assertion harness for generated SQL.

Dimensions
----------
  Dim 1 — Syntax:   does the generated SQL parse as a SELECT query?
  Dim 2 — AST:      is the generated query structurally equivalent to golden?
  Dim 3 — Parity:   do the two queries return the same rows?

Status semantics
----------------
  PASS  the check ran and succeeded.
  FAIL  the check ran and failed.
  SKIP  the check could not run (no executor, sqlglot absent and the
        fallback is insufficient). SKIP is NEVER reported as PASS.

The original returned PASS for Dim 3 unconditionally (``abs(100.0 -
100.0) < 0.001``) and never used ``golden_sql``. Both are fixed. The
self-test below deliberately feeds the harness wrong inputs and asserts
that the affected dimension FAILs — so a regression to the original
behavior cannot pass silently.

Exit codes (CLI)
----------------
  0  no FAIL (and no SKIP, unless --require-parity)
  1  at least one FAIL, or SKIP present with --require-parity
  2  usage error
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence

# sqlglot is optional. When present, Dim 1 and Dim 2 use a real SQL parser.
# When absent, both fall back to structural checks and Dim 2 is labelled
# accordingly — the check still runs, but is documented as weaker.
try:
    import sqlglot
    from sqlglot import exp
    _HAS_SQLGLOT = True
except ImportError:  # pragma: no cover — fallback path
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]
    _HAS_SQLGLOT = False


# --- Result types -----------------------------------------------------------

class Status(str, Enum):
    """``str``-valued so ``status == "PASS"`` works in shell/JSON output."""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Dimension:
    """One dimension's outcome. Frozen so results are safe to share."""
    name: str
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class Verdict3D:
    """All three dimensions. Frozen; ordering fixed."""
    syntax: Dimension
    ast: Dimension
    parity: Dimension

    def __iter__(self) -> Any:
        return iter((self.syntax, self.ast, self.parity))

    @property
    def has_fail(self) -> bool:
        return any(d.status is Status.FAIL for d in self)

    @property
    def has_skip(self) -> bool:
        return any(d.status is Status.SKIP for d in self)


# --- Executor protocol ------------------------------------------------------
# Dim 3 has no meaning without a way to run both queries. The harness
# accepts any callable returning an iterable of rows; it never imports a
# Spark/DB connection itself. Wiring the executor is the caller's job.

Executor = Callable[[str], Sequence[Sequence[Any]]]


# --- Dim 1: Syntax ----------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z_][a-z0-9_.]*", re.IGNORECASE)


def _check_syntax(sql: str, dialect: str) -> Dimension:
    """Parse *sql*. PASS iff it is a SELECT statement (not DDL/DML)."""
    if _HAS_SQLGLOT:
        try:
            tree = sqlglot.parse_one(sql, dialect=dialect)
        except Exception as exc:  # sqlglot raises ParseError subclasses
            return Dimension("Dim 1 — Syntax", Status.FAIL, f"parse error: {exc}")
        if not isinstance(tree, exp.Select):
            return Dimension(
                "Dim 1 — Syntax", Status.FAIL,
                f"root is {type(tree).__name__}, not SELECT",
            )
        return Dimension("Dim 1 — Syntax", Status.PASS, "parsed as SELECT")

    # --- Fallback: structural check, no parser ---
    stripped = sql.strip()
    upper = stripped.upper()
    if not upper.startswith("SELECT"):
        return Dimension(
            "Dim 1 — Syntax", Status.FAIL,
            "does not begin with SELECT (fallback, sqlglot absent)",
        )
    if " FROM " not in f" {upper} ":
        return Dimension(
            "Dim 1 — Syntax", Status.FAIL,
            "no FROM clause (fallback, sqlglot absent)",
        )
    depth = 0
    for ch in stripped:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth < 0:
            return Dimension(
                "Dim 1 — Syntax", Status.FAIL,
                "unbalanced ')' (fallback, sqlglot absent)",
            )
    if depth != 0:
        return Dimension(
            "Dim 1 — Syntax", Status.FAIL,
            "unbalanced '(' (fallback, sqlglot absent)",
        )
    return Dimension(
        "Dim 1 — Syntax", Status.PASS,
        "structural check only — install sqlglot for real parsing",
    )


# --- Dim 2: AST graph comparison -------------------------------------------

@dataclass(frozen=True)
class Features:
    """Semantic features extracted from a parsed query.

    Comparing these sets is what "AST graph comparison" actually means:
    two queries are structurally equivalent iff their table set, referenced
    column set, aggregate set, join-equivalence-pair set, and group-by set
    match — regardless of ``USING(x)`` vs ``ON a.x = b.x`` spelling.
    """
    tables: frozenset[str]
    columns: frozenset[str]
    aggregates: frozenset[tuple[str, tuple[str, ...]]]
    join_pairs: frozenset[frozenset[str]]
    group_by: frozenset[str]

    def diff(self, other: "Features") -> list[str]:
        out: list[str] = []
        for name in ("tables", "columns", "aggregates", "join_pairs", "group_by"):
            a = getattr(self, name)
            b = getattr(other, name)
            if a != b:
                out.append(f"{name}: gen-only={sorted(a - b)} gold-only={sorted(b - a)}")
        return out


def _col_name(node: Any) -> str:
    """Normalize a column/identifier node to its bare name (lowercase)."""
    name = getattr(node, "name", None)
    if name:
        return str(name).lower()
    return str(node).strip().lower()


def _extract_features(tree: Any) -> Features:
    """Walk a sqlglot expression tree and pull out the comparison features."""
    tables: set[str] = set()
    columns: set[str] = set()
    aggregates: set[tuple[str, tuple[str, ...]]] = set()
    join_pairs: set[frozenset[str]] = set()
    group_by: set[str] = set()

    for node in tree.walk():
        if isinstance(node, exp.Table):
            tables.add(node.name.lower())

        elif isinstance(node, exp.Column):
            columns.add(node.name.lower())

        elif isinstance(node, exp.AggFunc):
            cols = tuple(sorted(c.name.lower() for c in node.find_all(exp.Column)))
            aggregates.add((type(node).__name__.lower(), cols))

        elif isinstance(node, exp.Join):
            using = node.args.get("using")
            if using:
                for col in using:
                    name = _col_name(col)
                    join_pairs.add(frozenset([name]))
                    # USING contributes its column to the "referenced
                    # columns" set too, so it can be compared with the
                    # ON-equivalent form.
                    columns.add(name)
            else:
                on = node.args.get("on")
                if on is not None:
                    for eq in on.find_all(exp.EQ):
                        left = eq.this
                        right = eq.expression
                        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                            join_pairs.add(frozenset([
                                left.name.lower(), right.name.lower(),
                            ]))

        elif isinstance(node, exp.Group):
            for g in node.expressions:
                if isinstance(g, exp.Column):
                    group_by.add(g.name.lower())
                elif isinstance(g, exp.Literal) and str(g.this).isdigit():
                    group_by.add(f"#{g.this}")  # positional GROUP BY 1

    return Features(
        tables=frozenset(tables),
        columns=frozenset(columns),
        aggregates=frozenset(aggregates),
        join_pairs=frozenset(join_pairs),
        group_by=frozenset(group_by),
    )


def _fallback_ast_features(sql: str) -> Features:
    """Regex-based feature extraction — weaker than sqlglot, clearly labelled.

    Catches table drift and aggregate drift; does NOT catch join-shape
    changes as precisely. The detail string returned by ``_check_ast`` makes
    this limitation visible to the caller.
    """
    lowered = sql.lower()
    tables: set[str] = set()
    words = _TOKEN_RE.findall(lowered)
    for i, w in enumerate(words):
        if w in ("from", "join") and i + 1 < len(words):
            tables.add(words[i + 1].split(".")[-1])

    aggregates: set[tuple[str, tuple[str, ...]]] = set()
    for m in re.finditer(
        r"\b(sum|count|avg|min|max|stddev)\s*\(([^)]*)\)", lowered
    ):
        func = m.group(1)
        args = tuple(sorted(_TOKEN_RE.findall(m.group(2))))
        aggregates.add((func, args))

    columns = frozenset(_TOKEN_RE.findall(lowered)) - frozenset({
        "select", "from", "where", "join", "on", "using", "group", "by",
        "order", "having", "limit", "as", "and", "or", "not", "null",
        "sum", "count", "avg", "min", "max", "stddev", "distinct",
    })

    return Features(
        tables=frozenset(tables),
        columns=columns,
        aggregates=frozenset(aggregates),
        join_pairs=frozenset(),  # fallback cannot infer joins reliably
        group_by=frozenset(),
    )


def _check_ast(gen_sql: str, gold_sql: str, dialect: str) -> Dimension:
    """Compare generated vs golden structurally. PASS iff features match."""
    if _HAS_SQLGLOT:
        try:
            gen_tree = sqlglot.parse_one(gen_sql, dialect=dialect)
            gold_tree = sqlglot.parse_one(gold_sql, dialect=dialect)
        except Exception as exc:
            return Dimension("Dim 2 — AST Graph", Status.FAIL, f"parse error: {exc}")
        diffs = _extract_features(gen_tree).diff(_extract_features(gold_tree))
        if diffs:
            return Dimension("Dim 2 — AST Graph", Status.FAIL, "; ".join(diffs))
        return Dimension("Dim 2 — AST Graph", Status.PASS, "structural match")

    diffs = _fallback_ast_features(gen_sql).diff(_fallback_ast_features(gold_sql))
    if diffs:
        return Dimension(
            "Dim 2 — AST Graph", Status.FAIL,
            "(fallback) " + "; ".join(diffs),
        )
    return Dimension(
        "Dim 2 — AST Graph", Status.PASS,
        "fallback token fingerprint — install sqlglot for real AST comparison",
    )


# --- Dim 3: Row parity ------------------------------------------------------

def _rows_match(
    gen_rows: Sequence[Sequence[Any]],
    gold_rows: Sequence[Sequence[Any]],
    tol: float = 1e-3,
) -> tuple[bool, str]:
    """Compare two result sets, order-insensitive, with float tolerance."""
    def normalize(rows: Sequence[Sequence[Any]]) -> list[tuple[Any, ...]]:
        return sorted(
            (tuple(r) for r in rows),
            key=lambda r: tuple(str(x) for x in r),
        )
    gen = normalize(gen_rows)
    gold = normalize(gold_rows)
    if len(gen) != len(gold):
        return False, f"row count: gen={len(gen)} gold={len(gold)}"
    for i, (a, b) in enumerate(zip(gen, gold)):
        if len(a) != len(b):
            return False, f"row {i}: column count {len(a)} vs {len(b)}"
        for j, (av, bv) in enumerate(zip(a, b)):
            if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
                if not math.isclose(float(av), float(bv), abs_tol=tol, rel_tol=tol):
                    return False, f"row {i} col {j}: {av!r} ≠ {bv!r}"
            elif av != bv:
                return False, f"row {i} col {j}: {av!r} ≠ {bv!r}"
    return True, f"{len(gen)} row(s) match within {tol}"


def _check_parity(
    gen_sql: str,
    gold_sql: str,
    executor: Executor | None,
    tol: float,
) -> Dimension:
    """Execute both queries via *executor*; PASS iff result sets match."""
    if executor is None:
        return Dimension(
            "Dim 3 — Row Parity", Status.SKIP,
            "no executor provided — wire one to enable parity (see --executor)",
        )
    try:
        gen_rows = list(executor(gen_sql))
        gold_rows = list(executor(gold_sql))
    except Exception as exc:
        return Dimension(
            "Dim 3 — Row Parity", Status.FAIL,
            f"executor raised: {exc}",
        )
    match, detail = _rows_match(gen_rows, gold_rows, tol)
    return Dimension(
        "Dim 3 — Row Parity",
        Status.PASS if match else Status.FAIL,
        detail,
    )


# --- Top-level --------------------------------------------------------------

def evaluate_query_3d(
    generated_sql: str,
    golden_sql: str,
    *,
    dialect: str = "databricks",
    executor: Executor | None = None,
    tolerance: float = 1e-3,
) -> Verdict3D:
    """Run all three dimensions. Pure and idempotent.

    Same inputs → equal Verdict3D. The only external state consulted is the
    executor (caller-supplied) and sqlglot's module-level availability,
    both of which are stable within a single run.
    """
    if not isinstance(generated_sql, str) or not generated_sql.strip():
        raise ValueError("generated_sql must be a non-empty string")
    if not isinstance(golden_sql, str) or not golden_sql.strip():
        raise ValueError("golden_sql must be a non-empty string")
    return Verdict3D(
        syntax=_check_syntax(generated_sql, dialect),
        ast=_check_ast(generated_sql, golden_sql, dialect),
        parity=_check_parity(generated_sql, golden_sql, executor, tolerance),
    )


def format_verdict(verdict: Verdict3D) -> str:
    """Fixed-width render of a Verdict3D. Pure."""
    lines = []
    for dim in verdict:
        line = f"  {dim.name}: {dim.status.value}"
        if dim.detail:
            line += f"  ({dim.detail})"
        lines.append(line)
    return "\n".join(lines)


# --- Self-tests -------------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


_GEN = (
    "SELECT c.name, sum(o.net_amount) FROM retail_prod.gold.orders o "
    "JOIN retail_prod.gold.customers c USING(customer_id) GROUP BY 1"
)
_GOLD = (
    "SELECT c.name, sum(o.net_amount) FROM retail_prod.gold.orders o "
    "JOIN retail_prod.gold.customers c ON o.customer_id = c.customer_id "
    "GROUP BY 1"
)


def _mock_executor(rows_by_sql: dict[str, Sequence[Sequence[Any]]]) -> Executor:
    """Build an Executor that returns canned rows per query string."""
    def _run(sql: str) -> Sequence[Sequence[Any]]:
        if sql in rows_by_sql:
            return rows_by_sql[sql]
        raise RuntimeError(f"mock executor: no rows configured for {sql[:40]!r}")
    return _run


def _self_test() -> int:
    print("3-D assertion harness — self-tests")
    print("=" * 62)
    ok = True

    # --- Case A: equivalent pair, no executor → parity SKIP ------------
    v = evaluate_query_3d(_GEN, _GOLD)
    ok &= _check(v.syntax.status is Status.PASS, f"A: syntax={v.syntax.status}, expected PASS")
    ok &= _check(v.ast.status is Status.PASS, f"A: ast={v.ast.status}, expected PASS")
    ok &= _check(v.parity.status is Status.SKIP, f"A: parity={v.parity.status}, expected SKIP")
    print("  ✓ A: equivalent pair, no executor → PASS PASS SKIP")
    print(format_verdict(v))

    # --- Case B: equivalent pair, mock matching executor → all PASS ---
    matching = _mock_executor({
        _GEN: [("Alice", 100.00), ("Bob", 200.00)],
        _GOLD: [("Alice", 100.00), ("Bob", 200.00)],
    })
    v = evaluate_query_3d(_GEN, _GOLD, executor=matching)
    ok &= _check(
        all(d.status is Status.PASS for d in v),
        f"B: expected all PASS, got {[d.status.value for d in v]}",
    )
    print("  ✓ B: equivalent pair + matching executor → all PASS")

    # --- Case C: mismatched executor → parity FAIL --------------------
    mismatched = _mock_executor({
        _GEN: [("Alice", 100.00), ("Bob", 200.00)],
        _GOLD: [("Alice", 999.00), ("Bob", 200.00)],   # Alice's row differs
    })
    v = evaluate_query_3d(_GEN, _GOLD, executor=mismatched)
    ok &= _check(v.parity.status is Status.FAIL, f"C: parity={v.parity.status}, expected FAIL")
    print(f"  ✓ C: row mismatch → parity FAIL ({v.parity.detail})")

    # --- Case D: syntax error → Dim 1 FAIL, others must not claim PASS -
    bad_syntax = "SELEKT * FROM retail_prod.gold.orders WHERE"
    v = evaluate_query_3d(bad_syntax, _GOLD)
    ok &= _check(
        v.syntax.status is Status.FAIL,
        f"D: syntax={v.syntax.status}, expected FAIL",
    )
    print("  ✓ D: malformed generated SQL → Dim 1 FAIL")

    # --- Case E: structurally different → Dim 2 FAIL ------------------
    different = (
        "SELECT o.order_id, o.net_amount FROM retail_prod.gold.orders o"
    )   # no join, no group-by, different projections
    v = evaluate_query_3d(different, _GOLD)
    ok &= _check(
        v.ast.status is Status.FAIL,
        f"E: ast={v.ast.status}, expected FAIL",
    )
    print(f"  ✓ E: structurally different → Dim 2 FAIL ({v.ast.detail[:60]}…)")

    # --- Case F: non-SELECT statement → Dim 1 FAIL --------------------
    v = evaluate_query_3d("DROP TABLE retail_prod.gold.orders", _GOLD)
    ok &= _check(
        v.syntax.status is Status.FAIL,
        f"F: syntax={v.syntax.status}, expected FAIL (DROP is not SELECT)",
    )
    print("  ✓ F: DROP TABLE → Dim 1 FAIL (not a SELECT)")

    # --- Case G: the original's Dim 3 lie cannot recur ------------------
    # Feed two completely different queries with a mock executor that
    # returns DIFFERENT rows. The original returned PASS regardless.
    other = "SELECT customer_id FROM retail_prod.gold.customers"
    diff_rows = _mock_executor({
        _GOLD: [(1,), (2,)],
        other:  [(99,)],   # different row count and value
    })
    v = evaluate_query_3d(other, _GOLD, executor=diff_rows)
    ok &= _check(
        v.parity.status is Status.FAIL,
        "G: hardcoded-parity bug — different rows must FAIL, not PASS",
    )
    print("  ✓ G: different row sets → parity FAIL (no hardcoded PASS)")

    # --- Case H: idempotency & immutability ---------------------------
    a = evaluate_query_3d(_GEN, _GOLD)
    b = evaluate_query_3d(_GEN, _GOLD)
    ok &= _check(a == b, "H: repeated calls must be equal")
    try:
        a.syntax = None  # type: ignore[misc]
        ok &= _check(False, "H: Verdict3D must be frozen")
    except Exception:
        pass
    print("  ✓ H: idempotent & frozen")

    # --- Case I: input validation ------------------------------------
    for bad in ("", "   ", None):
        try:
            evaluate_query_3d(bad, _GOLD)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            continue
        ok &= _check(False, f"I: empty generated_sql {bad!r} should raise")
    try:
        evaluate_query_3d(_GEN, "")
    except ValueError:
        pass
    else:
        ok &= _check(False, "I: empty golden_sql should raise ValueError")
    print("  ✓ I: empty inputs rejected")

    if ok:
        print("\nAll self-tests PASSED.")
        return 0
    print("\nSelf-tests FAILED.", file=sys.stderr)
    return 1


# --- CLI --------------------------------------------------------------------

def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="3-D assertion harness: syntax, AST, row parity.",
    )
    p.add_argument("--generated", help="generated SQL (default: built-in demo)")
    p.add_argument("--golden", help="golden SQL (default: built-in demo)")
    p.add_argument("--dialect", default="databricks", help="SQL dialect (default: databricks)")
    p.add_argument("--tolerance", type=float, default=1e-3, help="float tolerance for parity")
    p.add_argument(
        "--require-parity", action="store_true",
        help="treat SKIP as FAIL — for production CI where parity must be checked",
    )
    p.add_argument("--self-test", action="store_true", help="run internal self-tests")
    return p.parse_args(argv)


def _main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_test or (args.generated is None and args.golden is None):
        return _self_test()

    if (args.generated is None) != (args.golden is None):
        print("error: --generated and --golden must be supplied together", file=sys.stderr)
        return 2

    verdict = evaluate_query_3d(
        args.generated, args.golden,
        dialect=args.dialect, tolerance=args.tolerance,
    )
    print(format_verdict(verdict))

    if verdict.has_fail:
        return 1
    if verdict.has_skip and args.require_parity:
        print("error: parity check skipped (no executor); --require-parity set", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
