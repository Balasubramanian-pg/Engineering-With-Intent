#!/usr/bin/env python3
"""Lint Databricks Assistant skill files (SKILL.md).

What gets checked
-----------------
Structural
  E001  file does not begin with a '---' frontmatter delimiter
  E002  frontmatter is not closed with a '---' line
  E003  frontmatter is not valid YAML (or uses constructs the fallback parser
        doesn't support)
  E008  body (below frontmatter) is empty
  E000  file cannot be read

Fields
  E004  required field missing (``name`` or ``description``)
  E005  ``name`` is not kebab-case
  E006  ``name`` exceeds 64 characters
  E007  ``description`` exceeds 1024 characters

Heuristics (warnings)
  W001  ``name`` disagrees with the containing directory
  W002  ``description`` merely repeats ``name``
  W003  duplicate key in frontmatter (YAML silently drops the earlier value)
  W004  unknown top-level key (likely typo of a real field)
  W005  ``description`` shorter than 20 characters (under-described)
  W006  body does not start with a ``#`` heading

Info
  I001  ``version`` field present
  I002  PyYAML not installed; a minimal fallback parser was used

Design guarantees
-----------------
* **Idempotent** — :func:`lint_source` is a pure function of
  ``(path, text)``. Linting the same text twice returns equal findings.
* **Stateless** — no globals, no caches, no filesystem writes. ``LintResult``
  is frozen and ``metadata`` is a sorted tuple of ``(key, str(value))``
  pairs, so nothing the caller holds can be mutated by this module.

Exit codes (script mode)
------------------------
  0  no findings, or only INFO
  1  WARN findings (or ERROR findings; see ``--strict``)
  2  usage / argument error
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

try:  # PyYAML is optional; fallback parser is used otherwise.
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# --- Constants --------------------------------------------------------------

DEFAULT_PATH = "/Workspace/.assistant/skills/lakeflow-builder/SKILL.md"
NAME_MAX = 64
DESC_MAX = 1024
DESC_MIN = 20

# Kebab-case: starts with lowercase letter, hyphen-separated lowercase words.
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# A top-level frontmatter line: ``key: value`` (key may include hyphen).
_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
# ``  - item`` inside a block list.
_LIST_ITEM = re.compile(r"^\s+-\s+(.*)$")
# ``[a, b, c]`` inline list.
_INLINE_LIST = re.compile(r"^\[(.*)\]$")

KNOWN_KEYS = frozenset({
    "name", "description", "version", "triggers", "allowed-tools",
    "license", "author", "tags", "metadata",
})


# --- Result types -----------------------------------------------------------

class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    """One linter finding. Frozen so results are safe to share."""
    severity: Severity
    code: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class LintResult:
    """Immutable outcome of linting one file.

    ``metadata`` is a sorted tuple of ``(key, str(value))`` pairs rather
    than a dict: a dict would leak mutability through the frozen outer
    object. Callers can reconstruct a dict with ``dict(r.metadata)`` when
    they need one for display.
    """
    path: Path
    findings: tuple[Finding, ...]
    metadata: tuple[tuple[str, str], ...] = ()

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARN)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- Fallback YAML subset parser -------------------------------------------
# Handles only the shapes frontmatter actually uses: scalar ``key: value``,
# quoted scalars, inline ``[a, b]`` lists, and block ``- item`` lists. Any
# other construct produces a parse error rather than silently misparsing.

def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _parse_simple(text: str) -> tuple[dict[str, Any], list[str]]:
    data: dict[str, Any] = {}
    errors: list[str] = []
    current_list_key: str | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        list_m = _LIST_ITEM.match(line)
        if list_m:
            if current_list_key is None:
                errors.append(f"line {lineno}: list item outside a list block")
                continue
            data[current_list_key].append(_unquote(list_m.group(1).strip()))
            continue

        if line[0] in (" ", "\t"):
            # Nested map values aren't supported by the fallback parser.
            errors.append(f"line {lineno}: indented map values unsupported; install PyYAML")
            current_list_key = None
            continue

        key_m = _KEY_LINE.match(line)
        if not key_m:
            errors.append(f"line {lineno}: expected 'key: value'")
            continue

        key, value = key_m.group(1), key_m.group(2).strip()
        current_list_key = None

        if value == "":
            data[key] = []
            current_list_key = key
            continue

        inline_m = _INLINE_LIST.match(value)
        if inline_m:
            items = [s.strip() for s in inline_m.group(1).split(",") if s.strip()]
            data[key] = [_unquote(s) for s in items]
            continue

        data[key] = _unquote(value)

    return data, errors


def _find_duplicate_keys(fm_text: str) -> list[tuple[str, int]]:
    """Return ``(key, lineno)`` for every repeated top-level key.

    Needed even when PyYAML succeeds: ``yaml.safe_load`` silently keeps
    the *last* value of a duplicate key, which is exactly the kind of bug
    a linter should surface. Line numbers are 1-based within ``fm_text``.
    """
    seen: dict[str, int] = {}
    dups: list[tuple[str, int]] = []
    for lineno, raw in enumerate(fm_text.splitlines(), start=1):
        line = raw.rstrip()
        if not line or line[0] in (" ", "\t", "#"):
            continue
        m = _KEY_LINE.match(line)
        if not m:
            continue
        key = m.group(1)
        if key in seen:
            dups.append((key, lineno))
        else:
            seen[key] = lineno
    return dups


# --- Linting ---------------------------------------------------------------

def lint_source(path: Path, text: str) -> LintResult:
    """Lint ``text`` as if it were a skill file at ``path``.

    Pure and idempotent: reads only its arguments, writes nothing.
    ``path`` is used solely for the directory/name consistency check and
    to populate the result; no filesystem access occurs here.
    """
    findings: list[Finding] = []

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        findings.append(Finding(
            Severity.ERROR, "E001",
            "file must begin with a '---' frontmatter delimiter", 1,
        ))
        return LintResult(path, tuple(findings))

    close_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        findings.append(Finding(
            Severity.ERROR, "E002",
            "frontmatter is not closed with a '---' line", None,
        ))
        return LintResult(path, tuple(findings))

    fm_text = "\n".join(lines[1:close_idx])
    body = "\n".join(lines[close_idx + 1:]).strip()

    # --- Parse frontmatter -------------------------------------------------
    data: dict[str, Any] = {}
    if not fm_text.strip():
        findings.append(Finding(
            Severity.ERROR, "E004", "frontmatter is empty (no fields present)", 2,
        ))
    elif _HAS_YAML:
        try:
            parsed = yaml.safe_load(fm_text)
            if not isinstance(parsed, dict):
                findings.append(Finding(
                    Severity.ERROR, "E003",
                    "frontmatter must be a YAML mapping (key: value pairs)", None,
                ))
                parsed = {}
            data = parsed or {}
        except yaml.YAMLError as exc:
            findings.append(Finding(
                Severity.ERROR, "E003", f"invalid YAML: {exc}", None,
            ))
    else:
        data, parse_errors = _parse_simple(fm_text)
        for err in parse_errors:
            findings.append(Finding(Severity.ERROR, "E003", err, None))
        findings.append(Finding(
            Severity.INFO, "I002",
            "PyYAML not installed; used fallback parser (install PyYAML for full YAML support)",
            None,
        ))

    # Duplicate keys — reported independently of the YAML parser because
    # ``yaml.safe_load`` hides them. Offset by +1 for the frontmatter
    # delimiter on line 1 of the file.
    for key, lineno in _find_duplicate_keys(fm_text):
        findings.append(Finding(
            Severity.WARN, "W003",
            f"duplicate key '{key}' (earlier value silently discarded)", lineno + 1,
        ))

    # --- Required fields ---------------------------------------------------
    name = data.get("name")
    description = data.get("description")

    if name is None:
        findings.append(Finding(Severity.ERROR, "E004", "required field 'name' is missing", None))
    elif not isinstance(name, str):
        findings.append(Finding(Severity.ERROR, "E005", "field 'name' must be a string", None))
    else:
        if not _KEBAB.match(name):
            findings.append(Finding(
                Severity.ERROR, "E005",
                f"'name' must be kebab-case, got '{name}'", None,
            ))
        if len(name) > NAME_MAX:
            findings.append(Finding(
                Severity.ERROR, "E006",
                f"'name' exceeds {NAME_MAX} chars ({len(name)})", None,
            ))
        parent = path.parent.name
        if parent and parent not in (".", "skills", "..") and parent != name:
            findings.append(Finding(
                Severity.WARN, "W001",
                f"'name' is '{name}' but containing directory is '{parent}'", None,
            ))

    if description is None:
        findings.append(Finding(Severity.ERROR, "E004", "required field 'description' is missing", None))
    elif not isinstance(description, str):
        findings.append(Finding(Severity.ERROR, "E007", "field 'description' must be a string", None))
    else:
        if len(description) > DESC_MAX:
            findings.append(Finding(
                Severity.ERROR, "E007",
                f"'description' exceeds {DESC_MAX} chars ({len(description)})", None,
            ))
        if len(description) < DESC_MIN:
            findings.append(Finding(
                Severity.WARN, "W005",
                f"'description' is only {len(description)} chars; describe when to use the skill",
                None,
            ))
        if isinstance(name, str) and description.strip().lower() == name.strip().lower():
            findings.append(Finding(
                Severity.WARN, "W002",
                "'description' just repeats 'name'; add usage guidance", None,
            ))

    # --- Body --------------------------------------------------------------
    if not body:
        findings.append(Finding(
            Severity.ERROR, "E008", "body below frontmatter is empty", None,
        ))
    elif not body.lstrip().startswith("#"):
        findings.append(Finding(
            Severity.WARN, "W006", "body should begin with a '# heading'", None,
        ))

    # --- Info & unknown-key check -----------------------------------------
    if "version" in data:
        findings.append(Finding(
            Severity.INFO, "I001", f"version present: {data['version']}", None,
        ))
    for key in data:
        if key not in KNOWN_KEYS:
            findings.append(Finding(
                Severity.WARN, "W004", f"unknown key '{key}' (typo?)", None,
            ))

    metadata = tuple(sorted((k, str(v)) for k, v in data.items()))
    return LintResult(path, tuple(findings), metadata)


def lint_file(path: Path | str) -> LintResult:
    """Read ``path`` and lint it. Pure apart from the read; no writes."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LintResult(p, (Finding(Severity.ERROR, "E000", f"file not found: {p}", None),))
    except (OSError, UnicodeDecodeError) as exc:
        return LintResult(p, (Finding(Severity.ERROR, "E000", f"cannot read {p}: {exc}", None),))
    return lint_source(p, text)


