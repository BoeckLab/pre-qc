#!/bin/bash
# One-time installer for pre-qc (macOS only).
#
# Run this once, from Terminal, after cloning the repo (private repo --
# needs a GitHub SSH key registered first, same one-time setup as
# cell-slate -- see README's Install section):
#   bash scripts/install.sh
#
# It clones/updates ~/pre-qc, builds an isolated venv, installs the
# package into it, and generates /Applications/QC.app -- a single shared
# launcher icon for the whole qc/ suite (pre-qc now, post-qc once it
# exists at ~/post-qc with its own venv -- the launcher below checks for
# that and only shows a picker once there's actually a second tool
# installed). It asks (via a native file picker) which CSV to review,
# then `exec`s the `pre-qc` console script in place of the launcher
# process itself -- not spawned in a separate Terminal window -- so the
# running app keeps the bundle's identity/Dock icon throughout, the same
# way cell-slate's own launcher does. (An earlier version of this script
# ran the command via `osascript ... tell application "Terminal"`,
# which spawned a second, Terminal-owned process with its own generic
# icon alongside the launcher's -- fixed because it looked like two
# different things were opening.) After this, open it from /Applications
# or the Dock like any other app -- no `python`/venv activation needed
# again for routine use. This script is only for the very first install,
# or to rebuild the launcher app if it's ever deleted; re-run it (or `cd
# ~/pre-qc && git pull && pip install -e .`) to pick up updates.
set -euo pipefail

REPO_URL="git@github.com:BoeckLab/pre-qc.git"
INSTALL_DIR="$HOME/pre-qc"
APP_NAME="QC.app"
APP_DIR="/Applications/$APP_NAME"

echo "==> Cloning/updating pre-qc..."
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "==> Setting up Python environment..."
cd "$INSTALL_DIR"
PYTHON=python3.11
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3.10
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3
"$PYTHON" -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

echo "==> Building launcher app..."
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

# Icon is optional -- only bundle one if the repo ships a source PNG.
# Shared across the qc/ suite (pre-qc and post-qc use the same icon/app
# identity), so it lives under a plain "qc_icon.png" name rather than a
# pre-qc-specific one. Missing = app gets macOS's generic app icon, not a
# build failure.
ICON_KEY=""
SRC_PNG="$INSTALL_DIR/src/pre_qc/assets/qc_icon.png"
if [ -f "$SRC_PNG" ]; then
    ICONSET_DIR="$(mktemp -d)/qc.iconset"
    mkdir -p "$ICONSET_DIR"
    for size in 16 32 128 256 512; do
        sips -z "$size" "$size" "$SRC_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
        double=$((size * 2))
        sips -z "$double" "$double" "$SRC_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET_DIR" -o "$APP_DIR/Contents/Resources/qc.icns"
    ICON_KEY="<key>CFBundleIconFile</key><string>qc.icns</string>"
fi

# The launcher picks a tool first (only if post-qc is actually installed
# alongside pre-qc -- otherwise it skips straight to pre-qc, since a
# picker with one real option and one "not installed" option is just
# friction), asks which CSV to review via a native file-picker dialog,
# then `exec`s straight into the console script -- no Terminal window,
# no second process. Startup warnings that used to print to that
# Terminal (e.g. CSV rows that couldn't be resolved to a file) are
# already surfaced inside the GUI itself (QCWidget shows each
# unresolved row's error inline, and warns again before Finish runs), so
# nothing is lost by dropping the Terminal step.
cat > "$APP_DIR/Contents/MacOS/launcher" <<'LAUNCHER'
#!/bin/bash
TOOL="pre-qc"
if [ -d "$HOME/post-qc/.venv" ]; then
    CHOICE=$(osascript -e 'choose from list {"Pre-QC (before pipeline)", "Post-QC (after pipeline)"} with prompt "Which QC step?" without multiple selections allowed') || exit 0
    [ "$CHOICE" = "false" ] && exit 0
    case "$CHOICE" in
        "Post-QC"*) TOOL="post-qc" ;;
        *) TOOL="pre-qc" ;;
    esac
fi

CSV_PATH=$(osascript -e 'POSIX path of (choose file with prompt "Select the QC input CSV (experiment_path, position columns):" of type {"csv"})') || exit 0

if [ "$TOOL" = "post-qc" ]; then
    VENV="$HOME/post-qc/.venv"
else
    VENV="$HOME/pre-qc/.venv"
fi

source "$VENV/bin/activate"
exec "$TOOL" "$CSV_PATH"
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/launcher"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>QC</string>
    <key>CFBundleDisplayName</key><string>QC</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    $ICON_KEY
    <key>CFBundleIdentifier</key><string>ch.unibas.boecklab.qc</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
</dict>
</plist>
PLIST

# Clear the quarantine flag (the app is unsigned -- without this, macOS
# Gatekeeper blocks the very first launch with an "unidentified
# developer" warning).
xattr -cr "$APP_DIR"

echo
echo "==> Done. Opening QC..."
echo "    Right-click its Dock icon -> Options -> Keep in Dock to pin it."
open "$APP_DIR"
