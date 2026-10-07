import numpy as np
import pandas as pd
import pytest
import tifffile

from pre_qc import manifest
from pre_qc.io import MovieResolutionError, load_any_movie, resolve_movie_path
from pre_qc.metrics import compute_movie_metrics, summarize


def _write_tiff(path, shape=(4, 2, 16, 16), dtype=np.uint16):
    data = np.random.randint(0, 1000, shape).astype(dtype)
    tifffile.imwrite(path, data, metadata={"axes": "TCYX"})
    return data


# ---------------------------------------------------------------------
# io.resolve_movie_path / load_any_movie
# ---------------------------------------------------------------------


def test_resolve_movie_path_finds_single_match(tmp_path):
    (tmp_path / "well_A1.ome.tiff").touch()
    found = resolve_movie_path(tmp_path, "A1")
    assert found.name == "well_A1.ome.tiff"


def test_resolve_movie_path_excludes_jetraw_compressed(tmp_path):
    (tmp_path / "well_A1.ome.p.tiff").touch()
    with pytest.raises(MovieResolutionError, match="No uncompressed movie"):
        resolve_movie_path(tmp_path, "A1")


def test_resolve_movie_path_raises_on_ambiguous_match(tmp_path):
    (tmp_path / "well_A1_rep1.tiff").touch()
    (tmp_path / "well_A1_rep2.tiff").touch()
    with pytest.raises(MovieResolutionError, match="Ambiguous"):
        resolve_movie_path(tmp_path, "A1")


def test_resolve_movie_path_raises_when_experiment_path_missing(tmp_path):
    with pytest.raises(MovieResolutionError, match="does not exist"):
        resolve_movie_path(tmp_path / "nope", "A1")


def test_load_any_movie_rejects_p_tiff_by_name(tmp_path):
    path = tmp_path / "well_A1.ome.p.tiff"
    tifffile.imwrite(path, np.zeros((5, 5), dtype=np.uint8))
    with pytest.raises(MovieResolutionError, match="JetRaw-compressed"):
        load_any_movie(path)


def test_load_any_movie_reads_plain_tiff(tmp_path):
    path = tmp_path / "well_A1.tiff"
    data = _write_tiff(path)
    movie = load_any_movie(path)
    assert movie.data.shape == data.shape


def test_load_any_movie_raises_actionable_error_for_jetraw_tiff_tag(tmp_path, monkeypatch):
    path = tmp_path / "well_A1.tiff"  # extension doesn't signal it, the TIFF tag does
    tifffile.imwrite(path, np.zeros((5, 5), dtype=np.uint8))

    class _FakePage:
        compression = 48124  # tifffile's JetRaw compression tag value

    class _FakeTiff:
        def __enter__(self):
            self.pages = [_FakePage()]
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(tifffile, "TiffFile", lambda _path: _FakeTiff())

    with pytest.raises(MovieResolutionError, match="JetRaw-compressed"):
        load_any_movie(path)


# ---------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------


def test_load_manifest_requires_expected_columns(tmp_path):
    csv_path = tmp_path / "input.csv"
    pd.DataFrame({"wrong_column": ["x"]}).to_csv(csv_path, index=False)
    with pytest.raises(ValueError, match="missing required column"):
        manifest.load_manifest(csv_path)


def test_manifest_mark_and_resume_round_trip(tmp_path):
    exp_dir = tmp_path / "exp"
    exp_dir.mkdir()
    _write_tiff(exp_dir / "well_A1.tiff")

    csv_path = tmp_path / "input.csv"
    pd.DataFrame({"experiment_path": [str(exp_dir)], "position": ["A1"]}).to_csv(csv_path, index=False)

    rows = manifest.load_or_resume(csv_path)
    assert len(rows) == 1
    assert rows[0].status == "unreviewed"
    assert rows[0].resolved_path.endswith("well_A1.tiff")

    manifest.mark(rows[0], "good", note="looks fine")
    manifest.save_results(csv_path, rows)

    resumed = manifest.load_or_resume(csv_path)
    assert resumed[0].status == "good"
    assert resumed[0].note == "looks fine"


def test_review_summary_counts_statuses():
    rows = [
        manifest.QCRow(experiment_path="e", position="A1", status="good"),
        manifest.QCRow(experiment_path="e", position="A2", status="bad"),
        manifest.QCRow(experiment_path="e", position="A3"),
        manifest.QCRow(experiment_path="e", position="A4", resolution_error="missing"),
    ]
    summary = manifest.review_summary(rows)
    assert summary == {
        "n_total": 4,
        "n_good": 1,
        "n_bad": 1,
        "n_unreviewed": 1,
        "n_unresolved": 1,
        "all_reviewed": False,
        "all_good": False,
    }


def test_review_summary_all_good_when_clean():
    rows = [manifest.QCRow(experiment_path="e", position=p, status="good") for p in ("A1", "A2")]
    summary = manifest.review_summary(rows)
    assert summary["all_reviewed"] is True
    assert summary["all_good"] is True


# ---------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------


def test_compute_movie_metrics_shapes(tmp_path):
    path = tmp_path / "movie.tiff"
    _write_tiff(path, shape=(5, 2, 32, 32))

    metrics = compute_movie_metrics(path)

    assert metrics.n_frames == 5
    assert metrics.n_channels == 2
    assert metrics.bf_sharpness.shape == (5,)
    assert metrics.bf_mean_intensity.shape == (5,)
    assert metrics.bf_contrast.shape == (5,)
    assert metrics.saturation_frac.shape == (5, 2)
    assert metrics.drift_px.shape == (5,)
    assert metrics.drift_px[0] == 0.0
    assert metrics.bf_foreground_frac.shape == (5,)
    # 2-channel movie -- PI (channel 1) metrics must be populated
    assert metrics.pi_mean_intensity.shape == (5,)
    assert metrics.pi_signal_ratio.shape == (5,)
    assert metrics.pi_positive_frac.shape == (5,)

    summary = summarize(metrics)
    assert summary["n_frames"] == 5
    assert 0.0 <= summary["bf_foreground_frac_median"] <= 1.0
    assert summary["pi_mean_intensity_median"] is not None
    assert 0.0 <= summary["pi_positive_frac_median"] <= 1.0


def test_compute_movie_metrics_single_channel_has_no_pi_metrics(tmp_path):
    path = tmp_path / "bf_only.tiff"
    _write_tiff(path, shape=(3, 1, 16, 16))

    metrics = compute_movie_metrics(path)

    assert metrics.n_channels == 1
    assert metrics.pi_mean_intensity is None
    assert metrics.pi_signal_ratio is None
    assert metrics.pi_positive_frac is None

    summary = summarize(metrics)
    assert summary["pi_mean_intensity_median"] is None


def test_compute_movie_metrics_flags_saturation(tmp_path):
    path = tmp_path / "saturated.tiff"
    data = np.full((2, 1, 8, 8), 65535, dtype=np.uint16)
    tifffile.imwrite(path, data, metadata={"axes": "TCYX"})

    metrics = compute_movie_metrics(path)

    assert np.all(metrics.saturation_frac == 1.0)
