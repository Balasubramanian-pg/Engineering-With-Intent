#!/usr/bin/env python3
"""Demonstrate token-buffer restoration on explicit session reset.

Provides two complementary, pure operations:

  * ``apply_reset(state) -> SessionState``  — *transitions* to a fresh state
    (returns a new frozen object; mutates nothing).
  * ``preview_reset(state) -> ResetReport`` — *reports* what a reset would
    do without performing it.

Both are pure functions of their inputs. Neither reads globals, writes files,
or touches the caller's data. Threading state through ``apply_reset`` is
equivalent to any other reducer pattern (Redux, Elm, Elixir) — easy to
replay, test, and parallelize.

Why a functional transition instead of ``list.clear()``?
--------------------------------------------------------
The original ``simulate_reset`` called ``turn_history.clear()``, which:

  1. **Destroyed caller data** — the caller's list was silently emptied.
  2. **Broke idempotency** — a second call saw ``[]`` and printed different
     "Before" numbers.
  3. **Made state a shared mutable singleton** — impossible to replay, diff,
     or verify.

A frozen ``SessionState`` + ``apply_reset`` returning a new state fixes all
three: the old state remains valid (useful for audit logs), the transition
is explicit, and repeating it always yields the same result.

Exit codes (script mode)
------------------------
  0  self-checks passed
  1  self-checks failed
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from typing import Sequence


# --- Immutable state & config ----------------------------------------------

@dataclass(frozen=True)
class SessionConfig:
    """Validated context-window parameters. Same shape as 02-context-cliff.py."""
    max_tokens: int = 16000
    reserved_for_response: int = 2000
    schema_tokens: int = 3500

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be > 0 (got {self.max_tokens})")
        if not (0 <= self.reserved_for_response < self.max_tokens):
            raise ValueError(
                f"reserved_for_response must be in [0, max_tokens) "
                f"(got {self.reserved_for_response} with max_tokens={self.max_tokens})"
            )
        if self.schema_tokens < 0:
            raise ValueError(f"schema_tokens must be >= 0 (got {self.schema_tokens})")


@dataclass(frozen=True)
class SessionState:
    """Immutable snapshot of a session's accumulated history.

    ``history`` is a *tuple*, not a list — immutable by construction, so
    nothing downstream can accidentally mutate it. To "add a turn," create
    a new state via :func:`add_turn` (or ``dataclasses.replace``).
    """
    history: tuple[int, ...] = ()
    config: SessionConfig = SessionConfig()

    @property
    def history_tokens(self) -> int:
        return sum(self.history)

    @property
    def free_budget(self) -> int:
        return max(
            0,
            self.config.max_tokens - self.history_tokens - self.config.reserved_for_response,
        )

    @property
    def schema_retention(self) -> float:
        if self.config.schema_tokens == 0:
            return 1.0
        return min(1.0, self.free_budget / self.config.schema_tokens)


@dataclass(frozen=True)
class ResetReport:
    """Preview of a reset. Immutable; safe to store, compare, or log."""
    before: SessionState
    after: SessionState
    restored_tokens: int
    retention_gain: float

    @property
    def was_already_healthy(self) -> bool:
        return self.before.schema_retention == 1.0

    @property
    def rescued_schema(self) -> bool:
        return not self.was_already_healthy and self.after.schema_retention == 1.0


# --- Pure operations -------------------------------------------------------

def add_turn(state: SessionState, turn_tokens: int) -> SessionState:
    """Return a *new* state with one turn appended. Pure."""
    if turn_tokens < 0:
        raise ValueError(f"turn_tokens must be >= 0 (got {turn_tokens})")
    return replace(state, history=state.history + (turn_tokens,))


def preview_reset(state: SessionState) -> ResetReport:
    """Report what a reset would do. Does not modify *state*.

    Pure and idempotent: calling twice on the same state returns equal
    reports and leaves the state unchanged.
    """
    fresh = SessionState(config=state.config)
    return ResetReport(
        before=state,
        after=fresh,
        restored_tokens=fresh.free_budget - state.free_budget,
        retention_gain=fresh.schema_retention - state.schema_retention,
    )


def apply_reset(state: SessionState) -> SessionState:
    """Return a *new* state with history dropped. Does not modify *state*.

    This is the explicit state transition. The original state remains
    valid (useful for audit logs and for A/B replay: ``pre_reset`` and
    ``post_reset`` coexist).

    Pure and idempotent: applying twice is equivalent to applying once,
    because an empty state re-resets to the same empty state.
    """
    return SessionState(config=state.config)


# --- Rendering -------------------------------------------------------------

def format_report(report: ResetReport) -> str:
    b, a = report.before, report.after
    lines = [
        f"  Before reset: {len(b.history)} turn(s), {b.history_tokens} tok history, "
        f"free={b.free_budget}, schema_retention={b.schema_retention * 100:.1f}%",
        f"  After  reset: {len(a.history)} turn(s), {a.history_tokens} tok history, "
        f"free={a.free_budget}, schema_retention={a.schema_retention * 100:.1f}%",
        f"  Delta:        +{report.restored_tokens} free tok, "
        f"+{report.retention_gain * 100:.1f}pp retention",
    ]
    if report.was_already_healthy:
        lines.append("  Verdict:      already healthy — reset restored headroom only.")
    elif report.rescued_schema:
        lines.append("  Verdict:      RESCUE — schema restored to 100%.")
    else:
        lines.append("  Verdict:      partial recovery — schema still degraded after reset.")
    return "\n".join(lines)


# --- Self-checks -----------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O`` (unlike stdlib ``assert``)."""
    if not condition:
        print(f"FAILED: {message}", file=sys.stderr)
        return False
    return True


