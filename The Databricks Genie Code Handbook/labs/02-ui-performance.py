#!/usr/bin/env python3
"""Closed-form estimate of DOM memory overhead: inline diffs vs side panel.

╔══════════════════════════════════════════════════════════════════════╗
║  THIS IS A MODEL, NOT A MEASUREMENT.                                 ║
║  It computes ``450 + 39·cells`` versus ``580``. There is no DOM, no   ║
║  browser, no worker, and no timing. The constants are PLACEHOLDERS.   ║
║  Replace them with numbers from Chrome DevTools → Memory (heap        ║
║  snapshot) before quoting any output from this script.                ║
╚══════════════════════════════════════════════════════════════════════╝

Model
-----
    inline_mb(cells) = inline_base + cells · inline_per_cell
    side_panel_mb    = side_panel_fixed        (constant, cell-count independent)

Crossover — the first cell count at which the side panel is cheaper — is::

    cells* = (side_panel_fixed - inline_base) / inline_per_cell

With the default placeholder constants that is ``(580-450)/39 ≈ 3.33``, so
the side panel first wins at **4 cells**. The original demo ran cells
10→50, i.e. it only ever showed cases where inline had *already* lost.

What this model does NOT capture
--------------------------------
* **Main-thread cost.** The reason to move diffs to a worker is that
  rendering 50 inline diffs blocks the UI thread; a worker doesn't. That
  is a *latency* property, invisible to a memory model. If the decision is
  "responsiveness vs memory," this table is half the picture.
* **Per-cell side-panel content.** Real side panels still hold diff text
  for the visible cell; memory is not truly constant. The fixed 580 is an
  assumption, documented here so it can be challenged.
* **GC behaviour, DOM node counts, string interning, framework overhead.**
  All real, none modelled.

Guarantees
----------
* **Idempotent** — :func:`estimate_overhead` is pure; same inputs, same
  output, no state.
* **Stateless** — no globals, no caches. Config and results are frozen
  dataclasses; the model constants are injectable.

Exit codes (script mode)
------------------------
  0  self-checks passed
  1  self-checks failed
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass


# --- Configurable model constants ------------------------------------------

@dataclass(frozen=True)
class UIModelConfig:
    """Placeholder constants for the memory model. Override with real data.

    All values are megabytes. Validation happens at construction so a
    nonsense config fails loudly rather than producing a nonsense table.

    Defaults are *illustrative*, not measured. They are chosen so the
    crossover lands in a plausible region (~4 cells) and the demo is
    non-degenerate; they have no provenance beyond that.
    """
    inline_base_mb: float = 450.0       # fixed inline-renderer overhead
    inline_per_cell_mb: float = 39.0    # marginal cost of one inline diff
    side_panel_fixed_mb: float = 580.0  # side panel, assumed cell-independent

    def __post_init__(self) -> None:
        if self.inline_base_mb < 0 or self.side_panel_fixed_mb < 0:
            raise ValueError("base/fixed memory costs must be >= 0")
        if self.inline_per_cell_mb <= 0:
            # A non-positive marginal cost makes the crossover undefined.
            raise ValueError(
                f"inline_per_cell_mb must be > 0 (got {self.inline_per_cell_mb})"
            )


DEFAULT_CONFIG = UIModelConfig()


# --- Result types ----------------------------------------------------------

@dataclass(frozen=True)
class OverheadEstimate:
    """Estimated memory for both options at one cell count. Immutable."""
    num_cells: int
    inline_mb: float
    side_panel_mb: float

    @property
    def delta_mb(self) -> float:
        """Inline minus side-panel; negative means inline is cheaper."""
        return self.inline_mb - self.side_panel_mb

    @property
    def winner(self) -> str:
        if math.isclose(self.delta_mb, 0.0, abs_tol=1e-9):
            return "tie"
        return "inline" if self.delta_mb < 0 else "side-panel"

    @property
    def verdict(self) -> str:
        if self.winner == "tie":
            return "TIE"
        # Exact equality on cell counts makes this an honest +/- comparison.
        return f"{self.winner.upper()} wins by {abs(self.delta_mb):.0f} MB"


# --- Pure model ------------------------------------------------------------

def estimate_overhead(
    num_cells: int,
    config: UIModelConfig = DEFAULT_CONFIG,
) -> OverheadEstimate:
    """Compute the memory estimate for ``num_cells`` cells. Pure.

    Raises ``ValueError`` on negative cell counts — negative cells are a
    caller bug, not a case to silently coerce.
    """
    if num_cells < 0:
        raise ValueError(f"num_cells must be >= 0 (got {num_cells})")
    return OverheadEstimate(
        num_cells=num_cells,
        inline_mb=config.inline_base_mb + num_cells * config.inline_per_cell_mb,
        side_panel_mb=config.side_panel_fixed_mb,
    )


def crossover_cells(config: UIModelConfig = DEFAULT_CONFIG) -> float:
    """Cell count at which inline == side panel (fractional). Pure.

    Returns ``inf`` if inline never catches up (impossible with a positive
    per-cell cost and finite side-panel cost, but the signature allows it
    for callers experimenting with configs).
    """
    if config.inline_per_cell_mb <= 0:
        return math.inf
    return (config.side_panel_fixed_mb - config.inline_base_mb) / config.inline_per_cell_mb


def first_side_panel_win(config: UIModelConfig = DEFAULT_CONFIG) -> int | None:
    """Smallest integer cell count where the side panel is cheaper.

    ``None`` means the side panel never wins for any non-negative cell
    count (only possible when the side panel already loses at 0 cells).
    """
    threshold = crossover_cells(config)
    if math.isinf(threshold):
        return None
    if threshold < 0:
        return 0  # side panel cheaper even with no cells
    # ``ceil`` handles the exact-integer case: if crossover is exactly 4.0,
    # inline ties at 4 and wins only at 5 — so the "first win" is 5.
    first = math.ceil(threshold)
    if math.isclose(threshold, first) and first > 0:
        first += 1
    return first


# --- Rendering -------------------------------------------------------------

def format_table(
    cell_counts: list[int],
    config: UIModelConfig = DEFAULT_CONFIG,
) -> str:
    """Render estimates + verdicts for each cell count. Pure."""
    lines = [
        "  Cells | Inline (est.) | Side-Panel (est.) | Verdict",
        "  ------+---------------+-------------------+---------------------------",
    ]
    for n in cell_counts:
        e = estimate_overhead(n, config)
        lines.append(
            f"  {n:5d} | {e.inline_mb:9.0f} MB | {e.side_panel_mb:14.0f} MB | {e.verdict}"
        )
    return "\n".join(lines)


# --- Self-checks -----------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _demo() -> int:
    cfg = DEFAULT_CONFIG
    print("UI memory model — inline diffs vs side panel")
    print("  ⚠  MODEL OUTPUT, NOT MEASUREMENT — see module docstring.")
    print("=" * 66)

    ok = True

    # Crossover math must match the placeholder constants exactly.
    cross = crossover_cells(cfg)
    ok &= _check(
        math.isclose(cross, (580 - 450) / 39, rel_tol=1e-9),
        f"crossover should be (580-450)/39, got {cross}",
    )
    first_win = first_side_panel_win(cfg)
    ok &= _check(first_win == 4, f"side panel should first win at 4 cells, got {first_win}")

    # Show the crossover explicitly, then the original demo range. The
    # original table only ever showed inline *losing*; this one brackets
    # the flip so the comparison is meaningful.
    print("\nAround the crossover (n = 2..5):")
    print(format_table([2, 3, 4, 5], cfg))

    print("\nOriginal demo range (n = 10..50):")
    print(format_table([10, 20, 35, 50], cfg))

    # At the crossover boundary: inline wins strictly below, side panel at/above.
    ok &= _check(
        estimate_overhead(3, cfg).winner == "inline",
        "inline should win at 3 cells",
    )
    ok &= _check(
        estimate_overhead(4, cfg).winner == "side-panel",
        "side panel should win at 4 cells",
    )

    # Monotonicity: inline cost grows with cell count, side panel is flat.
    costs = [estimate_overhead(n, cfg).inline_mb for n in range(0, 51)]
    ok &= _check(
        all(a <= b for a, b in zip(costs, costs[1:])),
        "inline estimate must be non-decreasing in cell count",
    )
    ok &= _check(
        len({estimate_overhead(n, cfg).side_panel_mb for n in range(0, 51)}) == 1,
        "side-panel estimate must be constant across cell counts",
    )

    # Idempotency: repeated calls return equal frozen results.
    ok &= _check(
        estimate_overhead(42, cfg) == estimate_overhead(42, cfg),
        "estimate_overhead must be idempotent",
    )

    # Immutability: frozen dataclasses reject assignment.
    try:
        estimate_overhead(1, cfg).inline_mb = 0  # type: ignore[misc]
        ok &= _check(False, "OverheadEstimate must be frozen")
    except Exception:
        pass  # expected: FrozenInstanceError

    # Config validation: bad configs fail at construction.
    for kwargs in (
        dict(inline_per_cell_mb=0),
        dict(inline_per_cell_mb=-1),
        dict(inline_base_mb=-1),
        dict(side_panel_fixed_mb=-1),
    ):
        try:
            UIModelConfig(**kwargs)
        except ValueError:
            pass
        else:
            ok &= _check(False, f"UIModelConfig({kwargs}) should raise ValueError")

    # Negative cells is a caller bug, not silently clamped.
    try:
        estimate_overhead(-1, cfg)
        ok &= _check(False, "estimate_overhead(-1) should raise ValueError")
    except ValueError:
        pass

    # Honest footer — the whole point of the rewrite is that this table
    # answers a narrower question than its original title claimed.
    print(
        "\n  Note: this model captures MEMORY ONLY. The side panel's real\n"
        "  advantage — running diffs off the main thread — is a latency\n"
        "  property and is not represented here."
    )

    if ok:
        print("\nAll self-checks PASSED.")
        return 0
    print("\nSelf-checks FAILED.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
