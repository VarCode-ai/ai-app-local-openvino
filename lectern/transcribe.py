#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Transcribe every audio/video file in a folder with Whisper on OpenVINO GenAI.

Uses a Whisper model already converted to OpenVINO IR (for example one downloaded with OVMS Manager or
straight from https://huggingface.co/OpenVINO), without PyTorch/transformers/optimum and without
re-converting it at every run:

    python transcribe.py --input /path/to/folder --device GPU

Dependencies: pip install openvino-genai numpy huggingface_hub  ·  ffmpeg installed on the system.
This module is also imported by lectern.py (shared paths, model lookup and text filters).
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time

# No usage statistics from the libraries either: Hugging Face downloads honor these switches (also for lectern.py)
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

# ================= DEFAULTS =================
# Folder holding the OpenVINO models (shared with OVMS Manager). Override with $OPENVINO_MODELS_DIR.
MODELS_DIR = os.path.abspath(os.path.expanduser(os.environ.get("OPENVINO_MODELS_DIR") or "~/models"))
# Whisper models looked for when --model is not given, in order of preference: large-v3 is the most
# accurate on hard audio (lectures, noise), turbo is much faster and only slightly less accurate.
# Hugging Face repo ids: used from MODELS_DIR/<repo id> if present, otherwise downloaded there.
MODEL_CANDIDATES = (
    "OpenVINO/whisper-large-v3-int8-ov",
    "OpenVINO/whisper-large-v3-turbo-int8-ov",
)
# Name of the output folder, created inside the folder being transcribed
OUTPUT_DIRNAME = "transcripts"
# ============================================

DEVICES = ("GPU", "CPU", "NPU")
# MULTI (GPU+CPU) is not available: the Whisper decoder is a "stateful" model and OpenVINO does not
# support it in cumulative MULTI/AUTO mode ("AUTO cumulative model doesn't support stateful model")

# Accepted audio/video extensions
EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".webm", ".mp4", ".mkv", ".mov")

SAMPLE_RATE = 16000  # Whisper works on 16 kHz mono audio

# ffmpeg filters applied before transcription. Whisper is already robust to noise: overly aggressive
# filters can make the result worse, which is why the default is "light".
CLEANUP_FILTERS = {
    "none": None,
    # cuts rumble/hum below 80 Hz and hiss above 8 kHz, raises the voice when the speaker moves
    # away from the microphone (dynamic normalization)
    "light": "highpass=f=80,lowpass=f=8000,dynaudnorm=f=250:g=15",
    # additionally: reduction of constant background noise (fans, projector, chatter)
    "strong": "highpass=f=80,lowpass=f=8000,afftdn=nf=-25,dynaudnorm=f=250:g=15",
}

# Phrases Whisper "hallucinates" on silence and noise (learned from YouTube subtitles). Short segments
# matching these are dropped. Add patterns for your language if you see recurring ones.
HALLUCINATIONS = re.compile(
    r"sottotitoli (creati|a cura|e revisione)|amara\.org|qtss|grazie (a tutti )?per (la visione|l'ascolto)"
    r"|iscriviti al canale|sottotitoli"
    r"|thanks? (you )?(so much )?for watching|subtitles by|please subscribe|like and subscribe"
    r"|untertitel (der|von|im auftrag)|sous-titrage|sous-titres réalisés", re.IGNORECASE)
# The same phrase repeated 3+ times in a row: typical Whisper loop on noisy audio
REPEATED = re.compile(r"(\b.{4,80}?)(?:[\s,.]*\1){2,}", re.IGNORECASE)

LANGUAGE_NAMES = {
    "english": "en", "italian": "it", "italiano": "it", "french": "fr", "français": "fr", "german": "de",
    "deutsch": "de", "spanish": "es", "español": "es", "portuguese": "pt", "português": "pt", "dutch": "nl",
}
FFMPEG_COMMANDS = ("sudo apt install ffmpeg  ·  sudo dnf install ffmpeg-free  ·  sudo pacman -S ffmpeg  ·  "
                   "sudo zypper install ffmpeg")
