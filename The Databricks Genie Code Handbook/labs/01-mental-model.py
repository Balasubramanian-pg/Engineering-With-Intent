"""Simulate how Databricks Genie Code synthesizes a prompt from cell state.

The synthesis pipeline assembles a payload from three inputs:

  * ``system_instructions`` — workspace / user / repo rules (truncated to a
    hard character budget so one giant file can't dominate the prompt)
  * ``catalog_context``     — table schemas the model may reference
  * ``active_cell``         — the notebook cell currently being edited

Guarantees
----------
* **Idempotent** — :func:`synthesize_context` is a pure function of its
  arguments. Equal inputs give an identical payload, token estimate, and
  truncation report; the function performs no I/O and mutates nothing.
  Running the demo N times yields byte-identical stdout.
* **Stateless**  — no module-level mutable state, no caches, no env reads.
  Results are carried in a frozen dataclass whose mappings are read-only
  ``MappingProxyType`` views, so callers cannot mutate shared state.

Notes on the token estimate
---------------------------
``len(json_str) // 4`` is the classic rule-of-thumb (≈4 chars/token for
English + ASCII). It is *not* a real tokenizer; we label the result
``estimated`` and document its limits rather than pretend it's exact.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

# Hard cap on system-instruction characters embedded in the payload.
# Mirrors the 2,000-char budget enforced by ``01-character-audit.py``.
DEFAULT_INSTRUCTION_BUDGET = 2000

# Rule-of-thumb chars-per-token for the rough token count.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class SynthesisResult:
    """Immutable outcome of one context-synthesis pass.

    All mappings are wrapped in ``MappingProxyType`` so they cannot be
    mutated by callers — this is what keeps the return value safe to share
    (and keeps the module stateless across calls).
    """

    payload: Mapping[str, object]
    instructions_truncated: bool
    instructions_original_len: int
    instructions_used_len: int
    estimated_tokens: int


def _freeze(obj: object) -> object:
    """Recursively convert dicts to read-only mappings.

    ``MappingProxyType`` is shallow, so nested dicts (e.g. per-table column
    lists inside ``catalog_context``) must be frozen layer by layer. Lists
    are converted to tuples for the same reason.
    """
    if isinstance(obj, dict):
        return MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_freeze(v) for v in obj)
    return obj


def estimate_tokens(text: str) -> int:
    """Rough token count: ``len(text) // 4``.

    Deliberately simple and deterministic. Real tokenizers (BPE, tiktoken)
    differ per model; this is a sizing heuristic, not a billing number.
    Unicode-heavy text will be *under*-estimated because multibyte chars
    count as one Python character but can cost several tokens.
    """
    return len(text) // CHARS_PER_TOKEN


def synthesize_context(
    cell_code: str,
    table_schemas: Mapping[str, object],
    instructions: str,
    *,
    instruction_budget: int = DEFAULT_INSTRUCTION_BUDGET,
) -> SynthesisResult:
    """Assemble the prompt payload Genie Code would send to the model.

    Pure function: reads only its arguments, writes nothing, and returns
    a fully-immutable :class:`SynthesisResult`. Safe to call repeatedly or
    concurrently with equal inputs (idempotent).

    Parameters
    ----------
    cell_code : str
        Source of the notebook cell being edited.
    table_schemas : Mapping[str, object]
        ``{table_name: [column_ddl, ...]}`` — passed through untouched.
    instructions : str
        System / workspace rules. Truncated to ``instruction_budget`` chars.
    instruction_budget : int, keyword-only
        Character cap for ``instructions``. Must be positive.
    """
    if instruction_budget <= 0:
        raise ValueError("instruction_budget must be a positive integer")

    original_len = len(instructions)
    # ``[:budget]`` is silently lossless when short; flag the truncation
    # explicitly so downstream code (and CI) can see it happened.
    truncated = original_len > instruction_budget
    used_instructions = instructions[:instruction_budget]

    payload = {
        "system_instructions": used_instructions,
        "catalog_context": table_schemas,
        "active_cell": cell_code,
    }

    # Serialize once, reuse the same string for both the estimate and any
    # debug logging. ``sort_keys`` makes the output order-stable, which is
    # what makes the token estimate deterministic across dict orderings.
    serialized = json.dumps(payload, sort_keys=True, default=str)
    est_tokens = estimate_tokens(serialized)

    return SynthesisResult(
        payload=_freeze(payload),  # type: ignore[arg-type]
        instructions_truncated=truncated,
        instructions_original_len=original_len,
        instructions_used_len=len(used_instructions),
        estimated_tokens=est_tokens,
    )


def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``.

    The stdlib ``assert`` is stripped under ``-O``, silently disabling
    verification. This keeps the demo's self-checks honest in CI.
    """
    if not condition:
        print(f"FAILED: {message}", file=sys.stderr)
        return False
    return True


def _demo() -> int:
    """Run the original scenario plus assertions and a truncation check.

    Deterministic (fixed inputs) => idempotent by construction. Returns a
    process exit code so it can gate CI: 0 on pass, 1 on fail.
    """
    schemas = {
        "gold_sales": [
            "order_id INT",
            "customer_id INT",
            "net_amount DECIMAL(10,2)",
        ]
    }
    cell = "df = spark.table('gold_sales')"
    instructions = "Enforce ANSI SQL."

    result = synthesize_context(cell, schemas, instructions)

    print(
        f"Synthesized context size: ~{result.estimated_tokens} tokens "
        f"(estimated; instructions {result.instructions_used_len}/"
        f"{result.instructions_original_len} chars, "
        f"truncated={result.instructions_truncated})"
    )
    print("Context synthesis ready.")

    ok = _check(
        result.payload["active_cell"] == cell,
        "active_cell must be preserved verbatim in the payload",
    )
    ok &= _check(
        result.payload["system_instructions"] == instructions,
        "instructions under budget must be embedded unchanged",
    )
    ok &= _check(
        result.instructions_truncated is False,
        "short instructions should not be reported as truncated",
    )
    ok &= _check(
        result.estimated_tokens > 0,
        "token estimate should be positive for a non-empty payload",
    )

    # Demonstrate the truncation path without touching any real file.
    big = "x" * (DEFAULT_INSTRUCTION_BUDGET + 500)
    big_result = synthesize_context(cell, schemas, big)
    ok &= _check(
        big_result.instructions_truncated is True
        and big_result.instructions_used_len == DEFAULT_INSTRUCTION_BUDGET,
        "oversized instructions must be truncated to the budget and flagged",
    )

    # Immutability check: payload must reject mutation attempts.
    try:
        result.payload["active_cell"] = "tampered"  # type: ignore[index]
        ok &= _check(False, "payload should be immutable, but assignment succeeded")
    except TypeError:
        pass  # expected: MappingProxyType blocks assignment

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_demo())
