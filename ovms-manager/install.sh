#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Install OVMS Manager in the application menu, for the current user only (no root needed).
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install.sh [--dev] [--with-optimum] | --uninstall | --help

  (no option)       install or update: copies the app to ~/.local/share/ovms-manager/app,
                    so the project folder can be moved or deleted afterwards
  --dev             development mode: the menu entry runs the files of THIS folder
  --with-optimum    also install optimum-intel (+ PyTorch CPU, a few GB) for the Converter page
  --uninstall       remove app, menu entry, icon, command and Python environments
                    (models in ~/models are kept)

Environment variables:
  PYTHON=python3.12   Python interpreter used to create the environment (default: python3)

Installed layout:
  ~/.local/share/ovms-manager/app/            copy of the app
  ~/.local/share/ovms-manager/venv/           PySide6, requests
  ~/.local/share/ovms-manager/optimum-venv/   optimum-intel (only with --with-optimum)
  ~/.local/share/applications/ovms-manager.desktop
  ~/.local/bin/ovms-manager
After editing the project files, run this script again to update the installed copy.
EOF
}

APP_ID="ovms-manager"   # = name of the .desktop file and of app.setDesktopFileName() in ovms_manager.py
SCRIPT="ovms_manager.py"
FILES=(ovms_manager.py ovms-manager.svg requirements.txt install.sh locale/it.json)
# shared UI kit of the ecosystem (themes, fonts, icons), copied from ../common next to the app
COMMON_FILES=(ui_kit.py fonts/InterVariable.ttf fonts/InterVariable-Italic.ttf fonts/Newsreader.ttf
              fonts/Newsreader-Italic.ttf fonts/OFL-Inter.txt fonts/OFL-Newsreader.txt)
CHECK="import PySide6.QtWidgets, requests"
MIN_PYTHON="3.10"

SOURCE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
SUITE_DIR="$DATA_DIR/$APP_ID"
APP_DIR="$SUITE_DIR/app"
VENV_DIR="$SUITE_DIR/venv"
OPTIMUM_VENV="$SUITE_DIR/optimum-venv"
BIN_DIR="$HOME/.local/bin"
DESKTOP_FILE="$DATA_DIR/applications/$APP_ID.desktop"
ICON_FILE="$DATA_DIR/icons/hicolor/scalable/apps/$APP_ID.svg"
PYTHON="${PYTHON:-python3}"

# distro_pkg <debian> <fedora> <arch> <opensuse>: install command with the right package name for this system
distro_pkg() {
    if [[ -e /run/ostree-booted ]] && command -v rpm-ostree >/dev/null; then echo "rpm-ostree install $2"
    elif command -v apt-get >/dev/null; then echo "sudo apt install $1"
    elif command -v dnf >/dev/null; then echo "sudo dnf install $2"
    elif command -v pacman >/dev/null; then echo "sudo pacman -S $3"
    elif command -v zypper >/dev/null; then echo "sudo zypper install $4"
    else echo "install '$1' with your package manager"; fi
}

has_lib() {  # has_lib libfoo.so.0: true if the dynamic linker can find the library
    local ldconfig
    ldconfig="$(command -v ldconfig || echo /sbin/ldconfig)"
    if [[ -x "$ldconfig" ]] && "$ldconfig" -p 2>/dev/null | grep -q "$1"; then return 0; fi
    compgen -G "/usr/lib*/$1*" >/dev/null || compgen -G "/usr/lib*/*-linux-gnu*/$1*" >/dev/null ||
        compgen -G "/lib*/*-linux-gnu*/$1*" >/dev/null
}

refresh_menu() {
    if command -v update-desktop-database >/dev/null; then update-desktop-database -q "$DATA_DIR/applications" || true; fi
    if command -v gtk-update-icon-cache >/dev/null; then gtk-update-icon-cache -q -t "$DATA_DIR/icons/hicolor" || true; fi
}

