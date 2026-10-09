"""CLI entry point: ``pre-qc <csv>`` launches napari with the QC review dock
widget."""

import argparse
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


# Every pre-qc movie is BF and/or PI -- 1 to 3 layers, never a long list --
# so napari's own default (layer list gets all the leftover vertical space,
# see napari's Window.__init__) wastes room that the QC checklist panel
# below it could use instead.
_LAYER_LIST_HEIGHT = 110


def _shrink_layer_list(viewer, checklist_dock) -> None:
    """Cap the native layer-list dock's height and let the checklist panel
    (docked below it in the same left-hand column) take the rest. Mirrors
    the resizeDocks call napari itself makes in Window.__init__ for
    layer controls vs. layer list, just adding our checklist as the third,
    space-absorbing widget."""
    try:
        from qtpy.QtCore import Qt as _Qt

        qt_viewer = viewer.window._qt_viewer
        qt_window = viewer.window._qt_window
        controls = qt_viewer.dockLayerControls
        layer_list = qt_viewer.dockLayerList
    except AttributeError:
        return

    qt_window.resizeDocks(
        [controls, layer_list, checklist_dock],
        [controls.minimumHeight(), _LAYER_LIST_HEIGHT, 10000],
        _Qt.Orientation.Vertical,
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-pipeline QC review: scroll each raw movie listed in a CSV "
            "(columns EXP, WELL, FRAME) and mark it good/bad, plus an "
            "optional Q/NQ/X post-QC label. Once every resolvable row is "
            "good, computes cheap analyzability metrics (sharpness/drift/"
            "saturation/foreground coverage/BF intensity histogram) and "
            "writes an HTML report -- before committing GPU hours to the "
            "real ASCT pipeline. The CSV is optional -- run with none and "
            "load one later from inside the app (Load CSV button)."
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
    parser.add_argument("csv", nargs="?", help="Input CSV with EXP, WELL and FRAME columns.")
    args = parser.parse_args(argv)

    import napari

    from .widget import ChecklistWidget, QCWidget

    viewer = napari.Viewer(title="pre-qc review")
    _apply_app_icon()

    # "left" is where napari's own layer controls + layer list panels
    # already live (added automatically by napari.Viewer()) -- docking
    # here stacks this checklist below them in the same column, rather
    # than competing for space in the QC review dock on the right.
    checklist_dock = viewer.window.add_dock_widget(
        ChecklistWidget(), name="QC checklist", area="left"
    )
    _shrink_layer_list(viewer, checklist_dock)

    # No CSV required up front -- the widget starts in an idle state (Load
    # CSV button, everything else disabled) and a CSV can be loaded any
    # time, including switching to a different experiment later, which
    # fully resets review state rather than merging with what came before.
    widget = QCWidget(viewer, None, None)
    viewer.window.add_dock_widget(widget, name="QC review", area="right")

    csv_path = args.csv or _prompt_for_csv()
    if csv_path:
        widget.open_csv(csv_path)

    napari.run()


def _prompt_for_csv() -> str:
    from qtpy.QtWidgets import QFileDialog

    path, _filter = QFileDialog.getOpenFileName(
        None, "Select the QC input CSV", "", "CSV files (*.csv)"
    )
    return path


if __name__ == "__main__":
    main()
