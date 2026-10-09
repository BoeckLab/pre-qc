"""Live per-frame intensity histogram panel for the left dock column.

Shows a reviewer the current frame's pixel-intensity distribution while
they scrub through a movie -- the same cells-vs-background density signal
the post-hoc HTML report computes (see report.py's
``_bf_histogram_figure``), but live and interactive instead of a single
summed-over-all-frames snapshot generated only at Finish time.
"""

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from qtpy.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from .metrics import _density_class, _foreground_fraction, _signal_ratio, _snr

_N_BINS = 64


class HistogramWidget(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.figure = Figure(figsize=(3, 5))
        self.canvas = FigureCanvasQTAgg(self.figure)
        # Stretch to fill whatever height the dock actually has (figsize is
        # only the initial hint) -- without this the canvas stays pinned
        # near its minimum size and most of the taller dock goes unused.
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.canvas)
        self.ax = self.figure.add_subplot(111)
        self._draw_empty()

    def _draw_empty(self) -> None:
        self.ax.clear()
        self.ax.set_title(f"{self._title} — no frame loaded", fontsize=8)
        self.ax.tick_params(labelsize=6)
        self.figure.tight_layout()
        # Immediate, not draw_idle() -- this is already called from the GUI
        # thread on a real event (movie loaded / frame scrubbed), so there's
        # no reason to defer and risk the repaint getting coalesced away by
        # whatever else the event loop does before an idle slot fires.
        self.canvas.draw()

    def set_frame(self, frame: np.ndarray) -> None:
        """Redraw for a single 2D frame, or clear if ``frame`` is None
        (no movie loaded, or this channel doesn't exist for it)."""
        if frame is None:
            self._draw_empty()
            return
        self.ax.clear()
        counts, bin_edges = np.histogram(frame.ravel(), bins=_N_BINS)
        centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        self.ax.bar(centers, counts, width=(bin_edges[1] - bin_edges[0]), color="#4c72b0")
        self.ax.set_yscale("log")
        self.ax.set_title(self._title, fontsize=8)
        self.ax.tick_params(labelsize=6)
        self.figure.tight_layout()
        # Immediate, not draw_idle() -- this is already called from the GUI
        # thread on a real event (movie loaded / frame scrubbed), so there's
        # no reason to defer and risk the repaint getting coalesced away by
        # whatever else the event loop does before an idle slot fires.
        self.canvas.draw()


class MeasuresWidget(QWidget):
    """Small live readout of the current frame's density/SNR numbers --
    the same proxies metrics.py computes for the post-hoc report, but
    live, so a reviewer can judge "is this movie dense or not" from an
    actual number instead of eyeballing the histogram shape alone.

    BF foreground fraction (+ its sparse/moderate/dense bucket) is the
    primary density call, but it's a single Otsu-threshold coverage number
    -- BF contrast backs it up (a flat/empty or badly out-of-focus frame
    reads as artificially "sparse" on foreground fraction alone, but also
    has low contrast, so the two together catch that case). PI SNR/signal
    ratio do the same job for the fluorescence channel: positive fraction
    alone can't distinguish a channel with no real dynamic range (SNR/
    signal ratio near the noise floor) from one with genuinely few PI+
    cells."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        self.setLayout(layout)
        self._label = QLabel("no frame loaded")
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

    def set_frames(self, bf_frame: np.ndarray, fl_frame: np.ndarray = None) -> None:
        """``bf_frame`` is required (channel 0); ``fl_frame`` (channel 1,
        PI/fluorescence) is optional -- omitted entirely for single-channel
        movies, same convention as metrics.py."""
        if bf_frame is None:
            self._label.setText("no frame loaded")
            return
        bf_frac = _foreground_fraction(bf_frame)
        lines = [
            f"BF foreground fraction: {bf_frac:.3f}  ({_density_class(bf_frac)})",
            f"BF contrast (std): {float(bf_frame.std()):.1f}",
        ]
        if fl_frame is not None:
            fl_frac = _foreground_fraction(fl_frame)
            lines.append(f"PI SNR: {_snr(fl_frame):.2f}")
            lines.append(f"PI positive fraction: {fl_frac:.3f}")
            lines.append(f"PI signal ratio (p99/median): {_signal_ratio(fl_frame):.2f}")
        self._label.setText("\n".join(lines))
