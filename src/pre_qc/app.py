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


def _fit_window_to_screen(viewer) -> None:
    """Maximize the main window to the user's actual available screen work
    area -- full width, height capped to what's actually there. Letting Qt
    maximize (rather than computing a geometry ourselves) adapts correctly
    per-monitor/per-OS, including taskbar/dock chrome, instead of guessing
    a margin that's wrong on some setups."""
    viewer.window._qt_window.showMaximized()


def _remove_layer_controls(viewer) -> None:
    """Delete napari's native layer-controls dock entirely (contrast
    limits/colormap/opacity sliders) -- pre-qc never needs to adjust those
    to judge good/bad, and the space is worth more to the histogram/
    checklist panels sharing the left column."""
    try:
        qt_window = viewer.window._qt_window
        controls = viewer.window._qt_viewer.dockLayerControls
    except AttributeError:
        return
    qt_window.removeDockWidget(controls)
    controls.deleteLater()


def _arrange_left_column(viewer, qc_panels_dock) -> None:
    """Cap the native layer-list dock's height and let the combined QC
    panels dock (histograms + checklist, each independently collapsible --
    see LeftPanelsWidget) take the rest. No fixed pixel height is needed
    for the histograms/checklist themselves -- collapsing a section inside
    that dock simply frees space for the others via normal Qt layout,
    which adapts to whatever height the dock actually has."""
    try:
        from qtpy.QtCore import Qt as _Qt

        qt_viewer = viewer.window._qt_viewer
        qt_window = viewer.window._qt_window
        layer_list = qt_viewer.dockLayerList
    except AttributeError:
        return

    qt_window.resizeDocks(
        [layer_list, qc_panels_dock],
        [90, 10000],
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

    from .histogram_widget import HistogramWidget
    from .widget import ChecklistWidget, LeftPanelsWidget, QCWidget

    viewer = napari.Viewer(title="pre-qc review")
    _apply_app_icon()
    _fit_window_to_screen(viewer)
    _remove_layer_controls(viewer)

    # "left" is where napari's own layer list already lives (added
    # automatically by napari.Viewer()) -- docking here stacks this below
    # it in the same column, rather than competing for space in the QC
    # review dock on the right. Histograms and checklist share one dock as
    # inline accordion sections (each with its own clickable arrowed
    # banner, see LeftPanelsWidget) instead of three separate docks plus a
    # remote list of toggles.
    hist_bf_widget = HistogramWidget("BF intensity")
    hist_fl_widget = HistogramWidget("PI/FL intensity")
    qc_panels_dock = viewer.window.add_dock_widget(
        LeftPanelsWidget(hist_bf_widget, hist_fl_widget, ChecklistWidget()),
        name="QC panels",
        area="left",
    )
    _arrange_left_column(viewer, qc_panels_dock)

    # No CSV required up front -- the widget starts in an idle state (Load
    # CSV button, everything else disabled) and a CSV can be loaded any
    # time, including switching to a different experiment later, which
    # fully resets review state rather than merging with what came before.
    widget = QCWidget(viewer, None, None, hist_bf=hist_bf_widget, hist_fl=hist_fl_widget)
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
