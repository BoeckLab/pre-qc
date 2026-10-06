"""pre-qc: pre-pipeline QC review tool.

Given a CSV of (experiment_path, position) pairs to spot-check, lets a
reviewer scroll each raw movie in napari and mark it good/bad, then -- once
every row is marked good -- computes cheap CPU-only metrics (sharpness,
drift, saturation, foreground coverage) to characterize whether the movies
are analyzable before committing GPU hours to the real ASCT pipeline.

Entry point: ``pre-qc <csv>`` (see app.py). Standalone project -- no
dependency on cell-slate or HiTMicTools, so it installs into its own small
venv for new researchers who haven't set up either yet. See sibling
directory ``../post-qc`` (not yet built) for classifying movies as
analyzable/discardable *after* a pipeline run.
"""
