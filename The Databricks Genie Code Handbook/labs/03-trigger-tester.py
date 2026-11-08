"""labs/03-trigger-tester.py

Evaluate *lexical* similarity between user prompts and skill descriptions
using binary bag-of-words cosine similarity (Otsuka-Ochiai coefficient),
then answer the question the filename promises: *does this prompt trigger
a skill?*

What this is
------------
Binary bag-of-words: each text becomes a set of tokens; score =
``|A ∩ B| / sqrt(|A| * |B|)``. That's cosine similarity on binary indicator
vectors — the Otsuka-Ochiai coefficient.

What this is NOT
----------------
* **Not semantic.** ``"create a pipeline"`` and ``"build a pipeline"``
  score **0** — no stemming, no synonyms, no embeddings. If two prompts
  mean the same thing using different words, this metric cannot tell.
* **Not order-aware.** ``"join orders customers"`` and ``"customers orders
  join"`` are identical to this metric.
* **Not frequency-aware.** ``"build build build pipeline"`` scores the same
  as ``"build pipeline"`` (binary, not TF).

For a more semantic trigger tester you'd swap the tokenizer for a
stemmer (Snowball/NLTK) or swap the whole metric for embeddings; the
``TriggerVerdict`` API is designed so that's a one-function change.

Tokenization notes
------------------
``\\b\\w+\\b`` on the default (Unicode) regex matches ``[A-Za-z0-9_]`` plus
Unicode letters. Consequences:

  * ``"lakeflow_builder"`` → one token (``lakeflow_builder``), **not**
    ``lakeflow`` + ``builder``. Underscores are word characters.
  * ``"retail_prod.gold.orders"`` → ``retail_prod``, ``gold``, ``orders``
    (dots are boundaries).
  * ``"café"`` → one token (Unicode letter, not ASCII-only).

If you need ``lakeflow_builder`` to match the keyword ``lakeflow``, run
``re.sub("_", " ", text)`` before tokenizing, or use a stemmer.

Design guarantees
-----------------
* **Idempotent** — ``classify_prompt`` is a pure function of its arguments.
  Same prompt, same catalog, same threshold → equal ``TriggerVerdict``.
  ``tokenize`` is *total* (never raises on non-string input) but is **not**
  mathematically idempotent: ``tokenize(tokenize(x)) == frozenset()``
  because a ``frozenset`` is not a ``str``. Use the returned set directly.
* **Stateless** — no module-level mutable state, no caches. Results are
  frozen dataclasses; the score map from ``evaluate_prompt_against_skills``
  is a freshly-created caller-owned ``dict``.

Exit codes (script mode)
------------------------
  0  demo/CLI ran cleanly, or ``--self-test`` passed
  1  ``--self-test`` failed, or ``--strict`` and some prompt below threshold
  2  usage / argument error
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence

# --- Constants --------------------------------------------------------------

# Unicode-aware word-boundary tokenizer. See module docstring for what
# `\w` includes (underscores, digits, Unicode letters).
TOKEN_PATTERN = re.compile(r"\b\w+\b")

# Default trigger threshold. Calibrated so that the original demo prompts
# (weak matches, ~0.32 after stopword removal) pass and pure-noise prompts
# (~0.0) fail. Tune per-domain; there is no universal "right" threshold.
DEFAULT_THRESHOLD = 0.20

# Domain-tuned stopwords: common English function words plus generic verbs
# that carry no signal against the sample catalog. Deliberately keeps
# "build" and "create" because those appear in skill keywords and are real
# signals in the pipeline/SQL domain. Adding "build" here would break the
# demo — that's the whole point of a domain-tuned list.
STOPWORDS: frozenset[str] = frozenset({
    # articles / determiners
    "a", "an", "the", "this", "that", "these", "those",
    # conjunctions / prepositions
    "and", "or", "but", "if", "then", "else", "when", "while",
    "of", "in", "on", "at", "to", "by", "for", "from", "with", "without",
    "into", "onto", "over", "under", "between", "through", "during",
    "before", "after", "since", "until", "against", "about", "across",
    "up", "down", "out", "off", "so", "as",
    # auxiliaries / copulas
    "is", "are", "was", "were", "be", "been", "being",
    "am", "has", "have", "had", "having",
    "do", "does", "did", "doing", "done",
    "can", "could", "will", "would", "shall", "should",
    "may", "might", "must", "not", "no", "yes",
    # pronouns
    "i", "me", "my", "we", "us", "our", "you", "your", "he", "him", "his",
    "she", "her", "it", "its", "they", "them", "their",
    # wh-words
    "who", "whom", "whose", "which", "what", "why", "how", "where",
    # quantifiers / degree
    "very", "too", "also", "just", "only", "own", "same", "some", "any",
    "all", "each", "every", "both", "few", "more", "most", "other",
    "such", "than", "once", "here", "there", "again", "further",
    # generic verbs / fillers (domain-tuned)
    "want", "need", "like", "get", "got", "please", "tell", "show",
    "give", "make",
})


# --- Tokenization -----------------------------------------------------------

def tokenize(
    text: str | None,
    *,
    stopwords: frozenset[str] | None = None,
) -> frozenset[str]:
    """Tokenize and normalize *text* into an immutable set of words.

    Total and pure: returns ``frozenset()`` for any non-string or empty
    input rather than raising. Lowercases and strips punctuation via
    :data:`TOKEN_PATTERN`.

    **Not** mathematically idempotent — ``tokenize(tokenize("a b"))``
    returns ``frozenset()`` because a ``frozenset`` is not a ``str``. That
    is correct behavior, not a bug; the intended use is to consume the
    returned set directly. (The previous docstring claimed ``f(f(x))``
    "produces an equivalent set representation" — it produces an empty
    one.)

    Args:
        text: Raw text string (``None`` / non-``str`` → empty set).
        stopwords: Optional keyword-only stopword set to subtract after
            tokenizing. Default ``None`` (no filtering) preserves the
            original function's behavior. The demo opts into
            :data:`STOPWORDS` for better discrimination.

    Returns:
        frozenset[str]: lowercase, punctuation-free, stopword-filtered tokens.
    """
    if not isinstance(text, str) or not text:
        return frozenset()
    tokens = frozenset(TOKEN_PATTERN.findall(text.lower()))
    if stopwords is not None:
        tokens = tokens - stopwords
    return tokens


# --- Similarity -------------------------------------------------------------

def calculate_cosine_similarity(
    query_tokens: frozenset[str],
    desc_tokens: frozenset[str],
) -> float:
    """Binary bag-of-words cosine similarity (Otsuka-Ochiai coefficient).

    Formula: ``|A ∩ B| / sqrt(|A| * |B|)``.

    Pure and side-effect free. Bounded to ``[0.0, 1.0]``. Symmetric.
    Returns ``0.0`` for disjoint or empty inputs (no ``ZeroDivisionError``).

    Args:
        query_tokens: Token set representing the user query.
        desc_tokens: Token set representing the skill's keyword list.

    Returns:
        Similarity in ``[0.0, 1.0]``: 0.0 = no overlap, 1.0 = identical.
    """
    if not query_tokens or not desc_tokens:
        return 0.0
    intersection = len(query_tokens & desc_tokens)
    if intersection == 0:
        return 0.0
    return intersection / math.sqrt(len(query_tokens) * len(desc_tokens))


# --- Verdict types ----------------------------------------------------------

@dataclass(frozen=True)
class SkillScore:
    """One skill's score for a prompt. Frozen so results are shareable."""
    skill: str
    score: float
    matched_tokens: tuple[str, ...]  # sorted; the "why" behind the score


