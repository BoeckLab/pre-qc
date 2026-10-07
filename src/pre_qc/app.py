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


def _apply_app_icon(main_window) -> None:
    if not _ICON_PATH.exists():
        return
    from qtpy.QtGui import QIcon
    from qtpy.QtWidgets import QApplication

    icon = QIcon(str(_ICON_PATH))
    main_window.setWindowIcon(icon)
    app = QApplication.instance()
    if app is not None:
        app.setWindowIcon(icon)


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
    parser.add_argument("csv", help="Input CSV with experiment_path and position columns.")
    args = parser.parse_args(argv)

    from . import manifest

    rows = manifest.load_or_resume(args.csv)
    if not rows:
        print(f"No rows found in {args.csv}", file=sys.stderr)
        sys.exit(1)

    n_unresolved = sum(1 for r in rows if r.resolution_error)
    if n_unresolved:
        print(f"Warning: {n_unresolved}/{len(rows)} row(s) could not be resolved to a file:")
        for row in rows:
            if row.resolution_error:
                print(f"  - {row.position}: {row.resolution_error}")

    import napari

    from .widget import QCWidget

    viewer = napari.Viewer(title="pre-qc review")
    widget = QCWidget(viewer, args.csv, rows)
    dock = viewer.window.add_dock_widget(widget, name="QC review", area="right")
    _apply_app_icon(dock.window())
    napari.run()


if __name__ == "__main__":
    main()
