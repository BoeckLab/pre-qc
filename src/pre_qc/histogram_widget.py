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
from qtpy.QtWidgets import QVBoxLayout, QWidget

_N_BINS = 64


class HistogramWidget(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.figure = Figure(figsize=(3, 2.2))
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout.addWidget(self.canvas)
        self.ax = self.figure.add_subplot(111)
        self._draw_empty()

    def _draw_empty(self) -> None:
        self.ax.clear()
        self.ax.set_title(f"{self._title} — no frame loaded", fontsize=8)
        self.ax.tick_params(labelsize=6)
        self.figure.tight_layout()
        self.canvas.draw_idle()

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
        self.canvas.draw_idle()