# create_venv DIR: (re)create a virtual environment with $PYTHON, explaining what to install if it fails
create_venv() {
    local dir="$1" want have
    want="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    if [[ -x "$dir/bin/python" ]]; then
        have="$("$dir/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        [[ "$have" == "$want" ]] && return 0
        echo "  (the environment uses Python ${have:-?}, recreating it with $want)"
        rm -rf "$dir"
    fi
    if ! "$PYTHON" -m venv "$dir"; then
        rm -rf "$dir"
        echo "Cannot create a Python virtual environment. Install the venv module: $(distro_pkg python3-venv python3 python python3)" >&2
        exit 1
    fi
}

pip_install() {  # pip_install VENV args...: pip install with a helpful message on failure
    local venv="$1"; shift
    if ! "$venv/bin/python" -m pip install -q --upgrade "$@"; then
        echo "pip could not install: $*" >&2
        echo "If no wheel exists for Python $("$venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])'), install an older Python" >&2
        echo "(e.g. 3.12) and run again with:  PYTHON=python3.12 $0" >&2
        exit 1
    fi
}

MODE="install"
WITH_OPTIMUM=0
for arg in "$@"; do
    case "$arg" in
        --dev) MODE="dev" ;;
        --uninstall) MODE="uninstall" ;;
        --with-optimum) WITH_OPTIMUM=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 1 ;;
    esac
done

if [[ "$MODE" == "uninstall" ]]; then
    rm -f "$DESKTOP_FILE" "$ICON_FILE" "$BIN_DIR/$APP_ID"
    rm -rf "$SUITE_DIR"   # if this script runs from there, bash has already read it: it can delete itself
    refresh_menu
    echo "✓ OVMS Manager removed. Models in ${OPENVINO_MODELS_DIR:-~/models} were not touched."
    echo "  The OVMS container image can be removed with:  podman rmi openvino/model_server:latest-gpu  (or docker rmi)"
    exit 0
fi

for f in "${FILES[@]}"; do
    [[ -f "$SOURCE_DIR/$f" ]] || { echo "Missing file: $SOURCE_DIR/$f" >&2; exit 1; }
done
COMMON_DIR="$(dirname "$SOURCE_DIR")/common"
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then  # the installed copy already has the kit next to it
    for f in "${COMMON_FILES[@]}"; do
        [[ -f "$COMMON_DIR/$f" ]] || { echo "Missing file: $COMMON_DIR/$f" >&2; exit 1; }
    done
fi
command -v "$PYTHON" >/dev/null || { echo "$PYTHON not found: $(distro_pkg python3 python3 python python3)" >&2; exit 1; }
"$PYTHON" -c "import sys; sys.exit(sys.version_info < tuple(map(int, '$MIN_PYTHON'.split('.'))))" ||
    { echo "Python $MIN_PYTHON or newer is required ($PYTHON is $("$PYTHON" -V 2>&1))." >&2; exit 1; }
if ! command -v podman >/dev/null && ! command -v docker >/dev/null; then
    echo "  (warning: neither Podman nor Docker found, one is needed to run the server: $(distro_pkg podman podman podman podman))"
fi
has_lib libxcb-cursor.so.0 || echo "  (warning: libxcb-cursor not found, Qt needs it on X11 sessions: $(distro_pkg libxcb-cursor0 xcb-util-cursor xcb-util-cursor libxcb-cursor0))"

if [[ "$MODE" == "dev" ]]; then
    RUN_DIR="$SOURCE_DIR"
    echo "→ Development mode: the menu entry will use $RUN_DIR"
elif [[ "$SOURCE_DIR" == "$APP_DIR" ]]; then
    RUN_DIR="$APP_DIR"
    echo "→ Running from the installed copy: updating only the environment and the menu entry"