# --- Rendering -------------------------------------------------------------

def format_result(result: LintResult) -> str:
    lines = [f"Linting: {result.path}"]
    if not result.findings:
        lines.append("  ✓ no findings")
    else:
        for f in result.findings:
            loc = f"line {f.line}: " if f.line else ""
            lines.append(f"  [{f.severity.value.upper():5}] {f.code} {loc}{f.message}")
    e = len(result.errors)
    w = len(result.warnings)
    i = len(result.findings) - e - w
    lines.append(f"  Summary: {e} error(s), {w} warning(s), {i} info")
    return "\n".join(lines)


def _exit_code(result: LintResult, strict: bool) -> int:
    if result.errors:
        return 1
    if strict and result.warnings:
        return 1
    return 0


# --- Self-tests ------------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O``."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


_GOOD = """\
---
name: lakeflow-builder
description: Use when asked to create or modify a Lakeflow/DLT pipeline. Builds streaming tables, MVs, and expectations.
version: 1.0.0
triggers:
  - "create a pipeline"
  - "dlt"
---
# Lakeflow Builder

Step-by-step guide for generating pipelines.
"""


def _self_test() -> int:
    print("Skill linter — self-tests")
    print("=" * 60)
    ok = True

    def expect_clean(label: str, text: str, *, warnings_allowed: bool = True) -> None:
        nonlocal ok
        r = lint_source(Path("lakeflow-builder/SKILL.md"), text)
        ok &= _check(r.ok, f"{label}: expected no errors, got {[f.code for f in r.errors]}")
        if not warnings_allowed:
            ok &= _check(
                not r.warnings,
                f"{label}: expected no warnings, got {[f.code for f in r.warnings]}",
            )
        print(f"  ✓ {label}")

    def expect_error(label: str, text: str, code: str) -> None:
        nonlocal ok
        r = lint_source(Path("lakeflow-builder/SKILL.md"), text)
        codes = [f.code for f in r.errors]
        ok &= _check(code in codes, f"{label}: expected {code}, got {codes}")
        print(f"  ✓ {label} → {code}")

    def expect_warning(label: str, text: str, code: str) -> None:
        nonlocal ok
        r = lint_source(Path("lakeflow-builder/SKILL.md"), text)
        codes = [f.code for f in r.warnings]
        ok &= _check(code in codes, f"{label}: expected warning {code}, got {codes}")
        print(f"  ✓ {label} → {code}")

    # 1. The good file passes cleanly.
    expect_clean("valid SKILL.md", _GOOD, warnings_allowed=False)

    # 2. Regression: original linter returned True unconditionally. Every
    #    one of these would have passed the original.
    expect_error("no frontmatter", "# Just a heading\n", "E001")
    expect_error("unclosed frontmatter", "---\nname: x\n# body", "E002")
    expect_error("missing name", "---\ndescription: d\n---\n# h\n", "E004")
    expect_error("missing description", "---\nname: x\n---\n# h\n", "E004")
    expect_error("name not kebab", "---\nname: Foo_Bar\ndescription: d\n---\n# h\n", "E005")
    expect_error("name too long", "---\nname: " + "a" * 65 + "\ndescription: d\n---\n# h\n", "E006")
    expect_error("description too long", "---\nname: x\ndescription: " + "d" * 1025 + "\n---\n# h\n", "E007")
    expect_error("empty body", "---\nname: x\ndescription: a real description here\n---\n", "E008")

    # 3. Heuristics that must warn (but not error).
    expect_warning(
        "name/dir mismatch",
        _GOOD.replace("name: lakeflow-builder", "name: other-skill"),
        "W001",
    )
    expect_warning(
        "duplicate key",
        "---\nname: a-b\ndescription: good description here\nname: c-d\n---\n# h\n",
        "W003",
    )
    expect_warning(
        "description == name",
        "---\nname: abc-skill\ndescription: abc-skill\n---\n# h\n",
        "W002",
    )
    expect_warning(
        "description too short",
        "---\nname: abc-skill\ndescription: short\n---\n# h\n",
        "W005",
    )
    expect_warning(
        "unknown key",
        "---\nname: abc-skill\ndescription: a good description here\ndescriptoin: typo\n---\n# h\n",
        "W004",
    )
    expect_warning(
        "body has no heading",
        "---\nname: abc-skill\ndescription: a good description here\n---\njust prose\n",
        "W006",
    )

    # 4. Idempotency: same input → equal findings.
    a = lint_source(Path("x/SKILL.md"), _GOOD)
    b = lint_source(Path("x/SKILL.md"), _GOOD)
    ok &= _check(a == b, "lint_source must be idempotent")
    print("  ✓ idempotent")

    # 5. Immutability: LintResult refuses assignment.
    try:
        a.path = Path("tampered")  # type: ignore[misc]
        ok &= _check(False, "LintResult must be frozen")
    except Exception:
        pass
    print("  ✓ frozen result")

    # 6. Fallback parser handles the common shapes.
    data, errors = _parse_simple(
        'name: x-y\ndescription: "quoted: with colon"\ntriggers:\n  - one\n  - two\n'
        "allowed-tools: [Read, Write]\n"
    )
    ok &= _check(errors == [], f"fallback parser should be error-free, got {errors}")
    ok &= _check(data.get("name") == "x-y", "fallback: scalar parse")
    ok &= _check(data.get("description") == "quoted: with colon", "fallback: quoted scalar")
    ok &= _check(data.get("triggers") == ["one", "two"], "fallback: block list")
    ok &= _check(data.get("allowed-tools") == ["Read", "Write"], "fallback: inline list")
    print("  ✓ fallback parser subset")

    if ok:
        print("\nSelf-tests PASSED.")
        return 0
    print("\nSelf-tests FAILED.", file=sys.stderr)
    return 1


# --- CLI -------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Lint Databricks Assistant skill files (SKILL.md).",
    )
    p.add_argument(
        "path", nargs="?", default=None,
        help=f"SKILL.md to lint (default: {DEFAULT_PATH} if present, else self-test)",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="treat warnings as errors (exit 1 if any WARN is emitted)",
    )
    p.add_argument(
        "--self-test", action="store_true",
        help="run internal linter regression tests instead of linting a file",
    )
    return p.parse_args(argv)


def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # If no path was passed and the default doesn't exist, run the
    # self-tests instead of failing with E000 — matches the spirit of the
    # original demo, but actually exercises the linter.
    if args.self_test or (args.path is None and not Path(DEFAULT_PATH).exists()):
        return _self_test()

    result = lint_file(args.path or DEFAULT_PATH)
    print(format_result(result))
    return _exit_code(result, args.strict)


if __name__ == "__main__":
    raise SystemExit(_main())
