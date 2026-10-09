"""napari dock widget driving the good/bad review loop.

One row (= one movie) is shown at a time as per-channel image layers (one
additive-blended layer per channel; napari's own slider scrubs frames).
Marking good/bad saves the sidecar results CSV immediately, so quitting
mid-review never loses progress, then auto-advances to the next unreviewed
row.

Two independent per-movie judgments are tracked, plus one experiment-level
decision:
- GOOD/BAD -- can this movie be reviewed at all. Drives a suggested
  experiment-level decision: any BAD -> suggest trashing the whole
  experiment; all GOOD -> suggest keeping it.
- Q/NQ/X label -- for a kept experiment, is this specific movie
  quantifiable, not quantifiable, or needs a second look, for downstream
  post-QC analysis. Independent of GOOD/BAD -- set it whenever useful.
- TRASH/KEEP -- one manual decision per experiment (never auto-applied,
  always a deliberate button click), appended to a shared, cross-experiment
  log (see manifest.append_experiment_decision).

A CSV can be loaded at any time via the "Load CSV…" button (or passed on
the command line / via the initial file-picker) -- loading a new one fully
resets review state (cleared layers, fresh index, fresh decision-box
suggestion) rather than merging with whatever was open before.

Movies are loaded on a background QThread, not the GUI thread: these can be
large (full-resolution multi-hundred-frame stacks) and routinely live on a
network mount (e.g. SSHFS/SMB to a remote server), so a synchronous read can take
anywhere from several seconds to over a minute. Loading it inline in
``__init__``/the GUI thread would freeze event processing before the window
even finishes its first paint -- napari's main window can appear to never
open at all, not just be slow, since nothing repaints until the blocking
call returns.
"""

import webbrowser
from pathlib import Path

import numpy as np
from qtpy.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from qtpy.QtGui import QDesktopServices, QPixmap
from qtpy.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import manifest, updater
from .checklist import CHECKLIST_ITEMS
from .io import load_any_movie

# Opens on GitHub rather than rendering in-app -- same call cell-slate's own
# help_widget.py makes: an embedded QTextBrowser chokes on a README with
# tables/images, and GitHub already renders this natively.
_README_URL = "https://github.com/BoeckLab/pre-qc/blob/main/README.md"

_BANNER_PATH = Path(__file__).resolve().parent / "assets" / "qc-banner.png"
_BANNER_WIDTH = 300


def _build_banner():
    """Top-of-dock banner image -- returns None (not an empty widget) if
    the asset is missing, same convention as cell-slate's own
    tools_widget._build_header, so a checkout without the binary asset
    still runs instead of crashing on a missing file."""
    if not _BANNER_PATH.exists():
        return None
    pixmap = QPixmap(str(_BANNER_PATH))
    if pixmap.isNull():
        return None
    if pixmap.width() != _BANNER_WIDTH:
        pixmap = pixmap.scaledToWidth(_BANNER_WIDTH, Qt.SmoothTransformation)
    label = QLabel()
    label.setPixmap(pixmap)
    return label
from .metrics import compute_batch_metrics
from .report import write_html_report


class _MovieLoadWorker(QObject):
    # (token, Movie or None, error message or None). The token travels with
    # the signal itself (rather than being captured in a lambda at connect
    # time) so this can connect straight to a bound QObject method -- see
    # QCWidget._load_current's comment on why that matters for thread safety.
    finished = Signal(int, object, object)

    def __init__(self, path, token: int):
        super().__init__()
        self.path = path
        self.token = token

    def run(self) -> None:
        try:
            movie = load_any_movie(self.path)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self.finished.emit(self.token, None, str(exc))
        else:
            self.finished.emit(self.token, movie, None)