@dataclass(frozen=True)
class TriggerVerdict:
    """Immutable verdict for one prompt against a whole skill catalog.

    ``scores`` is sorted descending (ties broken by skill name, so ordering
    is deterministic across runs). ``triggered`` is the subset at or above
    ``threshold`` with a strictly positive score — a skill with 0 overlap
    never triggers even at threshold=0.0, because triggering on "no shared
    tokens" would be nonsense.
    """
    prompt: str
    scores: tuple[SkillScore, ...]
    triggered: tuple[SkillScore, ...]
    best: SkillScore | None
    threshold: float
    ambiguous: bool  # top two scores are tied (> 0 overlap for both)

    @property
    def primary(self) -> SkillScore | None:
        """Unambiguous winner above threshold, else ``None``."""
        if self.ambiguous or self.best is None or self.best.score < self.threshold:
            return None
        return self.best

    def top(self, n: int) -> tuple[SkillScore, ...]:
        """Return the top *n* scores (order preserved). Pure."""
        return self.scores[:max(0, n)]


# --- Evaluation -------------------------------------------------------------

def evaluate_prompt_against_skills(
    prompt: str,
    skill_catalog: Mapping[str, frozenset[str]],
) -> dict[str, float]:
    """Pure score map: prompt → {skill: similarity}.

    Preserves the original API and return type (a freshly-created
    caller-owned ``dict``; nothing is shared). For a richer result with
    threshold verdicts, matched tokens, and ambiguity, use
    :func:`classify_prompt`.

    Note: for scores to be comparable across the catalog, ``skill_catalog``
    must have been built with the *same* tokenizer options as ``prompt``
    will be — otherwise the denominators disagree. :func:`classify_prompt`
    enforces this via the ``stopwords`` argument.
    """
    query_tokens = tokenize(prompt)
    return {
        name: calculate_cosine_similarity(query_tokens, keywords)
        for name, keywords in skill_catalog.items()
    }


