"""labs/07-golden-suite-runner.py

Run a golden-question benchmark suite against a pluggable executor and
report per-question verdicts with an honestly-computed pass rate.

Design guarantees
-----------------
* **Idempotent**  — given a deterministic executor (the same question
                    always returns the same answer), two runs produce equal
                    ``SuiteReport`` objects. The demo uses a dict-backed
                    mock executor so its output is byte-identical across
                    runs.
* **Stateless**   — no globals, no caches, no files written. Questions and
                    results are frozen dataclasses. All inputs (catalog,
                    executor, comparator, threshold) are injectable.

Honesty
-------
The original file printed ``Accuracy: 96.0%`` from a hardcoded constant and
never ran a question. This file does not contain a constant like that. The
one place a ``96%`` appears is in ``_self_test``, where it is *asserted as
the expected outcome* of a mock executor that answers 24 of 25 synthetic
questions correctly — proof that the number comes from the results, not
from a literal.

Exit codes (CLI)
----------------
  0  suite PASSED (accuracy >= threshold)
  1  suite FAILED, or any question ERRORed
  2  usage error
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


# --- Types -----------------------------------------------------------------

class Status(str, Enum):
    """String-valued so ``status == "PASS"`` works in logs and JSON."""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"      # runner not provided
    ERROR = "ERROR"    # executor raised


@dataclass(frozen=True)
class Question:
    """One golden question. Frozen so catalogs can be shared safely."""
    id: str
    category: str
    question: str
    expected: Any


@dataclass(frozen=True)
class QuestionResult:
    """Outcome for one question. Frozen."""
    question: Question
    status: Status
    actual: Any = None
    detail: str = ""


@dataclass(frozen=True)
class SuiteReport:
    """Full suite outcome. Frozen; ``results`` is a tuple."""
    results: tuple[QuestionResult, ...]
    threshold: float

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status is Status.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status is Status.FAIL)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.status is Status.ERROR)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status is Status.SKIP)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def accuracy(self) -> float:
        """Fraction of questions that PASSED, over the whole catalog.

        Zero-question catalog → 0.0 (not 1.0). "No questions ran" must not
        look like "100% accuracy."
        """
        if not self.results:
            return 0.0
        return self.passed / self.total

    @property
    def verdict(self) -> Status:
        """Overall suite verdict.

        ERROR beats FAIL beats SKIP beats PASS — a suite cannot be "clean"
        if any question errored, regardless of the pass rate.
        """
        if self.errored:
            return Status.ERROR
        if not self.results or self.skipped == self.total:
            return Status.SKIP
        if self.accuracy >= self.threshold:
            return Status.PASS
        return Status.FAIL

    def by_category(self) -> dict[str, "SuiteReport"]:
        """Per-category sub-reports. Pure; original ordering of categories
        follows first appearance in ``results``."""
        buckets: dict[str, list[QuestionResult]] = {}
        for r in self.results:
            buckets.setdefault(r.question.category, []).append(r)
        return {
            cat: SuiteReport(tuple(rows), self.threshold)
            for cat, rows in buckets.items()
        }


Runner = Callable[[Question], Any]
Comparator = Callable[[Any, Any], tuple[bool, str]]


# --- Comparison ------------------------------------------------------------

def default_compare(expected: Any, actual: Any, *, tol: float = 1e-6) -> tuple[bool, str]:
    """Default comparator: numeric within tolerance, else equality.

    Handles the common golden-test shapes: scalars, strings, numbers,
    lists/tuples (element-wise, order-preserving). Returns ``(match,
    detail)`` — the detail is empty on match, and a short human-readable
    diff on mismatch.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return (expected == actual, "" if expected == actual else
                f"expected {expected!r}, got {actual!r}")

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if math.isclose(float(expected), float(actual), abs_tol=tol, rel_tol=tol):
            return True, ""
        return False, f"expected {expected!r}, got {actual!r} (tol={tol:g})"

    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return False, f"length {len(actual)} != expected {len(expected)}"
        for i, (e, a) in enumerate(zip(expected, actual)):
            m, d = default_compare(e, a, tol=tol)
            if not m:
                return False, f"[{i}]: {d}"
        return True, ""

    if expected == actual:
        return True, ""
    return False, f"expected {expected!r}, got {actual!r}"


