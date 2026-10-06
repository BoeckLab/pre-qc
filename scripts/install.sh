#!/bin/bash
# One-time installer for pre-qc (macOS only).
#
# Run this once, from Terminal, after cloning the repo (private repo --
# needs a GitHub SSH key registered first, same one-time setup as
# cell-slate -- see README's Install section):
#   bash scripts/install.sh
#
# It clones/updates ~/pre-qc, builds an isolated venv, installs the
# package into it, and generates /Applications/Pre QC.app -- a thin
# launcher (not a frozen binary) that asks (via a native file picker)
# which CSV to review, then runs the `pre-qc` console script against it
# in a Terminal window (so any warnings -- e.g. unresolved CSV rows --
# are visible before napari opens). After this, open it from
# /Applications or the Dock like any other app -- no `python`/venv
# activation needed again for routine use. This script is only for the
# very first install, or to rebuild the launcher app if it's ever
# deleted; re-run it (or `cd ~/pre-qc && git pull && pip install -e .`)
# to pick up updates.
set -euo pipefail

REPO_URL="git@github.com:BoeckLab/pre-qc.git"
INSTALL_DIR="$HOME/pre-qc"
APP_NAME="Pre QC.app"
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

# Icon is optional -- only bundle one if the repo ships a source PNG
# (none does yet). Missing = app gets macOS's generic app icon, not a
# build failure.
ICON_KEY=""
SRC_PNG="$INSTALL_DIR/src/pre_qc/assets/pre_qc_icon.png"
if [ -f "$SRC_PNG" ]; then
    ICONSET_DIR="$(mktemp -d)/preqc.iconset"
    mkdir -p "$ICONSET_DIR"
    for size in 16 32 128 256 512; do
        sips -z "$size" "$size" "$SRC_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
        double=$((size * 2))
        sips -z "$double" "$double" "$SRC_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET_DIR" -o "$APP_DIR/Contents/Resources/pre_qc.icns"
    ICON_KEY="<key>CFBundleIconFile</key><string>pre_qc.icns</string>"
fi

# The launcher asks which CSV to review via a native file-picker dialog
# (pre-qc requires a CSV path -- there's no sensible double-click default),
# then runs the review in a visible Terminal window so startup warnings
# (e.g. CSV rows that couldn't be resolved to a file) are seen before
# napari opens, rather than silently swallowed by a backgrounded GUI app.
cat > "$APP_DIR/Contents/MacOS/launcher" <<'LAUNCHER'
#!/bin/bash
CSV_PATH=$(osascript -e 'POSIX path of (choose file with prompt "Select the QC input CSV (experiment_path, position columns):" of type {"csv"})' 2>/dev/null) || exit 0
osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    do script "source \"$HOME/pre-qc/.venv/bin/activate\" && pre-qc \"$CSV_PATH\""
end tell
APPLESCRIPT
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/launcher"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Pre QC</string>
    <key>CFBundleDisplayName</key><string>Pre QC</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    $ICON_KEY
    <key>CFBundleIdentifier</key><string>ch.unibas.boecklab.preqc</string>
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
echo "==> Done. Opening Pre QC..."
echo "    Right-click its Dock icon -> Options -> Keep in Dock to pin it."
open "$APP_DIR"
