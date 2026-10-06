"""Format-agnostic raw movie loading.

Deliberately uncompressed-formats-only: QC runs before the pipeline, on
whatever is sitting in the experiment folder straight off the scope (.nd2)
or already exported (.ome.tiff/.tiff). JetRaw-compressed files
(.ome.p.tiff/.p.tif) need a licensed SDK only set up on sciCORE (see
BacNets' ONBOARDING_JETRAW.md) -- rather than silently trying and failing
deep in a decode call, we refuse them upfront with a pointer to decompress
first.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile


@dataclass
class Movie:
    """A loaded movie.

    Attributes:
        data: ``(T, C, Y, X)`` array.
        channel_names: length-``C`` list, best-effort from metadata
            (falls back to ``["channel_0", "channel_1", ...]``).
        path: source file path.
    """

    data: np.ndarray
    channel_names: list
    path: Path


# The TIFF Compression tag value for the proprietary JetRaw codec (Dotphoton).
# Not always present as `tifffile.COMPRESSION.JETRAW` depending on version, so
# compare against the raw numeric tag value directly.
_JETRAW_COMPRESSION_TAG = 48124

# Recognized uncompressed movie extensions.
_UNCOMPRESSED_EXTENSIONS = (".ome.tiff", ".ome.tif", ".nd2", ".tiff", ".tif")

# Filename substrings that mark a file as JetRaw-compressed by naming
# convention -- reject by name before even trying to open it, faster and
# clearer than waiting for the TIFF-tag sniff below (which only fires for
# files that otherwise look like plain TIFFs).
_COMPRESSED_MARKERS = (".p.tiff", ".p.tif")


class MovieResolutionError(ValueError):
    """Raised when a CSV row's (experiment_path, position) can't be resolved
    to exactly one uncompressed movie file, or a file can't be loaded."""


def _raise_if_jetraw(path: Path) -> None:
    """Fail early with an actionable message instead of letting
    tifffile/imagecodecs crash deep inside a decode call -- JetRaw needs a
    licensed SDK that only exists on sciCORE, never on a laptop."""
    try:
        with tifffile.TiffFile(str(path)) as tif:
            compression = tif.pages[0].compression
    except Exception:
        return  # let the normal load path surface whatever the real error is
    if int(compression) == _JETRAW_COMPRESSION_TAG:
        raise MovieResolutionError(
            f"{path.name} is JetRaw-compressed -- pre-qc can't decode this "
            "codec locally (it needs a licensed SDK that's only set up on sciCORE). "
            "Decompress it on sciCORE first, then load the plain .tiff it produces "
            "(see BacNets' ONBOARDING_JETRAW.md section 4: "
            '`jetraw-tools decompress <folder> --extension ".ome.p.tiff"`).'
        )


