"""CSV-driven manifest of movies to review, with crash-safe progress.

Input CSV: one row per well/position to check, columns ``EXP`` (the shared
experiment folder path), ``WELL`` (e.g. ``A10``) and ``FRAME`` (e.g.
``p01``) -- a movie is resolved by finding the one file under ``EXP`` whose
name contains *both* ``WELL`` and ``FRAME`` as substrings. Extra columns
(e.g. a human-readable ``COND``) are carried through untouched. Progress is
written incrementally to a sidecar ``<input>_qc_results.csv`` next to the
input CSV, so quitting mid-review and re-running the tool on the same input
resumes instead of restarting.

Two independent labels are tracked per row:
- ``status`` (good/bad) -- can this movie even be reviewed/loaded. Any BAD
  movie means the whole experiment should be trashed; all-GOOD means the
  experiment can be kept.
- ``label`` (Q/NQ/X) -- for a kept experiment, is this *specific* movie
  quantifiable, not quantifiable, or needs a second look for downstream
  post-QC analysis. Meaningless for a trashed experiment.

Experiment-level trash/keep decisions are appended (never overwritten) to a
single shared log across every experiment ever reviewed -- see
``append_experiment_decision``/``DECISIONS_LOG_PATH``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .io import MovieResolutionError, resolve_movie_path

REQUIRED_COLUMNS = ("EXP", "WELL", "FRAME")
STATUSES = ("unreviewed", "good", "bad")
LABELS = ("", "Q", "NQ", "X")
DECISIONS = ("keep", "trash")

# Repo root (.../pre-qc/), three levels up from this file
# (src/pre_qc/manifest.py) -- a fixed, shared location so every
# experiment's trash/keep decision lands in the same append-only log
# regardless of which input CSV produced it.
DECISIONS_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "experiment_decisions.csv"


@dataclass
class QCRow:
    exp: str
    well: str
    frame: str
    extra: dict = field(default_factory=dict)
    resolved_path: str = ""
    resolution_error: str = ""
    status: str = "unreviewed"
    label: str = ""
    note: str = ""
    reviewed_at: str = ""


def results_path_for(csv_path) -> Path:
    csv_path = Path(csv_path)
    return csv_path.with_name(f"{csv_path.stem}_qc_results.csv")


def load_manifest(csv_path) -> list:
    """Read the input CSV and resolve each row to a concrete movie path.
    Resolution failures are recorded on the row (``resolution_error``)
    rather than raised, so one bad row doesn't block reviewing the rest."""
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Input CSV is missing required column(s): {missing}")

    extra_cols = [c for c in df.columns if c not in REQUIRED_COLUMNS]
    rows = []
    for _, record in df.iterrows():
        row = QCRow(
            exp=record["EXP"],
            well=record["WELL"],
            frame=record["FRAME"],
            extra={c: record[c] for c in extra_cols},
        )
        try:
            row.resolved_path = str(resolve_movie_path(row.exp, row.well, row.frame))
        except MovieResolutionError as exc:
            row.resolution_error = str(exc)
        rows.append(row)
    return rows


def load_or_resume(csv_path) -> list:
    """Resolve the input CSV, then overlay any previously saved
    status/label/note from the sidecar results file (matched by EXP + WELL +
    FRAME, so re-ordering or appending rows to the input CSV doesn't desync
    progress)."""
    rows = load_manifest(csv_path)
    results_path = results_path_for(csv_path)
    if not results_path.exists():
        return rows

    saved = pd.read_csv(results_path, dtype=str).fillna("")
    saved_by_key = {
        (r["EXP"], r["WELL"], r["FRAME"]): r for _, r in saved.iterrows()
    }
    for row in rows:
        prior = saved_by_key.get((row.exp, row.well, row.frame))
        if prior is not None:
            row.status = prior.get("status", "unreviewed") or "unreviewed"
            row.label = prior.get("LABEL", "")
            row.note = prior.get("NOTES", "")
            row.reviewed_at = prior.get("reviewed_at", "")
    return rows


