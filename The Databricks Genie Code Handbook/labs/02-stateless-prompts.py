#!/usr/bin/env python3
"""Classify prompts as stateless or conversationally dependent.

A *stateless* prompt is self-contained: every table it references is fully
identified, and it contains no back-reference to a prior turn. A
*conversational* prompt relies on session history ("Join it with orders" —
what is "it"?).

Heuristic
---------
1. Tokenize on word boundaries after lowercasing (``_TOKEN_RE``), so
   punctuation doesn't hide pronouns. The original used
   ``prompt.lower().split()``, which turns ``"it,"`` into ``"it,"`` (not
   ``"it"``) and silently misses punctuation-attached pronouns.
2. Collect entity references via four independent patterns, all matched on
   the *lowercased* prompt with word boundaries:
   ``@``-prefixed refs, dotted FQNs, ``gold_``/``dim_``/``fact_`` prefixes,
   and known table names.
3. STATELESS iff entity_hits AND NOT relative_hits. Anything else is
   CONVERSATIONAL — the conservative default, because a false-positive
   warning is cheaper than silently sending an unresolvable prompt.

Limitations (documented, not accidental)
----------------------------------------
* "it" in "Is it true?" is flagged even though it isn't a back-reference.
  Lexical heuristics can't do semantics; over-warning is intentional.
* Known tables are matched with ``\\b`` word boundaries, so a table named
  ``customer_orders`` won't match ``\\borders\\b`` — use an ``@``-ref or
  a dotted FQN to reference it explicitly. This is what fixes the original
  substring bug where ``reorders`` matched ``orders``.

Guarantees
----------
* **Idempotent** — ``analyze_prompt`` is pure; same input, same output.
* **Stateless** — no caches, no globals, no mutable module state.
  Results are frozen dataclasses carrying matched hits for debugging.

Exit codes (script mode)
------------------------
  0  self-checks passed
  1  self-checks failed
"""

from __future__ import annotations

import enum
import re
import sys
from dataclasses import dataclass
from typing import Iterable


# --- Classification vocabulary ---------------------------------------------

RELATIVE_TERMS: frozenset[str] = frozenset({
    "it", "its", "itself",
    "this", "that", "these", "those",
    "they", "them", "their", "there",
    "earlier", "previous", "previously", "above", "former", "last",
    "same", "aforementioned",
})

TABLE_PREFIXES: tuple[str, ...] = ("gold_", "dim_", "fact_")

KNOWN_TABLES: frozenset[str] = frozenset({"orders", "customers"})


# --- Regexes (compiled once; module-level immutable state) -----------------

# Word-boundary tokenizer: keeps digits and underscores, drops punctuation.
# Fixes the original ``split()`` bug where ``"it,"`` != ``"it"``.
_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# @-ref: ``@table``, ``@schema.table``, ``@cat.schema.table`` — no trailing dot.
_AT_REF_RE = re.compile(r"@\w+(?:\.\w+)*")

# Fully-qualified ``cat.schema.table`` even without the @.
_FQN_RE = re.compile(r"\b\w+\.\w+\.\w+\b")

# ``gold_``/``dim_``/``fact_`` prefixes, word-bounded so ``gold_`` matches
# ``gold_mine`` but a stray ``gold_`` inside an identifier doesn't.
_TABLE_PREFIX_RE = re.compile(r"\b(?:" + "|".join(TABLE_PREFIXES) + r")")

# Known table names, whole-word. ``\borders\b`` matches ``orders`` but NOT
# ``reorders`` (the original substring bug).
_KNOWN_TABLE_RE = re.compile(
    r"\b(?:" + "|".join(sorted(KNOWN_TABLES)) + r")\b"
)


# --- Result types ----------------------------------------------------------

class Verdict(str, enum.Enum):
    """``str``-valued enum so verdicts compare cleanly to their labels."""
    STATELESS = "STATELESS (PASS)"
    CONVERSATIONAL = "CONVERSATIONAL (WARN)"


@dataclass(frozen=True)
class PromptAnalysis:
    """Immutable classification result. Carries *why* for each verdict."""
    prompt: str
    verdict: Verdict
    relative_hits: tuple[str, ...]
    entity_hits: tuple[str, ...]

    @property
    def is_stateless(self) -> bool:
        return self.verdict is Verdict.STATELESS

    def reason(self) -> str:
        if self.is_stateless:
            return f"self-contained; entity: {', '.join(self.entity_hits)}"
        parts: list[str] = []
        if self.relative_hits:
            parts.append(f"relative: {', '.join(self.relative_hits)}")
        if not self.entity_hits:
            parts.append("no explicit entity")
        return "; ".join(parts)


# --- Pure analysis ---------------------------------------------------------

