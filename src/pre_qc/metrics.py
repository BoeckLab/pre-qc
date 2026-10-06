"""Cheap, CPU-only "is this movie analyzable" metrics.

Deliberately does not run the real segmentation model -- the whole point of
this QC gate is to catch acquisition-level problems *before* spending GPU
hours, so every metric here is a fast proxy computed directly off pixel
values:

- sharpness: variance-of-Laplacian per frame on channel 0 (assumed
  brightfield, matching the BF-then-FL channel convention used by the ASCT
  pipeline). Low/falling sharpness flags out-of-focus acquisition.
- saturation: fraction of pixels at the dtype's max value, per channel per
  frame. High saturation flags clipped/overexposed fluorescence.
- drift: frame-to-frame phase-cross-correlation shift on channel 0,
  accumulated into a running total. Large cumulative drift flags a
  stage/focus problem that will confuse tracking.
- foreground_fraction: Otsu-threshold coverage on channel 0 per frame -- a
  rough presence/confluency signal, not a cell count. Flags empty wells
  (~0) or fully confluent/unsegmentable fields (~1).

These are heuristic proxies for "will the real pipeline have something to
work with", not a substitute for it.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.ndimage import laplace
from skimage.filters import threshold_otsu
from skimage.registration import phase_cross_correlation

from .io import load_any_movie


@dataclass
class MovieMetrics:
    path: str
    n_frames: int
    n_channels: int
    height: int
    width: int
    dtype: str
    sharpness: np.ndarray  # (T,) on channel 0
    saturation_frac: np.ndarray  # (T, C)
    drift_px: np.ndarray  # (T,) cumulative, drift_px[0] == 0
    foreground_frac: np.ndarray  # (T,) on channel 0


def compute_movie_metrics(path) -> MovieMetrics:
    movie = load_any_movie(path)
    data = movie.data  # (T, C, Y, X)
    n_frames, n_channels, height, width = data.shape
    bf = data[:, 0, :, :]

    sharpness = np.array([_laplacian_variance(frame) for frame in bf])
    saturation_frac = np.array(
        [[_saturation_fraction(data[t, c]) for c in range(n_channels)] for t in range(n_frames)]
    )
    drift_px = _cumulative_drift(bf)
    foreground_frac = np.array([_foreground_fraction(frame) for frame in bf])

    return MovieMetrics(
        path=str(path),
        n_frames=n_frames,
        n_channels=n_channels,
        height=height,
        width=width,
        dtype=str(data.dtype),
        sharpness=sharpness,
        saturation_frac=saturation_frac,
        drift_px=drift_px,
        foreground_frac=foreground_frac,
    )


def _laplacian_variance(frame: np.ndarray) -> float:
    return float(laplace(frame.astype(np.float64)).var())


def _saturation_fraction(frame: np.ndarray) -> float:
    max_value = np.iinfo(frame.dtype).max if np.issubdtype(frame.dtype, np.integer) else frame.max()
    if max_value == 0:
        return 0.0
    return float(np.mean(frame >= max_value))


def _foreground_fraction(frame: np.ndarray) -> float:
    if frame.max() == frame.min():
        return 0.0  # flat frame -- nothing to threshold, treat as empty
    threshold = threshold_otsu(frame)
    return float(np.mean(frame > threshold))


def _cumulative_drift(bf_stack: np.ndarray) -> np.ndarray:
    n_frames = bf_stack.shape[0]
    drift = np.zeros(n_frames)
    cumulative = 0.0
    for t in range(1, n_frames):
        shift, _error, _diffphase = phase_cross_correlation(
            bf_stack[t - 1], bf_stack[t], upsample_factor=4
        )
        cumulative += float(np.hypot(*shift))
        drift[t] = cumulative
    return drift


def summarize(metrics: MovieMetrics) -> dict:
    return {
        "path": metrics.path,
        "n_frames": metrics.n_frames,
        "n_channels": metrics.n_channels,
        "height": metrics.height,
        "width": metrics.width,
        "dtype": metrics.dtype,
        "sharpness_median": float(np.median(metrics.sharpness)),
        "sharpness_min": float(np.min(metrics.sharpness)),
        "saturation_max_frac": float(np.max(metrics.saturation_frac)),
        "drift_cumulative_px": float(metrics.drift_px[-1]) if metrics.n_frames else 0.0,
        "foreground_frac_median": float(np.median(metrics.foreground_frac)),
    }


def compute_batch_metrics(rows) -> tuple:
    """Compute metrics for every row's resolved movie. Returns
    ``(summary_df, per_movie_metrics)`` where ``per_movie_metrics`` maps
    ``resolved_path -> MovieMetrics`` for the per-frame plots, and failures
    (e.g. a corrupt file) are recorded in the summary rather than aborting
    the whole batch."""
    summaries = []
    per_movie = {}
    for row in rows:
        record = {
            "experiment_path": row.experiment_path,
            "position": row.position,
            "resolved_path": row.resolved_path,
        }
        try:
            metrics = compute_movie_metrics(row.resolved_path)
            per_movie[row.resolved_path] = metrics
            record.update(summarize(metrics))
            record["error"] = ""
        except Exception as exc:  # noqa: BLE001 -- one bad file must not kill the batch
            record["error"] = str(exc)
        summaries.append(record)
    return pd.DataFrame.from_records(summaries), per_movie
