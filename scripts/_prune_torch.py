"""Drop the torch subtree that mlx-whisper declares but never imports.

mlx-whisper lists torch as a hard dependency, yet the only module that imports
it is mlx_whisper/torch_whisper.py, which nothing in the package references --
it exists for converting original OpenAI PyTorch weights to MLX. VideoLink
loads pre-converted mlx-community/whisper-*-mlx repos, so torch is ~530MB of
dead weight in the app venv.

This prunes torch and anything that becomes orphaned with it. It is
deliberately conservative: it refuses to run when any package other than
mlx-whisper genuinely needs torch (WhisperX refinement, for example), and it
never touches a protected package.

Usage:
  python _prune_torch.py [--dry-run]

Exit status is 0 whether or not anything was removed; a non-zero status means
the prune itself failed.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import subprocess
import sys
from collections import defaultdict

# Declares torch in its metadata but never imports it at runtime.
DECLARES_BUT_UNUSED = {"mlx-whisper"}

# Never remove these, whatever the dependency graph says.
PROTECTED = {
    "pip", "setuptools", "wheel",
    "mlx", "mlx-metal", "mlx-whisper",
    "numpy", "scipy", "numba", "llvmlite",
    "huggingface-hub", "tiktoken", "tqdm", "more-itertools", "regex",
    "fastapi", "starlette", "uvicorn", "pydantic", "pydantic-core", "websockets",
    # Only torch pulls these in today, but starlette's optional templating
    # wants them and they are ~1MB. Not worth reclaiming.
    "jinja2", "markupsafe",
}


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def req_name(req: str) -> str:
    return canon(re.split(r"[\s\[<>=!;~()]", req.strip(), maxsplit=1)[0])


def build_graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (requires, required_by), ignoring optional extras."""
    requires: dict[str, set[str]] = defaultdict(set)
    required_by: dict[str, set[str]] = defaultdict(set)
    for dist in md.distributions():
        name = canon(dist.metadata["Name"] or "")
        if not name:
            continue
        for raw in dist.requires or []:
            if "extra ==" in raw:  # optional, not installed on our behalf
                continue
            dep = req_name(raw)
            requires[name].add(dep)
            required_by[dep].add(name)
    return requires, required_by


def installed() -> set[str]:
    return {canon(d.metadata["Name"] or "") for d in md.distributions()} - {""}


def plan(root: str = "torch") -> tuple[list[str], str | None]:
    """Return (packages to remove, reason to skip)."""
    have = installed()
    if root not in have:
        return [], f"{root} is not installed; nothing to prune."

    requires, required_by = build_graph()

    real_users = required_by.get(root, set()) - DECLARES_BUT_UNUSED
    if real_users:
        return [], (
            f"{root} is required by {', '.join(sorted(real_users))}; keeping it."
        )

    # Everything reachable from torch is a candidate; keep only those that end
    # up with no dependents outside the removal set.
    candidates: set[str] = set()
    stack = [root]
    while stack:
        pkg = stack.pop()
        if pkg in candidates or pkg not in have or pkg in PROTECTED:
            continue
        candidates.add(pkg)
        stack.extend(requires.get(pkg, set()))

    removing = {root}
    changed = True
    while changed:
        changed = False
        for pkg in sorted(candidates - removing):
            dependents = required_by.get(pkg, set()) - removing - DECLARES_BUT_UNUSED
            if not dependents:
                removing.add(pkg)
                changed = True

    return sorted(removing), None


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]

    targets, skip_reason = plan()
    if skip_reason:
        print(f"torch prune: {skip_reason}")
        return 0
    if not targets:
        print("torch prune: nothing to remove.")
        return 0

    print(f"torch prune: removing unused {', '.join(targets)}")
    if dry_run:
        print("torch prune: --dry-run, no changes made.")
        return 0

    result = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", *targets],
        check=False,
    )
    if result.returncode != 0:
        print("torch prune: pip uninstall failed.", file=sys.stderr)
        return result.returncode

    print("torch prune: done. 'pip check' will note mlx-whisper's unused torch")
    print("             requirement; that is expected and harmless.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
