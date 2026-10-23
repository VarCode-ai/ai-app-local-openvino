#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Transcribe every audio/video file in a folder with Whisper on OpenVINO (interactive front end of transcribe.py).
#
# Usage:
#   transcribe.sh                      # asks for every option step by step
#   transcribe.sh ~/Lectures           # same, with the folder already given
#   transcribe.sh ~/Lectures GPU       # no questions, default settings (GPU | CPU | NPU)
#   transcribe.sh ~/Lectures GPU -c strong -l it --prompt "Calculus: derivatives, integrals" -t
#                                      # from the third argument on: transcribe.py options (-h for the list)
#
# Ctrl+C stops at any time: finished transcripts stay saved and are skipped on the next run,
# so you resume where you stopped.
set -euo pipefail

trap 'echo; echo "⏹  Interrupted. Finished transcripts are saved: run again to resume."; exit 130' INT

# readlink -f follows symlinks: works when started from ~/.local/bin/lectern-transcribe as well
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# Python: the environment created by install.sh if present, otherwise the system one
PYTHON="python3"
VENV_PYTHON="${XDG_DATA_HOME:-$HOME/.local/share}/lectern/venv/bin/python"
[[ -x "$VENV_PYTHON" ]] && PYTHON="$VENV_PYTHON"
MODELS_DIR="${OPENVINO_MODELS_DIR:-$HOME/models}"
EXTENSIONS=(mp3 wav m4a aac flac ogg opus wma webm mp4 mkv mov)

# ask_menu VARIABLE "Question" default "value|description" ...
# Shows a numbered menu and repeats the question until the answer is valid.
ask_menu() {
    local var="$1" question="$2" default="$3"; shift 3
    local options=("$@") i choice
    echo
    echo "$question"
    for i in "${!options[@]}"; do
        printf "  %d) %s\n" "$((i + 1))" "${options[$i]#*|}"
    done
    while true; do
        read -r -p "Choice [$default]: " choice
        choice="${choice:-$default}"
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#options[@]} )); then
            printf -v "$var" '%s' "${options[$((choice - 1))]%%|*}"
            return
        fi
        echo "  Invalid choice, enter a number from 1 to ${#options[@]}."
    done
}

ask_yes_no() {  # ask_yes_no VARIABLE "Question" y|n
    local var="$1" question="$2" default="$3" answer
    local hint="[y/N]"; [[ "$default" == "y" ]] && hint="[Y/n]"
    while true; do
        read -r -p "$question $hint: " answer
        answer="${answer:-$default}"
        case "${answer,,}" in
            y|yes) printf -v "$var" '%s' "y"; return ;;
            n|no)  printf -v "$var" '%s' "n"; return ;;
        esac
        echo "  Please answer y or n."
    done
}

# --- Direct mode: folder and device given on the command line ---
if [[ -n "${2:-}" ]]; then
    DEVICE="${2^^}"
    case "$DEVICE" in
        GPU|CPU|NPU) ;;
        MULTI) echo "MULTI is not supported by Whisper (stateful model): use GPU, CPU or NPU." >&2; exit 1 ;;
        *) echo "Invalid device: $DEVICE (use GPU, CPU or NPU)" >&2; exit 1 ;;
    esac
    DIR="${1/#\~/$HOME}"
    [[ -d "$DIR" ]] || { echo "Folder not found: $DIR" >&2; exit 1; }
    "$PYTHON" "$SCRIPT_DIR/transcribe.py" --input "$DIR" --device "$DEVICE" "${@:3}"
    exit 0
fi

echo "🎙  Whisper transcription · OpenVINO   (Ctrl+C to quit at any time)"

# --- 1. Folder ---------------------------------------------------------------
DIR="${1:-}"
while true; do
    if [[ -z "$DIR" ]]; then
        echo
        read -r -e -p "1. Folder to transcribe [$(pwd)]: " DIR
        DIR="${DIR:-$(pwd)}"
    fi
    DIR="${DIR/#\~/$HOME}"
    [[ -d "$DIR" ]] && break
    echo "  Folder not found: $DIR"
    DIR=""
