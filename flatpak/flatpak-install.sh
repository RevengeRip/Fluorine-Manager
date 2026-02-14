#!/usr/bin/env bash
# Build the Fluorine Manager Flatpak.
#
# Usage:
#   cd flatpak && bash flatpak-install.sh           # build & install locally
#   cd flatpak && bash flatpak-install.sh bundle     # build a .flatpak file to share
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
MANIFEST="${SCRIPT_DIR}/com.fluorine.manager.yml"
APP_ID="com.fluorine.manager"
BUILD_DIR="${PROJECT_DIR}/.flatpak-build"
MODE="${1:-install}"

# ── Ensure flathub remote exists ──
if ! flatpak remote-list --user | grep -q flathub; then
    echo "Adding flathub remote..."
    flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
fi

# ── Install required Flatpak runtime and SDK ──
echo "Installing KDE Platform and SDK (may take a while on first run)..."
flatpak install --user --noninteractive flathub org.kde.Platform//6.10 org.kde.Sdk//6.10

echo "Installing Rust SDK extension..."
flatpak install --user --noninteractive flathub org.freedesktop.Sdk.Extension.rust-stable//25.08

cd "${PROJECT_DIR}"

if [ "${MODE}" = "bundle" ]; then
    # ── Build a distributable .flatpak file ──
    echo ""
    echo "Building Flatpak bundle (this may take a while)..."
    flatpak-builder --repo="${PROJECT_DIR}/flatpak-repo" --force-clean --ccache \
        "${BUILD_DIR}" "${MANIFEST}"
    flatpak build-bundle \
        --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
        "${PROJECT_DIR}/flatpak-repo" "${PROJECT_DIR}/fluorine-manager.flatpak" "${APP_ID}"
    BUNDLE_SIZE="$(du -sh fluorine-manager.flatpak | cut -f1)"
    echo ""
    echo "=== Bundle created: fluorine-manager.flatpak (${BUNDLE_SIZE}) ==="
    echo ""
    echo "Share this file with testers. They install it with:"
    echo "  flatpak install --user fluorine-manager.flatpak"
else
    # ── Build and install locally ──
    echo ""
    echo "Building and installing Flatpak locally (this may take a while)..."
    flatpak-builder --install --user --force-clean --ccache \
        "${BUILD_DIR}" "${MANIFEST}"
    echo ""
    echo "=== Flatpak installed successfully ==="
fi

echo ""
echo "Usage:"
echo "  flatpak run ${APP_ID}                    # launch GUI"
echo "  flatpak run ${APP_ID} create-portable --name myinstance --game falloutnv"
echo "  flatpak run ${APP_ID} list-instances"
