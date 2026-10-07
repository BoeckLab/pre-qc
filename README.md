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
installs **QC.app** into `/Applications` — one shared app identity/icon for
the whole qc/ suite (pre-qc now; post-qc once it exists at `~/post-qc`,
at which point the same app picks between them). It's a thin launcher
(not a frozen binary) that does nothing but run `pre-qc` directly in place
of itself — no separate Terminal window, no interactive step before that
handoff — so there's only ever one icon/process for the whole thing, the
same way cell-slate's own launcher works; `pre-qc` itself asks which CSV to
review via a native file dialog once napari is already up. After this,
**open it from `/Applications` or the Dock** like any other app — no
Terminal needed again for routine use (right-click its Dock icon →
**Options** → **Keep in
Dock** to pin it).

To pick up updates later: re-run `bash scripts/install.sh`, or `cd
~/pre-qc && git pull && source .venv/bin/activate && pip install -e .`.

**If you installed before the switch from PyQt5 to PyQt6** (napari is
deprecating PyQt5 support), `pip install -e .` alone won't remove the old
PyQt5 install from an existing venv — delete and rebuild it instead:
`rm -rf ~/pre-qc/.venv && bash scripts/install.sh`.

## Install (terminal — Linux/other, or macOS without the click-through app)

Requires **Python 3.10+** (napari doesn't support older). Check what you
have:

```bash
python3 --version
```

If it's older than 3.10, install a newer interpreter first — e.g. on
Ubuntu/Debian: `sudo apt install python3.11 python3.11-venv`; on macOS:
`brew install python@3.11`.

**1. One-time SSH key, if you don't already have one registered with
GitHub** (private repo — any BoeckLab org member already has read access):

```bash
ssh -T git@github.com
```

If that prints `Hi <username>! You've successfully authenticated...`, skip
to step 2. If it says `Permission denied (publickey)`:

```bash
ssh-keygen -t ed25519 -C "your.email@unibas.ch"
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Paste the printed key at **github.com → Settings → SSH and GPG keys → New
SSH key**, then re-run `ssh -T git@github.com` to confirm it worked.

**2. Clone and install into a virtual environment:**

```bash
git clone git@github.com:BoeckLab/pre-qc.git
cd pre-qc
python3.11 -m venv .venv        # or whichever 3.10+ interpreter you have
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

**3. Run it** (see "Input CSV" below for the CSV format; `experiment_path`
needs to resolve to something locally readable from wherever you run this —
see "Accessing experiment data from sciCORE" if your movies live there):

```bash
pre-qc wells_to_check.csv
```

**4. Routine use after the first install** — just re-activate the venv,
no reinstall needed unless you want to pick up updates:

```bash
cd pre-qc && source .venv/bin/activate && pre-qc wells_to_check.csv
```

To pick up updates: `git pull && pip install -e .` inside that same venv.

(Standalone project — no dependency on `cell-slate` or `HiTMicTools`,
deliberately, so it's a light install for someone who hasn't set either up
yet.)

## Accessing experiment data from sciCORE

`experiment_path` in the input CSV must be a path that's locally readable
from wherever `pre-qc` runs — there's no built-in SSH/S3 support, pre-qc
only ever opens local files. **Don't run pre-qc itself on a sciCORE login
node** (interactive GUI apps don't belong there, and X11-forwarded napari
rendering is too laggy for real use anyway) — install it on your own
machine and bring the data to it, one of:

**A. SSHFS mount (persists across reboots without re-authenticating each time):**

```bash
# Linux:
sudo apt install sshfs
# macOS:
brew install macfuse && brew install gromgit/fuse/sshfs-mac   # macFUSE broke Homebrew's official sshfs cask; this tap has a working build

mkdir -p ~/scicore
sshfs <username>@login-node.scicore.unibas.ch:/scicore/home/boeluc00/<username> ~/scicore -o volname=scicore
```

(macOS: first install needs a one-time approval in **System Settings →
Privacy & Security**, sometimes needs a reboot to fully take.) Once
mounted, point `experiment_path` at e.g. `~/scicore/experiment_042`.
Unmount when done: `umount ~/scicore` (or `fusermount -u ~/scicore` on
Linux).

**B. macOS only — Finder's "Connect to Server" (no terminal, no install):**

Press **⌘K** in Finder, enter `smb://toucan-all.scicore.unibas.ch/RINFsci$`,
log in with your sciCORE credentials. Mounts under `/Volumes/RINFsci$`.

**C. Off-campus / one-off — rsync a copy locally:**

```bash
rsync -avP <username>@login-node.scicore.unibas.ch:/path/to/experiment_042/ ~/local_qc_data/experiment_042/
```

Then point `experiment_path` at `~/local_qc_data/experiment_042`.

**JetRaw-compressed files (`.ome.p.tiff`) can't be opened by pre-qc at all**
(see "Input CSV" below) — decompress them on sciCORE first:

```bash
# on sciCORE
jetraw-tools decompress <folder> --extension ".ome.p.tiff"
```

See BacNets' `ONBOARDING_JETRAW.md` for setup details, then bring the plain
`.tiff`/`.nd2` output over with one of A/B/C above.

## Input CSV

**One shared `experiment_path` for the whole experiment, one row per
well/position inside it.** `experiment_path` is the *single top-level
folder* that holds every movie for that experiment — it's the same value
repeated down the column, not a different folder per row. `position` is
just that well's label (letter + number, e.g. `A1`, `A12`) — pre-qc finds
the one file under `experiment_path` whose name contains it, so you don't
need to know or write out each movie's actual filename.

Copy [`templates/wells_to_check_template.csv`](templates/wells_to_check_template.csv)
and fill in your own path/positions rather than writing one from scratch:

```csv
experiment_path,position,condition
/scicore/projects/rinfsci/<you>/<your_experiment_folder>,A1,control
/scicore/projects/rinfsci/<you>/<your_experiment_folder>,A2,treatment
/scicore/projects/rinfsci/<you>/<your_experiment_folder>,A12,control
/scicore/projects/rinfsci/<you>/<your_experiment_folder>,B3,treatment
```

(All four rows point at the *same* `experiment_path` — only `position`
changes row to row. If you're checking wells across more than one
experiment, just repeat the pattern with a different `experiment_path`
value for that experiment's own rows.)

- `experiment_path` and `position` are required; any extra columns (e.g. a
  human-readable `condition`) are carried through untouched into the
  results sidecar, and shown/used to label movies in the report instead of
  their full file paths.
- The movie file is found by searching under `experiment_path` for a file
  whose name contains `position` — no fixed naming convention is assumed
  beyond that, so this works across different acquisition layouts (a plain
  well label like `A12` is the common case, but any unique substring
  works). If that match is ambiguous (more than one file) or missing, the
  row is flagged rather than guessed at.
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

The CSV argument is optional — run `pre-qc` with no argument (which is what
**QC.app** does) and it opens napari first, then asks for the CSV via a
native file dialog from inside that already-running window.

This opens napari with a **QC review** dock:

1. Scroll frames with napari's own slider (channels show as separate,
   additively-blended layers). A movie can take a while to load, especially
   over a network mount — a "Loading movie: ‹path›" popup stays up the
   whole time so it's clear the app is still working, not stuck.
2. Mark the current movie **GOOD** (key `g`) or **BAD** (key `b`) — this
   saves immediately to `<input>_qc_results.csv` and auto-advances to the
   next unreviewed row. Quitting partway through and re-running on the same
   CSV resumes from where you left off instead of restarting.
3. Once every resolvable row is marked good, **Finish** computes cheap
   CPU-only analyzability metrics and writes `<input>_qc_report.html` +
   `<input>_qc_metrics.csv`, then opens the HTML report in your browser
   automatically. If any row is still bad or unreviewed, Finish refuses to
   run until that's resolved. The report includes a per-metric comparison
   plot — every movie's full per-frame curve overlaid in its own color on
   the same axes (not just a medians bar chart), so you can directly see
   e.g. which movie drifted more or lost focus earlier than the others.

A **Tutorial** button (always visible, next to Check for Updates) opens this
README on GitHub in your browser — the same place a colleague you've shared
the repo link with would land.

A **QC checklist** panel sits on the left, below napari's own layer
controls/layer list — a reference list of what to actually look for when
deciding good vs. bad (focus, drift, PI signal, saturation, artifacts).
It's read-only in the app; see `checklist.py` to edit the list itself as
colleague feedback comes in.

## What "analyzability" means here

Deliberately **not** a run of the real segmentation model — that would
partly defeat the point of checking before spending GPU hours. Instead,
fast, CPU-only proxies computed directly on pixels, split by channel
(channel 0 = brightfield/BF, channel 1 = PI/fluorescence — the BF-then-FL
convention used elsewhere in the lab's pipelines; PI metrics are skipped for
single-channel movies):

**Brightfield (channel 0):**
- **sharpness** — variance-of-Laplacian per frame; low/falling values flag
  out-of-focus acquisition.
- **mean intensity / contrast** — flags a too-dark, blown-out, or
  flat/low-contrast acquisition.
- **drift** — frame-to-frame phase-cross-correlation shift, accumulated;
  large cumulative drift flags a stage/focus problem that will confuse
  tracking.
- **foreground fraction** — rough Otsu-threshold coverage, not a cell
  count; flags empty wells (~0) or fully confluent/unsegmentable fields
  (~1).

**PI / fluorescence (channel 1):**
- **mean intensity** — flags a channel that's effectively all-dark (wrong
  filter cube, laser off, bad exposure) rather than a real all-negative
  biological result.
