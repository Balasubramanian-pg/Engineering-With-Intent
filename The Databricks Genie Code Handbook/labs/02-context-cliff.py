"""Simulate token allocation and context degradation across multi-turn chats.

Model
-----
Each turn contributes ``(prompt_chars + code_chars) // 4`` estimated tokens to the running conversation history. Given a fixed context window:

    available_for_schema = max(0, window - history - reserved_for_response)
    schema_retention     = min(1.0, available_for_schema / schema_tokens)

When ``available_for_schema`` drops below ``schema_tokens``, retention falls linearly. Below ``cliff_threshold`` we flag the turn as degraded — that is the "context cliff".

Design guarantees
-----------------
* **Idempotent** — the simulation is a pure function of (turns, config).
  Replaying the same turns yields byte-identical output; nothing accumulates
  between calls.
* **Stateless** — no session object carries history across calls. Each
  ``simulate_session`` call builds a fresh, frozen ``SessionReport`` from
  its inputs alone. This is a deliberate departure from the original
  ``NotebookSession`` class, whose ``add_turn`` mutated ``self.history``
  and made the demo non-reproducible (running twice gave different numbers
  because the session was never reset).

Assumptions / limits
--------------------
* Token estimate is the ``chars // 4`` heuristic (same as
  ``01-mental-model.py``; under-counts non-ASCII and ignores tokenizer
  details).
* ``reserved_for_response`` is a fixed budget for the model's reply and
  tool-call overhead — a guess, not a measurement.
* Retention is a *first-order* model of attention, not literal truncation.
  Real degradation is gradual and depends on attention caching, model
  family, and position of the relevant context. Use the ratio as a
  directional signal, not a precise number.

Exit codes (script mode)
------------------------
  0  self-checks passed
  1  self-checks failed
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Sequence

# Kept numerically in sync with 01-mental-model.py. Duplicated rather than
# imported so each lab script runs standalone.
CHARS_PER_TOKEN = 4


# --- Immutable value types --------------------------------------------------

@dataclass(frozen=True)
class Turn:
    """One user request + one code cell. Immutable and hashable."""
    prompt: str
    code: str


@dataclass(frozen=True)
class SimulationConfig:
    """Simulation parameters. Frozen so they can't drift between calls.

    Validation happens in ``__post_init__`` so bad configs fail loudly at
    construction time, not halfway through a simulation.
    """
    max_tokens: int = 16000
    schema_tokens: int = 3500
    reserved_for_response: int = 2000
    cliff_threshold: float = 0.7

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be > 0 (got {self.max_tokens})")
        if self.schema_tokens < 0:
            raise ValueError(f"schema_tokens must be >= 0 (got {self.schema_tokens})")
        if self.reserved_for_response < 0:
            raise ValueError(
                f"reserved_for_response must be >= 0 (got {self.reserved_for_response})"
            )
        if self.reserved_for_response >= self.max_tokens:
            raise ValueError(
                f"reserved_for_response ({self.reserved_for_response}) must be "
                f"< max_tokens ({self.max_tokens})"
            )
        if not (0.0 < self.cliff_threshold <= 1.0):
            raise ValueError(
                f"cliff_threshold must be in (0, 1] (got {self.cliff_threshold})"
            )


@dataclass(frozen=True)
class TurnReport:
    """Outcome for a single turn. Immutable so it's safe to share."""
    index: int               # 1-based turn number
    turn_tokens: int         # estimated tokens added by this turn
    cumulative_history: int  # sum of turn_tokens through this turn
    available_for_schema: int
    retention: float         # in [0, 1]; 1.0 = full schema retained
    cliff: bool              # retention < config.cliff_threshold


@dataclass(frozen=True)
class SessionReport:
    """Outcome for a full session. Immutable and composable."""
    turns: tuple[TurnReport, ...]
    config: SimulationConfig
    first_cliff_turn: int | None  # 1-based, or None if never cliffed

    @property
    def healthy(self) -> bool:
        return self.first_cliff_turn is None

    @property
    def final_history(self) -> int:
        return self.turns[-1].cumulative_history if self.turns else 0

    @property
    def final_retention(self) -> float:
        return self.turns[-1].retention if self.turns else 1.0


# --- Pure simulation --------------------------------------------------------

def estimate_turn_tokens(turn: Turn) -> int:
    """Heuristic token count for one turn.

    ``(len(prompt) + len(code)) // 4`` — same rule-of-thumb as
    ``01-mental-model.py``. Deterministic and cheap; under-counts non-ASCII
    and ignores tokenizer specifics.
    """
    return (len(turn.prompt) + len(turn.code)) // CHARS_PER_TOKEN


def simulate_session(
    turns: Sequence[Turn],
    config: SimulationConfig | None = None,
) -> SessionReport:
    """Simulate a full chat session. Pure, idempotent, thread-safe.

    Equal inputs always produce equal reports — nothing is cached or
    mutated between calls.
    """
    cfg = config or SimulationConfig()
    reports: list[TurnReport] = []
    history = 0
    first_cliff: int | None = None

    for i, turn in enumerate(turns, start=1):
        t = estimate_turn_tokens(turn)
        history += t
        available = max(0, cfg.max_tokens - history - cfg.reserved_for_response)
        # Guard divide-by-zero: schema_tokens=0 means "no schema to forget".
        retention = (
            min(1.0, available / cfg.schema_tokens) if cfg.schema_tokens else 1.0
        )
        cliff = retention < cfg.cliff_threshold
        if cliff and first_cliff is None:
            first_cliff = i
        reports.append(
            TurnReport(
                index=i,
                turn_tokens=t,
                cumulative_history=history,
                available_for_schema=available,
                retention=retention,
                cliff=cliff,
            )
        )

    return SessionReport(tuple(reports), cfg, first_cliff)


