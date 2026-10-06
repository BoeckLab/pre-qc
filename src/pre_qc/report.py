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

from .metrics import MovieMetrics


def _fig_to_base64(fig) -> str:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _movie_label(path: str) -> str:
    return Path(path).name


def _per_movie_figure(label: str, metrics: MovieMetrics):
    fig, axes = plt.subplots(1, 3, figsize=(12, 2.8))
    fig.suptitle(label, fontsize=9)

    axes[0].plot(metrics.sharpness)
    axes[0].set_title("sharpness (Laplacian var)", fontsize=8)
    axes[0].set_xlabel("frame", fontsize=7)

    axes[1].plot(metrics.drift_px)
    axes[1].set_title("cumulative drift (px)", fontsize=8)
    axes[1].set_xlabel("frame", fontsize=7)

    axes[2].plot(metrics.foreground_frac)
    axes[2].set_ylim(0, 1)
    axes[2].set_title("foreground fraction", fontsize=8)
    axes[2].set_xlabel("frame", fontsize=7)

    for ax in axes:
        ax.tick_params(labelsize=7)
    fig.tight_layout(rect=[0, 0, 1, 0.92])
    return fig


def _cross_movie_figure(summary_df: pd.DataFrame):
    labels = [_movie_label(p) for p in summary_df["resolved_path"]]
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.2))

    axes[0].barh(labels, summary_df["sharpness_median"])
    axes[0].set_title("median sharpness", fontsize=8)

    axes[1].barh(labels, summary_df["drift_cumulative_px"])
    axes[1].set_title("cumulative drift (px)", fontsize=8)

    axes[2].barh(labels, summary_df["foreground_frac_median"])
    axes[2].set_xlim(0, 1)
    axes[2].set_title("median foreground fraction", fontsize=8)

    for ax in axes:
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
        sections.append(f'<h2>Across movies</h2><img src="data:image/png;base64,{cross_fig_b64}">')

    for _, record in ok.iterrows():
        metrics = per_movie.get(record["resolved_path"])
        if metrics is None:
            continue
        fig_b64 = _fig_to_base64(_per_movie_figure(_movie_label(record["resolved_path"]), metrics))
        sections.append(
            f"<h3>{_movie_label(record['resolved_path'])}</h3>"
            f"<p>{record['n_frames']} frames &middot; {record['n_channels']} channels &middot; "
            f"{record['height']}&times;{record['width']} &middot; {record['dtype']}</p>"
            f'<img src="data:image/png;base64,{fig_b64}">'
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

    table_html = ok[
        [
            "position",
            "n_frames",
            "n_channels",
            "sharpness_median",
            "sharpness_min",
            "saturation_max_frac",
            "drift_cumulative_px",
            "foreground_frac_median",
        ]
    ].to_html(index=False) if not ok.empty else "<p>No successfully analyzed movies.</p>"

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