# --- Runner ----------------------------------------------------------------

def run_question(question: Question, runner: Runner | None,
                 comparator: Comparator) -> QuestionResult:
    """Run one question. Pure modulo the executor's own behavior."""
    if runner is None:
        return QuestionResult(question, Status.SKIP,
                              detail="no runner provided")
    try:
        actual = runner(question)
    except Exception as exc:
        return QuestionResult(question, Status.ERROR,
                              detail=f"runner raised: {exc}")
    match, detail = comparator(question.expected, actual)
    return QuestionResult(
        question,
        Status.PASS if match else Status.FAIL,
        actual=actual,
        detail=detail,
    )


def run_suite(
    questions: Sequence[Question],
    *,
    runner: Runner | None,
    threshold: float = 0.95,
    comparator: Comparator = default_compare,
    category: str | None = None,
) -> SuiteReport:
    """Evaluate a catalog against a runner. Pure and idempotent (given a
    deterministic runner). ``category`` filters the catalog *before*
    running — questions in other categories are not executed.
    """
    if not (0.0 < threshold <= 1.0):
        raise ValueError(f"threshold must be in (0, 1] (got {threshold})")
    if not questions and runner is not None:
        raise ValueError("catalog is empty; refusing to run a zero-question suite")

    selected = (
        [q for q in questions if q.category == category]
        if category else list(questions)
    )
    results = tuple(run_question(q, runner, comparator) for q in selected)
    return SuiteReport(results, threshold)


# --- Rendering -------------------------------------------------------------

def format_report(report: SuiteReport) -> str:
    """Render a SuiteReport as a fixed-width table + summary. Pure."""
    if not report.results:
        return "  (no questions ran)"
    width_id = max(len("id"), *(len(r.question.id) for r in report.results))
    width_cat = max(len("category"), *(len(r.question.category) for r in report.results))
    lines = [
        f"  {'id':<{width_id}}  {'status':<5}  {'category':<{width_cat}}  detail",
        f"  {'-' * width_id}  {'-' * 5}  {'-' * width_cat}  {'-' * 30}",
    ]
    for r in report.results:
        lines.append(
            f"  {r.question.id:<{width_id}}  {r.status.value:<5}  "
            f"{r.question.category:<{width_cat}}  {r.detail}"
        )
    lines.append("")
    lines.append(
        f"  Accuracy: {report.accuracy * 100:.1f}% "
        f"({report.passed}/{report.total} passed, "
        f"{report.failed} failed, {report.errored} errored, "
        f"{report.skipped} skipped; threshold {report.threshold * 100:.1f}%)"
    )
    lines.append(f"  Verdict: {report.verdict.value}")
    return "\n".join(lines)


def format_by_category(report: SuiteReport) -> str:
    """Per-category breakdown. Pure."""
    lines = ["", "  Per-category breakdown:"]
    for cat, sub in report.by_category().items():
        lines.append(
            f"    {cat:<15} {sub.accuracy * 100:6.1f}%  "
            f"({sub.passed}/{sub.total}, verdict {sub.verdict.value})"
        )
    return "\n".join(lines)


# --- Catalog I/O -----------------------------------------------------------