def analyze_prompt(prompt: str) -> PromptAnalysis:
    """Classify one prompt. Pure and idempotent — no I/O, no mutation."""
    lowered = prompt.lower()
    tokens = set(_TOKEN_RE.findall(lowered))
    relative_hits = tuple(sorted(tokens & RELATIVE_TERMS))

    entities: set[str] = set()
    entities.update(_AT_REF_RE.findall(lowered))
    entities.update(_FQN_RE.findall(lowered))
    entities.update(_TABLE_PREFIX_RE.findall(lowered))
    entities.update(_KNOWN_TABLE_RE.findall(lowered))
    entity_hits = tuple(sorted(entities))

    verdict = (
        Verdict.STATELESS
        if entity_hits and not relative_hits
        else Verdict.CONVERSATIONAL
    )
    return PromptAnalysis(
        prompt=prompt,
        verdict=verdict,
        relative_hits=relative_hits,
        entity_hits=entity_hits,
    )


def scan_prompts(prompts: Iterable[str]) -> list[PromptAnalysis]:
    """Batch classifier — pure; preserves input order."""
    return [analyze_prompt(p) for p in prompts]


# --- Self-checks -----------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O`` (unlike stdlib ``assert``)."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _run_case(prompt: str, expected: Verdict, label: str = "") -> bool:
    """Compare one prompt's verdict against ``expected``; print the outcome."""
    r = analyze_prompt(prompt)
    ok = r.verdict is expected
    mark = "✓" if ok else "✗"
    heading = label or r.verdict.value
    print(f"  {mark} {heading:<22} ← {prompt!r}")
    if r.entity_hits or r.relative_hits:
        print(f"      {r.reason()}")
    return _check(ok, f"{prompt!r} → expected {expected.value}, got {r.verdict.value}")


def _demo() -> int:
    print("Prompt classification: stateless vs conversational")
    print("=" * 62)
    ok = True

    print("\nOriginal demo cases:")
    ok &= _run_case("Now join it with orders", Verdict.CONVERSATIONAL)
    ok &= _run_case(
        "Join @retail_prod.gold.customers with @retail_prod.gold.orders on customer_id",
        Verdict.STATELESS,
    )

    print("\nRegression tests (each previously misclassified):")

    # Bug 1 — punctuation attached to pronoun. Original: split() gave
    # ``["join", "it,", ...]`` so ``'it'`` never matched → STATELESS. Wrong.
    ok &= _run_case(
        "Join it, then use orders",
        Verdict.CONVERSATIONAL,
        "punctuation 'it,'",
    )

    # Bug 2 — case-sensitive entity check. Original: ``'gold_' in prompt``
    # failed on uppercase; the ``orders`` substring saved it, but a
    # ``GOLD_mine`` reference (no known table) would not have been.
    ok &= _run_case(
        "Use the GOLD_mine table",
        Verdict.STATELESS,
        "uppercase GOLD_",
    )

    # Bug 3 — substring false-positive. Original: ``'orders' in prompt``
    # matched inside ``"reorders"`` → false STATELESS. Now uses ``\borders\b``.
    ok &= _run_case(
        "The reorders are slow",  # no real table referenced
        Verdict.CONVERSATIONAL,
        "'reorders' ≠ 'orders'",
    )

    # Bug 4 — @-ref containing a relative word must not false-flag.
    # ``_TOKEN_RE`` tokenizes ``@this_table`` as one token, so ``this``
    # doesn't appear as a standalone pronoun.
    ok &= _run_case(
        "Join @this_table with @other_table",
        Verdict.STATELESS,
        "'@this_table'",
    )

    print("\nEdge cases:")
    ok &= _run_case("", Verdict.CONVERSATIONAL, "empty prompt")
    ok &= _run_case("that?", Verdict.CONVERSATIONAL, "bare pronoun + '?'")
    ok &= _run_case(
        "SELECT * FROM retail_prod.gold.orders",  # FQN, no @
        Verdict.STATELESS,
        "dotted FQN",
    )
    ok &= _run_case(
        "Show me fact_sales",
        Verdict.STATELESS,
        "fact_ prefix",
    )

    # Idempotency: same input twice → equal results.
    a = analyze_prompt("Join it with orders")
    b = analyze_prompt("Join it with orders")
    ok &= _check(a == b, "analyze_prompt must be idempotent")
    print(f"\n  ✓ idempotent: two calls returned equal PromptAnalysis objects")

    # Batch function preserves order and verdicts.
    batch = scan_prompts(["Join it with orders", "Join @gold.orders"])
    ok &= _check(
        [r.verdict for r in batch]
        == [Verdict.CONVERSATIONAL, Verdict.STATELESS],
        "scan_prompts must preserve input order and verdicts",
    )
    print(f"  ✓ batch: 2 prompts classified in order")

    if ok:
        print("\nAll self-checks PASSED.")
        return 0
    print("\nSelf-checks FAILED.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
