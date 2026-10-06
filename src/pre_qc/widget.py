"""napari dock widget driving the good/bad review loop.

One row (= one movie) is shown at a time as per-channel image layers (one
additive-blended layer per channel; napari's own slider scrubs frames).
Marking good/bad saves the sidecar results CSV immediately, so quitting
mid-review never loses progress, then auto-advances to the next unreviewed
row.
"""

from pathlib import Path

from qtpy.QtWidgets import (
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import manifest
from .io import load_any_movie
from .metrics import compute_batch_metrics
from .report import write_html_report


class QCWidget(QWidget):
    def __init__(self, viewer, csv_path, rows, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.csv_path = Path(csv_path)
        self.rows = rows
        self.index = self._first_unreviewed_index()
        self._layers = []

        layout = QVBoxLayout()
        self.setLayout(layout)
        layout.addWidget(self._build_box())
        layout.addStretch()

        self.viewer.bind_key("g", lambda v: self._mark("good"), overwrite=True)
        self.viewer.bind_key("b", lambda v: self._mark("bad"), overwrite=True)

        self._load_current()

    # ------------------------------------------------------------------

    def _build_box(self) -> QGroupBox:
        box = QGroupBox("QC review")
        vbox = QVBoxLayout()
        box.setLayout(vbox)

        self.position_label = QLabel()
        vbox.addWidget(self.position_label)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet("color: #888;")
        vbox.addWidget(self.path_label)

        hint = QLabel("Scroll frames with napari's slider, then mark GOOD (g) or BAD (b).")
        hint.setWordWrap(True)
        vbox.addWidget(hint)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("optional note")
        vbox.addWidget(self.note_edit)

        good_btn = QPushButton("Mark GOOD (g)")
        good_btn.clicked.connect(lambda: self._mark("good"))
        vbox.addWidget(good_btn)

        bad_btn = QPushButton("Mark BAD (b)")
        bad_btn.clicked.connect(lambda: self._mark("bad"))
        vbox.addWidget(bad_btn)

        prev_btn = QPushButton("← Prev")
        prev_btn.clicked.connect(self._go_prev)
        vbox.addWidget(prev_btn)
        next_btn = QPushButton("Next →")
        next_btn.clicked.connect(self._go_next)
        vbox.addWidget(next_btn)

        self.progress_label = QLabel()
        vbox.addWidget(self.progress_label)

        self.finish_btn = QPushButton("Finish — compute analyzability report")
        self.finish_btn.clicked.connect(self._on_finish)
        vbox.addWidget(self.finish_btn)

        return box

    # ------------------------------------------------------------------

    def _first_unreviewed_index(self) -> int:
        for i, row in enumerate(self.rows):
            if row.status == "unreviewed" and not row.resolution_error:
                return i
        return 0

    def _current_row(self):
        if not self.rows:
            return None
        return self.rows[self.index]

    def _load_current(self) -> None:
        for layer in self._layers:
            if layer in self.viewer.layers:
                self.viewer.layers.remove(layer)
        self._layers = []

        row = self._current_row()
        self._refresh_labels(row)
        self.note_edit.setText(row.note if row else "")

        if row is None or row.resolution_error:
            return

        movie = load_any_movie(row.resolved_path)
        for c, name in enumerate(movie.channel_names):
            layer = self.viewer.add_image(movie.data[:, c, :, :], name=name, blending="additive")
            self._layers.append(layer)
        if self.viewer.dims.current_step:
            self.viewer.dims.current_step = (0,) * len(self.viewer.dims.current_step)

    def _refresh_labels(self, row) -> None:
        summary = manifest.review_summary(self.rows)
        self.progress_label.setText(
            f"reviewed {summary['n_good'] + summary['n_bad']}/{summary['n_total']} "
            f"({summary['n_good']} good, {summary['n_bad']} bad, "
            f"{summary['n_unresolved']} unresolved)"
        )
        if row is None:
            self.position_label.setText("(no rows)")
            self.path_label.setText("")
            return
        self.position_label.setText(
            f"[{self.index + 1}/{len(self.rows)}] {row.position} — status: {row.status}"
        )
        self.path_label.setText(row.resolution_error or row.resolved_path)

    def _mark(self, status: str) -> None:
        row = self._current_row()
        if row is None or row.resolution_error:
            return
        manifest.mark(row, status, self.note_edit.text())
        manifest.save_results(self.csv_path, self.rows)
        self._go_next()

    def _go_next(self) -> None:
        if self.index < len(self.rows) - 1:
            self.index += 1
            self._load_current()
        else:
            self._refresh_labels(self._current_row())

    def _go_prev(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._load_current()

    def _on_finish(self) -> None:
        summary = manifest.review_summary(self.rows)
        if not summary["all_reviewed"]:
            QMessageBox.warning(
                self,
                "Not all rows reviewed",
                f"{summary['n_unreviewed']} row(s) still unreviewed. "
                "Review every row before computing the analyzability report.",
            )
            return
        if summary["n_bad"] > 0:
            bad_positions = ", ".join(r.position for r in self.rows if r.status == "bad")
            QMessageBox.warning(
                self,
                "Bad movies present",
                f"{summary['n_bad']} movie(s) marked BAD: {bad_positions}.\n\n"
                "Fix/exclude these before running the analyzability report -- "
                "it only runs once every reviewable movie is GOOD.",
            )
            return
        if summary["n_unresolved"] > 0:
            unresolved_positions = ", ".join(r.position for r in self.rows if r.resolution_error)
            proceed = QMessageBox.question(
                self,
                "Unresolved rows in CSV",
                f"{summary['n_unresolved']} row(s) couldn't be matched to a file and were "
                f"never reviewed: {unresolved_positions}.\n\n"
                "Fix the experiment_path/position in the input CSV and re-run to include them. "
                "Continue and generate the report for the resolvable movies anyway?",
            )
            if proceed != QMessageBox.Yes:
                return

        summary_df, per_movie = compute_batch_metrics(self.rows)
        out_path = write_html_report(
            summary_df, per_movie, self.csv_path.with_name(f"{self.csv_path.stem}_qc_report.html")
        )
        summary_df.to_csv(self.csv_path.with_name(f"{self.csv_path.stem}_qc_metrics.csv"), index=False)
        QMessageBox.information(
            self,
            "Report written",
            f"All {summary['n_good']} movies marked good.\n\nReport: {out_path}",
        )
