"""labs/08-diagnostic-suite.py

Diagnostic harness for known Genie Code / DBSQL failure modes.

For each symptom code the harness:

  1. REPRODUCES — measures whether the symptom is present in the current
                  workspace state and, if so, quantifies it (chars over
                  budget, FK count, relative-pronoun use, bronze scans).
  2. DIAGNOSES  — checks which known root cause(s) explain the symptom.
                  Multiple causes can match; one cause can explain several
                  symptoms. Unknown causes are reported as UNKNOWN_CAUSE,
                  not silently passed.
  3. VERIFIES   — simulates the documented fix by transforming the state,
                  re-runs the reproduction, and confirms the symptom is
                  GONE. A fix that leaves the symptom in place is reported
                  as FIX_UNVERIFIED — the harness refuses to call a fix
                  "verified" just because it was described.

The original file was a dict from symptom code to a one-line string. It
never ran a diagnostic, never reproduced a symptom, and never verified a
fix — the docstring's own claim ("replicating production symptom failures
and verifying fixes") was untrue. This version does what the name promises.

Design guarantees
-----------------
* **Idempotent** — ``diagnose(code, state)`` is a pure function of its two
                    arguments. Re-running with the same state yields an
                    equal ``DiagnosticReport``; the harness writes nothing
                    and mutates nothing.
* **Stateless**  — no globals, no caches, no temp files. Each report
                    carries its full evidence so it can be logged, diffed,
                    or re-inspected without re-running the harness.

Exit codes (CLI)
----------------
  0  every requested code is NOT_PRESENT or DIAGNOSED
  1  any code is UNMEASURED, UNKNOWN_CAUSE, FIX_UNVERIFIED, or ERROR
  2  usage error (unknown symptom code)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Iterable, Sequence


# --- Workspace state ------------------------------------------------------
# One frozen dataclass holds everything a diagnostic can measure. Each
# diagnostic reads only the fields it needs. ``None`` means "not measured"
# — a diagnostic that needs a None field reports UNMEASURED rather than
# guessing, so a missing measurement never looks like a passing symptom.

@dataclass(frozen=True)
class WorkspaceState:
    """Measured properties of the workspace under diagnosis.

    Every field is optional. ``None`` means "not measured", not "false".
    """
    instruction_size: int | None = None       # chars in the workspace instruction file
    fk_count_on_gold: int | None = None       # FK constraints on the primary gold schema
    uses_relative_prompt: bool | None = None  # last prompt had "it/that" without @entity
    scans_bronze_raw: bool | None = None      # gold queries resolve to raw bronze tables
    label: str = ""                           # for logs/diffs, never read by the harness


# --- Outcome types -------------------------------------------------------

class Status(str, Enum):
    """String-valued so ``status == "DIAGNOSED"`` works in logs and JSON."""
    UNMEASURED = "UNMEASURED"        # cannot tell whether the symptom is present
    NOT_PRESENT = "NOT_PRESENT"      # symptom reproduced as absent
    UNKNOWN_CAUSE = "UNKNOWN_CAUSE"  # symptom present, no known root cause matched
    DIAGNOSED = "DIAGNOSED"          # symptom present, cause(s) matched, fix verified
    FIX_UNVERIFIED = "FIX_UNVERIFIED"  # cause matched, but fix did NOT eliminate symptom
    ERROR = "ERROR"                  # a predicate raised


@dataclass(frozen=True)
class RootCause:
    """One named explanation, with a predicate that confirms it."""
    name: str
    confirm: Callable[[WorkspaceState], bool]


@dataclass(frozen=True)
class Diagnostic:
    """Everything needed to diagnose and verify one symptom code.

    Attributes
    ----------
    symptom_code : str
        Short identifier (same codes as the original fixes dict).
    symptom_description : str
        Human-readable description of the symptom.
    reproduce : Callable[[WorkspaceState], tuple[bool | None, str]]
        Returns ``(present, evidence)``. ``present`` is ``None`` when the
        symptom cannot be measured; ``evidence`` is a human-readable
        sentence shown in the report (and asserted by the self-tests).
    root_causes : tuple[RootCause, ...]
        Ordered; multiple may match. Ordered so output is deterministic.
    fix_description : str
        The documented fix — a human-readable sentence.
    fix_applied : Callable[[WorkspaceState], WorkspaceState]
        Returns a NEW state representing "the fix has been applied".
        The harness re-runs ``reproduce`` against this state to *prove*
        the fix works. A no-op here is what produces FIX_UNVERIFIED.
    """
    symptom_code: str
    symptom_description: str
    reproduce: Callable[[WorkspaceState], tuple[bool | None, str]]
    root_causes: tuple[RootCause, ...]
    fix_description: str
    fix_applied: Callable[[WorkspaceState], WorkspaceState]


@dataclass(frozen=True)
class DiagnosticReport:
    """Immutable result of one diagnosis. Frozen so it can be logged/diffed."""
    diagnostic: Diagnostic
    status: Status
    matched_causes: tuple[RootCause, ...]
    fix_verification: str
    reproduction_evidence: str = ""
    detail: str = ""


# --- The four known diagnostics ------------------------------------------
# Symptom codes and fix summaries preserved verbatim from the original.
# What changes: each is now backed by real predicates and a simulated fix.

def _repro_prompt_dilution(s: WorkspaceState) -> tuple[bool | None, str]:
    if s.instruction_size is None:
        return None, "instruction file size not measured"
    over = s.instruction_size - 2000
    if over > 0:
        return True, (
            f"instruction file is {s.instruction_size} chars "
            f"({over} over the 2,000-char budget); rules drift into noise"
        )
    return False, f"instruction file is {s.instruction_size} chars (within budget)"


def _repro_cartesian(s: WorkspaceState) -> tuple[bool | None, str]:
    if s.fk_count_on_gold is None:
        return None, "FK count on gold not measured"
    if s.fk_count_on_gold == 0:
        return True, (
            "no informational FK constraints on gold; Genie guesses join "
            "columns and can emit a cartesian product on any multi-table question"
        )
    return False, f"{s.fk_count_on_gold} FK(s) declared on gold"


def _repro_null_cast(s: WorkspaceState) -> tuple[bool | None, str]:
    if s.uses_relative_prompt is None:
        return None, "relative-pronoun usage not measured"
    if s.uses_relative_prompt:
        return True, (
            "recent prompt used a relative pronoun (\"it\", \"that\", \"this\") "
            "without an @entity reference; the model resolves it to an "
            "arbitrary referent from history"
        )
    return False, "no relative-pronoun misuse in recent prompts"


def _repro_bronze_scan(s: WorkspaceState) -> tuple[bool | None, str]:
    if s.scans_bronze_raw is None:
        return None, "scan target not measured"
    if s.scans_bronze_raw:
        return True, (
            "gold queries resolve to raw bronze tables; no curated view guard, "
            "no partition pruning on date predicates, full table scans"
        )
    return False, "queries resolve to curated gold views"


DIAGNOSTICS: dict[str, Diagnostic] = {
    "PROMPT_DILUTION": Diagnostic(
        symptom_code="PROMPT_DILUTION",
        symptom_description="Model ignores rules stated in the instruction file",
        reproduce=_repro_prompt_dilution,
        root_causes=(
            RootCause(
                "Instruction file exceeds the 2,000-char budget",
                lambda s: s.instruction_size is not None and s.instruction_size > 2000,
            ),
            RootCause(
                "Recipe-shaped content is inline instead of in skills",
                # Proxy: file >3000 chars implies recipes are inline (a real
                # provider would scan for heading patterns).
                lambda s: s.instruction_size is not None and s.instruction_size > 3000,
            ),
        ),
        fix_description=(
            "Prune the instruction file to ≤2,000 chars; move recipe content "
            "to skills under .assistant/skills/ and reference by name"
        ),
        fix_applied=lambda s: replace(s, instruction_size=1800),
    ),
    "CARTESIAN_EXPLOSION": Diagnostic(
        symptom_code="CARTESIAN_EXPLOSION",
        symptom_description="Query returns billions of rows or OOMs the warehouse",
        reproduce=_repro_cartesian,
        root_causes=(
            RootCause(
                "No FK constraint declared; Genie guessed the join key",
                lambda s: s.fk_count_on_gold is not None and s.fk_count_on_gold == 0,
            ),
        ),
        fix_description=(
            "Declare informational FOREIGN KEY constraints on gold with RELY "
            "(see labs/05-declare-constraints.sql)"
        ),
        fix_applied=lambda s: replace(s, fk_count_on_gold=3),
    ),
    "NULL_CAST": Diagnostic(
        symptom_code="NULL_CAST",
        symptom_description="Result contains NULLs or wrong values after a multi-turn follow-up",
        reproduce=_repro_null_cast,
        root_causes=(
            RootCause(
                "Relative pronoun used without an explicit entity",
                lambda s: s.uses_relative_prompt is True,
            ),
        ),
        fix_description=(
            "Enforce stateless prompt rewriting (e.g. \"join it\" → "
            "\"join @retail_prod.gold.orders\"); enable a 3-turn reset when "
            "history exceeds ~8k tokens (see labs/02-session-reset.py)"
        ),
        fix_applied=lambda s: replace(s, uses_relative_prompt=False),
    ),
    "BRONZE_SCAN": Diagnostic(
        symptom_code="BRONZE_SCAN",
        symptom_description="Queries are slow and read raw bronze instead of curated gold",
        reproduce=_repro_bronze_scan,
        root_causes=(
            RootCause(
                "No curated gold layer; queries resolve to bronze",
                lambda s: s.scans_bronze_raw is True,
            ),
        ),
        fix_description=(
            "Publish a gold_curated schema with daily_revenue_by_customer and "
            "similar views (see labs/06-curate-gold-views.sql); annotate "
            "event_date as the partition key so queries prune"
        ),
        fix_applied=lambda s: replace(s, scans_bronze_raw=False),
    ),
}


# --- The diagnostic loop -------------------------------------------------

def diagnose(symptom_code: str, state: WorkspaceState) -> DiagnosticReport:
    """Run one symptom's diagnosis against *state*. Pure and idempotent.

    Status semantics:

      UNMEASURED      no measurement available (state field is None)
      NOT_PRESENT     reproduction succeeded and showed absence
      UNKNOWN_CAUSE   symptom reproduced but no known root cause matched
      DIAGNOSED       cause(s) matched AND fix eliminates the symptom
      FIX_UNVERIFIED  cause(s) matched BUT fix leaves the symptom in place
      ERROR           a predicate raised
    """
    diag = DIAGNOSTICS.get(symptom_code)
    if diag is None:
        raise KeyError(
            f"unknown symptom code: {symptom_code!r} "
            f"(known: {sorted(DIAGNOSTICS)})"
        )

    try:
        present, evidence = diag.reproduce(state)
    except Exception as exc:
        return DiagnosticReport(
            diagnostic=diag, status=Status.ERROR, matched_causes=(),
            fix_verification="reproduce() raised", detail=str(exc),
        )

    if present is None:
        return DiagnosticReport(
            diagnostic=diag, status=Status.UNMEASURED, matched_causes=(),
            fix_verification="n/a (symptom not measurable)",
            reproduction_evidence=evidence,
        )
    if not present:
        return DiagnosticReport(
            diagnostic=diag, status=Status.NOT_PRESENT, matched_causes=(),
            fix_verification="n/a (symptom not present)",
            reproduction_evidence=evidence,
        )

    matched = tuple(c for c in diag.root_causes if c.confirm(state))
    if not matched:
        return DiagnosticReport(
            diagnostic=diag, status=Status.UNKNOWN_CAUSE, matched_causes=(),
            fix_verification="not attempted (cause unknown)",
            reproduction_evidence=evidence,
        )

    # Verify the fix by simulating it and re-measuring the symptom.
    try:
        post_fix = diag.fix_applied(state)
        still_present, post_evidence = diag.reproduce(post_fix)
    except Exception as exc:
        return DiagnosticReport(
            diagnostic=diag, status=Status.ERROR, matched_causes=matched,
            fix_verification="fix_applied() or re-reproduction raised",
            reproduction_evidence=evidence, detail=str(exc),
        )

    if still_present or still_present is None:
        return DiagnosticReport(
            diagnostic=diag, status=Status.FIX_UNVERIFIED, matched_causes=matched,
            fix_verification="fix did NOT eliminate the symptom",
            reproduction_evidence=evidence, detail=post_evidence,
        )

    return DiagnosticReport(
        diagnostic=diag, status=Status.DIAGNOSED, matched_causes=matched,
        fix_verification=f"fix eliminates symptom ({post_evidence})",
        reproduction_evidence=evidence,
    )


def diagnose_many(
    codes: Iterable[str], state: WorkspaceState,
) -> tuple[DiagnosticReport, ...]:
    """Run several codes against one state. Order preserved."""
    return tuple(diagnose(c, state) for c in codes)


# --- Rendering ----------------------------------------------------------

def format_report(report: DiagnosticReport) -> str:
    """Render one report as a human-readable block. Pure."""
    d = report.diagnostic
    lines = [f"[{report.status.value}] {d.symptom_code}: {d.symptom_description}"]
    if report.reproduction_evidence:
        lines.append(f"  Evidence:  {report.reproduction_evidence}")
    for c in report.matched_causes:
        lines.append(f"  Cause:     {c.name}")
    if report.status in (Status.DIAGNOSED, Status.FIX_UNVERIFIED):
        lines.append(f"  Fix:       {d.fix_description}")
        lines.append(f"  Verify:    {report.fix_verification}")
    elif report.status is Status.UNKNOWN_CAUSE:
        lines.append(
            f"  Cause:     none of {len(d.root_causes)} known root cause(s) matched"
        )
    if report.detail:
        lines.append(f"  Detail:    {report.detail}")
    return "\n".join(lines)


def format_all(reports: Sequence[DiagnosticReport]) -> str:
    return "\n\n".join(format_report(r) for r in reports)


# --- Self-tests ----------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _self_test() -> int:
    print("Diagnostic suite — self-tests")
    print("=" * 62)
    ok = True

    # --- 1. Healthy state: every symptom NOT_PRESENT -----------------
    healthy = WorkspaceState(
        instruction_size=1400, fk_count_on_gold=3,
        uses_relative_prompt=False, scans_bronze_raw=False,
        label="healthy",
    )
    reports = diagnose_many(DIAGNOSTICS, healthy)
    ok &= _check(
        all(r.status is Status.NOT_PRESENT for r in reports),
        f"healthy → expected all NOT_PRESENT, got {[r.status.value for r in reports]}",
    )
    print("  ✓ healthy state → all four NOT_PRESENT")

    # --- 2. Each symptom in isolation → DIAGNOSED with verified fix --
    cases = {
        "PROMPT_DILUTION": WorkspaceState(instruction_size=4000),
        "CARTESIAN_EXPLOSION": WorkspaceState(fk_count_on_gold=0),
        "NULL_CAST": WorkspaceState(uses_relative_prompt=True),
        "BRONZE_SCAN": WorkspaceState(scans_bronze_raw=True),
    }
    for code, state in cases.items():
        r = diagnose(code, state)
        ok &= _check(
            r.status is Status.DIAGNOSED,
            f"{code}: expected DIAGNOSED, got {r.status.value}",
        )
        ok &= _check(
            "eliminates" in r.fix_verification.lower(),
            f"{code}: fix verification should say 'eliminates'",
        )
    print("  ✓ each broken symptom: reproduced → diagnosed → fix verified")

    # --- 3. Unmeasured state → UNMEASURED (does NOT guess absence) ----
    blank = WorkspaceState()
    r = diagnose("PROMPT_DILUTION", blank)
    ok &= _check(
        r.status is Status.UNMEASURED,
        f"unmeasured PROMPT_DILUTION → expected UNMEASURED, got {r.status.value}",
    )
    print("  ✓ unmeasured state → UNMEASURED (never NOT_PRESENT by accident)")

    # --- 4. Symptom present but no cause → UNKNOWN_CAUSE --------------
    # Simulate a downstream diagnostic where we know the symptom is
    # present, but the cause isn't in our known list.
    ghost = Diagnostic(
        symptom_code="GHOST",
        symptom_description="Symptom with no known cause",
        reproduce=lambda s: (True, "symptom is present"),
        root_causes=(RootCause("Cause that never matches", lambda s: False),),
        fix_description="none",
        fix_applied=lambda s: s,
    )
    DIAGNOSTICS["GHOST"] = ghost
    try:
        r = diagnose("GHOST", WorkspaceState())
        ok &= _check(
            r.status is Status.UNKNOWN_CAUSE,
            f"ghost cause → expected UNKNOWN_CAUSE, got {r.status.value}",
        )
        print("  ✓ present symptom with no known cause → UNKNOWN_CAUSE")
    finally:
        del DIAGNOSTICS["GHOST"]

    # --- 5. The most important case: a fix that doesn't resolve ------
    # A diagnostic whose fix_applied is a no-op: the harness MUST report
    # FIX_UNVERIFIED, not DIAGNOSED. This is the whole reason the file
    # was rewritten — the original never verified anything, so it could
    # not possibly catch this case.
    broken_fix = Diagnostic(
        symptom_code="BROKEN_FIX",
        symptom_description="Symptom with a fix that doesn't actually fix it",
        reproduce=lambda s: (True, "symptom is present"),
        root_causes=(RootCause("Known cause", lambda s: True),),
        fix_description="documented fix that does not change the state",
        fix_applied=lambda s: s,   # no-op — the fix lies
    )
    DIAGNOSTICS["BROKEN_FIX"] = broken_fix
    try:
        r = diagnose("BROKEN_FIX", WorkspaceState())
        ok &= _check(
            r.status is Status.FIX_UNVERIFIED,
            f"no-op fix → expected FIX_UNVERIFIED, got {r.status.value}",
        )
        ok &= _check(
            "did NOT eliminate" in r.fix_verification,
            "FIX_UNVERIFIED report should explain that the symptom remained",
        )
        print("  ✓ a fix that doesn't resolve the symptom → FIX_UNVERIFIED")
    finally:
        del DIAGNOSTICS["BROKEN_FIX"]

    # --- 6. Multiple root causes match simultaneously ----------------
    # PROMPT_DILUTION has two causes; at 4000 chars both should match.
    r = diagnose("PROMPT_DILUTION", WorkspaceState(instruction_size=4000))
    ok &= _check(
        len(r.matched_causes) == 2,
        f"4000-char file should match both causes, got {len(r.matched_causes)}",
    )
    print("  ✓ multiple causes matched together (4000-char file → 2 causes)")

    # --- 7. Idempotency & immutability -------------------------------
    a = diagnose("CARTESIAN_EXPLOSION", WorkspaceState(fk_count_on_gold=0))
    b = diagnose("CARTESIAN_EXPLOSION", WorkspaceState(fk_count_on_gold=0))
    ok &= _check(a == b, "repeated diagnosis must be equal")
    try:
        a.status = Status.NOT_PRESENT  # type: ignore[misc]
        ok &= _check(False, "DiagnosticReport must be frozen")
    except Exception:
        pass
    print("  ✓ idempotent & frozen")

    # --- 8. Unknown symptom code raises (not a silent 'Unknown') -----
    try:
        diagnose("NONEXISTENT_CODE", healthy)
        ok &= _check(False, "unknown code should raise KeyError")
    except KeyError:
        pass
    print("  ✓ unknown code raises KeyError (original returned a string)")

    if ok:
        print("\nAll self-tests PASSED.")
        return 0
    print("\nSelf-tests FAILED.", file=sys.stderr)
    return 1


# --- Demo --------------------------------------------------------------

_BROKEN = WorkspaceState(
    instruction_size=4200,
    fk_count_on_gold=0,
    uses_relative_prompt=True,
    scans_bronze_raw=True,
    label="degraded demo",
)


def _demo(state: WorkspaceState = _BROKEN) -> int:
    print("Diagnostic Engine — running four known symptom codes")
    print("=" * 62)
    reports = diagnose_many(DIAGNOSTICS, state)
    print(format_all(reports))
    print()
    print("  Status summary:")
    for r in reports:
        print(f"    {r.diagnostic.symptom_code:<22} {r.status.value}")
    bad = (Status.UNMEASURED, Status.UNKNOWN_CAUSE,
           Status.FIX_UNVERIFIED, Status.ERROR)
    return 0 if all(r.status not in bad for r in reports) else 1


# --- CLI -----------------------------------------------------------------

def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Diagnose known Genie Code / DBSQL failure symptoms.",
    )
    p.add_argument(
        "--code", action="append",
        help="symptom code to diagnose (repeatable); default: all known codes",
    )
    p.add_argument(
        "--healthy", action="store_true",
        help="use a healthy demo state instead of the degraded one",
    )
    p.add_argument("--self-test", action="store_true")
    return p.parse_args(argv)


def _main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_test:
        return _self_test()

    state = WorkspaceState(
        instruction_size=1400, fk_count_on_gold=3,
        uses_relative_prompt=False, scans_bronze_raw=False,
        label="healthy demo",
    ) if args.healthy else _BROKEN

    codes = args.code or list(DIAGNOSTICS)
    for code in codes:
        if code not in DIAGNOSTICS:
            print(f"error: unknown symptom code {code!r}", file=sys.stderr)
            print(f"known: {sorted(DIAGNOSTICS)}", file=sys.stderr)
            return 2

    reports = diagnose_many(codes, state)
    print(format_all(reports))
    bad = (Status.UNMEASURED, Status.UNKNOWN_CAUSE,
           Status.FIX_UNVERIFIED, Status.ERROR)
    return 0 if all(r.status not in bad for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
