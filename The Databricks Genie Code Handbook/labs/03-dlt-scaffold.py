#!/usr/bin/env python3
"""Lakeflow Spark Declarative Pipeline scaffold — DBR 15.4 LTS.

This file is a *pipeline definition*, not a script. When deployed, the
pipeline runtime imports it and registers every ``@dp.*``-decorated
function as a dataset. Running it as ``python 03-dlt-scaffold.py`` instead
executes a static AST audit (no pyspark import, no cluster required).

API choices (DBR 15.4 LTS)
--------------------------
* ``from pyspark import pipelines as dp`` — the modern API, recommended
  over legacy ``import dlt`` [reference:3].
* ``spark.readStream.table(...)`` / ``spark.read.table(...)`` — the
  supported read path. ``dp.read()`` and ``dp.read_stream()`` are
  **deprecated** [reference:4].
* ``@dp.materialized_view()`` for batch/MV datasets; ``@dp.table()`` for
  streaming tables [reference:5].
* Expectations: ``@dp.expect_or_drop(description, constraint)`` and
  ``@dp.expect_all({description: constraint})`` are unchanged in the
  modern API [reference:6].

Idempotency & statelessness
---------------------------
* Pipeline execution is **declarative**: re-running produces the same
  target state. Re-deploying the same definitions is a no-op.
* This module performs **no I/O at definition time** — no writes, no
  DDL, no ``collect()``. Dataset functions return DataFrames and are
  free of side effects, as the Python reference requires [reference:7].
* The static checker is pure: it reads only its own source and prints;
  it mutates nothing and is safe to run repeatedly.

Exit codes (static-check mode)
------------------------------
  0  scaffold structure valid
  1  scaffold contains a deprecated call or structural defect
"""

from __future__ import annotations

import ast
import sys

# --- Pipeline definitions --------------------------------------------------
# The import is guarded so the file can be *statically checked* on a laptop
# without a Databricks runtime. In a real pipeline, ``dp`` and ``spark``
# are always available, so the definitions below execute normally.

try:
    from pyspark import pipelines as dp
    from pyspark.sql import functions as F
    _RUNTIME = True
except ImportError:  # local static-check mode only
    dp = None  # type: ignore[assignment]
    F = None   # type: ignore[assignment]
    _RUNTIME = False


if _RUNTIME:
    # --- Bronze: raw streaming ingestion via Auto Loader -------------------
    @dp.table(
        name="bronze_orders_raw",
        comment="Raw streaming ingestion from cloud storage",
    )
    def bronze_raw():
        # Auto Loader; schema inference is left to the pipeline. The volume
        # path is the source of truth; no writes happen here.
        return (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .load("/Volumes/retail_prod/raw/orders/")
        )

    # --- Silver: validated, typed records ----------------------------------
    @dp.table(
        name="silver_orders_clean",
        comment="Validated silver records",
    )
    @dp.expect_or_drop("valid_id", "order_id IS NOT NULL")
    @dp.expect_or_drop("positive_amount", "net_amount > 0")
    def silver_clean():
        # Modern API: use spark.readStream.table, not dp.read_stream
        # (dp.read_stream is deprecated [reference:8]).
        return (
            spark.readStream.table("bronze_orders_raw")
            .select(
                F.col("order_id").cast("bigint"),
                F.col("customer_id").cast("bigint"),
                F.col("net_amount").cast("decimal(10,2)"),
                F.to_date("event_timestamp").alias("event_date"),
            )
        )

    # --- Gold: pre-aggregated daily metrics --------------------------------
    # Batch DataFrame => materialized view, not @dp.table (legacy for batch).
    @dp.materialized_view(
        name="gold_orders_daily",
        comment="Pre-aggregated daily metrics",
    )
    @dp.expect_all({"valid_date": "event_date IS NOT NULL"})
    def gold_daily():
        # Modern API: spark.read.table for batch reads.
        return (
            spark.read.table("silver_orders_clean")
            .groupBy("event_date", "customer_id")
            .agg(
                F.count("order_id").alias("order_count"),
                F.sum("net_amount").alias("total_revenue"),
            )
        )


# --- Static scaffold audit (runs only when executed as a script) -----------

_DEPRECATED_CALLS = {("dp", "read"), ("dp", "read_stream"), ("dlt", "read"), ("dlt", "read_stream")}
_FORBIDDEN_METHODS = {"collect", "count", "toPandas", "save", "saveAsTable", "start", "toTable"}


def _qualified_name(node: ast.AST) -> str | None:
    """Return dotted name for ``a.b`` / ``a.b.c`` attribute chains, else None."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def audit_scaffold(source: str) -> list[str]:
    """Pure AST audit. Returns a list of findings (empty == clean)."""
    tree = ast.parse(source)
    findings: list[str] = []
    dataset_funcs = 0

    for node in ast.walk(tree):
        # 1. Deprecated dp.read / dp.read_stream calls.
        if isinstance(node, ast.Call):
            qn = _qualified_name(node.func)
            if qn:
                head, _, tail = qn.rpartition(".")
                if (head, tail) in _DEPRECATED_CALLS:
                    findings.append(
                        f"line {node.lineno}: deprecated '{qn}' — "
                        f"use spark.read.table / spark.readStream.table"
                    )
                # 2. Forbidden side-effect methods inside dataset code.
                if tail in _FORBIDDEN_METHODS and head:
                    findings.append(
                        f"line {node.lineno}: forbidden '{qn}()' in pipeline "
                        f"definition (no side effects allowed)"
                    )

        # 3. Dataset decorators must carry an explicit name.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                qn = _qualified_name(dec.func if isinstance(dec, ast.Call) else dec)
                if qn in {"dp.table", "dp.materialized_view", "dp.view"}:
                    dataset_funcs += 1
                    if isinstance(dec, ast.Call):
                        names = [kw.arg for kw in dec.keywords]
                        if "name" not in names:
                            findings.append(
                                f"line {node.lineno}: '{qn}' on "
                                f"'{node.name}' has no explicit name= kwarg"
                            )
                    else:
                        findings.append(
                            f"line {node.lineno}: '{qn}' on '{node.name}' "
                            f"is used without parentheses"
                        )

    if dataset_funcs == 0:
        findings.append("no dataset decorators found — not a pipeline definition?")
    return findings


def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _main() -> int:
    # Pure: read only this file's own source.
    from pathlib import Path
    source = Path(__file__).read_text(encoding="utf-8")
    findings = audit_scaffold(source)

    print("Lakeflow Declarative Pipeline — static scaffold audit")
    print("=" * 58)
    if findings:
        for f in findings:
            print(f"  ✗ {f}")
        return 1

    # Idempotency: running the audit twice yields identical output.
    again = audit_scaffold(source)
    print(f"  ✓ {len(again) == 0 and again == findings} idempotent audit")
    print("  ✓ no deprecated reads (dp.read / dp.read_stream)")
    print("  ✓ no forbidden side-effect methods in dataset code")
    print("  ✓ every dataset decorator declares an explicit name")
    print("\nStatic audit PASSED. Deploy to a pipeline to execute definitions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
