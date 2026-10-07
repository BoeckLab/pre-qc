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

import webbrowser
from pathlib import Path

from qtpy.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from qtpy.QtGui import QDesktopServices, QPixmap
from qtpy.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import manifest, updater
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
        self._loading_dialog = None
        self._report_thread = None
        self._report_worker = None

        layout = QVBoxLayout()
        self.setLayout(layout)
        banner = _build_banner()
        if banner is not None:
            layout.addWidget(banner, alignment=Qt.AlignHCenter)
        layout.addWidget(self._build_box())
        layout.addWidget(self._build_update_box())
        layout.addStretch()

        self.viewer.bind_key("g", lambda v: self._mark("good"), overwrite=True)
        self.viewer.bind_key("b", lambda v: self._mark("bad"), overwrite=True)

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

    def _build_update_box(self) -> QGroupBox:
        box = QGroupBox("Help & updates")
        hbox = QHBoxLayout()
        box.setLayout(hbox)

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