else
    RUN_DIR="$APP_DIR"
    echo "→ Copying the app to $APP_DIR"
    # copy into a temporary folder and then swap: an interrupted install never leaves half-copied files.
    # cp without -p: copying permissions/ACLs of the project (e.g. shared folders) can fail
    rm -rf "$APP_DIR.new" "$APP_DIR.old"
    mkdir -p "$APP_DIR.new"
    for f in "${FILES[@]}"; do
        mkdir -p "$(dirname "$APP_DIR.new/$f")"
        cp "$SOURCE_DIR/$f" "$APP_DIR.new/$f"
    done
    for f in "${COMMON_FILES[@]}"; do
        mkdir -p "$(dirname "$APP_DIR.new/$f")"
        cp "$COMMON_DIR/$f" "$APP_DIR.new/$f"
    done
    find "$APP_DIR.new" -type d -exec chmod 755 {} +
    find "$APP_DIR.new" -type f -exec chmod 644 {} +
    chmod 755 "$APP_DIR.new/install.sh" "$APP_DIR.new/$SCRIPT"
    if [[ -d "$APP_DIR" ]]; then mv "$APP_DIR" "$APP_DIR.old"; fi
    mv "$APP_DIR.new" "$APP_DIR"
    rm -rf "$APP_DIR.old"
fi

echo "→ Python environment in $VENV_DIR"
create_venv "$VENV_DIR"
pip_install "$VENV_DIR" pip
pip_install "$VENV_DIR" -r "$RUN_DIR/requirements.txt"
"$VENV_DIR/bin/python" -c "$CHECK" || { echo "Installing the dependencies failed." >&2; exit 1; }

if (( WITH_OPTIMUM )); then
    echo "→ Optimum environment in $OPTIMUM_VENV (downloads a few GB the first time)"
    create_venv "$OPTIMUM_VENV"
    pip_install "$OPTIMUM_VENV" pip
    # CPU-only PyTorch: conversion does not need CUDA, and it saves several GB
    "$OPTIMUM_VENV/bin/python" -m pip install -q --upgrade torch --index-url https://download.pytorch.org/whl/cpu ||
        echo "  (CPU-only PyTorch not available for this Python: pip will pick the default build)"
    pip_install "$OPTIMUM_VENV" "optimum-intel[openvino]"
    [[ -x "$OPTIMUM_VENV/bin/optimum-cli" ]] || { echo "optimum-cli was not installed." >&2; exit 1; }
fi

echo "→ Icon, menu entry and command"
install -Dm644 "$RUN_DIR/$APP_ID.svg" "$ICON_FILE"
mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP_FILE")"
printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$VENV_DIR/bin/python" "$RUN_DIR/$SCRIPT" > "$BIN_DIR/$APP_ID"
chmod 755 "$BIN_DIR/$APP_ID"
# Exec quoted: the paths may contain spaces
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=OVMS Manager
GenericName=OpenVINO model manager
GenericName[it]=Gestione dei modelli OpenVINO
Comment=Run OpenVINO Model Server, download models from Hugging Face and convert them with Optimum
Comment[it]=Avvia OpenVINO Model Server, scarica modelli da Hugging Face e convertili con Optimum
Exec="$VENV_DIR/bin/python" "$RUN_DIR/$SCRIPT"
Path=$RUN_DIR
Icon=$APP_ID
Terminal=false
Categories=Development;
Keywords=OpenVINO;OVMS;LLM;AI;Hugging Face;Whisper;Intel;
Keywords[it]=OpenVINO;OVMS;LLM;IA;AI;Hugging Face;Whisper;Intel;modelli;
StartupNotify=true
StartupWMClass=$APP_ID
DESKTOP
chmod 644 "$DESKTOP_FILE"
if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$DESKTOP_FILE" || echo "  (desktop-file-validate warnings: the entry works anyway)"
fi
refresh_menu

echo
echo "✓ OVMS Manager installed: look for it in the application menu."
if [[ "$RUN_DIR" == "$APP_DIR" ]]; then
    echo "  Runs from $APP_DIR: you can move or delete the project folder."
else
    echo "  Development mode: don't move $SOURCE_DIR."
fi
if [[ ":$PATH:" == *":$BIN_DIR:"* ]]; then
    echo "  From a terminal: ovms-manager"
else
    echo "  To start it from a terminal add $BIN_DIR to your PATH."
fi
(( WITH_OPTIMUM )) || echo "  The Converter page needs optimum-intel: run again with --with-optimum to add it."
echo "  To remove it: $RUN_DIR/install.sh --uninstall"