class _ReportWorker(QObject):
    # (html report path or None, error message or None). Same
    # bound-method-only connection rule as _MovieLoadWorker -- see
    # QCWidget._load_current's comment.
    finished = Signal(object, object)

    def __init__(self, rows, csv_path):
        super().__init__()
        self.rows = rows
        self.csv_path = csv_path

    def run(self) -> None:
        try:
            summary_df, per_movie = compute_batch_metrics(self.rows)
            out_path = write_html_report(
                summary_df, per_movie, self.csv_path.with_name(f"{self.csv_path.stem}_qc_report.html")
            )
            summary_df.to_csv(
                self.csv_path.with_name(f"{self.csv_path.stem}_qc_metrics.csv"), index=False
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self.finished.emit(None, str(exc))
        else:
            self.finished.emit(str(out_path), None)


class QCWidget(QWidget):
    def __init__(
        self, viewer, csv_path=None, rows=None, parent=None, hist_bf=None, hist_fl=None, measures=None
    ):
        super().__init__(parent)
        self.viewer = viewer
        self.csv_path = Path(csv_path) if csv_path else None
        self.rows = rows or []
        self._exps = []
        self.index = self._first_unreviewed_index()
        self._layers = []
        self._load_thread = None
        self._load_worker = None
        self._load_token = 0  # guards against a stale load finishing after Next/Prev moved on
        self._loading_dialog = None
        self._report_thread = None
        self._report_worker = None
        # Live per-frame histogram/measures panels docked elsewhere in the
        # left column (see app.py) -- optional, pushed to on movie
        # load/frame scrub rather than owned by this widget.
        self.hist_bf = hist_bf
        self.hist_fl = hist_fl
        self.measures = measures

        layout = QVBoxLayout()
        self.setLayout(layout)
        banner = _build_banner()
        if banner is not None:
            layout.addWidget(banner, alignment=Qt.AlignHCenter)
        layout.addWidget(self._build_load_box())
        layout.addWidget(self._build_decision_box())
        layout.addWidget(self._build_box())
        layout.addWidget(self._build_update_box())
        layout.addStretch()

        self.viewer.bind_key("g", lambda v: self._mark("good"), overwrite=True)
        self.viewer.bind_key("b", lambda v: self._mark("bad"), overwrite=True)
        self.viewer.dims.events.current_step.connect(self._on_frame_changed)

        self._refresh_decision_box()

        # Deferred rather than called directly: at this point in __init__,
        # the caller (app.py) hasn't embedded this widget into the main
        # window yet (that happens via add_dock_widget right after
        # construction) -- self.window() would still resolve to this
        # widget itself as an orphan, never-shown top-level window, so the
        # first movie's loading popup (parented via self.window() in
        # _show_busy_dialog) would be created but never actually
        # visible. Scheduling this for the next event-loop iteration
        # instead means it runs after add_dock_widget has already given
        # this widget a real parent window.
        QTimer.singleShot(0, self._load_current)

    # ------------------------------------------------------------------

    def _build_load_box(self) -> QGroupBox:
        box = QGroupBox("Experiment CSV")
        vbox = QVBoxLayout()
        box.setLayout(vbox)

        self.csv_label = QLabel(str(self.csv_path) if self.csv_path else "(no CSV loaded)")
        self.csv_label.setWordWrap(True)
        self.csv_label.setStyleSheet("color: #888;")
        vbox.addWidget(self.csv_label)

        load_btn = QPushButton("Load CSV…")
        load_btn.clicked.connect(self._on_load_csv_clicked)
        vbox.addWidget(load_btn)

        return box

    def _build_decision_box(self) -> QGroupBox:
        box = QGroupBox("Experiment decision — TRASH / KEEP")
        vbox = QVBoxLayout()
        box.setLayout(vbox)

        self.decision_info_label = QLabel("")
        self.decision_info_label.setWordWrap(True)
        vbox.addWidget(self.decision_info_label)

        hbox = QHBoxLayout()
        self.keep_btn = QPushButton("Keep experiment")
        self.keep_btn.clicked.connect(lambda: self._on_decide("keep"))
        hbox.addWidget(self.keep_btn)
        self.trash_btn = QPushButton("Trash experiment")
        self.trash_btn.clicked.connect(lambda: self._on_decide("trash"))
        hbox.addWidget(self.trash_btn)
        vbox.addLayout(hbox)

        return box

    def _build_box(self) -> QGroupBox:
        box = QGroupBox("QC review")
        vbox = QVBoxLayout()
        box.setLayout(vbox)

        self.position_label = QLabel()
        self.position_label.setWordWrap(True)
        vbox.addWidget(self.position_label)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("optional note")
        vbox.addWidget(self.note_edit)

        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save_clicked)
        vbox.addWidget(self.save_btn)

        self.good_btn = QPushButton("Mark GOOD (g)")
        self.good_btn.clicked.connect(lambda: self._mark("good"))
        vbox.addWidget(self.good_btn)

        self.bad_btn = QPushButton("Mark BAD (b)")
        self.bad_btn.clicked.connect(lambda: self._mark("bad"))
        vbox.addWidget(self.bad_btn)

        label_hint = QLabel("Post-QC label (for a kept experiment):")
        label_hint.setWordWrap(True)
        vbox.addWidget(label_hint)

        label_hbox = QHBoxLayout()
        self.label_q_btn = QPushButton("Q")
        self.label_q_btn.setToolTip("Quantifiable")
        self.label_q_btn.clicked.connect(lambda: self._set_label("Q"))
        label_hbox.addWidget(self.label_q_btn)
        self.label_nq_btn = QPushButton("NQ")
        self.label_nq_btn.setToolTip("Not quantifiable")
        self.label_nq_btn.clicked.connect(lambda: self._set_label("NQ"))
        label_hbox.addWidget(self.label_nq_btn)
        self.label_x_btn = QPushButton("X")
        self.label_x_btn.setToolTip("Check")
        self.label_x_btn.clicked.connect(lambda: self._set_label("X"))
        label_hbox.addWidget(self.label_x_btn)
        vbox.addLayout(label_hbox)

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

    def _build_update_box(self) -> QGroupBox:
        box = QGroupBox("Help & updates")
        hbox = QHBoxLayout()
        box.setLayout(hbox)

        checklist_btn = QPushButton("QC checklist")
        checklist_btn.clicked.connect(self._on_checklist_clicked)
        hbox.addWidget(checklist_btn)

        tutorial_btn = QPushButton("Tutorial")
        tutorial_btn.clicked.connect(self._on_tutorial_clicked)
        hbox.addWidget(tutorial_btn)

        self.update_btn = QPushButton("Check for Updates")
        self.update_btn.clicked.connect(self._on_check_for_updates)
        hbox.addWidget(self.update_btn)

        self.update_status_label = QLabel("")
        hbox.addWidget(self.update_status_label)
        hbox.addStretch()

        return box

    def _on_checklist_clicked(self) -> None:
        bullet_text = "\n\n".join(f"• {item}" for item in CHECKLIST_ITEMS)
        QMessageBox.information(self, "What to check for", bullet_text)

    def _on_tutorial_clicked(self) -> None:
        QDesktopServices.openUrl(QUrl(_README_URL))

    def _on_check_for_updates(self) -> None:
        # A plain git fetch/rev-parse is quick (seconds) and bounded, so
        # unlike movie loading/report computation this runs directly on
        # the GUI thread rather than needing a background QThread -- just
        # disable the button for the duration so a second click can't
        # overlap it.
        self.update_status_label.setText("Checking…")
        self.update_btn.setEnabled(False)
        try:
            available = updater.check_for_update()
        finally:
            self.update_btn.setEnabled(True)

        if not available:
            self.update_status_label.setText("Up to date")
            return

        self.update_status_label.setText("Update available")
        choice = QMessageBox.question(
            self,
            "Update available",
            "A newer version of pre-qc is available on GitHub. Update now?\n\n"
            "The app will close and reopen automatically. Any unsaved review "
            "progress is already saved to disk continuously, so nothing is lost.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if choice != QMessageBox.Yes:
            return

        try:
            updater.apply_update()
        except updater.UpdateCheckFailed as exc:
            QMessageBox.warning(self, "Update failed", str(exc))
            return

        updater.relaunch()
        from qtpy.QtWidgets import QApplication

        QApplication.instance().quit()

    # ------------------------------------------------------------------
    # CSV loading / experiment switching

    def _on_load_csv_clicked(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select the QC input CSV", "", "CSV files (*.csv)"
        )
        if not path:
            return
        self.open_csv(path)

    def open_csv(self, csv_path) -> None:
        """Load (or resume) ``csv_path`` as the active experiment,
        discarding whatever was previously loaded -- cleared layers, fresh
        review index, fresh decision-box state. Safe to call at any time,
        including when a different CSV is already mid-review."""
        try:
            rows = manifest.load_or_resume(csv_path)
        except ValueError as exc:
            QMessageBox.critical(self, "Failed to load CSV", str(exc))
            return
        if not rows:
            QMessageBox.warning(self, "No rows", f"No rows found in {csv_path}")
            return

        n_unresolved = sum(1 for r in rows if r.resolution_error)
        if n_unresolved:
            print(f"Warning: {n_unresolved}/{len(rows)} row(s) could not be resolved to a file:")
            for row in rows:
                if row.resolution_error:
                    print(f"  - {row.well}/{row.frame}: {row.resolution_error}")

        for layer in self._layers:
            if layer in self.viewer.layers:
                self.viewer.layers.remove(layer)
        self._layers = []
        self._load_token += 1  # invalidate any in-flight load from the previous CSV

        self.csv_path = Path(csv_path)
        self.rows = rows
        self.index = self._first_unreviewed_index()
        self.csv_label.setText(str(self.csv_path))
        self._refresh_decision_box()
        self._load_current()

    # ------------------------------------------------------------------
    # Experiment-level trash/keep decision

    def _refresh_decision_box(self) -> None:
        if not self.rows:
            self.decision_info_label.setText("Load a CSV to decide trash/keep.")
            self.keep_btn.setEnabled(False)
            self.trash_btn.setEnabled(False)
            return

        self.keep_btn.setEnabled(True)
        self.trash_btn.setEnabled(True)
        self._exps = sorted(set(r.exp for r in self.rows))
        suggestion = manifest.suggested_decision(self.rows)
        summary = manifest.review_summary(self.rows)

        lines = [f"Suggested: {suggestion.upper()} ({summary['n_bad']} bad / {summary['n_total']} total)"]
        for exp in self._exps:
            last = manifest.last_experiment_decision(exp)
            if last:
                lines.append(f"{Path(exp).name}: last recorded = {last['decision']} ({last['timestamp']})")
        self.decision_info_label.setText("\n".join(lines))

    def _on_decide(self, decision: str) -> None:
        if not self.rows:
            return
        for exp in self._exps:
            manifest.append_experiment_decision(exp, decision, self.rows)
        self._refresh_decision_box()
        QMessageBox.information(
            self,
            "Decision recorded",
            f"Recorded '{decision}' for {len(self._exps)} experiment(s) in:\n{manifest.DECISIONS_LOG_PATH}",
        )

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
        self.viewer.text_overlay.visible = False
        self._update_histograms()

        row = self._current_row()
        self._refresh_labels(row)
        self.note_edit.setText(row.note if row else "")

        if row is None or row.resolution_error:
            return

        self._load_token += 1
        token = self._load_token
        self._set_controls_enabled(False)
        self._show_busy_dialog(f"Loading movie:\n{row.resolved_path}")

        thread = QThread(self)
        worker = _MovieLoadWorker(row.resolved_path, token)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Connect straight to the bound method (never a lambda/closure) --
        # Qt only auto-detects "this must be marshaled back to the GUI
        # thread" when the slot is a bound method of a QObject, so it can
        # read the receiver's (self's) thread affinity. A lambda has no
        # such affinity, so Qt has no reliable signal to fall back to a
        # queued connection -- it can end up invoking _on_movie_loaded (and
        # therefore viewer.add_image(), which touches Qt/napari internals)
        # directly on this background thread, which napari/Qt do not
        # support and can crash on.
        worker.finished.connect(self._on_movie_loaded)
        worker.finished.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        # Keep references alive on self -- nothing else holds them, and a
        # GC'd QThread/QObject mid-run is a crash, not a no-op.
        self._load_thread = thread
        self._load_worker = worker
        thread.start()

    def _show_busy_dialog(self, message: str) -> None:
        # Indeterminate (min == max == 0) -- there's no byte-level progress
        # to report, just "still working". No cancel button: cancelling a
        # load/report run mid-flight isn't supported, so offering one would
        # be a dead end. Parented to the top-level window (not `self`,
        # which is just the side dock) so it actually centers over the
        # napari canvas.
        dialog = QProgressDialog(message, None, 0, 0, self.window())
        dialog.setWindowTitle("pre-qc")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setCancelButton(None)
        dialog.show()
        self._loading_dialog = dialog

    def _close_busy_dialog(self) -> None:
        if self._loading_dialog is not None:
            self._loading_dialog.close()
            self._loading_dialog = None

    def _on_movie_loaded(self, token: int, movie, error) -> None:
        if token != self._load_token:
            return  # user already navigated away (Next/Prev) -- discard this stale result
        self._close_busy_dialog()
        self._set_controls_enabled(True)
        row = self._current_row()
        if error is not None:
            self.position_label.setText(f"FAILED TO LOAD: {error}")
            return
        if row is None:
            return

        try:
            for c, name in enumerate(movie.channel_names):
                layer = self.viewer.add_image(movie.data[:, c, :, :], name=name, blending="additive")
                self._layers.append(layer)
            if self.viewer.dims.current_step:
                self.viewer.dims.current_step = (0,) * len(self.viewer.dims.current_step)

            # So the reviewer always knows what they're looking at without
            # reading the dock -- COND if the CSV has one, else WELL/FRAME.
            self.viewer.text_overlay.text = self._condition_text(row)
            self.viewer.text_overlay.position = "top_left"
            self.viewer.text_overlay.visible = True
        finally:
            # Always, even if a napari-internal error interrupted the layer
            # setup above (this has happened -- see the layer-controls
            # KeyError fix) -- the histograms are useless if a problem
            # elsewhere in this method silently skips the line that feeds
            # them real data, leaving them stuck on "no frame loaded".
            self._update_histograms()

    @staticmethod
    def _condition_text(row) -> str:
        cond = row.extra.get("COND") or row.extra.get("condition")
        return cond if cond else f"{row.well}/{row.frame}"

    def _on_frame_changed(self, event=None) -> None:
        self._update_histograms()

    def _update_histograms(self) -> None:
        """Push the currently-displayed frame's pixel data into the live
        histogram/measures panels (see app.py/histogram_widget.py) --
        channel 0 is BF, channel 1 (if present) is PI/FL, matching the
        convention used throughout metrics.py. Clears everything when
        nothing is loaded."""
        if not self._layers:
            if self.hist_bf is not None:
                self.hist_bf.set_frame(None)
            if self.hist_fl is not None:
                self.hist_fl.set_frame(None)
            if self.measures is not None:
                self.measures.set_frames(None)
            return

        t = self.viewer.dims.current_step[0] if self.viewer.dims.current_step else 0
        bf_frame = np.asarray(self._layers[0].data[t])
        fl_frame = np.asarray(self._layers[1].data[t]) if len(self._layers) > 1 else None

        if self.hist_bf is not None:
            self.hist_bf.set_frame(bf_frame)
        if self.hist_fl is not None:
            self.hist_fl.set_frame(fl_frame)
        if self.measures is not None:
            self.measures.set_frames(bf_frame, fl_frame)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.good_btn,
            self.bad_btn,
            self.label_q_btn,
            self.label_nq_btn,
            self.label_x_btn,
            self.save_btn,
            self.prev_btn,
            self.next_btn,
            self.finish_btn,
        ):
            widget.setEnabled(enabled)

    def _refresh_labels(self, row) -> None:
        summary = manifest.review_summary(self.rows)
        self.progress_label.setText(
            f"reviewed {summary['n_good'] + summary['n_bad']}/{summary['n_total']} "
            f"({summary['n_good']} good, {summary['n_bad']} bad, "
            f"{summary['n_unresolved']} unresolved)"
        )
        if row is None:
            self.position_label.setText("(no rows loaded)")
            return
        text = (
            f"[{self.index + 1}/{len(self.rows)}] {row.well}/{row.frame} — "
            f"status: {row.status} — label: {row.label or '(none)'}"
        )
        if row.resolution_error:
            text += f"\n{row.resolution_error}"
        self.position_label.setText(text)

    def _mark(self, status: str) -> None:
        row = self._current_row()
        if row is None or row.resolution_error:
            return
        manifest.mark(row, status=status, note=self.note_edit.text())
        manifest.save_results(self.csv_path, self.rows)
        self._refresh_decision_box()
        self._refresh_labels(row)
        # A GOOD movie without a Q/NQ/X label yet stays put -- the post-QC
        # label is the point of marking it good, not an afterthought, so
        # don't let auto-advance skip past it unlabeled. BAD movies have no
        # such requirement (there's nothing to label), so they still
        # auto-advance immediately.
        if status != "good" or row.label:
            self._go_next()

    def _set_label(self, label: str) -> None:
        row = self._current_row()
        if row is None or row.resolution_error:
            return
        manifest.mark(row, label=label)
        manifest.save_results(self.csv_path, self.rows)
        self._refresh_labels(row)
        if row.status == "good":
            self._go_next()

    def _on_save_clicked(self) -> None:
        row = self._current_row()
        if row is None or row.resolution_error:
            return
        manifest.mark(row, note=self.note_edit.text())
        manifest.save_results(self.csv_path, self.rows)
        self._refresh_labels(row)

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
            bad_positions = ", ".join(f"{r.well}/{r.frame}" for r in self.rows if r.status == "bad")
            QMessageBox.warning(
                self,
                "Bad movies present",
                f"{summary['n_bad']} movie(s) marked BAD: {bad_positions}.\n\n"
                "Fix/exclude these before running the analyzability report -- "
                "it only runs once every reviewable movie is GOOD.",
            )
            return
        if summary["n_unresolved"] > 0:
            unresolved_positions = ", ".join(f"{r.well}/{r.frame}" for r in self.rows if r.resolution_error)
            proceed = QMessageBox.question(
                self,
                "Unresolved rows in CSV",
                f"{summary['n_unresolved']} row(s) couldn't be matched to a file and were "
                f"never reviewed: {unresolved_positions}.\n\n"
                "Fix EXP/WELL/FRAME in the input CSV and re-run to include them. "
                "Continue and generate the report for the resolvable movies anyway?",
            )
            if proceed != QMessageBox.Yes:
                return

        self._n_good_for_report_message = summary["n_good"]
        self._set_controls_enabled(False)
        self._show_busy_dialog(f"Computing analyzability report for {summary['n_good']} movies...")

        thread = QThread(self)
        worker = _ReportWorker(self.rows, self.csv_path)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_report_finished)  # bound method, not a lambda -- see above
        worker.finished.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self._report_thread = thread
        self._report_worker = worker
        thread.start()

    def _on_report_finished(self, out_path, error) -> None:
        self._close_busy_dialog()
        self._set_controls_enabled(True)
        if error is not None:
            QMessageBox.critical(self, "Report failed", f"Could not compute the report:\n\n{error}")
            return
        webbrowser.open(Path(out_path).resolve().as_uri())
        QMessageBox.information(
            self,
            "Report written",
            f"All {self._n_good_for_report_message} movies marked good.\n\n"
            f"Report: {out_path}\n\n(opened in your browser)",
        )


