#!/usr/bin/env python3
"""labs/04-engine-benchmark.py

Closed-form model of *two compute SKU choices* for a given data size:
a Serverless SQL Warehouse vs a Classic PySpark cluster.

╔══════════════════════════════════════════════════════════════════════╗
║  THIS IS A MODEL, NOT A MEASUREMENT.                                 ║
║  The constants below are placeholders, not benchmarked numbers.      ║
║  Before quoting any output, replace them with measured cold-start    ║
║  times, throughputs, and DBU rates from your actual workspace.       ║
╚══════════════════════════════════════════════════════════════════════╝

Framing correction (important)
------------------------------
The original called this an "engine benchmark" comparing ANSI SQL vs
PySpark. It is not:

  * You can run **SQL on Classic Compute** and **PySpark on Serverless**.
    The choice of *engine* and the choice of *compute SKU* are independent.
  * The ~12s vs ~140s gap in the original is dominated by **cold start**
    (Serverless ≈ 2s; Classic ≈ 60–90s of cluster spin-up), not by query
    execution.
  * The DBU/h rates (4 vs 16) differ by *cluster size*, not by engine.

So the honest question this file answers is: *"For a given data size,
which SKU is faster and cheaper?"* — not *"which engine is faster?"*.

Model
-----
    latency(sku, gb) = cold_start + gb / throughput
    dbu(sku, gb)     = (latency / 3600) * dbu_per_hour
    usd(sku, gb)     = dbu * usd_per_dbu

Two breakevens are computed:

  * **Latency crossover** — data size at which the two SKUs tie on wall
    time. Below it, the lower-cold-start SKU wins; above it, the higher-
    throughput SKU wins.
  * **Cost crossover** — data size at which they tie on dollars. Depends
    on both throughput *and* DBU rate, so it is usually a different size.

Guarantees
----------
* **Idempotent** — :func:`predict` and :func:`compare` are pure functions
  of their arguments. Same inputs → equal output, no state.
* **Stateless** — no globals, no caches, no filesystem. Results are frozen
  dataclasses; the demo runs deterministically.
* ``data_size_gb`` **is honoured** — :func:`predict` and :func:`compare`
  actually vary with it (verified by a regression test that the original
  script would have failed).

Exit codes (script mode)
------------------------
  0  self-checks passed
  1  self-checks failed
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


HOUR = 3600.0  # seconds per hour, for DBU accumulation


# --- Immutable value types --------------------------------------------------

@dataclass(frozen=True)
class ComputeProfile:
    """A compute SKU's performance model. Placeholder constants; replace.

    Attributes
    ----------
    name : str
        Human-readable SKU label.
    cold_start_s : float
        Fixed time before useful work starts (cluster spin-up, warehouse
        wake). Dominates latency for small jobs.
    throughput_gb_per_s : float
        Steady-state processing rate once warm.
    dbu_per_hour : float
        Databricks Units consumed per hour of *running* time (cold start
        included — a spinning-up cluster still bills).
    caveat : str
        Free-form note shown in the demo so readers know what to distrust.
    """
    name: str
    cold_start_s: float
    throughput_gb_per_s: float
    dbu_per_hour: float
    caveat: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ComputeProfile.name must be non-empty")
        if self.cold_start_s < 0:
            raise ValueError(
                f"{self.name}: cold_start_s must be >= 0 (got {self.cold_start_s})"
            )
        if self.throughput_gb_per_s <= 0:
            raise ValueError(
                f"{self.name}: throughput_gb_per_s must be > 0 "
                f"(got {self.throughput_gb_per_s})"
            )
        if self.dbu_per_hour <= 0:
            raise ValueError(
                f"{self.name}: dbu_per_hour must be > 0 (got {self.dbu_per_hour})"
            )


@dataclass(frozen=True)
class BenchmarkConfig:
    """Two SKUs plus a per-DBU dollar rate. Frozen; validated."""
    sql: ComputeProfile
    spark: ComputeProfile
    usd_per_dbu: float = 0.55

    def __post_init__(self) -> None:
        if self.usd_per_dbu <= 0:
            raise ValueError(
                f"usd_per_dbu must be > 0 (got {self.usd_per_dbu})"
            )


@dataclass(frozen=True)
class Outcome:
    """Predicted latency and cost for one SKU at one data size. Frozen."""
    profile: str
    latency_s: float
    dbu: float
    usd: float


@dataclass(frozen=True)
class Comparison:
    """Predicted head-to-head at one data size. Frozen."""
    data_gb: float
    sql: Outcome
    spark: Outcome

    @property
    def latency_ratio(self) -> float:
        """spark.latency / sql.latency. > 1 means SQL is faster."""
        return self.spark.latency_s / self.sql.latency_s

    @property
    def cost_ratio(self) -> float:
        """spark.usd / sql.usd. > 1 means SQL is cheaper."""
        return self.spark.usd / self.sql.usd

    @property
    def faster(self) -> str:
        if self.latency_ratio > 1.0:
            return "sql"
        if self.latency_ratio < 1.0:
            return "spark"
        return "tie"

    @property
    def cheaper(self) -> str:
        if self.cost_ratio > 1.0:
            return "sql"
        if self.cost_ratio < 1.0:
            return "spark"
        return "tie"


# --- Pure model -------------------------------------------------------------

def predict(
    profile: ComputeProfile,
    data_gb: float,
    usd_per_dbu: float,
) -> Outcome:
    """Predict latency/DBU/USD for *profile* at *data_gb*. Pure and idempotent.

    Raises ``ValueError`` on negative data size — a caller bug, not a case
    to silently coerce.
    """
    if data_gb < 0:
        raise ValueError(f"data_gb must be >= 0 (got {data_gb})")
    latency = profile.cold_start_s + data_gb / profile.throughput_gb_per_s
    dbu = (latency / HOUR) * profile.dbu_per_hour
    return Outcome(
        profile=profile.name,
        latency_s=latency,
        dbu=dbu,
        usd=dbu * usd_per_dbu,
    )


def compare(data_gb: float, config: BenchmarkConfig) -> Comparison:
    """Predict both SKUs at *data_gb*. Pure and idempotent."""
    return Comparison(
        data_gb=data_gb,
        sql=predict(config.sql, data_gb, config.usd_per_dbu),
        spark=predict(config.spark, data_gb, config.usd_per_dbu),
    )


def latency_breakeven_gb(config: BenchmarkConfig) -> float | None:
    """Data size at which latency ties, or ``None`` if no positive crossover.

    Solves ``cold_sql + d/ts_sql = cold_spark + d/ts_spark`` for *d*.
    Returns ``None`` when the SKUs have identical throughput (latency
    advantage never flips) or when the crossover would be at negative size
    (one SKU wins on both axes).
    """
    inv_sql = 1.0 / config.sql.throughput_gb_per_s
    inv_spark = 1.0 / config.spark.throughput_gb_per_s
    denom = inv_sql - inv_spark
    if abs(denom) < 1e-12:
        return None
    d = (config.spark.cold_start_s - config.sql.cold_start_s) / denom
    return d if d > 0 else None


def cost_breakeven_gb(config: BenchmarkConfig) -> float | None:
    """Data size at which dollar cost ties, or ``None``.

    Solves ``(cold_sql + d/ts_sql)·dbu_sql = (cold_spark + d/ts_spark)·dbu_spark``
    for *d* — the ``usd_per_dbu`` factor cancels. Returns ``None`` under the
    same conditions as :func:`latency_breakeven_gb`.
    """
    a = config.sql.dbu_per_hour / config.sql.throughput_gb_per_s
    b = config.spark.dbu_per_hour / config.spark.throughput_gb_per_s
    denom = a - b
    if abs(denom) < 1e-12:
        return None
    num = (
        config.spark.cold_start_s * config.spark.dbu_per_hour
        - config.sql.cold_start_s * config.sql.dbu_per_hour
    )
    d = num / denom
    return d if d > 0 else None


# --- Default placeholder profiles ------------------------------------------

DEFAULT_CONFIG = BenchmarkConfig(
    sql=ComputeProfile(
        name="Serverless SQL Warehouse",
        cold_start_s=2.0,          # seconds; no cluster to spin up
        throughput_gb_per_s=1.0,   # placeholder
        dbu_per_hour=4.0,          # placeholder; 2X-Small ballpark
        caveat="Photon-accelerated; effectively zero cold start.",
    ),
    spark=ComputeProfile(
        name="Classic PySpark Cluster",
        cold_start_s=80.0,         # 60–90s spin-up, placeholder midpoint
        throughput_gb_per_s=1.5,   # higher steady-state than SQL (more workers)
        dbu_per_hour=16.0,         # 4-worker cluster, placeholder
        caveat="Spin-up dominates small jobs; wins only once data is large.",
    ),
    usd_per_dbu=0.55,              # placeholder, Databricks list price ballpark
)


# --- Rendering --------------------------------------------------------------

def format_comparison(c: Comparison) -> str:
    """One-row render of a Comparison. Pure."""
    verdict = (
        f"{c.faster.upper()} faster by "
        f"{max(c.latency_ratio, 1 / c.latency_ratio):.2f}x; "
        f"{c.cheaper.upper()} cheaper by "
        f"{max(c.cost_ratio, 1 / c.cost_ratio):.2f}x"
    )
    return (
        f"  {c.data_gb:>7.0f} GB | "
        f"SQL {c.sql.latency_s:>7.1f}s ${c.sql.usd:>7.4f} | "
        f"Spark {c.spark.latency_s:>7.1f}s ${c.spark.usd:>7.4f} | "
        f"{verdict}"
    )


def format_sweep(
    sizes: tuple[float, ...], config: BenchmarkConfig
) -> str:
    """Multi-row sweep at each size. Pure."""
    lines = [
        "  Data GB | SQL lat / USD           | Spark lat / USD         | Verdict",
        "  --------+-------------------------+-------------------------+--------",
    ]
    lines.extend(format_comparison(compare(gb, config)) for gb in sizes)
    return "\n".join(lines)


# --- Self-checks ------------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _demo() -> int:
    cfg = DEFAULT_CONFIG
    print("Compute-SKU model — Serverless SQL vs Classic PySpark")
    print("  ⚠  MODEL OUTPUT, NOT MEASUREMENT — see module docstring.")
    print("=" * 78)
    ok = True

    # --- The headline bug: does data size matter? -------------------------
    # The original's `benchmark_simulation(50)` and `benchmark_simulation(5000)`
    # produced identical output. Verify that here.
    small = predict(cfg.sql, 50, cfg.usd_per_dbu)
    large = predict(cfg.sql, 5000, cfg.usd_per_dbu)
    ok &= _check(
        small != large,
        "predict() must vary with data_gb (original ignored it)",
    )
    ok &= _check(
        large.latency_s > small.latency_s,
        "latency must increase with data size",
    )
    print("\n  ✓ data_gb is load-bearing (50 GB ≠ 5000 GB)")

    # --- Scale sweep, showing the flip -----------------------------------
    sizes = (1.0, 10.0, 50.0, 100.0, 500.0, 1000.0)
    print("\nScale sweep (model estimates):")
    print(format_sweep(sizes, cfg))

    # --- Breakevens ------------------------------------------------------
    lat_b = latency_breakeven_gb(cfg)
    cost_b = cost_breakeven_gb(cfg)
    ok &= _check(lat_b is not None and lat_b > 0, "latency crossover must exist")
    ok &= _check(cost_b is not None and cost_b > 0, "cost crossover must exist")
    print(f"\nLatency crossover: {lat_b:,.1f} GB  "
          f"(below → SQL faster; above → Spark faster)")
    print(f"Cost crossover:    {cost_b:,.1f} GB  "
          f"(below → SQL cheaper; above → Spark cheaper)")

    # Crossover math verifies against compare(): just below/above flips.
    just_below = compare(lat_b * 0.99, cfg)
    just_above = compare(lat_b * 1.01, cfg)
    ok &= _check(
        just_below.faster == "sql" and just_above.faster == "spark",
        f"latency verdict must flip across {lat_b:.1f} GB "
        f"(got {just_below.faster} → {just_above.faster})",
    )
    print("  ✓ verdicts flip across the latency crossover")

    # --- Axioms ----------------------------------------------------------
    # Zero data → just cold start.
    zero = predict(cfg.sql, 0.0, cfg.usd_per_dbu)
    ok &= _check(
        abs(zero.latency_s - cfg.sql.cold_start_s) < 1e-9,
        "predict(0 GB) should equal cold_start_s exactly",
    )
    # Linearity: doubling data above cold start roughly doubles marginal latency.
    a = predict(cfg.spark, 1000.0, cfg.usd_per_dbu).latency_s
    b = predict(cfg.spark, 2000.0, cfg.usd_per_dbu).latency_s
    ok &= _check(b > a, "predict must be strictly increasing in data_gb")
    # USD linear in DBU at fixed price.
    half = predict(cfg.sql, 100.0, cfg.usd_per_dbu * 0.5)
    full = predict(cfg.sql, 100.0, cfg.usd_per_dbu)
    ok &= _check(
        abs(half.usd * 2 - full.usd) < 1e-9,
        "USD must be linear in usd_per_dbu",
    )
    print("  ✓ predict: zero-data, linearity, USD linearity")

    # --- Idempotency & immutability -------------------------------------
    ok &= _check(
        compare(50.0, cfg) == compare(50.0, cfg),
        "compare must be idempotent",
    )
    try:
        compare(50.0, cfg).data_gb = 999.0  # type: ignore[misc]
        ok &= _check(False, "Comparison must be frozen")
    except Exception:
        pass
    print("  ✓ idempotent & frozen")

    # --- Validation -----------------------------------------------------
    for kwargs in (
        dict(cold_start_s=-1.0),
        dict(throughput_gb_per_s=0.0),
        dict(dbu_per_hour=-1.0),
        dict(name=""),
    ):
        base = dict(
            name="x", cold_start_s=0.0, throughput_gb_per_s=1.0, dbu_per_hour=1.0
        )
        base.update(kwargs)
        try:
            ComputeProfile(**base)
        except ValueError:
            pass
        else:
            ok &= _check(False, f"ComputeProfile({kwargs}) should raise ValueError")
    for bad in (-1.0, -100.0):
        try:
            predict(cfg.sql, bad, cfg.usd_per_dbu)
        except ValueError:
            pass
        else:
            ok &= _check(False, f"predict(data_gb={bad}) should raise ValueError")
    print("  ✓ config & input validated")

    # --- Framing correction footer ---------------------------------------
    print(
        "\n  Correction: this model compares two COMPUTE SKUs, not two\n"
        "  engines. The SQL/Spark latency gap is dominated by cold start\n"
        "  (Classic cluster spin-up), not query execution. You can run\n"
        "  SQL on Classic and PySpark on Serverless; the answer flips."
    )

    if ok:
        print("\nAll self-checks PASSED.")
        return 0
    print("\nSelf-checks FAILED.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
