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
- p95 (95th-percentile intensity): a direct brightness percentile, less
  sensitive to a handful of hot/dead pixels than a true max, and less
  sensitive to overall exposure than mean_intensity -- "how bright does
  the brighter end of this frame actually get".
- snr: (mean of Otsu-foreground pixels - mean of Otsu-background pixels) /
  std of Otsu-background pixels -- a standard microscopy signal-to-noise
  definition, distinct from signal_ratio (that's a cheap percentile ratio;
  this one is background-noise-aware, so it also flags a channel with
  fine dynamic range but too much background noise to actually segment
  PI+ cells from).
- positive_fraction: Otsu-threshold coverage, analogous to BF's
  foreground_fraction -- a rough estimate of how much of the field reads as
  "bright" on this channel, not a real PI+ classification.

Saturation (fraction of pixels at the dtype's max value) is tracked for
every channel, not just BF/PI, since it's cheap and channel-agnostic.

bf_density_class buckets bf_foreground_frac_median into sparse/moderate/
dense -- a coarse "is this movie worth analyzing" call for empty-field vs.
confluent-overgrowth extremes.

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
    bf_hist_counts: np.ndarray  # (n_bins,) pixel-intensity histogram, summed over every frame
    bf_hist_bin_edges: np.ndarray  # (n_bins + 1,)
    bf_otsu_threshold_median: float  # median per-frame Otsu split -- the cells/background cut line

    # PI / fluorescence (channel 1) -- None if the movie has no 2nd channel
    pi_mean_intensity: np.ndarray | None = None  # (T,)
    pi_signal_ratio: np.ndarray | None = None  # (T,)
    pi_p95: np.ndarray | None = None  # (T,)
    pi_snr: np.ndarray | None = None  # (T,)
    pi_positive_frac: np.ndarray | None = None  # (T,)


def compute_movie_metrics(path) -> MovieMetrics:
    movie = load_any_movie(path)
    data = movie.data  # (T, C, Y, X)
    n_frames, n_channels, height, width = data.shape
    bf = data[:, BF_CHANNEL, :, :]

    saturation_frac = np.array(
        [[_saturation_fraction(data[t, c]) for c in range(n_channels)] for t in range(n_frames)]
    )

    bf_hist_counts, bf_hist_bin_edges = _bf_histogram(bf)

    pi_mean_intensity = pi_signal_ratio = pi_p95 = pi_snr = pi_positive_frac = None
    if n_channels > PI_CHANNEL:
        pi = data[:, PI_CHANNEL, :, :]
        pi_mean_intensity = np.array([float(frame.mean()) for frame in pi])
        pi_signal_ratio = np.array([_signal_ratio(frame) for frame in pi])
        pi_p95 = np.array([float(np.percentile(frame, 95)) for frame in pi])
        pi_snr = np.array([_snr(frame) for frame in pi])
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
        bf_hist_counts=bf_hist_counts,
        bf_hist_bin_edges=bf_hist_bin_edges,
        bf_otsu_threshold_median=_bf_otsu_median(bf),
        pi_mean_intensity=pi_mean_intensity,
        pi_signal_ratio=pi_signal_ratio,
        pi_p95=pi_p95,
        pi_snr=pi_snr,
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


def _snr(frame: np.ndarray) -> float:
    """Standard microscopy-style SNR: (foreground mean - background mean) /
    background std, with foreground/background split by Otsu threshold.
    0 for a flat frame (nothing to threshold) or zero-variance background
    (avoids a division by zero)."""
    if frame.max() == frame.min():
        return 0.0
    threshold = threshold_otsu(frame)
    foreground = frame[frame > threshold]
    background = frame[frame <= threshold]
    if background.size == 0 or foreground.size == 0:
        return 0.0
    background_std = float(background.std())
    if background_std == 0:
        return 0.0
    return float((foreground.mean() - background.mean()) / background_std)


_BF_HIST_BINS = 64

# bf_foreground_frac thresholds for the coarse density call surfaced in the
# report/checklist -- "is this movie dense or not" (colleague-facing
# shorthand, distinct from PI's SNR metric even though it was requested
# under that name). Empty/near-empty fields and overgrown/confluent fields
# both need flagging, for opposite reasons (nothing to track vs. nothing
# segmentable).
_DENSITY_SPARSE_MAX = 0.03
_DENSITY_DENSE_MIN = 0.5


def _bf_histogram(bf_stack: np.ndarray) -> tuple:
    """Pixel-intensity histogram of the BF channel, summed over every frame
    -- a density proxy: a bimodal histogram (background peak + separate
    brighter/darker cell peak) means cells stand out, a single narrow peak
    means an empty or saturated field."""
    vmin, vmax = float(bf_stack.min()), float(bf_stack.max())
    if vmin == vmax:
        vmax = vmin + 1.0  # avoid a zero-width np.histogram range on a flat stack
    counts, bin_edges = np.histogram(bf_stack.ravel(), bins=_BF_HIST_BINS, range=(vmin, vmax))
    return counts, bin_edges


def _bf_otsu_median(bf_stack: np.ndarray) -> float:
    thresholds = [
        float(threshold_otsu(frame)) for frame in bf_stack if frame.max() != frame.min()
    ]
    return float(np.median(thresholds)) if thresholds else 0.0


def _density_class(bf_foreground_frac_median: float) -> str:
    if bf_foreground_frac_median < _DENSITY_SPARSE_MAX:
        return "sparse/empty"
    if bf_foreground_frac_median > _DENSITY_DENSE_MIN:
        return "dense/confluent"
    return "moderate"


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
    record["bf_density_class"] = _density_class(record["bf_foreground_frac_median"])
    if metrics.pi_mean_intensity is not None:
        record["pi_mean_intensity_median"] = float(np.median(metrics.pi_mean_intensity))
        record["pi_signal_ratio_median"] = float(np.median(metrics.pi_signal_ratio))
        record["pi_p95_median"] = float(np.median(metrics.pi_p95))
        record["pi_snr_median"] = float(np.median(metrics.pi_snr))
        record["pi_positive_frac_median"] = float(np.median(metrics.pi_positive_frac))
    else:
        record["pi_mean_intensity_median"] = None
        record["pi_signal_ratio_median"] = None
        record["pi_p95_median"] = None
        record["pi_snr_median"] = None
        record["pi_positive_frac_median"] = None
    return record


def compute_batch_metrics(rows) -> tuple:
    """Compute metrics for every row's resolved movie. Returns
    ``(summary_df, per_movie_metrics)`` where ``per_movie_metrics`` maps
    ``resolved_path -> MovieMetrics`` for the per-frame plots, and failures
    (e.g. a corrupt file) are recorded in the summary rather than aborting
    the whole batch. ``summary_df`` carries each row's status/LABEL/NOTES
    too, written out as ``<input>_qc_metrics.csv`` -- a separate file from
    the review sidecar (``<input>_qc_results.csv``), self-contained enough
    on its own to see what was decided about a movie alongside its
    computed metrics."""
    summaries = []
    per_movie = {}
    for row in rows:
        record = {
            "EXP": row.exp,
            "WELL": row.well,
            "FRAME": row.frame,
            "resolved_path": row.resolved_path,
            **row.extra,  # e.g. "COND" -- carried through so the report can label by it
            "status": row.status,
            "LABEL": row.label,
            "NOTES": row.note,
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