# --- Rendering & self-checks ------------------------------------------------

def format_report(report: SessionReport) -> str:
    """Render a SessionReport as a fixed-width table plus a summary line."""
    lines = [
        "Turn | Turn Tok | History | Schema Avail | Retention",
        "-----+----------+---------+--------------+-----------",
    ]
    for r in report.turns:
        marker = " (CLIFF WARNING)" if r.cliff else ""
        lines.append(
            f"{r.index:02d}   | {r.turn_tokens:8d} | {r.cumulative_history:7d} | "
            f"{r.available_for_schema:12d} | {r.retention * 100:6.1f}%{marker}"
        )
    verdict = (
        "HEALTHY" if report.healthy else f"CLIFF at turn {report.first_cliff_turn}"
    )
    lines.append("")
    lines.append(
        f"Summary: {len(report.turns)} turn(s), "
        f"final history={report.final_history} tok, "
        f"final retention={report.final_retention * 100:.1f}%, "
        f"verdict={verdict}"
    )
    return "\n".join(lines)


def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"FAILED: {message}", file=sys.stderr)
        return False
    return True


# Demo turns, kept close to the original data. The only change is turn 4's
# ``date_trunc`` — the original used double quotes (``"month"``), which
# Databricks SQL parses as an identifier rather than a string literal.
# Single quotes are the correct form for the unit argument.
DEMO_TURNS: tuple[Turn, ...] = (
    Turn(
        prompt="Generate revenue query for @orders",
        code=(
            "SELECT customer_id, sum(net_amount) FROM retail_prod.gold.orders "
            "GROUP BY 1"
        ),
    ),
    Turn(
        prompt="Now add customer name from @customers",
        code=(
            "SELECT c.name, sum(o.net_amount) FROM orders o "
            "JOIN customers c USING(customer_id) GROUP BY 1"
        ),
    ),
    Turn(
        prompt="Filter for active VIP members",
        code=(
            "SELECT c.name, sum(o.net_amount) FROM orders o "
            "JOIN customers c USING(customer_id) WHERE c.vip = true GROUP BY 1"
        ),
    ),
    Turn(
        prompt="Add monthly trend and discount rates",
        code=(
            "SELECT date_trunc('month', o.order_date), avg(o.discount) "
            "FROM orders o GROUP BY 1"
        ),
    ),
)


def _demo() -> int:
    print("Context-cliff simulation")
    print("=" * 60)
    ok = True

    # Scenario A — production-realistic default window. Four short turns
    # barely dent a 16k window, so retention stays at 100%. This is the
    # honest "not every session degrades" case — the original script
    # *only* showed this, which is why the cliff never actually appeared.
    print("\nScenario A — default 16k window (4 short turns):")
    healthy = simulate_session(DEMO_TURNS, SimulationConfig())
    print(format_report(healthy))
    ok &= _check(
        healthy.healthy and healthy.first_cliff_turn is None,
        "default config with short turns must stay healthy (no cliff)",
    )
    ok &= _check(
        all(r.retention == 1.0 for r in healthy.turns),
        "default config retention should be 1.0 throughout",
    )

    # Scenario B — same turns, deliberately tight window, so the cliff
    # actually triggers within the demo. The math is identical; only the
    # constants differ. A 16k window would need ~350 turns of this size
    # to reach the cliff — too long for a demo. We scale the window down
    # and say so, rather than hide it in the numbers.
    print("\nScenario B — scaled-down 400-token window (same 4 turns):")
    tight_config = SimulationConfig(
        max_tokens=400, reserved_for_response=100, schema_tokens=260,
    )
    cliffy = simulate_session(DEMO_TURNS, tight_config)
    print(format_report(cliffy))
    ok &= _check(
        cliffy.first_cliff_turn is not None,
        "tight window must trigger the cliff",
    )
    ok &= _check(
        cliffy.first_cliff_turn == 4,
        f"cliff should first appear at turn 4 (got {cliffy.first_cliff_turn})",
    )

    # Idempotency — replay the same simulation, expect an equal report.
    replay = simulate_session(DEMO_TURNS, tight_config)
    ok &= _check(
        replay == cliffy,
        "repeating simulate_session with identical inputs must return an equal report",
    )

    # Monotonicity — retention must not rise as history accumulates.
    retentions = [r.retention for r in cliffy.turns]
    ok &= _check(
        all(x >= y for x, y in zip(retentions, retentions[1:])),
        "retention must be non-increasing as history grows",
    )

    # Edge case: empty session is trivially healthy.
    empty = simulate_session([], SimulationConfig())
    ok &= _check(
        empty.turns == () and empty.healthy and empty.final_retention == 1.0,
        "empty session must be healthy with retention 1.0",
    )

    # Edge case: schema_tokens=0 ⇒ no division by zero, always 100%.
    no_schema = simulate_session(DEMO_TURNS, SimulationConfig(schema_tokens=0))
    ok &= _check(
        all(r.retention == 1.0 for r in no_schema.turns),
        "schema_tokens=0 must yield retention=1.0 without dividing by zero",
    )

    # Config validation — bad inputs must raise at construction time.
    bad_configs = (
        dict(max_tokens=0),
        dict(max_tokens=400, reserved_for_response=400),
        dict(schema_tokens=-1),
        dict(reserved_for_response=-1),
        dict(cliff_threshold=0.0),
        dict(cliff_threshold=1.5),
    )
    for kwargs in bad_configs:
        try:
            SimulationConfig(**kwargs)
        except ValueError:
            pass
        else:
            ok &= _check(False, f"SimulationConfig({kwargs}) should raise ValueError")

    if ok:
        print("\nAll self-checks PASSED.")
        return 0
    print("\nSelf-checks FAILED.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
