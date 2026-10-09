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


# Fallback screen height for the proportional sizing below, used only if
# Qt can't report a real screen (e.g. headless) -- a conservative laptop
# figure, not a target to size up to.
_FALLBACK_SCREEN_HEIGHT = 900

# Window/dock margin so pre-qc never claims the *entire* screen (leaves
# room for the OS taskbar/menu bar and window chrome).
_SCREEN_MARGIN = 80


def _available_screen_height() -> int:
    from qtpy.QtWidgets import QApplication

    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return _FALLBACK_SCREEN_HEIGHT
    return screen.availableGeometry().height()


def _fit_window_to_screen(viewer) -> None:
    """Cap the whole napari window to the user's actual available screen
    geometry (primary monitor minus taskbar/dock chrome), never bigger --
    napari's own default size can otherwise exceed a smaller laptop
    screen. Only shrinks; never grows a window the user already resized
    down further."""
    from qtpy.QtWidgets import QApplication

    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return
    available = screen.availableGeometry()
    qt_window = viewer.window._qt_window
    current = qt_window.geometry()
    max_w = available.width() - _SCREEN_MARGIN
    max_h = available.height() - _SCREEN_MARGIN
    new_w = min(current.width(), max_w)
    new_h = min(current.height(), max_h)
    if (new_w, new_h) != (current.width(), current.height()):
        qt_window.resize(new_w, new_h)
    # Keep it fully on-screen even after shrinking (e.g. if it had been
    # positioned near the edge of a larger virtual desktop).
    qt_window.move(
        max(available.x(), min(qt_window.x(), available.x() + available.width() - new_w)),
        max(available.y(), min(qt_window.y(), available.y() + available.height() - new_h)),
    )


def _arrange_left_column(viewer, hist_bf_dock, hist_fl_dock, checklist_dock) -> None:
    """Cap the native layer-list dock's and both histogram docks' heights
    (scaled to the user's actual screen height, not a fixed pixel count
    that could dwarf a smaller display), and let the checklist panel
    (docked last in the same left-hand column) take whatever's left.
    Mirrors the resizeDocks call napari itself makes in Window.__init__
    for layer controls vs. layer list, just extending it to our own added
    docks. Five panels (controls, layer list, 2 histograms, checklist)
    can't all be comfortably full-height at once -- PanelTogglesWidget
    lets each be collapsed independently; these are just a reasonable
    starting split for everything open together."""
    try:
        from qtpy.QtCore import Qt as _Qt

        qt_viewer = viewer.window._qt_viewer
        qt_window = viewer.window._qt_window
        controls = qt_viewer.dockLayerControls
        layer_list = qt_viewer.dockLayerList
    except AttributeError:
        return

    screen_height = _available_screen_height()
    layer_list_height = max(70, min(110, int(screen_height * 0.10)))
    histogram_height = max(120, min(320, int(screen_height * 0.20)))

    qt_window.resizeDocks(
        [controls, layer_list, hist_bf_dock, hist_fl_dock, checklist_dock],
        [controls.minimumHeight(), layer_list_height, histogram_height, histogram_height, 10000],
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
    from .widget import ChecklistWidget, PanelTogglesWidget, QCWidget

    viewer = napari.Viewer(title="pre-qc review")
    _apply_app_icon()
    _fit_window_to_screen(viewer)

    # "left" is where napari's own layer controls + layer list panels
    # already live (added automatically by napari.Viewer()) -- docking
    # here stacks these below them in the same column, rather than
    # competing for space in the QC review dock on the right.
    hist_bf_widget = HistogramWidget("BF intensity")
    hist_fl_widget = HistogramWidget("PI/FL intensity")
    hist_bf_dock = viewer.window.add_dock_widget(
        hist_bf_widget, name="Histogram BF", area="left"
    )
    hist_fl_dock = viewer.window.add_dock_widget(
        hist_fl_widget, name="Histogram FL", area="left"
    )
    checklist_dock = viewer.window.add_dock_widget(
        ChecklistWidget(), name="QC checklist", area="left"
    )
    _arrange_left_column(viewer, hist_bf_dock, hist_fl_dock, checklist_dock)

    # A checkbox per left-column dock so all five (native layer controls,
    # native layer list, both live histograms, checklist) can coexist --
    # collapse whichever isn't needed right now instead of them fighting
    # over fixed heights.
    viewer.window.add_dock_widget(
        PanelTogglesWidget(
            {
                "Layer controls": viewer.window._qt_viewer.dockLayerControls,
                "Layer list": viewer.window._qt_viewer.dockLayerList,
                "Histogram BF": hist_bf_dock,
                "Histogram FL": hist_fl_dock,
                "QC checklist": checklist_dock,
            }
        ),
        name="Panels",
        area="left",
    )

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
