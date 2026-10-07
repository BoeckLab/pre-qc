"""Cheap, CPU-only "is this movie analyzable" metrics.

Deliberately does not run the real segmentation model -- the whole point of
this QC gate is to catch acquisition-level problems *before* spending GPU
hours, so every metric here is a fast proxy computed directly off pixel
values. Channel 0 is assumed to be brightfield (BF) and channel 1 the
fluorescence/PI channel, matching the BF-then-FL convention used by the
ASCT pipeline -- PI-specific metrics are skipped (reported as ``None``) for
single-channel movies.

Brightfield (channel 0):
- sharpness: variance-of-Laplacian per frame. Low/falling sharpness flags
  out-of-focus acquisition.
- mean_intensity / contrast (std of intensity): flags a too-dark, blown-out,
  or flat/low-contrast acquisition.
- drift: frame-to-frame phase-cross-correlation shift, accumulated into a
  running total. Large cumulative drift flags a stage/focus problem that
  will confuse tracking.
- foreground_fraction: Otsu-threshold coverage -- a rough presence/
  confluency signal, not a cell count. Flags empty wells (~0) or fully
  confluent/unsegmentable fields (~1).

PI / fluorescence (channel 1):
- mean_intensity: flags a channel that's effectively all-dark (wrong filter
  cube, laser off, exposure misconfigured) rather than a real biological
  all-negative result.
- signal_ratio (99th-percentile / median intensity): a resolution-free,
  segmentation-free contrast proxy -- near 1 means no distinguishable
  bright signal above background (flat/noise-only channel); well above 1
  means real dynamic range exists for PI+ cells to stand out against.
- positive_fraction: Otsu-threshold coverage, analogous to BF's
  foreground_fraction -- a rough estimate of how much of the field reads as
  "bright" on this channel, not a real PI+ classification.

Saturation (fraction of pixels at the dtype's max value) is tracked for
every channel, not just BF/PI, since it's cheap and channel-agnostic.

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

BF_CHANNEL = 0
PI_CHANNEL = 1


@dataclass
class MovieMetrics:
    path: str
    n_frames: int
    n_channels: int
    height: int
    width: int
    dtype: str
    saturation_frac: np.ndarray  # (T, C) -- every channel

    # brightfield (channel 0) -- always present
    bf_sharpness: np.ndarray  # (T,)
    bf_mean_intensity: np.ndarray  # (T,)
    bf_contrast: np.ndarray  # (T,)
    bf_foreground_frac: np.ndarray  # (T,)
    drift_px: np.ndarray  # (T,) cumulative, drift_px[0] == 0, computed on BF

    # PI / fluorescence (channel 1) -- None if the movie has no 2nd channel
    pi_mean_intensity: np.ndarray | None = None  # (T,)
    pi_signal_ratio: np.ndarray | None = None  # (T,)
    pi_positive_frac: np.ndarray | None = None  # (T,)


def compute_movie_metrics(path) -> MovieMetrics:
    movie = load_any_movie(path)
    data = movie.data  # (T, C, Y, X)
    n_frames, n_channels, height, width = data.shape
    bf = data[:, BF_CHANNEL, :, :]

    saturation_frac = np.array(
        [[_saturation_fraction(data[t, c]) for c in range(n_channels)] for t in range(n_frames)]
    )

    pi_mean_intensity = pi_signal_ratio = pi_positive_frac = None
    if n_channels > PI_CHANNEL:
        pi = data[:, PI_CHANNEL, :, :]
        pi_mean_intensity = np.array([float(frame.mean()) for frame in pi])
        pi_signal_ratio = np.array([_signal_ratio(frame) for frame in pi])
        pi_positive_frac = np.array([_foreground_fraction(frame) for frame in pi])

    return MovieMetrics(
        path=str(path),
        n_frames=n_frames,
        n_channels=n_channels,
        height=height,
        width=width,
        dtype=str(data.dtype),
        saturation_frac=saturation_frac,
        bf_sharpness=np.array([_laplacian_variance(frame) for frame in bf]),
        bf_mean_intensity=np.array([float(frame.mean()) for frame in bf]),
        bf_contrast=np.array([float(frame.std()) for frame in bf]),
        bf_foreground_frac=np.array([_foreground_fraction(frame) for frame in bf]),
        drift_px=_cumulative_drift(bf),
        pi_mean_intensity=pi_mean_intensity,
        pi_signal_ratio=pi_signal_ratio,
        pi_positive_frac=pi_positive_frac,
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


def _signal_ratio(frame: np.ndarray) -> float:
    """99th-percentile / median intensity -- near 1 for a flat/noise-only
    channel, well above 1 when real bright signal stands out against
    background. Avoids a division by zero on an all-dark frame."""
    median = float(np.median(frame))
    p99 = float(np.percentile(frame, 99))
    if median <= 0:
        return 0.0 if p99 <= 0 else float("inf")
    return p99 / median


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
    record = {
        "path": metrics.path,
        "n_frames": metrics.n_frames,
        "n_channels": metrics.n_channels,
        "height": metrics.height,
        "width": metrics.width,
        "dtype": metrics.dtype,
        "saturation_max_frac": float(np.max(metrics.saturation_frac)),
        "bf_sharpness_median": float(np.median(metrics.bf_sharpness)),
        "bf_sharpness_min": float(np.min(metrics.bf_sharpness)),
        "bf_mean_intensity_median": float(np.median(metrics.bf_mean_intensity)),
        "bf_contrast_median": float(np.median(metrics.bf_contrast)),
        "bf_foreground_frac_median": float(np.median(metrics.bf_foreground_frac)),
        "drift_cumulative_px": float(metrics.drift_px[-1]) if metrics.n_frames else 0.0,
    }
    if metrics.pi_mean_intensity is not None:
        record["pi_mean_intensity_median"] = float(np.median(metrics.pi_mean_intensity))
        record["pi_signal_ratio_median"] = float(np.median(metrics.pi_signal_ratio))
        record["pi_positive_frac_median"] = float(np.median(metrics.pi_positive_frac))
    else:
        record["pi_mean_intensity_median"] = None
        record["pi_signal_ratio_median"] = None
        record["pi_positive_frac_median"] = None
    return record


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
