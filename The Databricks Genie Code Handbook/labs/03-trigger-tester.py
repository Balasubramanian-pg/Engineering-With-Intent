"""labs/03-trigger-tester.py.

Evaluates semantic similarity scores between user prompts and skill descriptions
using a binary bag-of-words cosine similarity metric (Otsuka-Ochiai coefficient).
"""

from __future__ import annotations

import math
import re
from typing import Mapping, Sequence

# Compiled regex for tokenization: extracts alphanumeric words, stripping punctuation
TOKEN_PATTERN = re.compile(r"\b\w+\b")


def tokenize(text: str | None) -> frozenset[str]:
    """Tokenize and normalize input text into an immutable set of words.

    This function is idempotent and resilient to `None` or non-standard inputs.
    f(f(x)) produces an equivalent set representation.

    Args:
        text: Raw text string to process.

    Returns:
        frozenset[str]: Set of lowercase, punctuation-free tokens.
    """
    if not text or not isinstance(text, str):
        return frozenset()

    # Normalize to lowercase and extract word boundaries
    tokens = TOKEN_PATTERN.findall(text.lower())
    return frozenset(tokens)


def calculate_cosine_similarity(
    query_tokens: frozenset[str], desc_tokens: frozenset[str]
) -> float:
    """Calculate the cosine similarity (Otsuka-Ochiai coefficient) between two token sets.

    Formula:
        similarity = |A ∩ B| / sqrt(|A| * |B|)

    This function is stateless, side-effect free, and strictly bounded to [0.0, 1.0].

    Args:
        query_tokens: Token set representing the user query.
        desc_tokens: Token set representing the skill/action keywords.

    Returns:
        float: Similarity score between 0.0 (no overlap) and 1.0 (identical sets).
    """
    # Guard against empty inputs to prevent ZeroDivisionError
    if not query_tokens or not desc_tokens:
        return 0.0

    intersection_size = len(query_tokens & desc_tokens)
    if intersection_size == 0:
        return 0.0

    denominator = math.sqrt(len(query_tokens) * len(desc_tokens))
    return intersection_size / denominator


def evaluate_prompt_against_skills(
    prompt: str, skill_catalog: Mapping[str, frozenset[str]]
) -> dict[str, float]:
    """Pure evaluation function mapping a prompt to similarity scores across skills.

    Args:
        prompt: Raw user prompt string.
        skill_catalog: Mapping of skill names to pre-tokenized keyword sets.

    Returns:
        dict[str, float]: Mapping of skill names to computed similarity scores.
    """
    query_tokens = tokenize(prompt)

    # Evaluate each skill independently without modifying catalog state
    return {
        skill_name: calculate_cosine_similarity(query_tokens, skill_keywords)
        for skill_name, skill_keywords in skill_catalog.items()
    }


def main(prompts: Sequence[str], catalog_definitions: Mapping[str, str]) -> None:
    """Entry point demonstrating deterministic prompt evaluation."""
    # Pre-tokenize catalog keywords into immutable sets (idempotent setup)
    skills_catalog: dict[str, frozenset[str]] = {
        name: tokenize(keywords) for name, keywords in catalog_definitions.items()
    }

    # Evaluate all test prompts
    for prompt in prompts:
        print(f'Prompt: "{prompt}"')
        scores = evaluate_prompt_against_skills(prompt, skills_catalog)

        # Print sorted descending by score for easier inspection
        sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        for skill_name, score in sorted_scores:
            print(f"  -> Skill: {skill_name:18s} Score: {score:.3f}")


if __name__ == "__main__":
    # Test skill dictionary (keyword definitions)
    sample_skills = {
        "lakeflow-builder": (
            "build declarative streaming lakeflow pipeline expectation materialized view"
        ),
        "sql-optimizer": (
            "optimize sql query photon join explain analyze partition"
        ),
    }

    # Test prompts containing punctuation and varied casing
    sample_prompts = [
        "How do I create a streaming silver table with expectation?",
        "Why is this query running slow without partition pruning?",
        "",  # Edge case: empty input
    ]

    main(prompts=sample_prompts, catalog_definitions=sample_skills)