def resolve_movie_path(experiment_path, position: str) -> Path:
    """Find the single uncompressed movie file under ``experiment_path``
    whose name contains ``position``.

    No project-wide file-naming convention is assumed beyond "the position
    string appears somewhere in the filename" -- deliberately loose so the
    tool works across the lab's different acquisition layouts, trading
    precision for not needing per-pipeline path-construction logic
    maintained here. Raises if zero or more than one candidate is found.
    """
    root = Path(experiment_path)
    if not root.exists():
        raise MovieResolutionError(f"experiment_path does not exist: {root}")

    candidates = []
    for path in root.rglob(f"*{position}*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if any(marker in name for marker in _COMPRESSED_MARKERS):
            continue
        if any(name.endswith(ext) for ext in _UNCOMPRESSED_EXTENSIONS):
            candidates.append(path)

    if not candidates:
        raise MovieResolutionError(
            f"No uncompressed movie found under {root} matching position "
            f"{position!r}. (JetRaw-compressed .p.tiff/.p.tif files are "
            "deliberately excluded -- decompress them first.)"
        )
    if len(candidates) > 1:
        listed = "\n  ".join(str(c) for c in sorted(candidates))
        raise MovieResolutionError(
            f"Ambiguous: {len(candidates)} files under {root} match position "
            f"{position!r}:\n  {listed}\nNarrow the position string so it "
            "matches exactly one file."
        )
    return candidates[0]


def load_any_movie(path) -> Movie:
    """Load a movie of any supported uncompressed format into the shared
    ``(T, C, Y, X)`` :class:`Movie`."""
    path = Path(path)
    name = path.name.lower()
    if any(marker in name for marker in _COMPRESSED_MARKERS):
        raise MovieResolutionError(
            f"{path.name} is JetRaw-compressed by naming convention -- pre-qc "
            "only accepts uncompressed input."
        )
    if name.endswith(".nd2"):
        return _load_nd2(path)
    return _load_tiff(path)


def _load_tiff(path: Path) -> Movie:
    _raise_if_jetraw(path)  # catches a mislabeled extension via the real TIFF tag
    with tifffile.TiffFile(str(path)) as tif:
        series = tif.series[0]
        arr = series.asarray()
        axes = series.axes  # e.g. "TCYX", "CYX", "TYX", "YX"

        arr, axes = _ensure_tcyx(arr, axes)
        channel_names = _read_channel_names(tif, n_channels=arr.shape[1])

    return Movie(data=arr, channel_names=channel_names, path=path)


def _load_nd2(path: Path) -> Movie:
    import nd2

    with nd2.ND2File(str(path)) as reader:
        arr = reader.asarray()
        channel_names = [c.channel.name for c in reader.metadata.channels] if reader.metadata else []

    if arr.ndim == 2:
        arr = arr[np.newaxis, np.newaxis, :, :]
    elif arr.ndim == 3:
        arr = arr[np.newaxis, :, :, :]
    elif arr.ndim != 4:
        raise MovieResolutionError(f"{path.name}: unsupported nd2 array shape {arr.shape}")

    n_channels = arr.shape[1]
    if len(channel_names) != n_channels:
        channel_names = [f"channel_{i}" for i in range(n_channels)]

    return Movie(data=arr, channel_names=channel_names, path=path)


def _ensure_tcyx(arr: np.ndarray, axes: str) -> tuple:
    """Reshape/broadcast an arbitrary-axis-order array to ``(T, C, Y, X)``."""
    axes = axes.upper()
    if set(axes) - set("TCZYXS"):
        raise MovieResolutionError(f"Unsupported axes order: {axes!r}")

    # Drop a trailing/leading singleton Z if present (Z=1 volumes).
    if "Z" in axes:
        z_pos = axes.index("Z")
        if arr.shape[z_pos] == 1:
            arr = np.squeeze(arr, axis=z_pos)
            axes = axes.replace("Z", "")
        else:
            raise MovieResolutionError(f"3D (Z>1) movies are not supported, got axes={axes!r}")

    if "Y" not in axes or "X" not in axes:
        raise MovieResolutionError(f"Movie is missing spatial axes, got axes={axes!r}")

    if "T" not in axes:
        arr = arr[np.newaxis, ...]
        axes = "T" + axes
    if "C" not in axes:
        arr = arr[:, np.newaxis, ...]
        axes = axes[0] + "C" + axes[1:]

    order = [axes.index(a) for a in "TCYX"]
    arr = np.transpose(arr, order)
    return arr, "TCYX"


def _read_channel_names(tif: tifffile.TiffFile, n_channels: int) -> list:
    names = None
    try:
        if tif.imagej_metadata and "Labels" in tif.imagej_metadata:
            labels = tif.imagej_metadata["Labels"]
            if len(labels) >= n_channels:
                names = list(labels[:n_channels])
    except Exception:
        names = None

    if names is None:
        try:
            ome = tif.ome_metadata
            if ome:
                import ome_types

                ome_obj = ome_types.from_xml(ome)
                channels = ome_obj.images[0].pixels.channels
                if len(channels) >= n_channels:
                    names = [c.name or f"channel_{i}" for i, c in enumerate(channels[:n_channels])]
        except Exception:
            names = None

    if names is None:
        names = [f"channel_{i}" for i in range(n_channels)]
    return names
