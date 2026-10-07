"""Static HTML + CSV report for the computed QC metrics.

Self-contained output file (run locally, open in a browser), no server.
Plots are matplotlib, embedded as base64 PNGs.
"""

import base64
from io import BytesIO
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def _fig_to_base64(fig) -> str:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _movie_label(path: str) -> str:
    return Path(path).name


def _display_label(record: dict) -> str:
    """"A12 (control)" instead of the full resolved file path -- position
    plus any "condition"-like column (carried through from the input CSV)
    is what a reviewer actually recognizes at a glance, not a long sciCORE
    path. Falls back to the filename if even position is missing."""
    position = record.get("position") or ""
    condition = record.get("condition")
    if position and condition:
        return f"{position} ({condition})"
    if position:
        return position
    return _movie_label(record.get("resolved_path", ""))


#  bf_foreground_frac and pi_saturation_frac are deliberately NOT plotted
# per-frame here: within a single movie neither one varies meaningfully
# frame-to-frame (confluency barely shifts; saturation should normally sit
# at 0 and only matters as a rare spike), so their time curves are either
# flat or empty -- a wasted subplot. Both are still genuinely useful
# *across* movies as a single number, so they stay in the medians view
# below (_BF_SUMMARY_SPECS / _PI_SUMMARY_SPECS) and in the results table.
_BF_SPECS = [
    ("bf_sharpness", "BF sharpness (Laplacian var)", None),
    ("bf_mean_intensity", "BF mean intensity", None),
    ("drift_px", "BF cumulative drift (px)", None),
]
_PI_SPECS = [
    ("pi_mean_intensity", "PI mean intensity", None),
    ("pi_signal_ratio", "PI signal ratio (p99/median)", None),
    ("pi_p95", "PI p95 intensity", None),
    ("pi_snr", "PI SNR (Otsu fg/bg)", None),
    ("pi_positive_frac", "PI positive fraction", (0, 1)),
]


def _comparison_figure(per_movie: dict, labels_by_path: dict):
    """One figure, one subplot per metric, every movie's full per-frame
    curve overlaid in its own color on that subplot -- lets you directly
    compare e.g. "did movie B drift more than movie A", not just their
    medians (see _cross_movie_figure for the single-number-per-movie
    view)."""
    movies = list(per_movie.items())  # [(resolved_path, MovieMetrics), ...]
    has_pi = any(m.pi_mean_intensity is not None for _, m in movies)
    n_rows = 3 if has_pi else 1
    fig, axes = plt.subplots(n_rows, 4, figsize=(16, 3.2 * n_rows), squeeze=False)

    for ax, (attr, title, ylim) in zip(axes[0], _BF_SPECS):
        for path, metrics in movies:
            ax.plot(getattr(metrics, attr), label=labels_by_path.get(path, _movie_label(path)))
        ax.set_title(title, fontsize=8)
        ax.set_xlabel("frame", fontsize=7)
        ax.tick_params(labelsize=7)
        if ylim:
            ax.set_ylim(*ylim)
    for ax in axes[0][len(_BF_SPECS):]:
        ax.axis("off")

    if has_pi:
        pi_axes = list(axes[1]) + list(axes[2])
        for ax, (attr, title, ylim) in zip(pi_axes, _PI_SPECS):
            for path, metrics in movies:
                values = getattr(metrics, attr)
                if values is not None:
                    ax.plot(values, label=labels_by_path.get(path, _movie_label(path)))
            ax.set_title(title, fontsize=8)
            ax.set_xlabel("frame", fontsize=7)
            ax.tick_params(labelsize=7)
            if ylim:
                ax.set_ylim(*ylim)
        for ax in pi_axes[len(_PI_SPECS):]:
            ax.axis("off")

    # One shared legend (every subplot has the same set of movie lines/
    # colors) rather than repeating it in each of up to 12 subplots.
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 1.0 + 0.02 * n_rows),
               ncol=min(len(labels), 6), fontsize=7)
    fig.tight_layout(rect=[0, 0, 1, 1.0 - 0.03 * n_rows])
    return fig


_BF_SUMMARY_SPECS = [
    ("bf_sharpness_median", "median BF sharpness", None),
    ("bf_contrast_median", "median BF contrast", None),
    ("drift_cumulative_px", "BF cumulative drift (px)", None),
    ("bf_foreground_frac_median", "median BF foreground fraction", (0, 1)),
]
_PI_SUMMARY_SPECS = [
    ("pi_mean_intensity_median", "median PI mean intensity", None),
    ("pi_signal_ratio_median", "median PI signal ratio", None),
    ("pi_p95_median", "median PI p95 intensity", None),
    ("pi_snr_median", "median PI SNR (Otsu fg/bg)", None),
    ("pi_positive_frac_median", "median PI positive fraction", (0, 1)),
    ("saturation_max_frac", "max saturation (any channel)", (0, 1)),
]


