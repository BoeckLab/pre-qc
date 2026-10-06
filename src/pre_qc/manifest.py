"""CSV-driven manifest of movies to review, with crash-safe progress.

Input CSV: one row per well/position to check, columns ``experiment_path``
and ``position`` (extra columns, e.g. a human-readable ``condition``, are
carried through untouched). Progress is written incrementally to a sidecar
``<input>_qc_results.csv`` next to the input CSV, so quitting mid-review and
re-running the tool on the same input resumes instead of restarting.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .io import MovieResolutionError, resolve_movie_path

REQUIRED_COLUMNS = ("experiment_path", "position")
STATUSES = ("unreviewed", "good", "bad")


@dataclass
class QCRow:
    experiment_path: str
    position: str
    extra: dict = field(default_factory=dict)
    resolved_path: str = ""
    resolution_error: str = ""
    status: str = "unreviewed"
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
            experiment_path=record["experiment_path"],
            position=record["position"],
            extra={c: record[c] for c in extra_cols},
        )
        try:
            row.resolved_path = str(resolve_movie_path(row.experiment_path, row.position))
        except MovieResolutionError as exc:
            row.resolution_error = str(exc)
        rows.append(row)
    return rows


def load_or_resume(csv_path) -> list:
    """Resolve the input CSV, then overlay any previously saved
    status/note from the sidecar results file (matched by experiment_path +
    position, so re-ordering or appending rows to the input CSV doesn't
    desync progress)."""
    rows = load_manifest(csv_path)
    results_path = results_path_for(csv_path)
    if not results_path.exists():
        return rows

    saved = pd.read_csv(results_path, dtype=str).fillna("")
    saved_by_key = {
        (r["experiment_path"], r["position"]): r for _, r in saved.iterrows()
    }
    for row in rows:
        prior = saved_by_key.get((row.experiment_path, row.position))
        if prior is not None:
            row.status = prior.get("status", "unreviewed") or "unreviewed"
            row.note = prior.get("note", "")
            row.reviewed_at = prior.get("reviewed_at", "")
    return rows


def mark(row: QCRow, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    row.status = status
    row.note = note
    row.reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_results(csv_path, rows: list) -> Path:
    out_path = results_path_for(csv_path)
    records = []
    for row in rows:
        record = {
            "experiment_path": row.experiment_path,
            "position": row.position,
            **row.extra,
            "resolved_path": row.resolved_path,
            "resolution_error": row.resolution_error,
            "status": row.status,
            "note": row.note,
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
