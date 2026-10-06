# pre-qc

A small, standalone napari tool for new researchers to spot-check raw
microscopy movies **before** running the full ASCT pipeline — so an
acquisition problem (empty well, out-of-focus, drifting stage, saturated
fluorescence) gets caught before GPU hours are spent on unusable data.

Sibling tool `../post-qc` (not built yet) will cover the opposite end:
classifying movies as analyzable/discardable *after* a pipeline run.

## Install (macOS — click-through app)

Requires **Python 3.10+** (napari doesn't support older). If your default
`python3` is older, install a newer one first (e.g. `brew install
python@3.11`); `scripts/install.sh` picks it up automatically.

**Private repo — any BoeckLab org member already has read access.** If you
don't have a GitHub SSH key registered yet:

```bash
ssh -T git@github.com   # if this fails with "Permission denied (publickey)":
ssh-keygen -t ed25519 -C "your.email@unibas.ch"
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub   # paste at github.com -> Settings -> SSH and GPG keys
```

Then clone and install:

```bash
git clone git@github.com:BoeckLab/pre-qc.git
cd pre-qc
bash scripts/install.sh
```

This clones (or updates) `~/pre-qc`, builds an isolated venv there, and
installs **Pre QC.app** into `/Applications` — a thin launcher (not a
frozen binary) that asks which CSV to review (native file picker), then
runs `pre-qc` against it in a visible Terminal window so any startup
warnings are seen before napari opens. After this, **open it from
`/Applications` or the Dock** like any other app — no Terminal needed again
for routine use (right-click its Dock icon → **Options** → **Keep in
Dock** to pin it).

To pick up updates later: re-run `bash scripts/install.sh`, or `cd
~/pre-qc && git pull && source .venv/bin/activate && pip install -e .`.

## Install (manual / development / other OS)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
pre-qc wells_to_check.csv
```

(Standalone project — no dependency on `cell-slate` or `HiTMicTools`,
deliberately, so it's a light install for someone who hasn't set either up
yet.)

## Input CSV

One row per well/position to check:

| experiment_path | position | ... |
|---|---|---|
| /scicore/.../experiment_042 | A1 | ... |

- `experiment_path` and `position` are required; any extra columns (e.g. a
  human-readable `condition`) are carried through untouched into the
  results sidecar.
- The movie file is found by searching under `experiment_path` for a file
  whose name contains `position` — no fixed naming convention is assumed,
  so this works across different acquisition layouts. If that match is
  ambiguous (more than one file) or missing, the row is flagged rather than
  guessed at.
- Any **uncompressed** format works: `.nd2`, `.ome.tiff`/`.ome.tif`, plain
  `.tiff`/`.tif`. JetRaw-compressed `.ome.p.tiff`/`.p.tif` files are
  rejected with a pointer to decompress first (see BacNets'
  `ONBOARDING_JETRAW.md`) — QC is meant to run on exactly what the
  pipeline will see, and JetRaw decoding needs a licensed SDK only set up
  on sciCORE.

## Run

```bash
pre-qc wells_to_check.csv
```

This opens napari with a **QC review** dock:

1. Scroll frames with napari's own slider (channels show as separate,
   additively-blended layers).
2. Mark the current movie **GOOD** (key `g`) or **BAD** (key `b`) — this
   saves immediately to `<input>_qc_results.csv` and auto-advances to the
   next unreviewed row. Quitting partway through and re-running on the same
   CSV resumes from where you left off instead of restarting.
3. Once every resolvable row is marked good, **Finish** computes cheap
   CPU-only analyzability metrics and writes `<input>_qc_report.html` +
   `<input>_qc_metrics.csv`. If any row is still bad or unreviewed, Finish
   refuses to run until that's resolved.

## What "analyzability" means here

Deliberately **not** a run of the real segmentation model — that would
partly defeat the point of checking before spending GPU hours. Instead,
four fast, CPU-only proxies computed directly on pixels (channel 0 is
assumed to be brightfield, matching the BF-then-FL convention used
elsewhere in the lab's pipelines):

- **sharpness** — variance-of-Laplacian per frame; low/falling values flag
  out-of-focus acquisition.
- **drift** — frame-to-frame phase-cross-correlation shift, accumulated;
  large cumulative drift flags a stage/focus problem that will confuse
  tracking.
- **saturation** — fraction of pixels at the dtype's max value per channel;
  high values flag clipped/overexposed fluorescence.
- **foreground fraction** — rough Otsu-threshold coverage, not a cell
  count; flags empty wells (~0) or fully confluent/unsegmentable fields
  (~1).

These are heuristic proxies for "will the real pipeline have something to
work with", not a guarantee of downstream pipeline quality.

## Layout

```
pre-qc/
├── pyproject.toml
├── README.md
├── scripts/
│   └── install.sh   # macOS one-time: clone/venv/pip install -e . + generates /Applications/Pre QC.app
├── src/pre_qc/
│   ├── io.py        # uncompressed-movie loading (nd2 + tiff/ome-tiff), path resolution
│   ├── manifest.py  # CSV manifest, crash-safe good/bad progress tracking
│   ├── metrics.py   # sharpness/drift/saturation/foreground-fraction proxies
│   ├── report.py    # static HTML + CSV report (matplotlib plots, base64-embedded)
│   ├── widget.py     # napari dock widget: scroll, mark good/bad, Finish
│   └── app.py       # CLI entry point (`pre-qc <csv>`)
└── tests/
    └── test_pre_qc.py  # io/manifest/metrics logic (no napari/Qt needed)
```
