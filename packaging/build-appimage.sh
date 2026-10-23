#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build portable AppImages of the apps: one file each, with its own Python, Qt and OpenVINO inside, that runs
# on any x86-64 Linux distribution with glibc 2.34 or newer (the floor set by the Qt 6 wheels), without
# installing anything.
#
#   packaging/build-appimage.sh                     both apps, from this checkout, into dist/
#   packaging/build-appimage.sh lectern             only one app (lectern | ovms-manager)
#   packaging/build-appimage.sh --source DIR        build another checkout (e.g. a release tag in a worktree)
#   packaging/build-appimage.sh --out DIR           where to write the AppImages (default: dist/)
#
# How: a relocatable CPython from python-build-standalone (built for glibc 2.17), the official manylinux
# wheels of the requirements, the app files listed in its install.sh, and appimagetool.
# Needs: bash, curl, tar, sha256sum and network access. No root.
set -euo pipefail

PY_RELEASE="20261021"          # https://github.com/astral-sh/python-build-standalone/releases
PY_VERSION="3.12.15"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/cpython-${PY_VERSION}+${PY_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
SOURCE="$ROOT"
OUT="$ROOT/dist"
APPS=()
while (( $# )); do
    case "$1" in
        --source) SOURCE="$(cd "$2" && pwd)"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        lectern|ovms-manager) APPS+=("$1"); shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
done
(( ${#APPS[@]} )) || APPS=(ovms-manager lectern)
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="${TMPDIR:-/tmp}/appimage-build"
mkdir -p "$WORK/cache"

fetch() {  # fetch URL FILE: download once into the cache
    [[ -s "$2" ]] || { echo "→ Downloading $(basename "$2")"; curl -fL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"; }
}
fetch "$PY_URL" "$WORK/cache/python-$PY_VERSION.tar.gz"
fetch "$APPIMAGETOOL_URL" "$WORK/cache/appimagetool"
chmod +x "$WORK/cache/appimagetool"

for app in "${APPS[@]}"; do
    case "$app" in
        lectern)      NAME="Lectern";      SCRIPT="lectern.py";      VERSION_FILE="lectern.py";
                      CATEGORIES="Education;"; COMMENT="Lecture notebook with live transcription (Whisper on OpenVINO, fully local)" ;;
        ovms-manager) NAME="OVMS Manager"; SCRIPT="ovms_manager.py"; VERSION_FILE="ovms_manager.py";
                      CATEGORIES="Development;"; COMMENT="Run OpenVINO Model Server and download OpenVINO models from Hugging Face" ;;
    esac
    src="$SOURCE/$app"
    version="$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' "$src/$VERSION_FILE")"
    [[ -n "$version" ]] || { echo "No APP_VERSION in $src/$VERSION_FILE" >&2; exit 1; }
    appdir="$WORK/$app.AppDir"
    echo "=== $NAME $version"
    rm -rf "$appdir"
    mkdir -p "$appdir/usr/app"

    echo "→ Python $PY_VERSION"
    tar -xzf "$WORK/cache/python-$PY_VERSION.tar.gz" -C "$appdir/usr"   # creates usr/python
    py="$appdir/usr/python/bin/python3"

    echo "→ Python packages (manylinux wheels only)"
    eval "$(grep '^CHECK=' "$src/install.sh")"   # the imports the app needs, as checked by its installer
    # PySide6-Essentials has Widgets and Network; QtMultimedia (Lectern's microphone) is in PySide6-Addons, which
    # is installed only when needed and then trimmed below
    if [[ "$CHECK" == *QtMultimedia* ]]; then
        cp "$src/requirements.txt" "$WORK/requirements-$app.txt"
    else
        sed 's/^PySide6>=/PySide6-Essentials>=/' "$src/requirements.txt" > "$WORK/requirements-$app.txt"
    fi
    "$py" -m pip install -q --disable-pip-version-check --no-cache-dir --only-binary=:all: \
        -r "$WORK/requirements-$app.txt"

    echo "→ App files (the same list as install.sh)"
    eval "$(sed -n '/^FILES=(/,/)/p' "$src/install.sh")"
    eval "$(sed -n '/^COMMON_FILES=(/,/)$/p' "$src/install.sh")"
    for f in "${FILES[@]}"; do
        [[ "$f" == *.sh ]] && continue   # installers and launchers are not used inside the AppImage
        install -Dm644 "$src/$f" "$appdir/usr/app/$f"
    done
    for f in "${COMMON_FILES[@]}"; do
        install -Dm644 "$SOURCE/common/$f" "$appdir/usr/app/$f"
    done

    echo "→ Trimming what the apps never use"
    site="$(echo "$appdir"/usr/python/lib/python3.*/site-packages)"
    stdlib="$(dirname "$site")"
    rm -rf "$appdir/usr/python/include" "$appdir/usr/python/share" \
           "$stdlib"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$stdlib"/lib-dynload/_tkinter* \
           "$appdir"/usr/python/lib/{tcl,tk,itcl,thread}* "$appdir"/usr/python/lib/lib{tcl,tk}* \
           "$site"/pip "$site"/pip-* "$appdir"/usr/python/bin/pip* \
           "$site"/PySide6/{designer,assistant,linguist,lupdate,lrelease,uic,rcc,qmlformat,qmllint,qmlls,balsam,balsamui} \
           "$site"/PySide6/{include,typesystems,glue,examples,scripts} "$site"/PySide6/Qt/qml \
           "$site"/numpy/*/tests "$site"/numpy/tests
    find "$site/PySide6/Qt/translations" -name '*.qm' ! -name 'qt_*' ! -name 'qtbase_*' -delete 2>/dev/null || true
    rm -rf "$site/PySide6/Qt/translations/qtwebengine_locales" "$site/PySide6/Qt/resources" "$site/PySide6/Qt/libexec"
    # Qt add-on modules the apps never use (QtWebEngine alone is a whole browser): Python bindings, libraries
    # and plugins
    for module in WebEngine WebView WebChannel WebSockets HttpServer Pdf 3D Quick3D Charts DataVisualization Graphs \
                  Location Positioning Sensors Bluetooth Nfc SerialPort SerialBus RemoteObjects Scxml StateMachine \
                  VirtualKeyboard TextToSpeech SpatialAudio Designer Help UiTools QuickTest Test; do
        rm -f "$site"/PySide6/Qt"$module"*.abi3.so "$site"/PySide6/Qt"$module"*.pyi \
              "$site"/PySide6/Qt/lib/libQt6"$module"*.so*
    done
    rm -rf "$site"/PySide6/Qt/plugins/{webview,sceneparsers,geometryloaders,renderers,renderplugins,assetimporters} \
           "$site"/PySide6/Qt/plugins/{texttospeech,position,geoservices,sensors,canbus,designer,qmltooling} \
           "$site"/PySide6/Qt/plugins/{scxmldatamodel,egldeviceintegrations} \
           "$site"/PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so \
           "$site"/PySide6/Qt/plugins/imageformats/libqpdf.so

    echo "→ Checking the trimmed environment"
    "$py" -c "$CHECK" || { echo "The bundled Python cannot import what $NAME needs." >&2; exit 1; }
    # every Qt library must resolve inside the AppImage: one found on the build host (or not at all) would break
    # on a system without that Qt
    missing=""
    while IFS= read -r -d '' lib; do
        bad="$( { ldd "$lib" 2>/dev/null || true; } | { grep -E 'libQt6|libpyside|libshiboken' || true; } \
               | { grep -v -F "$site" || true; } | awk '{print $1}' | sort -u | tr '\n' ' ')"
        [[ -n "$bad" ]] && missing+="  ${lib#"$site/"}: $bad"$'\n'
    done < <(find "$site/PySide6" -name '*.so*' -type f -print0)
    if [[ -n "$missing" ]]; then
        printf 'Qt libraries that would not be found inside the AppImage:\n%s' "$missing" >&2
        exit 1
    fi

    echo "→ AppDir metadata"
    sed "s|@SCRIPT@|$SCRIPT|" "$ROOT/packaging/AppRun" > "$appdir/AppRun"
    chmod 755 "$appdir/AppRun"
    cp "$src/$app.svg" "$appdir/$app.svg"
    ln -sf "$app.svg" "$appdir/.DirIcon"
    cat > "$appdir/$app.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$NAME
Comment=$COMMENT
Exec=$app
Icon=$app
Terminal=false
Categories=$CATEGORIES
StartupWMClass=$app
DESKTOP

    file_name="${NAME// /-}-$version-x86_64.AppImage"
    echo "→ $file_name"
    rm -f "$OUT/$file_name"
    ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$WORK/cache/appimagetool" --no-appstream -n "$appdir" "$OUT/$file_name" \
        > "$WORK/appimagetool-$app.log" 2>&1 || { cat "$WORK/appimagetool-$app.log" >&2; exit 1; }
    chmod 755 "$OUT/$file_name"
    echo "   $(du -h "$OUT/$file_name" | cut -f1)"
done

( cd "$OUT" && sha256sum ./*.AppImage | sed 's|\./||' > SHA256SUMS )
echo
echo "✓ AppImages in $OUT"
cat "$OUT/SHA256SUMS"