FFMPEG_HINT = f"install it with your package manager, e.g.  {FFMPEG_COMMANDS}"


def whisper_language(value):
    """'italian' / 'it' / '<|it|>' -> '<|it|>' (the format WhisperPipeline expects); '' / 'auto' -> None."""
    value = (value or "").strip().lower()
    if value in ("", "auto"):
        return None
    if value.startswith("<|"):
        return value
    return f"<|{LANGUAGE_NAMES.get(value, value)}|>"


def find_whisper_models(base=MODELS_DIR, max_depth=4):
    """Folders under `base` that contain an OpenVINO Whisper model, sorted by path."""
    found, depth0 = [], base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base, followlinks=True):
        dirs.sort()
        if "openvino_encoder_model.xml" in files and "whisper" in root.lower():
            found.append(root)
            dirs[:] = []
        elif root.count(os.sep) - depth0 >= max_depth:
            dirs[:] = []
    return found


def default_model():
    """First preferred model available locally, then any local Whisper, then the turbo repo (downloaded)."""
    for candidate in MODEL_CANDIDATES:
        if os.path.isdir(os.path.join(MODELS_DIR, candidate)):
            return candidate
    local = find_whisper_models()
    return local[0] if local else MODEL_CANDIDATES[-1]


def resolve_model(model):
    """Return the model folder; if `model` is a Hugging Face repo id, download it into MODELS_DIR."""
    path = os.path.abspath(os.path.expanduser(model))
    if os.path.isdir(path):
        return path
    if model.count("/") != 1 or model.startswith((".", "~", "/")):
        sys.exit(f"Model not found: {path}\n"
                 "Pass a local folder or a Hugging Face repo id such as OpenVINO/whisper-large-v3-turbo-int8-ov.")
    target = os.path.join(MODELS_DIR, model)
    if os.path.isdir(target):
        return target
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit(f"Downloading {model} requires huggingface_hub:  pip install huggingface_hub\n"
                 "Alternatively download it with OVMS Manager and pass the local folder.")
    print(f"Downloading {model} into {target}…")
    return snapshot_download(model, local_dir=target)


def load_audio(path, audio_filter=None):
    """Decode any audio/video format with ffmpeg into float32 mono 16 kHz."""
    import numpy as np
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-vn"]
    if audio_filter:
        cmd += ["-af", audio_filter]
    cmd += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode(errors="replace").strip() or f"ffmpeg exit code {res.returncode}")
    return np.frombuffer(res.stdout, dtype=np.float32)


def clean_segments(chunks):
    """Drop hallucinated phrases and repetitions. Return (segments, characters removed by loops)."""
    segments, removed, previous = [], 0, None
    for start, end, text in chunks:
        text = text.strip()
        collapsed = REPEATED.sub(r"\1", text)
        removed += len(text) - len(collapsed)
        text = collapsed.strip()
        if not text or (len(text.split()) <= 12 and HALLUCINATIONS.search(text)):
            continue
        if previous and text.lower() == previous.lower():
            continue  # same as the previous segment
        segments.append((start, end, text))
        previous = text
    return segments, removed


def timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def format_text(segments, with_timestamps):
    """Readable text: a new paragraph after every long pause, or one line per segment with its time."""
    if with_timestamps:
        return "\n".join(f"[{timestamp(start)}] {text}" for start, _, text in segments)
    paragraphs, current, last_end = [], [], None
    for start, end, text in segments:
        if current and last_end is not None and start - last_end > 2.0:
            paragraphs.append(" ".join(current))
            current = []
        current.append(text)
        last_end = end
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def find_audio_files(input_dir, output_dir, recursive):
    """Audio/video files in `input_dir` (and its subfolders if `recursive`, skipping `output_dir`)."""
    found = []
    for root, dirs, files in os.walk(input_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and os.path.join(root, d) != output_dir) if recursive else []
        found += [os.path.join(root, f) for f in files if f.lower().endswith(EXTENSIONS)]
    return sorted(found)


