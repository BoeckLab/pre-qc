"""Self-update via git: compares the installed editable checkout's HEAD
against origin/main and, on request, fast-forwards + reinstalls. Assumes
an editable (`pip install -e .`) install from a git clone -- the normal
distribution path for this tool (see scripts/install.sh) -- so the
installed package always lives inside a real working tree with an
`origin` remote."""

import subprocess
import sys
from pathlib import Path


class UpdateCheckFailed(Exception):
    """Raised for any non-exceptional failure (no repo, no network, no
    fast-forward possible, ...) -- callers show `str(exc)` to the user
    rather than crashing."""


def _repo_root() -> Path:
    # src-layout: src/pre_qc/updater.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def _run(args, cwd, timeout=20):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def check_for_update() -> bool:
    """Returns True if origin/main has commits not yet present locally.
    Returns False (never raises) on any failure -- a failed check should
    be silent, not an error dialog, unless the user explicitly asked by
    clicking the button (see widget.py, which does show a status either
    way)."""
    root = _repo_root()
    if not (root / ".git").exists():
        return False
    try:
        fetch = _run(["git", "fetch", "--quiet", "origin", "main"], cwd=root)
        if fetch.returncode != 0:
            return False
        local = _run(["git", "rev-parse", "HEAD"], cwd=root)
        remote = _run(["git", "rev-parse", "origin/main"], cwd=root)
        if local.returncode != 0 or remote.returncode != 0:
            return False
        local_sha = local.stdout.strip()
        remote_sha = remote.stdout.strip()
        return bool(local_sha) and bool(remote_sha) and local_sha != remote_sha
    except (subprocess.SubprocessError, OSError):
        return False


def apply_update() -> None:
    """Fast-forwards the checkout and reinstalls into the current venv.
    Raises UpdateCheckFailed with a human-readable message on failure
    (dirty tree, diverged history, pip error, ...) -- callers show this
    in a dialog rather than letting a traceback surface."""
    root = _repo_root()
    pull = _run(["git", "pull", "--ff-only", "origin", "main"], cwd=root, timeout=60)
    if pull.returncode != 0:
        raise UpdateCheckFailed("Update failed during `git pull`:\n" + (pull.stderr or pull.stdout))
    install = _run(
        [sys.executable, "-m", "pip", "install", "-e", str(root), "--quiet"],
        cwd=root,
        timeout=300,
    )
    if install.returncode != 0:
        raise UpdateCheckFailed(
            "Update was pulled but reinstalling failed:\n" + (install.stderr or install.stdout)
        )


def relaunch() -> None:
    """Starts a fresh instance of the app and lets the caller quit this
    one -- `open -a` is macOS-only, matching this tool's primary supported
    install path (see README's click-through QC.app install)."""
    subprocess.Popen(["open", "-a", "QC"])