def mark(row: QCRow, status: str = None, label: str = None, note: str = None) -> None:
    """Update whichever of status/label/note is provided (any combination),
    stamping ``reviewed_at`` on every call. ``status`` (good/bad) and
    ``label`` (Q/NQ/X) are independent -- set either on its own without
    disturbing the other."""
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        row.status = status
    if label is not None:
        if label not in LABELS:
            raise ValueError(f"label must be one of {LABELS}, got {label!r}")
        row.label = label
    if note is not None:
        row.note = note
    row.reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_results(csv_path, rows: list) -> Path:
    out_path = results_path_for(csv_path)
    records = []
    for row in rows:
        record = {
            "EXP": row.exp,
            "WELL": row.well,
            "FRAME": row.frame,
            **row.extra,
            "resolved_path": row.resolved_path,
            "resolution_error": row.resolution_error,
            "status": row.status,
            "LABEL": row.label,
            "NOTES": row.note,
            "reviewed_at": row.reviewed_at,
        }
        records.append(record)
    df = pd.DataFrame.from_records(records)
    # Write to a temp file then replace, so a crash mid-write never corrupts
    # the previous, still-usable results.
    tmp_path = out_path.with_suffix(".csv.tmp")
    df.to_csv(tmp_path, index=False)
    tmp_path.replace(out_path)
    return out_path


def review_summary(rows: list) -> dict:
    n_total = len(rows)
    n_good = sum(1 for r in rows if r.status == "good")
    n_bad = sum(1 for r in rows if r.status == "bad")
    n_unresolved = sum(1 for r in rows if r.resolution_error)
    # Unresolved rows can never be marked good/bad in the UI (there's no
    # file to show), so "reviewed" only tracks the resolvable rows -- an
    # unfixable typo in the CSV must not permanently block finishing.
    n_reviewable = n_total - n_unresolved
    n_unreviewed = n_reviewable - n_good - n_bad
    return {
        "n_total": n_total,
        "n_good": n_good,
        "n_bad": n_bad,
        "n_unreviewed": n_unreviewed,
        "n_unresolved": n_unresolved,
        "all_reviewed": n_unreviewed == 0,
        "all_good": n_unreviewed == 0 and n_bad == 0 and n_unresolved == 0 and n_good == n_total,
    }


def label_summary(rows: list) -> dict:
    return {
        "n_Q": sum(1 for r in rows if r.label == "Q"),
        "n_NQ": sum(1 for r in rows if r.label == "NQ"),
        "n_X": sum(1 for r in rows if r.label == "X"),
        "n_unlabeled": sum(1 for r in rows if r.label == ""),
    }


def suggested_decision(rows: list) -> str:
    """Any BAD movie -> suggest trashing the whole experiment; otherwise
    (all GOOD, or still unreviewed) -> suggest keeping it. A suggestion
    only -- the actual decision is always a deliberate button click, never
    auto-recorded."""
    summary = review_summary(rows)
    return "trash" if summary["n_bad"] > 0 else "keep"


def append_experiment_decision(exp: str, decision: str, rows: list) -> Path:
    """Append one row to the shared, cross-experiment decisions log --
    never overwritten, so re-deciding (or re-running on the same CSV later)
    just adds another line to the history rather than replacing it."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    summary = review_summary(rows)
    labels = label_summary(rows)
    record = {
        "EXP": exp,
        "decision": decision,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_movies": summary["n_total"],
        "n_good": summary["n_good"],
        "n_bad": summary["n_bad"],
        "n_Q": labels["n_Q"],
        "n_NQ": labels["n_NQ"],
        "n_X": labels["n_X"],
    }
    DECISIONS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = not DECISIONS_LOG_PATH.exists()
    pd.DataFrame.from_records([record]).to_csv(DECISIONS_LOG_PATH, mode="a", header=header, index=False)
    return DECISIONS_LOG_PATH


def last_experiment_decision(exp: str) -> dict:
    """Most recent logged decision for this EXP, or None if it's never been
    decided before."""
    if not DECISIONS_LOG_PATH.exists():
        return None
    df = pd.read_csv(DECISIONS_LOG_PATH, dtype=str).fillna("")
    matches = df[df["EXP"] == exp]
    if matches.empty:
        return None
    return matches.iloc[-1].to_dict()