- **signal ratio** (99th-percentile / median intensity) — a
  segmentation-free contrast proxy; near 1 means no bright signal stands
  out above background at all, well above 1 means real dynamic range
  exists for PI+ cells to be distinguishable.
- **p95** (95th-percentile intensity) — a direct brightness percentile,
  less sensitive to a handful of hot/dead pixels than a true max, and less
  sensitive to overall exposure than mean intensity.
- **SNR** ((Otsu-foreground mean − Otsu-background mean) / Otsu-background
  std) — a standard microscopy signal-to-noise definition, distinct from
  signal ratio: this one is background-noise-aware, so it also flags a
  channel with fine dynamic range but too much background noise to
  actually segment PI+ cells from.
- **positive fraction** — rough Otsu-threshold coverage on this channel,
  analogous to BF's foreground fraction; not a real PI+ classification.

**Both channels:**
- **saturation** — fraction of pixels at the dtype's max value, tracked per
  channel; high values flag clipping/overexposure.

These are heuristic proxies for "will the real pipeline have something to
work with", not a guarantee of downstream pipeline quality.

## Keeping it up to date

The **Check for Updates** button (always visible, below the main review
controls) does a quick `git fetch` against GitHub and, if your install is
behind, offers to update: `git pull --ff-only` + `pip install -e .` into the
same venv, then automatically closes and reopens the app (`open -a "QC"`,
so this only self-relaunches when installed via `scripts/install.sh`'s
click-through app — a terminal-only install just needs to be re-run by
hand after the pull). Any in-progress review is already saved continuously
to the `_qc_results.csv` sidecar, so nothing is lost by the restart.