class CollapsibleSection(QWidget):
    """A clickable arrowed banner that shows/hides its own content directly
    below it when clicked -- an inline accordion section, not a separate
    panel remotely controlling something elsewhere."""

    def __init__(self, title: str, content: QWidget, expanded: bool = True, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self._header = QToolButton()
        self._header.setCheckable(True)
        self._header.setChecked(expanded)
        self._header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._header.setStyleSheet(
            "QToolButton { border: none; text-align: left; font-weight: bold; }"
        )
        self._header.setText(title)
        self._header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._header.toggled.connect(self._on_toggled)
        layout.addWidget(self._header)

        self._content = content
        self._content.setVisible(expanded)
        layout.addWidget(self._content)

    def _on_toggled(self, checked: bool) -> None:
        self._content.setVisible(checked)
        self._header.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)


class LeftPanelsWidget(QWidget):
    """One combined dock holding the two live histogram panels plus a
    small density/SNR measures readout, as inline accordion sections (see
    CollapsibleSection) -- each collapses independently via its own
    banner. The checklist itself lives behind a popup button in the Help &
    updates box instead (see
    QCWidget._build_update_box/_on_checklist_clicked) rather than
    permanently occupying space here."""

    def __init__(self, hist_bf: QWidget, hist_fl: QWidget, measures: QWidget, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        self.setLayout(layout)
        layout.addWidget(CollapsibleSection("Histogram BF", hist_bf), 3)
        layout.addWidget(CollapsibleSection("Histogram FL", hist_fl), 3)
        layout.addWidget(CollapsibleSection("Density measures", measures), 0)