def main():
    # Line-buffered output: progress shows up immediately even with `| tee log.txt`
    sys.stdout.reconfigure(line_buffering=True)
    # Ctrl+C must stop right away: transcription runs in native code and Python's regular
    # KeyboardInterrupt would only arrive at the end of the file (possibly minutes later)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    parser = argparse.ArgumentParser(description="Batch transcription with Whisper on OpenVINO GenAI. "
                                                 "Files already transcribed are skipped, so an interrupted "
                                                 "run resumes where it stopped.")
    parser.add_argument("--input", "-i", default=".", help="folder with the files to transcribe (default: current)")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="also transcribe files in subfolders (the output mirrors the folder structure)")
    parser.add_argument("--device", "-d", default="GPU", type=str.upper, choices=DEVICES,
                        help="OpenVINO device (default: GPU, falls back to CPU if the GPU is not usable)")
    parser.add_argument("--output", "-o", help=f"folder for the .txt files (default: <input>/{OUTPUT_DIRNAME})")
    parser.add_argument("--model", "-m", default=None,
                        help="OpenVINO model folder or Hugging Face repo id (default: whisper-large-v3 if "
                             f"present in {MODELS_DIR}, otherwise large-v3-turbo, downloaded if missing)")
    parser.add_argument("--language", "-l", default="auto",
                        help="audio language: ISO code (en, it, de…), name, or 'auto' to detect it (default)")
    parser.add_argument("--cleanup", "-c", default="light", choices=CLEANUP_FILTERS,
                        help="audio filters applied before transcription (default: light)")
    parser.add_argument("--beam", "-b", type=int, default=5,
                        help="beam search width: higher = more accurate but slower (default: 5)")
    parser.add_argument("--fast", action="store_true", help="greedy decoding (--beam 1): 3-5x faster")
    parser.add_argument("--prompt", default="",
                        help="context (subject, technical terms, names). Experimental: if the model starts "
                             "looping the file is transcribed again without the prompt")
    parser.add_argument("--timestamps", "-t", action="store_true",
                        help="one line per sentence prefixed with [hh:mm:ss], handy to find passages again")
    args = parser.parse_args()
    beams = 1 if args.fast else max(1, args.beam)

    input_dir = os.path.abspath(os.path.expanduser(args.input))
    if not os.path.isdir(input_dir):
        sys.exit(f"Folder not found: {input_dir}")
    output_dir = os.path.abspath(os.path.expanduser(args.output or os.path.join(input_dir, OUTPUT_DIRNAME)))

    audio_files = find_audio_files(input_dir, output_dir, args.recursive)
    if not audio_files:
        sys.exit(f"No audio/video files found in {input_dir}" + ("" if args.recursive else
                                                                 " (use --recursive to include subfolders)."))
    if not shutil.which("ffmpeg"):
        sys.exit(f"ffmpeg not found: {FFMPEG_HINT}.")

    print(f"Found {len(audio_files)} files to process in {input_dir}.")
    print(f"Transcripts will be saved in {output_dir}\n")
    os.makedirs(output_dir, exist_ok=True)

    # Heavy import only after the checks, so mistakes in the arguments are reported right away
    try:
        import openvino_genai
    except ImportError:
        sys.exit("openvino-genai is missing:  pip install openvino-genai numpy")

    model_dir = resolve_model(args.model or default_model())
    device = args.device
    print(f"Loading {model_dir} on {device}…")
    load_start = time.time()
    try:
        pipe = openvino_genai.WhisperPipeline(model_dir, device)
    except Exception as e:  # noqa: BLE001 - any compilation error deserves a clear message
        if device == "CPU":
            sys.exit(f"Cannot load the model on CPU: {e}")
        print(f"⚠ Cannot load the model on {device} ({str(e).strip().splitlines()[-1][:160]}): using the CPU.")
        device = "CPU"
        pipe = openvino_genai.WhisperPipeline(model_dir, device)

    # Settings from the most accurate to the simplest. Some model/device combinations do not support
    # beam search or timestamps (e.g. on GPU: "Logits batch size doesn't match the number of beams",
    # "iremote_tensor ... Not Implemented"): in that case we step down a level, and the level that works
    # is kept for all the following files.
    levels = list(dict.fromkeys(((beams, True), (1, True), (1, False))))
    state = {"level": 0}
    language = whisper_language(args.language)

    def make_config(prompt, num_beams, timestamps):
        config = pipe.get_generation_config()
        if language:
            config.language = language
        config.task = "transcribe"
        config.return_timestamps = timestamps  # needed to filter segments and to build paragraphs
        config.num_beams = num_beams
        if prompt:
            config.initial_prompt = prompt
        return config

    def transcribe(audio, prompt):
        while True:
            num_beams, timestamps = levels[state["level"]]
            try:
                result = pipe.generate(audio, make_config(prompt, num_beams, timestamps))
            except RuntimeError as e:
                if state["level"] == len(levels) - 1:
                    raise
                reason = str(e).strip().splitlines()[-1][:120]
                state["level"] += 1
                next_beams, next_ts = levels[state["level"]]
                change = (f"beam {num_beams} → {next_beams}" if next_beams != num_beams
                          else "without timestamps (no paragraphs or times)")
                print(f"   ⚠ Setting not supported on {device} ({reason}): retrying {change}, "
                      "and keeping it for the next files.")
                if not next_ts and args.timestamps:
                    print("   ⚠ [hh:mm:ss] times are not available with this model/device.")
                continue
            chunks = [(c.start_ts, c.end_ts, c.text) for c in (result.chunks or [])]
            return chunks or [(0.0, 0.0, "".join(result.texts))]

    audio_filter = CLEANUP_FILTERS[args.cleanup]
    print(f"Model ready in {time.time() - load_start:.1f} s · beam {beams} · audio cleanup: {args.cleanup}"
          + (" · with prompt" if args.prompt else "") + "\n")

    total_start = time.time()
    for n, file_path in enumerate(audio_files, 1):
        rel = os.path.relpath(os.path.splitext(file_path)[0], input_dir)
        out_txt_path = os.path.join(output_dir, rel + ".txt")

        # Skip files transcribed by a previous run
        if os.path.exists(out_txt_path):
            print(f"[{n}/{len(audio_files)}] Skipped (already done): {file_path}")
            continue

        print(f"[{n}/{len(audio_files)}] Transcribing: {file_path}")
        start_time = time.time()
        try:
            try:
                audio = load_audio(file_path, audio_filter)
            except RuntimeError as e:
                if not audio_filter:
                    raise
                print(f"   ⚠ Audio filters not applicable ({e}): transcribing the original audio.")
                audio = load_audio(file_path)
            chunks = transcribe(audio, args.prompt)
            segments, removed = clean_segments(chunks)
            total = sum(len(c[2]) for c in chunks)
            if args.prompt and total and removed / total > 0.3:
                # the prompt sent the model into a loop: try again without it
                print("   ⚠ The model looped with the prompt: transcribing again without it.")
                segments, removed = clean_segments(transcribe(audio, ""))
        except Exception as e:  # noqa: BLE001 - one broken file must not stop the whole batch
            print(f"   ✗ Error, file skipped: {e}\n")
            continue

        if removed:
            print(f"   Removed abnormal repetitions ({removed} characters).")
        # times only if the current level really produces timestamps (otherwise they would all be 00:00:00)
        text = format_text(segments, args.timestamps and levels[state["level"]][1])
        # atomic write: an interruption never leaves a half-written .txt (it would be skipped next time)
        os.makedirs(os.path.dirname(out_txt_path), exist_ok=True)
        tmp_path = out_txt_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp_path, out_txt_path)

        elapsed = time.time() - start_time
        duration = len(audio) / SAMPLE_RATE
        speed = f", {duration / elapsed:.1f}x real time" if elapsed > 0 else ""
        print(f"   Done in {elapsed:.1f} s ({duration / 60:.1f} min of audio{speed}). Saved to: {out_txt_path}\n")

    print(f"All files processed in {time.time() - total_start:.0f} seconds.")


# Required: openvino-genai uses multiprocessing and, with the "forkserver" start method (the default
# since Python 3.14), the child process re-imports this file. Without the guard the whole batch would restart.
if __name__ == "__main__":
    main()