## Layout

```
pre-qc/
├── pyproject.toml
├── README.md
├── scripts/
│   └── install.sh   # macOS one-time: clone/venv/pip install -e . + generates /Applications/QC.app
├── templates/
│   └── wells_to_check_template.csv  # copy + fill in -- see "Input CSV"
├── src/pre_qc/
│   ├── io.py        # uncompressed-movie loading (nd2 + tiff/ome-tiff), path resolution
│   ├── manifest.py  # CSV manifest, crash-safe good/bad progress tracking
│   ├── metrics.py   # per-channel (BF/PI) sharpness/drift/saturation/intensity/signal-ratio proxies
│   ├── report.py    # static HTML + CSV report (matplotlib plots, base64-embedded, auto-opened)
│   ├── widget.py     # napari dock widgets: QC review (scroll/mark/Finish/Tutorial/Updates) + checklist
│   ├── checklist.py # plain-data list of good/bad review criteria shown in the checklist panel
│   ├── updater.py   # git fetch/pull + pip reinstall, backing the update button
│   ├── app.py       # CLI entry point (`pre-qc <csv>`)
│   └── assets/
│       ├── qc_icon.png    # shared app icon -- same one post-qc will use
│       └── qc-banner.png  # top-of-dock banner inside the app
└── tests/
    └── test_pre_qc.py  # io/manifest/metrics logic (no napari/Qt needed)
```