def load_catalog(path: Path) -> tuple[Question, ...]:
    """Load a JSON catalog. Pure (reads one file, writes nothing)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"catalog at {path} must be a JSON array of questions")
    out: list[Question] = []
    for i, item in enumerate(data):
        for key in ("id", "category", "question", "expected"):
            if key not in item:
                raise ValueError(f"catalog entry {i} is missing '{key}'")
        out.append(Question(
            id=str(item["id"]),
            category=str(item["category"]),
            question=str(item["question"]),
            expected=item["expected"],
        ))
    return tuple(out)


# --- Demo catalog & mock runner ------------------------------------------

_DEMO_CATALOG: tuple[Question, ...] = (
    Question("rev_01", "revenue",   "Total net revenue in January 2024?",        1234567.89),
    Question("rank_01", "ranking",  "Top 3 customers by 2024 spend?",
             ["Acme", "Globex", "Initech"]),
    Question("cnt_01", "counts",    "Orders placed on 2024-01-15?",              482),
    Question("reg_01", "regions",   "Which region had the lowest Q4 revenue?",   "APAC"),
    Question("ret_01", "retention", "30-day churn rate for Q3 2024?",            0.0423),
)


def _demo_runner(question: Question) -> Any:
    """Deterministic mock executor — one wrong answer, by design.

    Returns the correct answer for every question except ``cnt_01``, so the
    demo produces an honest 4/5 = 80% accuracy and a FAIL verdict against
    the 95% threshold. The original printed 96% because it never ran
    anything; this demo *runs* the suite and reports what actually happened.
    """
    answers: dict[str, Any] = {
        "rev_01": 1234567.89,
        "rank_01": ["Acme", "Globex", "Initech"],
        "cnt_01": 480,                # ← wrong (expected 482)
        "reg_01": "APAC",
        "ret_01": 0.0423,
    }
    return answers[question.id]


# --- Self-tests -----------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _make_catalog(n: int, correct_n: int) -> tuple[tuple[Question, ...], Runner]:
    """Build *n* synthetic questions and a runner that answers *correct_n*."""
    questions = tuple(
        Question(id=f"q{i:02d}", category="synthetic",
                 question=f"Q{i}", expected=i * 10)
        for i in range(n)
    )
    def runner(q: Question) -> Any:
        idx = int(q.id[1:])
        return q.expected if idx < correct_n else q.expected + 1
    return questions, runner


def _self_test() -> int:
    print("Golden suite runner — self-tests")
    print("=" * 62)
    ok = True

    # --- Case A: the original's 24/25 = 96% claim, computed honestly ---
    questions, runner = _make_catalog(25, correct_n=24)
    report = run_suite(questions, runner=runner, threshold=0.95)
    ok &= _check(report.total == 25, f"A: total={report.total}, expected 25")
    ok &= _check(report.passed == 24, f"A: passed={report.passed}, expected 24")
    ok &= _check(
        math.isclose(report.accuracy, 0.96, rel_tol=1e-9),
        f"A: accuracy={report.accuracy}, expected 0.96",
    )
    ok &= _check(report.verdict is Status.PASS,
                 f"A: verdict={report.verdict}, expected PASS (0.96 >= 0.95)")
    print("  ✓ A: 24/25 → 96.0% → PASS  (computed, not printed)")

    # --- Case B: same catalog, worse runner → different verdict --------
    _, worse_runner = _make_catalog(25, correct_n=20)
    report = run_suite(questions, runner=worse_runner, threshold=0.95)
    ok &= _check(report.passed == 20, f"B: passed={report.passed}, expected 20")
    ok &= _check(math.isclose(report.accuracy, 0.80, rel_tol=1e-9),
                 f"B: accuracy={report.accuracy}, expected 0.80")
    ok &= _check(report.verdict is Status.FAIL,
                 f"B: verdict={report.verdict}, expected FAIL")
    print("  ✓ B: 20/25 → 80.0% → FAIL  (discriminates)")

    # --- Case C: same runner, different threshold → different verdict --
    report = run_suite(questions, runner=worse_runner, threshold=0.75)
    ok &= _check(report.verdict is Status.PASS,
                 f"C: verdict={report.verdict}, expected PASS (0.80 >= 0.75)")
    print("  ✓ C: same 80% at threshold 0.75 → PASS  (threshold is honoured)")

    # --- Case D: no runner → SKIP, never PASS -------------------------
    report = run_suite(_DEMO_CATALOG, runner=None)
    ok &= _check(report.verdict is Status.SKIP,
                 f"D: verdict={report.verdict}, expected SKIP")
    ok &= _check(report.skipped == report.total,
                 f"D: skipped={report.skipped}, total={report.total}")
    print("  ✓ D: no runner → SKIP (never PASS)")

    # --- Case E: runner raises → ERROR dominates verdict --------------
    def boom(_q: Question) -> Any:
        raise RuntimeError("executor crashed")
    report = run_suite(_DEMO_CATALOG, runner=boom)
    ok &= _check(report.verdict is Status.ERROR,
                 f"E: verdict={report.verdict}, expected ERROR")
    ok &= _check(report.errored == report.total,
                 f"E: errored={report.errored}, expected {report.total}")
    print("  ✓ E: executor raising → ERROR verdict")

    # --- Case F: empty catalog → SKIP, accuracy 0.0 ------------------
    report = run_suite((), runner=_demo_runner)
    ok &= _check(report.verdict is Status.SKIP,
                 f"F: empty verdict={report.verdict}, expected SKIP")
    ok &= _check(report.accuracy == 0.0,
                 f"F: empty accuracy={report.accuracy}, expected 0.0")
    print("  ✓ F: empty catalog → SKIP, accuracy 0.0")

    # --- Case G: category filter runs only that category --------------
    filtered = run_suite(_DEMO_CATALOG, runner=_demo_runner, category="revenue")
    ok &= _check(filtered.total == 1,
                 f"G: filtered total={filtered.total}, expected 1")
    ok &= _check(all(r.question.category == "revenue" for r in filtered.results),
                 "G: filtered results contain non-revenue questions")
    print("  ✓ G: category filter isolates questions")

    # --- Case H: the demo's own honest result (4/5 = 80%) ------------
    demo = run_suite(_DEMO_CATALOG, runner=_demo_runner, threshold=0.95)
    ok &= _check(demo.accuracy == 0.80,
                 f"H: demo accuracy={demo.accuracy}, expected 0.80")
    ok &= _check(demo.verdict is Status.FAIL,
                 f"H: demo verdict={demo.verdict}, expected FAIL")
    ok &= _check(demo.passed == 4 and demo.failed == 1,
                 f"H: demo passed={demo.passed}, failed={demo.failed}")
    print("  ✓ H: demo suite → 4/5 = 80%, FAIL (not fake 96%)")

    # --- Case I: idempotency & frozen results ------------------------
    a = run_suite(_DEMO_CATALOG, runner=_demo_runner)
    b = run_suite(_DEMO_CATALOG, runner=_demo_runner)
    ok &= _check(a == b, "I: repeated runs must be equal")
    try:
        a.threshold = 0.5  # type: ignore[misc]
        ok &= _check(False, "I: SuiteReport must be frozen")
    except Exception:
        pass
    print("  ✓ I: idempotent & frozen")

    # --- Case J: threshold validation -------------------------------
    for bad in (0.0, -0.1, 1.1):
        try:
            run_suite(_DEMO_CATALOG, runner=_demo_runner, threshold=bad)
        except ValueError:
            continue
        ok &= _check(False, f"J: threshold={bad} should raise ValueError")
    print("  ✓ J: threshold validated to (0, 1]")

    if ok:
        print("\nAll self-tests PASSED.")
        return 0
    print("\nSelf-tests FAILED.", file=sys.stderr)
    return 1


# --- CLI ------------------------------------------------------------------

def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a golden-question benchmark suite against a runner.",
    )
    p.add_argument("--catalog", type=Path,
                   help="JSON catalog of questions (default: built-in demo)")
    p.add_argument("--threshold", type=float, default=0.95,
                   help="minimum pass rate to call the suite PASS (default: 0.95)")
    p.add_argument("--category", help="filter to one category")
    p.add_argument("--self-test", action="store_true",
                   help="run internal regression tests instead of a suite")
    return p.parse_args(argv)


def _main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_test:
        return _self_test()

    catalog = load_catalog(args.catalog) if args.catalog else _DEMO_CATALOG
    # The demo runner only knows the demo catalog. For a real catalog,
    # a runner must be wired by the caller — not implemented here, since
    # that would require a DB/model connection this file has no business
    # opening.
    runner: Runner | None = _demo_runner if args.catalog is None else None
    if runner is None:
        print(
            "error: --catalog was supplied but no runner is wired. "
            "This demo file ships only a mock runner for the built-in catalog. "
            "Wire your own `Runner` (a Callable[[Question], Any]) and call "
            "run_suite() from Python; or omit --catalog to run the demo.",
            file=sys.stderr,
        )
        return 2

    report = run_suite(catalog, runner=runner,
                       threshold=args.threshold, category=args.category)
    print(format_report(report))
    if report.by_category():
        print(format_by_category(report))

    return 0 if report.verdict is Status.PASS else 1


if __name__ == "__main__":
    raise SystemExit(_main())