def _build_state(turns: Sequence[int], config: SessionConfig | None = None) -> SessionState:
    """Fold ``turns`` into a SessionState via repeated ``add_turn`` — pure."""
    state = SessionState(config=config or SessionConfig())
    for t in turns:
        state = add_turn(state, t)
    return state


HEALTHY_HISTORY = (1200, 1850, 2400, 3100)         # sum=8550, retention=100% before
STARVED_HISTORY = (1200, 1850, 2400, 3100, 1200, 2250)  # sum=12000, retention≈57%


def _demo() -> int:
    print("Session-reset — functional state transitions")
    print("=" * 60)
    ok = True

    # Scenario A — original history was already healthy before reset.
    print("\nScenario A — original demo history (already healthy):")
    healthy_before = _build_state(HEALTHY_HISTORY)
    healthy_report = preview_reset(healthy_before)
    print(format_report(healthy_report))
    ok &= _check(healthy_report.was_already_healthy, "scenario A must start healthy")

    # Apply the reset *functionally* — the original state remains valid.
    healthy_after = apply_reset(healthy_before)
    ok &= _check(
        healthy_before.history_tokens == sum(HEALTHY_HISTORY),
        "apply_reset must not modify the original state",
    )
    ok &= _check(
        healthy_after.history == () and healthy_after.history_tokens == 0,
        "post-reset state must have empty history",
    )
    ok &= _check(
        healthy_after.config is healthy_before.config,
        "post-reset state must reuse the original config (no accidental copy)",
    )

    # Scenario B — history large enough to degrade schema.
    print("\nScenario B — starved history (schema actually degraded):")
    starved_before = _build_state(STARVED_HISTORY)
    starved_report = preview_reset(starved_before)
    print(format_report(starved_report))
    ok &= _check(not starved_report.was_already_healthy, "scenario B must start starved")
    ok &= _check(starved_report.rescued_schema, "reset must rescue schema in scenario B")

    # --- Idempotency -----------------------------------------------------
    # preview_reset twice → equal reports; input unchanged.
    ok &= _check(
        preview_reset(starved_before) == preview_reset(starved_before),
        "preview_reset must be idempotent",
    )
    # apply_reset twice → same final state.
    once = apply_reset(starved_before)
    twice = apply_reset(once)
    ok &= _check(once == twice, "apply_reset must be idempotent")

    # --- Immutability ----------------------------------------------------
    # SessionState is frozen; assignment must raise.
    try:
        starved_before.history = ()  # type: ignore[misc]
        ok &= _check(False, "SessionState.history must be read-only")
    except Exception:
        pass  # expected: FrozenInstanceError / AttributeError

    # --- Edge cases ------------------------------------------------------
    # Empty state: reset is a no-op with zero delta.
    empty_report = preview_reset(SessionState())
    ok &= _check(
        empty_report.restored_tokens == 0 and empty_report.before == empty_report.after,
        "empty-state reset must have zero delta",
    )
    # History exceeds window: restored tokens cap at max - reserved.
    huge = _build_state((50000,))
    huge_report = preview_reset(huge)
    ok &= _check(
        huge_report.restored_tokens == huge.config.max_tokens - huge.config.reserved_for_response,
        f"restored_tokens must cap at max-reserved (got {huge_report.restored_tokens})",
    )
    ok &= _check(huge.free_budget == 0, "free_budget must clamp to 0 when over window")

    # --- Config validation -----------------------------------------------
    for kwargs in (
        dict(max_tokens=0),
        dict(max_tokens=100, reserved_for_response=100),
        dict(reserved_for_response=-1),
        dict(schema_tokens=-1),
    ):
        try:
            SessionConfig(**kwargs)
        except ValueError:
            pass
        else:
            ok &= _check(False, f"SessionConfig({kwargs}) should raise ValueError")

    if ok:
        print("\nAll self-checks PASSED.")
        return 0
    print("\nSelf-checks FAILED.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