def _cross_movie_figure(summary_df: pd.DataFrame):
    labels = [_display_label(r) for r in summary_df.to_dict("records")]
    has_pi = summary_df["pi_mean_intensity_median"].notna().any()
    n_rows = 3 if has_pi else 1
    fig, axes = plt.subplots(n_rows, 4, figsize=(15, 3.2 * n_rows), squeeze=False)

    for ax, (column, title, xlim) in zip(axes[0], _BF_SUMMARY_SPECS):
        ax.barh(labels, summary_df[column])
        ax.set_title(title, fontsize=8)
        if xlim:
            ax.set_xlim(*xlim)

    if has_pi:
        pi_axes = list(axes[1]) + list(axes[2])
        for ax, (column, title, xlim) in zip(pi_axes, _PI_SUMMARY_SPECS):
            ax.barh(labels, summary_df[column])
            ax.set_title(title, fontsize=8)
            if xlim:
                ax.set_xlim(*xlim)
        for ax in pi_axes[len(_PI_SUMMARY_SPECS):]:
            ax.axis("off")

    for row in axes:
        for ax in row:
            ax.tick_params(labelsize=6)
    fig.tight_layout()
    return fig


def write_html_report(summary_df: pd.DataFrame, per_movie: dict, out_path) -> Path:
    out_path = Path(out_path)
    ok = summary_df[summary_df["error"] == ""]
    failed = summary_df[summary_df["error"] != ""]

    sections = []
    if not ok.empty:
        cross_fig_b64 = _fig_to_base64(_cross_movie_figure(ok))
        sections.append(
            "<h2>Across movies — medians</h2>"
            f'<img src="data:image/png;base64,{cross_fig_b64}">'
        )

    if per_movie:
        labels_by_path = {r["resolved_path"]: _display_label(r) for r in ok.to_dict("records")}
        comparison_fig_b64 = _fig_to_base64(_comparison_figure(per_movie, labels_by_path))
        sections.append(
            "<h2>Across movies — full per-frame curves</h2>"
            "<p>Each color is one movie -- see the legend above the plots.</p>"
            f'<img src="data:image/png;base64,{comparison_fig_b64}">'
        )

    failed_html = ""
    if not failed.empty:
        rows_html = "".join(
            f"<tr><td>{r['position']}</td><td>{r['resolved_path']}</td><td>{r['error']}</td></tr>"
            for _, r in failed.iterrows()
        )
        failed_html = (
            "<h2>Failed to compute metrics</h2>"
            "<table border='1' cellpadding='4'><tr><th>position</th><th>path</th><th>error</th></tr>"
            f"{rows_html}</table>"
        )

    table_columns = ["position"]
    if not ok.empty and "condition" in ok.columns:
        table_columns.append("condition")
    table_columns += [
        "n_frames",
        "n_channels",
        "bf_sharpness_median",
        "bf_sharpness_min",
        "bf_mean_intensity_median",
        "bf_contrast_median",
        "bf_foreground_frac_median",
        "drift_cumulative_px",
        "saturation_max_frac",
    ]
    if not ok.empty and ok["pi_mean_intensity_median"].notna().any():
        table_columns += [
            "pi_mean_intensity_median",
            "pi_signal_ratio_median",
            "pi_p95_median",
            "pi_snr_median",
            "pi_positive_frac_median",
        ]
    table_html = ok[table_columns].to_html(index=False) if not ok.empty else "<p>No successfully analyzed movies.</p>"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>QC analyzability report</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
img {{ max-width: 100%; }}
table {{ border-collapse: collapse; font-size: 0.85em; }}
th, td {{ padding: 4px 8px; }}
</style>
</head>
<body>
<h1>QC analyzability report</h1>
<p>Heuristic, CPU-only proxies computed on raw pixels -- not a run of the real
segmentation model. Use this to catch obviously unanalyzable movies (empty,
out of focus, drifting, saturated) before committing GPU hours, not as a
guarantee of downstream pipeline quality.</p>
{table_html}
{"".join(sections)}
{failed_html}
</body>
</html>"""

    out_path.write_text(html)
    return out_path
