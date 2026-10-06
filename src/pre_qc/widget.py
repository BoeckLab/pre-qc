"""napari dock widget driving the good/bad review loop.

One row (= one movie) is shown at a time as per-channel image layers (one
additive-blended layer per channel; napari's own slider scrubs frames).
Marking good/bad saves the sidecar results CSV immediately, so quitting
mid-review never loses progress, then auto-advances to the next unreviewed
row.

Movies are loaded on a background QThread, not the GUI thread: these can be
large (full-resolution multi-hundred-frame stacks) and routinely live on a
network mount (sciCORE via SSHFS/SMB), so a synchronous read can take
anywhere from several seconds to over a minute. Loading it inline in
``__init__``/the GUI thread would freeze event processing before the window
even finishes its first paint -- napari's main window can appear to never
open at all, not just be slow, since nothing repaints until the blocking
call returns.
"""

from pathlib import Path

from qtpy.QtCore import QObject, QThread, Signal
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


class _MovieLoadWorker(QObject):
    finished = Signal(object, object)  # (Movie or None, error message or None)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self) -> None:
        try:
            movie = load_any_movie(self.path)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self.finished.emit(None, str(exc))
        else:
            self.finished.emit(movie, None)


class QCWidget(QWidget):
    def __init__(self, viewer, csv_path, rows, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.csv_path = Path(csv_path)
        self.rows = rows
        self.index = self._first_unreviewed_index()
        self._layers = []
        self._load_thread = None
        self._load_worker = None
        self._load_token = 0  # guards against a stale load finishing after Next/Prev moved on

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

        self.good_btn = QPushButton("Mark GOOD (g)")
        self.good_btn.clicked.connect(lambda: self._mark("good"))
        vbox.addWidget(self.good_btn)

        self.bad_btn = QPushButton("Mark BAD (b)")
        self.bad_btn.clicked.connect(lambda: self._mark("bad"))
        vbox.addWidget(self.bad_btn)

        self.prev_btn = QPushButton("← Prev")
        self.prev_btn.clicked.connect(self._go_prev)
        vbox.addWidget(self.prev_btn)
        self.next_btn = QPushButton("Next →")
        self.next_btn.clicked.connect(self._go_next)
        vbox.addWidget(self.next_btn)

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

        self._load_token += 1
        token = self._load_token
        self._set_controls_enabled(False)
        self.path_label.setText(f"loading {row.resolved_path} ...")

        thread = QThread(self)
        worker = _MovieLoadWorker(row.resolved_path)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda movie, error: self._on_movie_loaded(token, movie, error))
        worker.finished.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        # Keep references alive on self -- nothing else holds them, and a
        # GC'd QThread/QObject mid-run is a crash, not a no-op.
        self._load_thread = thread
        self._load_worker = worker
        thread.start()

    def _on_movie_loaded(self, token: int, movie, error) -> None:
        if token != self._load_token:
            return  # user already navigated away (Next/Prev) -- discard this stale result
        self._set_controls_enabled(True)
        row = self._current_row()
        if error is not None:
            self.path_label.setText(f"FAILED TO LOAD: {error}")
            return
        if row is None:
            return

        for c, name in enumerate(movie.channel_names):
            layer = self.viewer.add_image(movie.data[:, c, :, :], name=name, blending="additive")
            self._layers.append(layer)
        if self.viewer.dims.current_step:
            self.viewer.dims.current_step = (0,) * len(self.viewer.dims.current_step)
        self.path_label.setText(row.resolution_error or row.resolved_path)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.good_btn, self.bad_btn, self.prev_btn, self.next_btn, self.finish_btn):
            widget.setEnabled(enabled)

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
