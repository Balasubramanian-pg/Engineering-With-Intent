#!/usr/bin/env python3
"""labs/04-tree-walk-audit.py

Trace directory tree-walking resolution for AGENTS.md discovery.

The discovery algorithm mimics how Databricks Assistant / Genie Code
locates the nearest instruction file for a notebook cell:

    From the cell's directory, walk up one level at a time looking for
    ``AGENTS.md``. Stop at the repo root — instructions ABOVE the repo
    do not apply. Return the first file found (nearest = highest priority).

Design
------
* **Idempotent** — :func:`find_nearest_agents_md` is a pure function of
  ``(start_path, repo_root, filename)``. Same inputs → equal results.
  Reads the filesystem, writes nothing, keeps no state.
* **Stateless** — no module-level mutable state, no caches, no temp
  files left behind. ``DiscoveryResult`` is frozen; ``searched`` is a
  tuple so nothing the caller holds can be mutated by this module.

What the original got wrong
---------------------------
* **No demonstration.** ``__main__`` printed "initialized" and exited.
  A file named *audit* must produce an auditable trace — this one does.
* **``AGENTS.md`` as a directory was accepted.** ``target.exists()``
  returns ``True`` for directories; downstream ``open()`` would crash.
  Fixed with ``is_file()``, and the audit trace shows the skip.
* **File start paths walked one level too far.** ``/repo/nb.py`` checked
  ``/repo/nb.py/AGENTS.md`` first — a guaranteed-miss stat. Fixed: file
  and non-existent start paths walk from their parent.
* **``repo_root`` was not validated as an ancestor.** A ``start_path``
  outside ``repo_root`` silently walked to the filesystem root and
  reported "not found" — a different answer than the intended
  "invalid input." Fixed: raises ``ValueError``.

Exit codes
----------
  0  self-checks passed
  1  self-checks failed
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


# --- Result type -----------------------------------------------------------

@dataclass(frozen=True)
class DiscoveryResult:
    """Immutable audit record for one discovery run.

    ``searched`` is the exact list of candidate paths checked, nearest
    first. It includes the found path (if any), so the full trace is
    reconstructable from the result — nothing must be recomputed. This is
    what makes the result *auditable*; the original returned only the
    final path or ``None``.
    """
    start: Path
    root: Path
    filename: str
    searched: tuple[Path, ...]
    found: Path | None

    @property
    def depth(self) -> int | None:
        """Levels traversed from start to the found file.

        ``0`` means ``AGENTS.md`` sat in the starting directory itself.
        ``None`` means not found.
        """
        if self.found is None:
            return None
        return self.searched.index(self.found)


# --- Helpers ---------------------------------------------------------------

def _is_ancestor_or_same(ancestor: Path, descendant: Path) -> bool:
    """True if ``ancestor`` contains ``descendant`` (or they are equal)."""
    try:
        descendant.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _resolve_start(start_path: Path) -> Path:
    """Determine the directory to begin the walk from.

    * Existing directory → the directory itself.
    * Existing file → its parent.
    * Non-existent path → its parent (assumed to be a file the caller is
      about to create — the common case when auditing notebooks).
    """
    resolved = start_path.resolve(strict=False)
    if resolved.is_dir():
        return resolved
    return resolved.parent


# --- Core algorithm --------------------------------------------------------

def find_nearest_agents_md(
    start_path: Path | str,
    repo_root: Path | str,
    *,
    filename: str = "AGENTS.md",
) -> DiscoveryResult:
    """Walk up from *start_path* to find the nearest *filename*.

    Pure and idempotent. Reads the filesystem; writes nothing.

    Args:
        start_path: A directory, a file, or a path that doesn't exist yet.
            Directories walk from themselves; files and non-existent
            paths walk from their parent (one level of ``stat`` saved and
            the trace reads correctly).
        repo_root: Upper bound of the walk. Must be an ancestor of (or
            equal to) ``start_path``. Walking does not cross this line.
        filename: Name of the file to look for. Default ``"AGENTS.md"``.
            Override to support case variants (``"agents.md"``) or future
            renames (``"INSTRUCTIONS.md"``).

    Raises:
        ValueError: if ``repo_root`` is not an ancestor of ``start_path``,
            or if either argument is empty.

    Returns:
        A :class:`DiscoveryResult` with ``found`` set to the nearest
        matching file, or ``None`` if none exists between ``start_path``
        and ``repo_root`` inclusive.
    """
    if not start_path or not repo_root:
        raise ValueError("start_path and repo_root must be non-empty")

    start = _resolve_start(Path(start_path))
    root = Path(repo_root).resolve(strict=False)

    if not _is_ancestor_or_same(root, start):
        raise ValueError(
            f"repo_root {root} is not an ancestor of start_path {start}"
        )

    searched: list[Path] = []
    curr = start
    while True:
        candidate = curr / filename
        searched.append(candidate)
        if candidate.is_file():        # not .exists() — see module docstring
            return DiscoveryResult(
                start=start, root=root, filename=filename,
                searched=tuple(searched), found=candidate,
            )
        if curr == root:
            break
        if curr.parent == curr:         # filesystem root; defensive
            break
        curr = curr.parent

    return DiscoveryResult(
        start=start, root=root, filename=filename,
        searched=tuple(searched), found=None,
    )


def discover_all_agents_md(
    start_path: Path | str,
    repo_root: Path | str,
    *,
    filename: str = "AGENTS.md",
) -> tuple[Path, ...]:
    """Return *all* matching files between start and root, nearest first.

    Useful when AGENTS.md files cascade (workspace + user + repo layers):
    returns every file that would apply, in priority order.
    """
    result = find_nearest_agents_md(start_path, repo_root, filename=filename)
    if result.found is None:
        return ()
    return tuple(p for p in result.searched if p.is_file())


# --- Rendering -------------------------------------------------------------

def format_result(result: DiscoveryResult) -> str:
    """Render a DiscoveryResult as a human-readable audit trace. Pure."""
    lines = [f"  start: {result.start}", f"  root:  {result.root}"]
    if result.found is None:
        lines.append(
            f"  result: NOT FOUND ({len(result.searched)} candidate(s) checked)"
        )
    else:
        lines.append(
            f"  result: FOUND {result.found} at depth {result.depth}"
        )
    lines.append("  trace:")
    for i, p in enumerate(result.searched):
        marker = "→" if p == result.found else " "
        lines.append(f"    [{i}] {marker} {p.name}")
    return "\n".join(lines)


# --- Self-checks -----------------------------------------------------------

def _check(condition: bool, message: str) -> bool:
    """Assertion helper that survives ``python -O`` (stdlib ``assert`` doesn't)."""
    if not condition:
        print(f"  ✗ FAILED: {message}", file=sys.stderr)
        return False
    return True


def _demo() -> int:
    print("Tree-walk audit — AGENTS.md discovery")
    print("=" * 62)
    ok = True

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        repo = td_path / "repo"
        repo.mkdir()
        (repo / "AGENTS.md").write_text("repo-level rules")

        src = repo / "src"
        src.mkdir()
        foo = src / "foo"
        foo.mkdir()
        (foo / "AGENTS.md").write_text("foo-level rules")

        empty = repo / "empty"
        empty.mkdir()
        sub = empty / "sub"
        sub.mkdir()

        # --- Case 1: nearest wins --------------------------------------
        r = find_nearest_agents_md(foo / "nb.py", repo)
        ok &= _check(
            r.found == (foo / "AGENTS.md").resolve(),
            f"nearest should be foo/AGENTS.md, got {r.found}",
        )
        ok &= _check(r.depth == 0, f"depth should be 0, got {r.depth}")
        print("  ✓ case 1: nearest (local) AGENTS.md wins")

        # --- Case 2: fall through to repo root -------------------------
        r = find_nearest_agents_md(sub / "nb.py", repo)
        ok &= _check(
            r.found == (repo / "AGENTS.md").resolve(),
            f"should fall through to repo/AGENTS.md, got {r.found}",
        )
        ok &= _check(
            r.depth == 2,
            f"depth should be 2 (sub→empty→repo), got {r.depth}",
        )
        print("  ✓ case 2: no local AGENTS.md → falls through to repo root")

        # --- Case 3: file start path (original checked the file itself) -
        r = find_nearest_agents_md(src / "notebook.py", repo)
        ok &= _check(
            r.found == (repo / "AGENTS.md").resolve(),
            f"file start should walk from parent, got {r.found}",
        )
        ok &= _check(
            all(not str(p).endswith("notebook.py/AGENTS.md") for p in r.searched),
            "trace must not contain nonsense 'notebook.py/AGENTS.md' candidate",
        )
        print("  ✓ case 3: file start path walks from parent, not from file")

        # --- Case 4: AGENTS.md as a directory is skipped ---------------
        weird = repo / "weird"
        weird.mkdir()
        (weird / "AGENTS.md").mkdir()          # a directory, not a file
        r = find_nearest_agents_md(weird, repo)
        ok &= _check(
            r.found == (repo / "AGENTS.md").resolve(),
            f"directory named AGENTS.md must be skipped, got {r.found}",
        )
        ok &= _check(
            r.searched[0] == (weird / "AGENTS.md"),
            "trace should still show the skipped directory candidate",
        )
        print("  ✓ case 4: directory named AGENTS.md is skipped (uses is_file)")

        # --- Case 5: repo_root not an ancestor → ValueError ------------
        outside = td_path / "outside"
        outside.mkdir()
        try:
            find_nearest_agents_md(outside, repo)
            ok &= _check(False, "start outside repo_root should raise ValueError")
        except ValueError:
            pass
        print("  ✓ case 5: start outside repo_root raises ValueError")

        # --- Case 6: start == root -------------------------------------
        r = find_nearest_agents_md(repo, repo)
        ok &= _check(
            r.found == (repo / "AGENTS.md").resolve() and r.depth == 0,
            "start==root should find AGENTS.md at depth 0",
        )
        print("  ✓ case 6: start==root checks root only")

        # --- Case 7: nothing found -------------------------------------
        clean = td_path / "clean"
        clean.mkdir()
        (clean / "inner").mkdir()
        r = find_nearest_agents_md(clean / "inner", clean)
        ok &= _check(r.found is None, f"clean tree should find nothing, got {r.found}")
        ok &= _check(
            len(r.searched) == 2,
            f"should check 2 candidates (inner, clean), got {len(r.searched)}",
        )
        print("  ✓ case 7: no AGENTS.md → found=None, full trace retained")

        # --- Case 8: custom filename (case variant support) ------------
        (foo / "agents.md").write_text("lowercase variant")
        r = find_nearest_agents_md(foo, repo, filename="agents.md")
        ok &= _check(
            r.found == (foo / "agents.md").resolve(),
            f"custom filename should match 'agents.md', got {r.found}",
        )
        print("  ✓ case 8: custom filename supports case variants")

        # --- Case 9: discover_all returns the full cascade -------------
        all_found = discover_all_agents_md(foo / "nb.py", repo)
        ok &= _check(
            len(all_found) == 2
            and all_found[0] == (foo / "AGENTS.md").resolve()
            and all_found[1] == (repo / "AGENTS.md").resolve(),
            f"discover_all should return foo then repo, got {all_found}",
        )
        print("  ✓ case 9: discover_all returns cascade (nearest → repo)")

        # --- Idempotency & immutability -------------------------------
        a = find_nearest_agents_md(foo / "nb.py", repo)
        b = find_nearest_agents_md(foo / "nb.py", repo)
        ok &= _check(a == b, "repeated discovery must be idempotent")
        try:
            a.found = None                      # type: ignore[misc]
            ok &= _check(False, "DiscoveryResult must be frozen")
        except Exception:
            pass
        print("  ✓ idempotent & frozen")

        # --- Audit-trace rendering -----------------------------------
        print("\n  Sample audit trace (case 2):")
        print(format_result(find_nearest_agents_md(sub / "nb.py", repo)))

    if ok:
        print("\nAll self-checks PASSED.")
        return 0
    print("\nSelf-checks FAILED.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_demo())
