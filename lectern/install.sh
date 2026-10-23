#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Install Lectern (lecture notebook with live transcription from the microphone) and lectern-transcribe
# (batch transcription of folders from the terminal), for the current user only (no root needed).
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install.sh [--dev] | --uninstall | --help

  (no option)   install or update: copies the files to ~/.local/share/lectern/app,
                so the project folder can be moved or deleted afterwards
  --dev         development mode: the menu entry runs the files of THIS folder
  --uninstall   remove app, menu entry, icon, commands and Python environment
                (models, notes and settings are kept)

Environment variables:
  PYTHON=python3.12   Python interpreter used to create the environment (default: python3)

Installed layout:
  ~/.local/share/lectern/app/     copy of the files
  ~/.local/share/lectern/venv/    OpenVINO GenAI, PySide6, …
  ~/.local/share/applications/lectern.desktop
  ~/.local/bin/lectern, ~/.local/bin/lectern-transcribe
Whisper models are looked for in ~/models (or $OPENVINO_MODELS_DIR).
After editing the project files, run this script again to update the installed copy.
EOF
}

APP_ID="lectern"   # = name of the .desktop file and APP_ID in lectern.py
SCRIPT="lectern.py"
FILES=(lectern.py transcribe.py transcribe.sh lectern.sh lectern.svg requirements.txt install.sh locale/it.json)
# shared UI kit of the ecosystem (themes, fonts, icons), copied from ../common next to the app
COMMON_FILES=(ui_kit.py fonts/InterVariable.ttf fonts/InterVariable-Italic.ttf fonts/Newsreader.ttf
              fonts/Newsreader-Italic.ttf fonts/OFL-Inter.txt fonts/OFL-Newsreader.txt)
CHECK="import PySide6.QtMultimedia, requests, numpy, openvino, openvino_genai"
MIN_PYTHON="3.10"

SOURCE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
SUITE_DIR="$DATA_DIR/$APP_ID"
APP_DIR="$SUITE_DIR/app"
VENV_DIR="$SUITE_DIR/venv"
BIN_DIR="$HOME/.local/bin"
DESKTOP_FILE="$DATA_DIR/applications/$APP_ID.desktop"
ICON_FILE="$DATA_DIR/icons/hicolor/scalable/apps/$APP_ID.svg"
PYTHON="${PYTHON:-python3}"

# User's Documents folder (localized: Documents, Documenti, Dokumente…), where the app keeps the notebook
DOCS_DIR="$(xdg-user-dir DOCUMENTS 2>/dev/null || true)"
[[ -n "$DOCS_DIR" && "$DOCS_DIR" != "$HOME" ]] || DOCS_DIR="$HOME/Documents"
# = NOTEBOOK_DIRNAME in lectern.py and its translations: an existing notebook keeps its name
NOTEBOOK_DIR="$DOCS_DIR/Lecture Notes"
UI_LANG="${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}"
if [[ -d "$DOCS_DIR/Appunti delle lezioni" ]] || [[ ! -d "$NOTEBOOK_DIR" && "$UI_LANG" == it* ]]; then
    NOTEBOOK_DIR="$DOCS_DIR/Appunti delle lezioni"
fi

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
for arg in "$@"; do
    case "$arg" in
        --dev) MODE="dev" ;;
        --uninstall) MODE="uninstall" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 1 ;;
    esac
done

if [[ "$MODE" == "uninstall" ]]; then
    rm -f "$DESKTOP_FILE" "$ICON_FILE" "$BIN_DIR/$APP_ID" "$BIN_DIR/lectern-transcribe"
    rm -rf "$SUITE_DIR"   # if this script runs from there, bash has already read it: it can delete itself
    refresh_menu
    echo "✓ Lectern removed. Models (${OPENVINO_MODELS_DIR:-~/models}), notes ($NOTEBOOK_DIR) and settings were not touched."
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
command -v ffmpeg >/dev/null ||
    echo "  (warning: ffmpeg not found, needed to transcribe files other than .wav: $(distro_pkg ffmpeg ffmpeg-free ffmpeg ffmpeg))"
has_lib libxcb-cursor.so.0 ||
    echo "  (warning: libxcb-cursor not found, Qt needs it on X11 sessions: $(distro_pkg libxcb-cursor0 xcb-util-cursor xcb-util-cursor libxcb-cursor0))"
# Whisper on an Intel GPU runs on this machine (not in a container): it needs the Intel compute runtime
if compgen -G "/dev/dri/renderD*" >/dev/null && ! compgen -G "/etc/OpenCL/vendors/intel*.icd" >/dev/null &&
   ! has_lib libze_intel_gpu.so; then
    echo "  (warning: Intel GPU compute runtime not found, without it only the CPU works:" \
         "$(distro_pkg intel-opencl-icd intel-compute-runtime intel-compute-runtime intel-opencl))"
fi

# The app is single-instance: if it is still open (even minimized), starting it from the menu brings the
# window already open to the front, that is the old version. It must be closed before updating.
# Only Python processes running the script (not, for example, an editor with the file open)
RUNNING='^[^ ]*python[0-9.]* (/.*/)?lectern\.py( |$)'
pids=""
command -v pgrep >/dev/null && pids="$(pgrep -f "$RUNNING" || true)"
if [[ -n "$pids" ]]; then
    echo "→ Lectern is open (PID ${pids//$'\n'/ }): it must be closed, otherwise the menu brings back the old version."
    answer="y"
    if [[ -t 0 ]]; then
        read -rp "  Close it now? Transcribed text is saved, a recording in progress stops. [Y/n] " answer
    fi
    if [[ "${answer:-y}" =~ ^[nN] ]]; then
        echo "  OK: close it yourself before opening it again from the menu."
    else
        # shellcheck disable=SC2086  # one argument per PID
        kill $pids 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$RUNNING" >/dev/null || break; sleep 0.5; done
        pkill -9 -f "$RUNNING" 2>/dev/null || true
        echo "  Closed."
    fi