done
COUNT=0
for f in "$DIR"/*; do
    [[ -f "$f" ]] || continue
    ext="${f##*.}"
    for e in "${EXTENSIONS[@]}"; do
        if [[ "${ext,,}" == "$e" ]]; then COUNT=$((COUNT + 1)); break; fi
    done
done
RECURSIVE="n"
if (( COUNT == 0 )); then
    echo "  No audio/video files directly in $DIR."
    ask_yes_no RECURSIVE "  Look in its subfolders too?" y
    [[ "$RECURSIVE" == "y" ]] || exit 1
else
    echo "  → $COUNT audio/video files found."
fi

# --- 2. Device ---------------------------------------------------------------
ask_menu DEVICE "2. Which device should run Whisper?" 1 \
    "GPU|GPU  - Intel integrated or Arc GPU (recommended, fastest)" \
    "CPU|CPU  - always works, slower" \
    "NPU|NPU  - Intel AI Boost, low power (may not support every model)"

# --- 3. Model ----------------------------------------------------------------
MODELS=()
if [[ -d "$MODELS_DIR" ]]; then
    while IFS= read -r xml; do
        MODELS+=("$(dirname "$xml")")
    done < <(find -L "$MODELS_DIR" -maxdepth 4 -name openvino_encoder_model.xml -ipath '*whisper*' 2>/dev/null | sort)
fi
MODEL_OPTIONS=()
DEFAULT_MODEL=1
for i in "${!MODELS[@]}"; do
    name="$(basename "${MODELS[$i]}")"
    note=""
    case "$name" in
        *large-v3-turbo*) note=" - fast, good accuracy" ;;
        *large-v3*)       note=" - most accurate on hard audio (recommended for lectures)"
                          DEFAULT_MODEL=$((i + 1)) ;;
        *distil*)         note=" - English only" ;;
    esac
    MODEL_OPTIONS+=("${MODELS[$i]}|$name$note")
done
MODEL_OPTIONS+=("OpenVINO/whisper-large-v3-turbo-int8-ov|Download whisper-large-v3-turbo-int8-ov from Hugging Face (~1 GB)")
MODEL=""   # assigned by ask_menu through printf -v
ask_menu MODEL "3. Which Whisper model?" "$DEFAULT_MODEL" "${MODEL_OPTIONS[@]}"

# --- 4. Language -------------------------------------------------------------
echo
read -r -p "4. Audio language as ISO code (en, it, de, fr, es…) [auto-detect]: " LANGUAGE
LANGUAGE="${LANGUAGE:-auto}"

# --- 5. Quality --------------------------------------------------------------
ask_menu QUALITY "5. Transcription quality?" 1 \
    "high|High - beam search 5, more accurate (recommended)" \
    "fast|Fast - about 3-5 times quicker, a few more mistakes"

# --- 6. Audio cleanup --------------------------------------------------------
ask_menu CLEANUP "6. Clean up the audio before transcribing?" 1 \
    "light|Light  - removes hum and hiss, raises distant voices (recommended)" \
    "strong|Strong - also reduces background noise (noisy rooms, fans)" \
    "none|None   - original audio"

# --- 7. Timestamps -----------------------------------------------------------
echo
ask_yes_no TIMESTAMPS "7. Prefix every sentence with its time [hh:mm:ss]?" n

# --- 8. Prompt ---------------------------------------------------------------
echo
echo "8. Context (optional): subject, technical terms, names."
echo "   Helps with difficult words; if the model starts looping the file is redone without it."
read -r -e -p "   Context (Enter for none): " PROMPT

# --- Summary -----------------------------------------------------------------
echo
echo "──────────── Summary ────────────"
echo "Folder:      $DIR ($([[ $RECURSIVE == y ]] && echo "with subfolders" || echo "$COUNT files"))"
echo "Device:      $DEVICE"
echo "Model:       $(basename "$MODEL")"
echo "Language:    $LANGUAGE"
echo "Quality:     $QUALITY"
echo "Cleanup:     $CLEANUP"
echo "Timestamps:  $([[ $TIMESTAMPS == y ]] && echo yes || echo no)"
echo "Context:     ${PROMPT:-none}"
echo "─────────────────────────────────"
ask_yes_no GO "Start transcribing?" y
[[ "$GO" == "y" ]] || { echo "Cancelled."; exit 0; }
echo

ARGS=(--input "$DIR" --device "$DEVICE" --model "$MODEL" --language "$LANGUAGE" --cleanup "$CLEANUP")
[[ "$RECURSIVE" == "y" ]] && ARGS+=(--recursive)
[[ "$QUALITY" == "fast" ]] && ARGS+=(--fast)
[[ "$TIMESTAMPS" == "y" ]] && ARGS+=(--timestamps)
[[ -n "$PROMPT" ]] && ARGS+=(--prompt "$PROMPT")
"$PYTHON" "$SCRIPT_DIR/transcribe.py" "${ARGS[@]}"
