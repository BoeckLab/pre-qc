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


def _comparison_figure(per_movie: dict):
    """One figure, one subplot per metric, every movie's full per-frame
    curve overlaid in its own color on that subplot -- lets you directly
    compare e.g. "did movie B drift more than movie A", not just their
    medians (see _cross_movie_figure for the single-number-per-movie
    view)."""
    movies = list(per_movie.items())  # [(resolved_path, MovieMetrics), ...]
    has_pi = any(m.pi_mean_intensity is not None for _, m in movies)
    n_rows = 2 if has_pi else 1
    fig, axes = plt.subplots(n_rows, 4, figsize=(16, 3.2 * n_rows), squeeze=False)

    bf_specs = [
        ("bf_sharpness", "BF sharpness (Laplacian var)", None),
        ("bf_mean_intensity", "BF mean intensity", None),
        ("drift_px", "BF cumulative drift (px)", None),
        ("bf_foreground_frac", "BF foreground fraction", (0, 1)),
    ]
    for ax, (attr, title, ylim) in zip(axes[0], bf_specs):
        for path, metrics in movies:
            ax.plot(getattr(metrics, attr), label=_movie_label(path))
        ax.set_title(title, fontsize=8)
        ax.set_xlabel("frame", fontsize=7)
        ax.tick_params(labelsize=7)
        if ylim:
            ax.set_ylim(*ylim)

    if has_pi:
        pi_specs = [
            ("pi_mean_intensity", "PI mean intensity", None),
            ("pi_signal_ratio", "PI signal ratio (p99/median)", None),
            ("pi_positive_frac", "PI positive fraction", (0, 1)),
        ]
        for ax, (attr, title, ylim) in zip(axes[1][:3], pi_specs):
            for path, metrics in movies:
                values = getattr(metrics, attr)
                if values is not None:
                    ax.plot(values, label=_movie_label(path))
            ax.set_title(title, fontsize=8)
            ax.set_xlabel("frame", fontsize=7)
            ax.tick_params(labelsize=7)
            if ylim:
                ax.set_ylim(*ylim)

        ax = axes[1][3]
        for path, metrics in movies:
            if metrics.saturation_frac.shape[1] > 1:
                ax.plot(metrics.saturation_frac[:, 1], label=_movie_label(path))
        ax.set_ylim(0, 1)
        ax.set_title("PI saturation fraction", fontsize=8)
        ax.set_xlabel("frame", fontsize=7)
        ax.tick_params(labelsize=7)

    # One shared legend (every subplot has the same set of movie lines/
    # colors) rather than repeating it in each of up to 8 subplots.
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 1.08 if has_pi else 1.12),
               ncol=min(len(labels), 6), fontsize=7)
    fig.tight_layout(rect=[0, 0, 1, 0.92 if has_pi else 0.88])
    return fig


def _cross_movie_figure(summary_df: pd.DataFrame):
    labels = [_movie_label(p) for p in summary_df["resolved_path"]]
    has_pi = summary_df["pi_mean_intensity_median"].notna().any()
    n_rows = 2 if has_pi else 1
    fig, axes = plt.subplots(n_rows, 4, figsize=(15, 3.2 * n_rows), squeeze=False)

    bf_row = axes[0]
    bf_row[0].barh(labels, summary_df["bf_sharpness_median"])
    bf_row[0].set_title("median BF sharpness", fontsize=8)
    bf_row[1].barh(labels, summary_df["bf_contrast_median"])
    bf_row[1].set_title("median BF contrast", fontsize=8)
    bf_row[2].barh(labels, summary_df["drift_cumulative_px"])
    bf_row[2].set_title("BF cumulative drift (px)", fontsize=8)
    bf_row[3].barh(labels, summary_df["bf_foreground_frac_median"])
    bf_row[3].set_xlim(0, 1)
    bf_row[3].set_title("median BF foreground fraction", fontsize=8)

    if has_pi:
        pi_row = axes[1]
        pi_row[0].barh(labels, summary_df["pi_mean_intensity_median"])
        pi_row[0].set_title("median PI mean intensity", fontsize=8)
        pi_row[1].barh(labels, summary_df["pi_signal_ratio_median"])
        pi_row[1].set_title("median PI signal ratio", fontsize=8)
        pi_row[2].barh(labels, summary_df["pi_positive_frac_median"])
        pi_row[2].set_xlim(0, 1)
        pi_row[2].set_title("median PI positive fraction", fontsize=8)
        pi_row[3].barh(labels, summary_df["saturation_max_frac"])
        pi_row[3].set_xlim(0, 1)
        pi_row[3].set_title("max saturation (any channel)", fontsize=8)

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
        comparison_fig_b64 = _fig_to_base64(_comparison_figure(per_movie))
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

    table_columns = [
        "position",
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
        table_columns += ["pi_mean_intensity_median", "pi_signal_ratio_median", "pi_positive_frac_median"]
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