fi

if [[ "$MODE" == "dev" ]]; then
    RUN_DIR="$SOURCE_DIR"
    echo "→ Development mode: the menu entry will use $RUN_DIR"
elif [[ "$SOURCE_DIR" == "$APP_DIR" ]]; then
    RUN_DIR="$APP_DIR"
    echo "⚠ You are running the copy that is ALREADY installed ($APP_DIR): the app files are NOT updated," >&2
    echo "  only the Python environment and the menu entry. To install a new version run the script" >&2
    echo "  from the project folder:  <project>/lectern/install.sh" >&2
else
    RUN_DIR="$APP_DIR"
    echo "→ Copying the files to $APP_DIR"
    # copy into a temporary folder and then swap: an interrupted install never leaves half-copied files.
    # cp without -p: copying permissions/ACLs of the project (e.g. shared folders) can fail and stop
    # the installation leaving the old version; chmod below sets the right permissions
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
    chmod 755 "$APP_DIR.new/install.sh" "$APP_DIR.new/transcribe.sh" "$APP_DIR.new/lectern.sh" \
              "$APP_DIR.new/$SCRIPT" "$APP_DIR.new/transcribe.py"
    if [[ -d "$APP_DIR" ]]; then mv "$APP_DIR" "$APP_DIR.old"; fi
    mv "$APP_DIR.new" "$APP_DIR"
    rm -rf "$APP_DIR.old"
fi

echo "→ Python environment in $VENV_DIR (downloads a few hundred MB the first time)"
create_venv "$VENV_DIR"
pip_install "$VENV_DIR" pip
pip_install "$VENV_DIR" -r "$RUN_DIR/requirements.txt"
"$VENV_DIR/bin/python" -c "$CHECK" || { echo "Installing the dependencies failed." >&2; exit 1; }

echo "→ Icon, menu entry and commands"
install -Dm644 "$RUN_DIR/$APP_ID.svg" "$ICON_FILE"
mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP_FILE")"
printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$VENV_DIR/bin/python" "$RUN_DIR/$SCRIPT" > "$BIN_DIR/$APP_ID"
chmod 755 "$BIN_DIR/$APP_ID"
ln -sf "$RUN_DIR/transcribe.sh" "$BIN_DIR/lectern-transcribe"   # transcribe.sh follows the link to find transcribe.py
# Exec quoted: the paths may contain spaces. A single main category (Education),
# otherwise the app shows up twice in some menus
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=Lectern
GenericName=Lecture notebook
GenericName[it]=Quaderno degli appunti delle lezioni
Comment=Record lectures and transcribe them in real time, organized by course, lecture and category (Whisper on OpenVINO, fully local)
Comment[it]=Registra le lezioni e trascrivile in tempo reale, organizzate per corso, lezione e categoria (Whisper su OpenVINO, tutto in locale)
Exec="$VENV_DIR/bin/python" "$RUN_DIR/$SCRIPT"
Path=$RUN_DIR
Icon=$APP_ID
Terminal=false
Categories=Education;
Keywords=Whisper;transcription;dictation;lecture;notes;notebook;university;course;microphone;speech;OpenVINO;
Keywords[it]=Whisper;trascrizione;dettatura;lezioni;appunti;quaderno;università;corso;microfono;voce;OpenVINO;
StartupNotify=true
StartupWMClass=$APP_ID
DESKTOP
chmod 644 "$DESKTOP_FILE"
if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$DESKTOP_FILE" || echo "  (desktop-file-validate warnings: the entry works anyway)"
fi
refresh_menu

if [[ "$RUN_DIR" != "$SOURCE_DIR" ]] && ! cmp -s "$SOURCE_DIR/$SCRIPT" "$RUN_DIR/$SCRIPT"; then
    echo "⚠ $RUN_DIR/$SCRIPT differs from the project one: the menu will NOT start the version you just edited." >&2
fi
version="$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' "$RUN_DIR/$SCRIPT")"

echo
echo "✓ Lectern ${version:+$version }installed: look for it in the application menu."
echo "  The menu runs: $RUN_DIR/$SCRIPT"
if [[ "$RUN_DIR" == "$APP_DIR" ]]; then
    echo "  Runs from $APP_DIR: you can move or delete the project folder."
else
    echo "  Development mode: don't move $SOURCE_DIR."
fi
if [[ ":$PATH:" == *":$BIN_DIR:"* ]]; then
    echo "  From a terminal: lectern · lectern-transcribe <folder>"
else
    echo "  To use the commands lectern and lectern-transcribe add $BIN_DIR to your PATH."
fi
echo "  Notes are saved in: $NOTEBOOK_DIR"
echo "  Whisper models go in ${OPENVINO_MODELS_DIR:-~/models}: download them with OVMS Manager → Explore,"
echo "  or let lectern-transcribe download whisper-large-v3-turbo-int8-ov on first use."
echo "  To remove it: $RUN_DIR/install.sh --uninstall"
