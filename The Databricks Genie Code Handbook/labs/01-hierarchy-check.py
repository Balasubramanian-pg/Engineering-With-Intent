#!/usr/bin/env python3
"""Verify Databricks Assistant instruction hierarchy precedence resolution.

Resolution order (highest authority first, matching the original comment):
    1. Workspace root      — org / team policy
    2. User profile        — personal overrides
    3. Repository AGENTS.md — baseline defaults

The resolver is a pure function of its three input mappings. It performs
no I/O, mutates no inputs, and keeps no module-level state. Consequently
it is both:

  * **Idempotent** — running it any number of times with equal inputs
    produces identical output and has no side effects.
  * **Stateless**  — no caches, globals, config files, or environment
    reads are involved; the result depends only on the arguments.

The per-key ``provenance`` map records *which* layer supplied each final
value, which is useful when debugging why a given setting won.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

# Single source of truth for layer ordering, highest authority first.
# Iterating ``reversed(PRECEDENCE_HIGH_TO_LOW)`` gives the apply order
# (lowest first), so plain dict assignment naturally lets higher layers
# overwrite lower ones. Add a layer here and both resolution and the
# provenance report stay in sync.
PRECEDENCE_HIGH_TO_LOW: tuple[str, ...] = ("workspace", "user", "repo")


@dataclass(frozen=True)
class Resolution:
    """Immutable outcome of precedence resolution.

    ``effective`` and ``provenance`` are read-only ``MappingProxyType``
    views so callers cannot mutate the result — reinforcing the stateless
    contract (no shared mutable state escapes the function).
    """

    effective: Mapping[str, object]
    provenance: Mapping[str, str]  # key -> name of the layer that supplied it


def resolve_precedence(
    workspace_rules: Mapping[str, object],
    user_rules: Mapping[str, object],
    repo_rules: Mapping[str, object],
) -> Resolution:
    """Merge rule layers according to :data:`PRECEDENCE_HIGH_TO_LOW`.

    Pure and idempotent: builds a fresh dict and never touches the inputs.

    Example
    -------
    >>> r = resolve_precedence({"x": 1}, {"x": 2}, {"x": 3})
    >>> r.effective["x"], r.provenance["x"]
    (1, 'workspace')
    """
    layers: dict[str, Mapping[str, object]] = {
        "workspace": workspace_rules,
        "user": user_rules,
        "repo": repo_rules,
    }

    effective: dict[str, object] = {}
    provenance: dict[str, str] = {}

    # Apply lowest authority first so higher layers overwrite them.
    for layer_name in reversed(PRECEDENCE_HIGH_TO_LOW):
        for key, value in layers[layer_name].items():
            effective[key] = value
            provenance[key] = layer_name

    return Resolution(
        effective=MappingProxyType(effective),
        provenance=MappingProxyType(provenance),
    )


def _check(condition: bool, message: str) -> bool:
    """Tiny assertion helper — works even under ``python -O``.

    The stdlib ``assert`` is stripped by the ``-O`` flag, which silently
    disables verification. Using an explicit check keeps CI honest.
    """
    if not condition:
        print(f"FAILED: {message}", file=sys.stderr)
        return False
    return True


def _demo() -> int:
    """Run the original sanity check plus a provenance report.

    Returns a process exit code so this can gate CI: 0 on pass, 1 on fail.
    Deterministic (hard-coded inputs) => idempotent by construction.
    """
    workspace = {"catalog_boundary": "retail_prod.gold", "sql_indent": 2}
    user = {"editor_theme": "dark", "sql_indent": 4}
    repo = {"sql_dialect": "ANSI", "sql_indent": 4, "naming": "snake_case"}

    result = resolve_precedence(workspace, user, repo)

    print("Resolved precedence rules (key: value [winning layer]):")
    for key in sorted(result.effective):
        print(f"  {key}: {result.effective[key]!r} [{result.provenance[key]}]")

    # Invariant 1: workspace must override user + repo for ``sql_indent``.
    ok = _check(
        result.effective.get("sql_indent") == 2,
        f"sql_indent should be 2 (workspace), got {result.effective.get('sql_indent')!r}",
    )
    # Invariant 2: provenance should attribute ``sql_indent`` to workspace.
    ok &= _check(
        result.provenance.get("sql_indent") == "workspace",
        f"sql_indent provenance should be 'workspace', "
        f"got {result.provenance.get('sql_indent')!r}",
    )
    # Invariant 3: keys unique to a single layer pass through unchanged.
    ok &= _check(
        result.effective.get("editor_theme") == "dark"
        and result.provenance.get("editor_theme") == "user",
        "user-only key 'editor_theme' should survive resolution",
    )
    ok &= _check(
        result.effective.get("naming") == "snake_case"
        and result.provenance.get("naming") == "repo",
        "repo-only key 'naming' should survive resolution",
    )

    if ok:
        print("Rule hierarchy verification PASSED.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