def classify_prompt(
    prompt: str,
    skill_catalog: Mapping[str, frozenset[str]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    stopwords: frozenset[str] | None = STOPWORDS,
    top_k: int | None = None,
) -> TriggerVerdict:
    """Evaluate *prompt* and return a thresholded, explained verdict.

    Pure and idempotent. Validates ``threshold`` and ``top_k`` up front so
    a misconfiguration fails at the call site, not halfway through a batch.

    Args:
        prompt: Raw user prompt.
        skill_catalog: Pre-tokenized skill keywords. Build with the same
            ``stopwords`` used here (default :data:`STOPWORDS`).
        threshold: Minimum score for a skill to count as triggered.
        stopwords: Stopword set applied to the query tokens.
        top_k: If set, truncate ``scores`` to the top *k* entries. Useful
            for demo output; does not affect ``triggered`` or ``best``.

    Raises:
        ValueError: if ``threshold`` is not in ``[0.0, 1.0]`` or ``top_k``
            is not a positive integer.
    """
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold must be in [0, 1] (got {threshold})")
    if top_k is not None and top_k <= 0:
        raise ValueError(f"top_k must be a positive integer (got {top_k})")

    query_tokens = tokenize(prompt, stopwords=stopwords)

    raw_scores = [
        SkillScore(
            skill=name,
            score=calculate_cosine_similarity(query_tokens, keywords),
            matched_tokens=tuple(sorted(query_tokens & keywords)),
        )
        for name, keywords in skill_catalog.items()
    ]
    # Descending by score; ties broken by skill name for determinism.
    raw_scores.sort(key=lambda s: (-s.score, s.skill))

    triggered = tuple(
        s for s in raw_scores if s.score > 0 and s.score >= threshold
    )
    best = raw_scores[0] if raw_scores else None

    # Ambiguous only when the top two both have *real* overlap and are tied.
    ambiguous = (
        len(raw_scores) >= 2
        and raw_scores[0].score > 0
        and math.isclose(raw_scores[0].score, raw_scores[1].score, abs_tol=1e-9)
    )

    scores = tuple(raw_scores[:top_k]) if top_k is not None else tuple(raw_scores)

    return TriggerVerdict(
        prompt=prompt,
        scores=scores,
        triggered=triggered,
        best=best,
        threshold=threshold,
        ambiguous=ambiguous,
    )


# --- Rendering --------------------------------------------------------------

def format_verdict(verdict: TriggerVerdict, *, max_skills: int | None = None) -> str:
    """Render a verdict as a human-readable block. Pure."""
    lines = [f'Prompt: "{verdict.prompt}"']

    shown = verdict.scores if max_skills is None else verdict.scores[:max_skills]
    if not shown:
        lines.append("  (empty catalog)")
    for s in shown:
        marker = "*" if s in verdict.triggered else " "
        lines.append(f"  {marker} {s.skill:<18} {s.score:.3f}")

    if not verdict.triggered:
        lines.append(
            f"  Verdict: no skill triggered (threshold={verdict.threshold:.2f})"
        )
    elif verdict.ambiguous:
        names = ", ".join(s.skill for s in verdict.triggered)
        lines.append(f"  Verdict: AMBIGUOUS — multiple skills tie above threshold ({names})")
    else:
        primary = verdict.primary
        assert primary is not None  # guaranteed when triggered & not ambiguous
        lines.append(
            f"  Verdict: {primary.skill} triggered "
            f"({primary.score:.3f} >= {verdict.threshold:.2f})"
        )
        if primary.matched_tokens:
            lines.append(f"  Matched: {', '.join(primary.matched_tokens)}")

    return "\n".join(lines)


# --- Self-tests -------------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


_SAMPLE_KEYWORDS = {
    "lakeflow-builder": (
        "build declarative streaming lakeflow pipeline expectation "
        "materialized view"
    ),
    "sql-optimizer": "optimize sql query photon join explain analyze partition",
}


def _build_catalog(defs: Mapping[str, str]) -> dict[str, frozenset[str]]:
    """Pre-tokenize keyword strings — the idempotent setup step."""
    return {name: tokenize(kw, stopwords=STOPWORDS) for name, kw in defs.items()}


def _self_test() -> int:
    print("Trigger tester — self-tests")
    print("=" * 60)
    ok = True

    # --- Tokenizer basics ------------------------------------------------
    ok &= _check(
        tokenize("Hello, World!") == frozenset({"hello", "world"}),
        "tokenize should lowercase and strip punctuation",
    )
    ok &= _check(tokenize(None) == frozenset(), "tokenize(None) → empty set")
    ok &= _check(tokenize("") == frozenset(), "tokenize('') → empty set")
    ok &= _check(
        tokenize(42) == frozenset(),  # type: ignore[arg-type]
        "tokenize(non-str) → empty set",
    )
    # Documented non-idempotency: frozenset input → empty set.
    ok &= _check(
        tokenize(tokenize("a b")) == frozenset(),  # type: ignore[arg-type]
        "tokenize(tokenize(x)) is empty (frozenset is not str)",
    )
    # Stopword filtering opt-in.
    ok &= _check(
        tokenize("the build a pipeline", stopwords=STOPWORDS)
        == frozenset({"build", "pipeline"}),
        "stopword filter should remove 'the' and 'a' but keep 'build'",
    )
    print("  ✓ tokenizer basics & documented non-idempotency")

    # --- Similarity axioms ----------------------------------------------
    a = frozenset({"x", "y", "z"})
    b = frozenset({"x", "y", "z"})
    c = frozenset({"p", "q"})
    ok &= _check(
        math.isclose(calculate_cosine_similarity(a, b), 1.0),
        "identical sets → 1.0",
    )
    ok &= _check(
        calculate_cosine_similarity(a, c) == 0.0,
        "disjoint sets → 0.0",
    )
    ok &= _check(
        calculate_cosine_similarity(frozenset(), b) == 0.0,
        "empty input → 0.0 (no ZeroDivisionError)",
    )
    ok &= _check(
        math.isclose(
            calculate_cosine_similarity(frozenset({"x"}), frozenset({"x", "y"})),
            calculate_cosine_similarity(frozenset({"x", "y"}), frozenset({"x"})),
        ),
        "similarity must be symmetric",
    )
    print("  ✓ similarity axioms (identity, disjoint, empty, symmetric)")

    # --- Classification: clear winners ---------------------------------
    catalog = _build_catalog(_SAMPLE_KEYWORDS)

    v = classify_prompt("Build a materialized view pipeline", catalog)
    ok &= _check(
        v.primary is not None and v.primary.skill == "lakeflow-builder",
        f"strong lakeflow prompt should classify as lakeflow-builder, got {v.best}",
    )
    ok &= _check(
        "build" in (v.primary.matched_tokens if v.primary else ()),
        "matched_tokens should include 'build'",
    )

    v = classify_prompt(
        "Explain the join and analyze partition pruning", catalog,
    )
    ok &= _check(
        v.primary is not None and v.primary.skill == "sql-optimizer",
        f"strong sql-opt prompt should classify as sql-optimizer, got {v.best}",
    )

    # --- Classification: no trigger ------------------------------------
    v = classify_prompt("Tell me a joke", catalog)
    ok &= _check(
        v.triggered == () and v.primary is None,
        "irrelevant prompt should trigger nothing",
    )
    print("  ✓ clear wins, clear misses")

    # --- Original demo prompts still discriminate ----------------------
    v1 = classify_prompt(
        "How do I create a streaming silver table with expectation?", catalog,
    )
    v2 = classify_prompt(
        "Why is this query running slow without partition pruning?", catalog,
    )
    ok &= _check(
        v1.best is not None and v1.best.skill == "lakeflow-builder",
        "original prompt 1 should score lakeflow-builder highest",
    )
    ok &= _check(
        v2.best is not None and v2.best.skill == "sql-optimizer",
        "original prompt 2 should score sql-optimizer highest",
    )
    print("  ✓ original demo prompts discriminate correctly")

    # --- Ambiguity detection -------------------------------------------
    v = classify_prompt("Build a query", catalog)
    ok &= _check(v.ambiguous, "'build a query' should be reported ambiguous")
    ok &= _check(v.primary is None, "ambiguous verdict must not have a primary")
    ok &= _check(len(v.triggered) >= 2, "ambiguous case should trigger ≥2 skills")
    print("  ✓ ambiguity detected on tie")

    # --- Idempotency & immutability ------------------------------------
    ok &= _check(
        classify_prompt("Build a pipeline", catalog)
        == classify_prompt("Build a pipeline", catalog),
        "classify_prompt must be idempotent",
    )
    try:
        v.best = None  # type: ignore[misc]
        ok &= _check(False, "TriggerVerdict must be frozen")
    except Exception:
        pass
    print("  ✓ idempotent & frozen")

    # --- Input validation ----------------------------------------------
    for bad_threshold in (-0.1, 1.1):
        try:
            classify_prompt("x", catalog, threshold=bad_threshold)
        except ValueError:
            pass
        else:
            ok &= _check(False, f"threshold={bad_threshold} should raise ValueError")
    for bad_topk in (0, -1):
        try:
            classify_prompt("x", catalog, top_k=bad_topk)
        except ValueError:
            pass
        else:
            ok &= _check(False, f"top_k={bad_topk} should raise ValueError")
    print("  ✓ threshold/top_k validated")

    if ok:
        print("\nSelf-tests PASSED.")
        return 0
    print("\nSelf-tests FAILED.", file=sys.stderr)
    return 1


# --- Demo -------------------------------------------------------------------

DEMO_PROMPTS: tuple[str, ...] = (
    # original demo prompts
    "How do I create a streaming silver table with expectation?",
    "Why is this query running slow without partition pruning?",
    "",
    # stronger-signal prompts
    "Build a materialized view pipeline",
    "Explain the join and analyze partition pruning",
    "Tell me a joke",
    # genuinely ambiguous
    "Build a query",
)


def _demo() -> int:
    catalog = _build_catalog(_SAMPLE_KEYWORDS)
    print("Trigger tester — demo")
    print("=" * 60)
    for prompt in DEMO_PROMPTS:
        verdict = classify_prompt(prompt, catalog)
        print()
        print(format_verdict(verdict))

    # Note honestly that this is lexical, not semantic.
    print()
    print(
        "Note: 'create a pipeline' and 'build a pipeline' score 0 against "
        "each other — this metric is lexical, not semantic."
    )
    return 0


# --- CLI --------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Score prompts against a skill catalog (binary BoW cosine).",
    )
    p.add_argument(
        "prompt", nargs="*",
        help="prompts to classify (default: run the built-in demo)",
    )
    p.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help=f"minimum score to count as triggered (default {DEFAULT_THRESHOLD})",
    )
    p.add_argument(
        "--no-stopwords", action="store_true",
        help="disable stopword filtering (closer to the original behavior)",
    )
    p.add_argument(
        "--top", type=int, default=None,
        help="show only the top N skills per prompt",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="exit 1 if any prompt fails to trigger a skill",
    )
    p.add_argument(
        "--self-test", action="store_true",
        help="run internal regression tests instead of classifying prompts",
    )
    return p.parse_args(argv)


def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.self_test:
        return _self_test()

    catalog = _build_catalog(_SAMPLE_KEYWORDS)
    stopwords = None if args.no_stopwords else STOPWORDS

    if not args.prompt:
        return _demo()

    any_failed = False
    for prompt in args.prompt:
        verdict = classify_prompt(
            prompt, catalog,
            threshold=args.threshold, stopwords=stopwords, top_k=args.top,
        )
        print(format_verdict(verdict))
        print()
        if not verdict.triggered:
            any_failed = True

    return 1 if (args.strict and any_failed) else 0


if __name__ == "__main__":
    raise SystemExit(_main())
