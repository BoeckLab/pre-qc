"""CLI entry point: ``pre-qc <csv>`` launches napari with the QC review dock
widget."""

import argparse
import sys
from pathlib import Path

# napari/Qt sets its own default app icon (the napari logo) on the running
# process's Dock tile as soon as the QApplication is created -- this
# overrides whatever icon the macOS .app bundle's Info.plist declared at
# launch (see scripts/install.sh's launcher, which `exec`s straight into
# this console script). Explicitly re-setting it after napari starts is
# what makes the QC icon actually stick instead of flipping to napari's.
_ICON_PATH = Path(__file__).resolve().parent / "assets" / "qc_icon.png"


def _apply_app_icon() -> None:
    """Set on the QApplication itself (not a specific window) -- that's
    what actually controls the Dock tile on macOS, and is available as
    soon as napari.Viewer() has run (which creates the QApplication),
    before any dock widget or window reference exists yet."""
    if not _ICON_PATH.exists():
        return
    from qtpy.QtGui import QIcon
    from qtpy.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setWindowIcon(QIcon(str(_ICON_PATH)))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-pipeline QC review: scroll each raw movie listed in a CSV "
            "(columns experiment_path, position) and mark it good/bad. Once "
            "every resolvable row is good, computes cheap analyzability "
            "metrics (sharpness/drift/saturation/foreground coverage) and "
            "writes an HTML report -- before committing GPU hours to the "
            "real ASCT pipeline."
        )
    )
    # Optional, not required: the macOS app launcher execs straight into
    # this script with no argument and lets the CSV get picked here
    # instead (see _prompt_for_csv) -- deliberately NOT via an `osascript
    # choose file` dialog run from the bash launcher before exec, which
    # was found to disrupt macOS's association between the running
    # process and the .app bundle it was launched from (a second,
    # differently-iconed Dock entry would appear right as the dialog
    # closed and napari started). Asking from inside the already-running,
    # already-correctly-iconed Qt process avoids that entirely.
    parser.add_argument("csv", nargs="?", help="Input CSV with experiment_path and position columns.")
    args = parser.parse_args(argv)

    import napari

    from .widget import QCWidget

    viewer = napari.Viewer(title="pre-qc review")
    _apply_app_icon()

    csv_path = args.csv or _prompt_for_csv()
    if not csv_path:
        sys.exit(0)  # user cancelled the picker -- quit quietly, nothing was loaded yet

    from . import manifest

    rows = manifest.load_or_resume(csv_path)
    if not rows:
        print(f"No rows found in {csv_path}", file=sys.stderr)
        sys.exit(1)

    n_unresolved = sum(1 for r in rows if r.resolution_error)
    if n_unresolved:
        print(f"Warning: {n_unresolved}/{len(rows)} row(s) could not be resolved to a file:")
        for row in rows:
            if row.resolution_error:
                print(f"  - {row.position}: {row.resolution_error}")

    widget = QCWidget(viewer, csv_path, rows)
    viewer.window.add_dock_widget(widget, name="QC review", area="right")
    napari.run()


def _prompt_for_csv() -> str:
    from qtpy.QtWidgets import QFileDialog

    path, _filter = QFileDialog.getOpenFileName(
        None, "Select the QC input CSV", "", "CSV files (*.csv)"
    )
    return path


if __name__ == "__main__":
    main()
