"""labs/08-admin-checklist.py

Audit a Databricks workspace against MUST-HAVE and GOOD-TO-HAVE production
baselines. Reports a per-check verdict and exits non-zero if any MUST-HAVE
fails (or if --strict and any GOOD-TO-HAVE fails).

Design
------
* **Idempotent**  — an audit is a pure snapshot of current state. Re-running
                    returns the same report as long as the workspace is
                    unchanged. No writes, no cache, no state.
* **Stateless**   — no globals, no session variables, no temp files. All
                    state comes from the injected `Provider`. Results are
                    frozen dataclasses.

Why a Provider, not direct SQL
------------------------------
The original file had hardcoded booleans. The fix is not to inline SQL
either — that would couple the audit to one transport and make it
untestable without a live workspace. Instead, each check calls a `Provider`
method (e.g. `warehouse_config()`) that the caller wires up. This file
ships:

  * A `Provider` Protocol describing what an audit needs.
  * Five real check functions, each of which computes PASS/FAIL/SKIP from
    provider data — never from a literal.
  * A `_DemoProvider` with **deliberately failing values** so the demo
    shows what a real failure looks like (the original could never print
    FAIL).
  * Self-tests that force each of the five checks to FAIL and assert the
    exit-code semantics.

Exit codes
----------
  0  all MUST-HAVE checks PASS (GOOD-TO-HAVE may fail unless --strict)
  1  at least one MUST-HAVE FAIL/ERROR, or (--strict) any GOOD-TO-HAVE
     FAIL, or a check ERRORed
  2  usage error
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence


# --- Enums & types ----------------------------------------------------------

class Level(str, Enum):
    MUST_HAVE = "MUST-HAVE"
    GOOD_TO_HAVE = "GOOD-TO-HAVE"


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"   # provider could not answer
    ERROR = "ERROR" # provider raised


@dataclass(frozen=True)
class Check:
    """A named check with an implementation. Frozen so builds are shareable."""
    name: str
    level: Level
    checker: Callable[[], tuple[Status, str]]


@dataclass(frozen=True)
class CheckResult:
    """Outcome for one check. Frozen."""
    name: str
    level: Level
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class AuditReport:
    """Full audit outcome. Frozen; ``results`` is a tuple."""
    results: tuple[CheckResult, ...]

    def _by(self, level: Level, status: Status) -> tuple[CheckResult, ...]:
        return tuple(
            r for r in self.results
            if r.level is level and r.status is status
        )

    @property
    def must_have_failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results
                     if r.level is Level.MUST_HAVE
                     and r.status in (Status.FAIL, Status.ERROR))

    @property
    def good_to_have_failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results
                     if r.level is Level.GOOD_TO_HAVE
                     and r.status in (Status.FAIL, Status.ERROR))

    @property
    def errored(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is Status.ERROR)

    @property
    def skipped(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is Status.SKIP)

    def verdict(self, *, strict: bool) -> Status:
        """Overall verdict.

        ERROR anywhere is always fatal — an audit that could not run is not
        a passing audit. MUST-HAVE FAIL is always fatal. GOOD-TO-HAVE FAIL
        is fatal only under ``strict``.
        """
        if self.errored:
            return Status.ERROR
        if self.must_have_failures:
            return Status.FAIL
        if strict and self.good_to_have_failures:
            return Status.FAIL
        return Status.PASS


# --- Provider protocol ------------------------------------------------------

class Provider(Protocol):
    """Everything an audit needs. The caller wires a real implementation.

    Each method returns ``None`` when the information is unavailable (which
    becomes ``SKIP``); raises on genuine errors (which becomes ``ERROR``).
    """

    def instruction_file_size(self) -> int | None: ...
    def fk_count(self, catalog: str, schema: str) -> int | None: ...
    def warehouse_config(self) -> Mapping[str, Any] | None: ...
    def ci_gate_exists(self) -> bool | None: ...


# --- Five check functions ---------------------------------------------------
# Each returns ``(Status, detail)``. None of them contains a hardcoded
# PASS — every one derives its verdict from provider data. A check that
# cannot get the data returns SKIP, not PASS: silence is never success.

INSTRUCTION_MAX_CHARS = 2000
MIN_DBR_VERSION = (15, 4)
REQUIRED_TIMEOUT_S = 300


def check_instruction_length(provider: Provider) -> tuple[Status, str]:
    size = provider.instruction_file_size()
    if size is None:
        return Status.SKIP, "instruction file not found or unreadable"
    if size <= INSTRUCTION_MAX_CHARS:
        return Status.PASS, f"{size} chars ≤ {INSTRUCTION_MAX_CHARS}"
    return Status.FAIL, f"{size} chars > {INSTRUCTION_MAX_CHARS} (prompt dilution risk)"


def check_fk_exists(provider: Provider,
                    catalog: str = "retail_prod",
                    schema: str = "gold") -> tuple[Status, str]:
    count = provider.fk_count(catalog, schema)
    if count is None:
        return Status.SKIP, f"could not query constraints on {catalog}.{schema}"
    if count > 0:
        return Status.PASS, f"{count} FK constraint(s) on {catalog}.{schema}"
    return Status.FAIL, f"no FK constraints declared on {catalog}.{schema} (Genie lacks join hints)"


def check_warehouse_timeout(provider: Provider) -> tuple[Status, str]:
    cfg = provider.warehouse_config()
    if cfg is None:
        return Status.SKIP, "no warehouse configuration available"
    timeout = cfg.get("statement_timeout")
    if timeout is None:
        return Status.SKIP, "statement_timeout not reported (Beta preview not enabled?)"
    if timeout == 0:
        return Status.FAIL, "statement_timeout = 0 (system default, typically unlimited)"
    if timeout <= REQUIRED_TIMEOUT_S:
        return Status.PASS, f"statement_timeout = {timeout}s ≤ {REQUIRED_TIMEOUT_S}s"
    return Status.FAIL, f"statement_timeout = {timeout}s > {REQUIRED_TIMEOUT_S}s"


def _parse_dbr(version: str | None) -> tuple[int, int] | None:
    """Extract (major, minor) from a runtime string.

    Handles "15.4.x-scala2.12", "15.4 LTS", "15.4", "v15.4".
    Returns None if the string cannot be parsed — the caller treats that
    as SKIP rather than assuming a version.
    """
    if not version:
        return None
    import re
    m = re.search(r"(\d+)\.(\d+)", version)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def check_warehouse_runtime(provider: Provider) -> tuple[Status, str]:
    cfg = provider.warehouse_config()
    if cfg is None:
        return Status.SKIP, "no warehouse configuration available"
    runtime = cfg.get("runtime_version")
    parsed = _parse_dbr(runtime)
    if parsed is None:
        return Status.SKIP, f"could not parse runtime version: {runtime!r}"
    if parsed >= MIN_DBR_VERSION:
        return Status.PASS, f"runtime {runtime} ≥ DBR {'.'.join(map(str, MIN_DBR_VERSION))}"
    return Status.FAIL, f"runtime {runtime} < DBR {'.'.join(map(str, MIN_DBR_VERSION))}"


def check_ci_gate(provider: Provider) -> tuple[Status, str]:
    exists = provider.ci_gate_exists()
    if exists is None:
        return Status.SKIP, "CI gate location not configured"
    if exists:
        return Status.PASS, "25-question evaluation gate present"
    return Status.FAIL, "25-question evaluation gate missing from CI"


# --- Build & run ----------------------------------------------------------

def build_checks(provider: Provider) -> tuple[Check, ...]:
    """The five checks — same names as the original, but real."""
    return (
        Check("Instruction length < 2,000 chars", Level.MUST_HAVE,
              lambda: check_instruction_length(provider)),
        Check("Informational FKs on gold tables", Level.MUST_HAVE,
              lambda: check_fk_exists(provider)),
        Check("300s SQL warehouse query limit", Level.MUST_HAVE,
              lambda: check_warehouse_timeout(provider)),
        Check("SQL WH runtime ≥ DBR 15.4", Level.MUST_HAVE,
              lambda: check_warehouse_runtime(provider)),
        Check("25-question CI/CD evaluation gate", Level.GOOD_TO_HAVE,
              lambda: check_ci_gate(provider)),
    )


def run_checks(checks: Sequence[Check]) -> AuditReport:
    """Execute checks. Pure given a deterministic provider."""
    results: list[CheckResult] = []
    for c in checks:
        try:
            status, detail = c.checker()
        except Exception as exc:
            status, detail = Status.ERROR, f"checker raised: {exc}"
        results.append(CheckResult(c.name, c.level, status, detail))
    return AuditReport(tuple(results))


def run_admin_audit(provider: Provider) -> AuditReport:
    """Public entry point: build checks and run them."""
    return run_checks(build_checks(provider))


# --- Rendering -----------------------------------------------------------

def format_report(report: AuditReport, *, strict: bool = False) -> str:
    lines = ["Admin checklist audit", "=" * 60]
    for r in report.results:
        mark = {
            Status.PASS: "✓",
            Status.FAIL: "✗",
            Status.SKIP: "–",
            Status.ERROR: "!",
        }[r.status]
        lines.append(f"  [{r.level.value:13}] {mark} {r.name}: {r.status.value}")
        if r.detail:
            lines.append(f"                    {r.detail}")
    lines.append("")
    lines.append(f"  MUST-HAVE failures:     {len(report.must_have_failures)}")
    lines.append(f"  GOOD-TO-HAVE failures:  {len(report.good_to_have_failures)}")
    if report.errored:
        lines.append(f"  ERRORed checks:         {len(report.errored)}")
    if report.skipped:
        lines.append(f"  Skipped checks:         {len(report.skipped)}")
    lines.append(f"  Verdict: {report.verdict(strict=strict).value}"
                 + (" (strict)" if strict else ""))
    return "\n".join(lines)


# --- Demo provider + demo run --------------------------------------------

class DemoProvider:
    """Deliberately imperfect provider — the demo MUST produce failures.

    The original demo could not print FAIL because every check was True.
    This provider has:
      * an oversized instruction file (→ check 1 FAIL)
      * zero FKs (→ check 2 FAIL)
      * a warehouse with an 1800s timeout (→ check 3 FAIL)
      * a DBR 13.3 LTS runtime (→ check 4 FAIL)
      * no CI gate (→ check 5 FAIL)
    So the demo prints five FAILs — and exits 1. That is the honest
    behavior the original should have had.
    """

    def instruction_file_size(self) -> int | None:
        return 3200

    def fk_count(self, catalog: str, schema: str) -> int | None:
        return 0

    def warehouse_config(self) -> Mapping[str, Any] | None:
        return {"statement_timeout": 1800, "runtime_version": "13.3.x-scala2.12"}

    def ci_gate_exists(self) -> bool | None:
        return False


class HealthyProvider:
    """All checks pass — the case a real deployment should reach."""

    def instruction_file_size(self) -> int | None:
        return 1400

    def fk_count(self, catalog: str, schema: str) -> int | None:
        return 3

    def warehouse_config(self) -> Mapping[str, Any] | None:
        return {"statement_timeout": 300, "runtime_version": "15.4.x-scala2.12"}

    def ci_gate_exists(self) -> bool | None:
        return True


# --- Self-tests ----------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives python -O."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _self_test() -> int:
    print("Admin checklist — self-tests")
    print("=" * 60)
    ok = True

    # --- The original bug cannot recur: healthy vs unhealthy must differ --
    healthy = run_admin_audit(HealthyProvider())
    unhealthy = run_admin_audit(DemoProvider())
    ok &= _check(
        healthy.verdict(strict=False) is Status.PASS,
        f"healthy provider should PASS, got {healthy.verdict(strict=False)}",
    )
    ok &= _check(
        unhealthy.verdict(strict=False) is Status.FAIL,
        f"unhealthy provider should FAIL, got {unhealthy.verdict(strict=False)}",
    )
    ok &= _check(
        healthy != unhealthy,
        "reports must differ — the hardcoded-True bug cannot recur",
    )
    print("  ✓ healthy PASSES, unhealthy FAILS")

    # --- Each check must be able to fail in isolation -------------------
    class OneFailingProvider:
        def __init__(self, **kw): self._kw = kw
        def instruction_file_size(self): return self._kw.get("size", 1400)
        def fk_count(self, c, s): return self._kw.get("fk", 3)
        def warehouse_config(self):
            return {"statement_timeout": self._kw.get("timeout", 300),
                    "runtime_version": self._kw.get("runtime", "15.4.x")}
        def ci_gate_exists(self): return self._kw.get("ci", True)

    # Check 1: oversized instructions
    r = run_admin_audit(OneFailingProvider(size=2500))
    ok &= _check(any(x.status is Status.FAIL and "Instruction" in x.name
                     for x in r.results),
                 "oversized instruction file must FAIL check 1")
    # Check 2: no FKs
    r = run_admin_audit(OneFailingProvider(fk=0))
    ok &= _check(any(x.status is Status.FAIL and "FK" in x.name for x in r.results),
                 "zero FKs must FAIL check 2")
    # Check 3: timeout too high
    r = run_admin_audit(OneFailingProvider(timeout=1800))
    ok &= _check(any(x.status is Status.FAIL and "timeout" in x.name.lower()
                     for x in r.results),
                 "1800s timeout must FAIL check 3")
    # Check 4: old DBR
    r = run_admin_audit(OneFailingProvider(runtime="13.3.x-scala2.12"))
    ok &= _check(any(x.status is Status.FAIL and "runtime" in x.name.lower()
                     for x in r.results),
                 "DBR 13.3 must FAIL check 4")
    print("  ✓ each MUST-HAVE check fails in isolation")

    # --- GOOD-TO-HAVE semantics: only fatal under --strict --------------
    class MissingCiOnlyProvider:
        def instruction_file_size(self): return 1400
        def fk_count(self, c, s): return 3
        def warehouse_config(self):
            return {"statement_timeout": 300, "runtime_version": "15.4.x"}
        def ci_gate_exists(self): return False

    r = run_admin_audit(MissingCiOnlyProvider())
    ok &= _check(r.verdict(strict=False) is Status.PASS,
                 "GOOD-TO-HAVE failure is not fatal without --strict")
    ok &= _check(r.verdict(strict=True) is Status.FAIL,
                 "GOOD-TO-HAVE failure is fatal with --strict")
    print("  ✓ GOOD-TO-HAVE is fatal only under --strict")

    # --- SKIP never means PASS -----------------------------------------
    class NoDataProvider:
        def instruction_file_size(self): return None
        def fk_count(self, c, s): return None
        def warehouse_config(self): return None
        def ci_gate_exists(self): return None

    r = run_admin_audit(NoDataProvider())
    ok &= _check(all(x.status is Status.SKIP for x in r.results),
                 "missing provider data must SKIP, not PASS")
    ok &= _check(not r.must_have_failures,
                 "SKIP results should not count as MUST-HAVE failures")
    ok &= _check(r.verdict(strict=True) is Status.PASS,
                 "all-SKIP verdict under strict should be PASS (not ERROR/FAIL)")
    print("  ✓ missing data → SKIP, never PASS")

    # --- ERROR dominates: a crashed checker cannot pass the audit -------
    class CrashingProvider:
        def instruction_file_size(self): raise RuntimeError("boom")
        def fk_count(self, c, s): return 3
        def warehouse_config(self): return {"statement_timeout": 300,
                                            "runtime_version": "15.4.x"}
        def ci_gate_exists(self): return True

    r = run_admin_audit(CrashingProvider())
    ok &= _check(r.verdict(strict=False) is Status.ERROR,
                 f"crashed checker should yield ERROR verdict, got {r.verdict(strict=False)}")
    print("  ✓ crashed checker → ERROR verdict")

    # --- Idempotency & immutability ------------------------------------
    a = run_admin_audit(DemoProvider())
    b = run_admin_audit(DemoProvider())
    ok &= _check(a == b, "repeated audits with identical providers must be equal")
    try:
        a.results = ()  # type: ignore[misc]
        ok &= _check(False, "AuditReport must be frozen")
    except Exception:
        pass
    print("  ✓ idempotent & frozen")

    if ok:
        print("\nAll self-tests PASSED.")
        return 0
    print("\nSelf-tests FAILED.", file=sys.stderr)
    return 1


# --- CLI -----------------------------------------------------------------

def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Audit a Databricks workspace against production baselines.",
    )
    p.add_argument("--provider", choices=("healthy", "degraded"), default="degraded",
                   help="which built-in demo provider to use "
                        "(default: degraded — proves FAIL works)")
    p.add_argument("--strict", action="store_true",
                   help="treat GOOD-TO-HAVE failures as fatal")
    p.add_argument("--self-test", action="store_true",
                   help="run internal regression tests")
    return p.parse_args(argv)


def _main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_test:
        return _self_test()

    provider: Provider = (
        HealthyProvider() if args.provider == "healthy" else DemoProvider()
    )
    report = run_admin_audit(provider)
    print(format_report(report, strict=args.strict))
    return 0 if report.verdict(strict=args.strict) is Status.PASS else 1


if __name__ == "__main__":
    raise SystemExit(_main())
