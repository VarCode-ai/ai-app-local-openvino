#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lectern — a lecture notebook that records from the microphone and transcribes in real time
with Whisper on OpenVINO GenAI, entirely on your own computer.

Install:        ./install.sh   → "Lectern" entry in the application menu
From a terminal: ./lectern.sh

Notes are plain text files, organized in folders that are readable from any file manager too:
    Documents/Lecture Notes/<Course>/Lecture 03 — 2026-10-01 — <Topic>/01 — Explanation.txt
Each lecture can hold several transcripts, each with a category (Explanation, Exercises, Questions…).
Folder names, categories and the interface follow the interface language (English or Italian).

How it works: microphone audio goes into a buffer and Silero VAD (run with OpenVINO on the CPU) marks,
every 32 ms, where there is speech. A separate thread transcribes: while you speak it shows a preview
(in grey) and at every pause it transcribes the sentence for good, saving it to file right away.
"From file…" transcribes already recorded audio the way transcribe.py does (ffmpeg cleanup and
Whisper long-form transcription).
"""
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
import zlib
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler

import numpy as np
from PySide6.QtCore import (QDate, QEvent, QFile, QLibraryInfo, QLocale, QRectF, QSettings, QSize,
                            QStandardPaths, Qt, QThread, QTimer, QTranslator, QUrl, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter,
                           QPalette, QTextBlockFormat, QTextCharFormat, QTextCursor)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QStyle,
                               QStyledItemDelegate, QTabBar, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget, QWidgetAction)

_HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(_HERE, "ui_kit.py")):  # running from the project: the kit is in ../common
    sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "common"))
import ui_kit as kit  # noqa: E402  (shared UI kit of the ecosystem: themes, fonts, icons, command palette)

# Shared with the batch transcriber: models folder, Whisper lookup and anti-hallucination filters
from transcribe import (CLEANUP_FILTERS, FFMPEG_COMMANDS, HALLUCINATIONS, MODELS_DIR, REPEATED, clean_segments,
                        find_whisper_models, timestamp, whisper_language)

THEME = None   # kit.ThemeManager, created by init_theme()

APP_NAME = "Lectern"
APP_ID = "lectern"     # must match the .desktop file created by install.sh
APP_VERSION = "1.0.0"    # also read by install.sh to confirm what it installed
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), APP_ID)

# ================= TRANSLATIONS =================
# User-visible text is written in English and translated through locale/<language>.json, a plain
# {"English text": "translation"} map. N_() only marks text defined before the language is known; it is
# translated with tr() where it is shown. tools/check_translations.py checks that the catalogs are complete.
def N_(text):
    return text

_catalog = {}

def tr(text):
    return _catalog.get(text, text)

def all_translations(text):
    """`text` and its translation in every available catalog (to find folders named in another language)."""
    names = [text]
    for path in sorted(glob.glob(os.path.join(APP_DIR, "locale", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                name = json.load(f).get(text)
        except (OSError, ValueError):
            continue
        if name and name not in names:
            names.append(name)
    return names

def setup_language(app, choice):
    """Load the catalog for `choice` ('' = system language) and Qt's own translations (standard buttons and
    dialogs). Return the language code in use."""
    global _catalog, LOCALE
    lang = choice or QLocale.system().name().split("_")[0]
    try:
        with open(os.path.join(APP_DIR, "locale", f"{lang}.json"), encoding="utf-8") as f:
            _catalog = json.load(f)
    except (OSError, ValueError):
        _catalog, lang = {}, "en"
    LOCALE = QLocale(lang) if choice else QLocale.system()
    QLocale.setDefault(LOCALE)
    qt = QTranslator(app)
    if lang != "en" and qt.load(f"qtbase_{lang}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt)
    return lang

UI_LANGUAGES = [("", N_("System language")), ("en", "English"), ("it", "Italiano")]
LOCALE = QLocale()  # dates in the interface language; set by setup_language()
# ================================================

# ================= LOOK =================
# Colors come from the shared kit (warm neutrals, light and dark): C always holds the tokens of the current theme
ACCENT = "ember"   # Lectern's accent: warm terracotta
C = kit.T
THEMES = [("system", N_("System")), ("light", N_("Light")), ("dark", N_("Dark"))]
READING_SIZES = [(14, N_("Small")), (16, N_("Normal")), (18, N_("Large"))]
# each typeface is shown in the menu with its own font, so the difference is visible at a glance
READING_FONTS = [("sans", N_("Modern")), ("serif", N_("Book"))]
# Suggested transcript categories (any other can be typed in), with the color of their tab
CATEGORIES = {
    N_("Explanation"): "#4a6f8a", N_("Exercises"): "#4d7c3a", N_("Examples"): "#7a5a9a",
    N_("Definitions and theorems"): "#9b3d2f", N_("Questions"): "#b9771b", N_("Summary"): "#2f6f6a",
    N_("Announcements and exams"): "#a8552b", N_("Other"): "#8b7e6c",
}
DEFAULT_CATEGORY = N_("Explanation")
OTHER_CATEGORY = N_("Other")
PALETTE = ("#9b3d2f", "#4a6f8a", "#4d7c3a", "#7a5a9a", "#b9771b", "#2f6f6a", "#a8552b", "#6b5b4b")
NARROW_W = 900    # window narrower than this: the sidebar hides (reopen it with ☰)
COMPACT_W = 860   # main area narrower than this: compact bars, shorter labels
NOTEBOOK_DIRNAME = N_("Lecture Notes")
COURSE_META = ".course.json"    # in the course folder: {"context": terms for Whisper, "audio": folder}
REGISTRY = ".courses.json"      # in the default folder: courses saved in other folders {"external": [...]}
LESSON_WORD = N_("Lecture")
# "<word> NN — yyyy-mm-dd[ — topic]": any word is accepted, so folders created in another language still parse
LESSON_RE = re.compile(r"^(\w+)\s+(\d+)\s+—\s+(\d{4}-\d{2}-\d{2})(?:\s+—\s+(.+))?$")
NOTE_RE = re.compile(r"^(\d+)\s+—\s+([^—]+?)(?:\s+—\s+(.+))?$")
# ========================================

# ================= PARAMETERS =================
SR = 16000                 # Whisper works on 16 kHz mono audio
FRAME = 512                # 32 ms: Silero VAD window at 16 kHz, unit of every cut
FPS = SR / FRAME           # frames per second (31.25)
MAX_SEGMENT_S = 10         # beyond this length the segment is closed anyway: on long segments
                           # Whisper tends to skip parts of sentences
SOFT_SEGMENT_S = 6         # from here on a short pause is enough to close the segment...
SHORT_PAUSE_S = 0.25       # ...as long as this (people speaking continuously only pause like this)
MIN_SEGMENT_S = 1.5        # shorter segments wait for the next sentence (Whisper gets fragments wrong)
LONG_PAUSE_S = 1.5         # ...unless the pause is long: then even an isolated "yes" is transcribed
MIN_CUT_S = 4              # a forced cut looks for a pause no earlier than this
PARTIAL_STEP_S = 0.5       # the preview is redone only if at least this much new audio arrived
PRE_ROLL_S = 0.4           # audio kept before speech starts (don't cut the first syllable)
TAIL_S = 0.3               # audio kept after speech ends
MIN_SPEECH_FRAMES = 4      # less than ~130 ms of speech = isolated noise
MIC_STALL_S = 2.0          # no data from the microphone for this long = stalled stream, reopen it
VAD_RESET_S = 30           # after this much continuous silence Silero's internal state is reset
PARAGRAPH_GAP_S = 4.0      # pause after which a new paragraph starts
LOG_PATH = os.path.join(CACHE_DIR, "lectern.log")  # diagnostics: timings and decisions of every step
PARTIAL_BUDGET_S = 3.0     # a preview taking longer than this is interrupted: it must not block the rest
OV_CACHE = os.path.join(CACHE_DIR, "ov_cache")  # GPU kernel cache: loading is faster from the 2nd start
# Silero VAD variant published specifically for OpenVINO (MIT license, ~1.3 MB, downloaded on first use)
SILERO_URL = ("https://raw.githubusercontent.com/snakers4/silero-vad/master/"
              "src/silero_vad/data/silero_vad_openvino_16k.onnx")
SILERO_PATH = os.path.join(CACHE_DIR, "silero_vad_openvino_16k.onnx")
# ==============================================

# Whisper languages, with their own names; labels in N_() are translated where they are shown
LANGUAGES = [("English", "en"), ("Italiano", "it"), ("Français", "fr"), ("Deutsch", "de"),
             ("Español", "es"), ("Português", "pt"), (N_("Auto-detect"), "")]
DEVICES = [(N_("GPU — Intel integrated or Arc"), "GPU"), ("CPU", "CPU"),
           (N_("NPU — Intel AI Boost (experimental)"), "NPU")]
PRECISIONS = [(N_("Fast (greedy) — recommended live"), 1), (N_("High (beam 5) — more delay"), 5)]
# (label, probability threshold for Silero, noise factor for the fallback VAD)
SENSITIVITIES = [(N_("High — quiet or distant voice"), (0.35, 2.0)), (N_("Medium"), (0.5, 3.0)),
                 (N_("Low — noisy room"), (0.65, 5.0))]

os.makedirs(CACHE_DIR, exist_ok=True)
log = logging.getLogger(APP_ID)
if not log.handlers:
    _h = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=1, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

def first_line(err, limit=140):
    """Last line of an error message (native OpenVINO errors are long), or the exception type."""
    text = str(err).strip()
    return text.splitlines()[-1][:limit] if text else type(err).__name__

def ffmpeg_hint():
    return tr("install it with your package manager, e.g. {commands}").format(commands=FFMPEG_COMMANDS)

def is_looping(text):
    """True if the tail of the generated text is a repetition (Whisper looping on noise or silence)."""
    tail = text[-300:]
    return len(tail) >= 120 and len(REPEATED.sub(r"\1", tail)) < 0.6 * len(tail)

def prompt_echo(text, prompt):
    """True if the text is little more than a copy of the prompt: sometimes Whisper, especially with beam
    search, "transcribes" the course terms instead of the audio."""
    words = re.findall(r"\w{3,}", text.lower())
    known = set(re.findall(r"\w{3,}", prompt.lower()))
    return bool(words and known) and sum(w in known for w in words) >= 0.7 * len(words)

def too_short(text, voiced_s, preview=0):
    """True if the text is too short for the speech it contains (less than 3 characters per second, against
    12-15 for normal speech) or for the preview of the same audio: the model skipped part of it."""
    return voiced_s >= 3 and (len(text) < 3 * voiced_s or len(text) < 0.5 * preview)

def clean_text(text):
    """Remove loops and the phrases Whisper invents on silence ("Subtitles by…")."""
    text = REPEATED.sub(r"\1", text.strip()).strip()
    if not text or (len(text.split()) <= 12 and HALLUCINATIONS.search(text)):
        return ""
    return text

def to_float32(raw, fmt):
    """Microphone bytes -> float32 mono in [-1, 1], whatever format the system granted."""
    sf = fmt.sampleFormat()
    if sf == QAudioFormat.SampleFormat.Int16:
        x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    elif sf == QAudioFormat.SampleFormat.Int32:
        x = np.frombuffer(raw, np.int32).astype(np.float32) / 2 ** 31
    elif sf == QAudioFormat.SampleFormat.Float:
        x = np.frombuffer(raw, np.float32).copy()
    elif sf == QAudioFormat.SampleFormat.UInt8:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    else:
        return np.zeros(0, np.float32)
    ch = fmt.channelCount()
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
    return x

class Resampler:
    """Block-wise linear resampling, without jumps between one block and the next.
    Only needed if the microphone does not accept 16 kHz directly (with PipeWire it usually does)."""

    def __init__(self, src_rate):
        self.step = src_rate / SR
        self.pos = 0.0
        self.prev = np.zeros(0, np.float32)

    def __call__(self, x):
        if self.step == 1 or not len(x):
            return x
        buf = np.concatenate([self.prev, x])
        if len(buf) - 1 < self.pos:
            self.prev = buf
            return np.zeros(0, np.float32)
        n = int((len(buf) - 1 - self.pos) / self.step) + 1
        out = np.interp(self.pos + np.arange(n) * self.step, np.arange(len(buf)), buf)
        nxt = self.pos + n * self.step
        keep = int(nxt)
        self.prev, self.pos = buf[keep:], nxt - keep
        return out.astype(np.float32)

# ---------------------------------------------------------------------------
# Voice activity detection
# ---------------------------------------------------------------------------
class SileroVAD:
    """Speech probability for every 32 ms frame. Detects quiet, distant voices and works with automatic
    microphone gain, where a volume threshold gets it wrong (and loses words)."""
    name = "Silero VAD"
    CONTEXT = 64  # samples of the previous frame the model wants in front of every frame

    def __init__(self):
        import openvino as ov
        if not os.path.exists(SILERO_PATH):
            import requests
            r = requests.get(SILERO_URL, timeout=20)
            r.raise_for_status()
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(SILERO_PATH + ".tmp", "wb") as f:
                f.write(r.content)
            os.replace(SILERO_PATH + ".tmp", SILERO_PATH)
        model = ov.Core().compile_model(SILERO_PATH, "CPU")
        self.req = model.create_infer_request()
        self.out_prob, self.out_state = model.outputs[0], model.outputs[1]
        self.reset()

    def reset(self):
        self.reset_state()
        self.pending = np.zeros(0, np.float32)

    def reset_state(self):
        """Reset only the model memory: `pending` stays, so audio and probabilities stay aligned."""
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context = np.zeros((1, self.CONTEXT), np.float32)

    def __call__(self, x):
        buf = np.concatenate([self.pending, x])
        n = len(buf) // FRAME
        self.pending = buf[n * FRAME:]
        probs = np.empty(n, np.float32)
        for i in range(n):
            inp = np.concatenate([self.context, buf[None, i * FRAME:(i + 1) * FRAME]], axis=1)
            res = self.req.infer({"input": inp, "state": self.state})
            probs[i] = float(np.ravel(res[self.out_prob])[0])
            self.state = res[self.out_state]
            self.context = inp[:, -self.CONTEXT:]
        return probs

class EnergyVAD:
    """Fallback when Silero is not available: volume threshold relative to the estimated background noise.
    Returns a pseudo-probability that is exactly 0.5 at the threshold."""
    name = N_("volume-threshold VAD")

    def __init__(self, factor):
        self.factor = factor
        self.reset()

    def reset(self):
        self.noise = 0.002
        self.pending = np.zeros(0, np.float32)

    def reset_state(self):
        pass

    def __call__(self, x):
        buf = np.concatenate([self.pending, x])
        n = len(buf) // FRAME
        self.pending = buf[n * FRAME:]
        rms = np.sqrt(np.mean(buf[: n * FRAME].reshape(n, FRAME) ** 2, axis=1) + 1e-12) if n else np.zeros(0)
        probs = np.empty(n, np.float32)
        for i, r in enumerate(rms):
            self.noise = 0.8 * self.noise + 0.2 * r if r < self.noise else self.noise * 1.002
            self.noise = max(self.noise, 1e-4)
            probs[i] = min(1.0, 0.5 * r / max(self.noise * self.factor, 0.004))
        return probs

def speech_mask(probs, threshold):
    """Hysteresis: speech starts above the threshold and continues until it drops well below it,
    so weak consonants in the middle of a word don't break the sentence."""
    off = max(0.05, threshold - 0.15)
    mask = np.zeros(len(probs), bool)
    active = False
    for i, p in enumerate(probs):
        active = p >= threshold or (active and p >= off)
        mask[i] = active
    return mask

class AudioBuffer:
    """Audio not yet transcribed for good, with the speech probability of every frame.
    `offset` = position (in samples) of the first sample relative to the start of the recording."""

    def __init__(self):
        self.lock = threading.Lock()
        self.chunks, self.pchunks = [], []
        self.offset = 0
        self.new = threading.Event()

    def append(self, x, probs):
        with self.lock:
            self.chunks.append(x)
            self.pchunks.append(probs)
        self.new.set()

    def _consolidate(self):
        if len(self.chunks) != 1:
            self.chunks = [np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)]
        if len(self.pchunks) != 1:
            self.pchunks = [np.concatenate(self.pchunks) if self.pchunks else np.zeros(0, np.float32)]

    def snapshot(self):
        with self.lock:
            self._consolidate()
            return self.offset, self.chunks[0], self.pchunks[0]

    def drop_frames(self, k):
        """Drop the first k frames (always whole frames: audio and probabilities stay aligned)."""
        with self.lock:
            self._consolidate()
            k = min(k, len(self.pchunks[0]))
            self.chunks = [self.chunks[0][k * FRAME:]]
            self.pchunks = [self.pchunks[0][k:]]
            self.offset += k * FRAME

# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
class Transcriber(QThread):
    realtime = True                      # False for files: no warnings about lagging behind the speaker
    status = Signal(str, str, bool)      # text, color token ("ok", "warn", "accent"…), pulsing
    device_changed = Signal(str)         # the device actually in use (after a fallback to the CPU)
    partial = Signal(str)
    final = Signal(str, float, float)    # text, start and end in seconds from the beginning
    info = Signal(str)
    failed = Signal(str)
    pipe_ready = Signal(object, object)  # key (model, device), pipeline

    def __init__(self, buf, opts, cached=None, parent=None):
        super().__init__(parent)
        self.buf, self.o, self.cached = buf, opts, cached
        self.finishing = False
        self.abort = False
        self.final_beams = opts["beams"]
        self.next_partial = 0.0
        self.slow_count = 0
        self.errors = 0
        self.recoveries = []  # times of recent recoveries: if there are too many, switch to the CPU
        self.pipe = None
        self.busy = None  # (kind, start) while the model works: read by the UI for diagnostics
        self.stop_value = True

    # called from the UI thread
    def finish(self):
        """Stop waiting for new audio: transcribe what is left and exit."""
        self.finishing = True
        self.buf.new.set()

    def cancel(self):
        self.abort = True
        self.buf.new.set()

    # --- model -------------------------------------------------------------------
    def load(self, force=False):
        key = (self.o["model"], self.o["device"])
        if not force and self.cached and self.cached[0] == key:
            return self.cached[1]
        try:
            import openvino_genai
        except ImportError:
            raise RuntimeError(tr("openvino-genai is missing: pip install openvino-genai")) from None
        name = os.path.basename(self.o["model"])
        self.status.emit(tr("Loading {name}…").format(name=name), "warn", True)
        self.info.emit(tr("Compiling {name} on {device}: the first start can take up to a minute. "
                          "You can already speak, the audio is kept aside.").format(name=name, device=self.o["device"]))
        t0 = time.time()
        props = {"CACHE_DIR": OV_CACHE} if self.o["device"] == "GPU" else {}
        try:
            pipe = openvino_genai.WhisperPipeline(self.o["model"], self.o["device"], **props)
        except Exception:  # noqa: BLE001 - retry without the cache before giving up
            if not props:
                raise
            pipe = openvino_genai.WhisperPipeline(self.o["model"], self.o["device"])
        self.info.emit(tr("Model ready in {seconds} s.").format(seconds=f"{time.time() - t0:.1f}"))
        log.info("model loaded in %.1f s", time.time() - t0)
        self.pipe_ready.emit(key, pipe)
        return pipe

    def start_pipeline(self):
        """Load the model; if the chosen accelerator cannot be used (no Intel GPU, missing driver…) fall back
        to the CPU. Return False, after emitting `failed`, if not even the CPU works."""
        try:
            self.pipe = self.load()
            return True
        except Exception as e:  # noqa: BLE001
            log.exception("load failed on %s", self.o["device"])
            first = e
        device = self.o["device"]
        if device != "CPU":
            self.o["device"] = "CPU"
            try:
                self.pipe = self.load()
                self.device_changed.emit("CPU")
                self.info.emit(tr("⚠ Cannot use the {device} ({reason}): transcribing on the CPU, which is "
                                  "slower.").format(device=device, reason=first_line(first)))
                return True
            except Exception:  # noqa: BLE001
                log.exception("load failed on the CPU too")
        self.failed.emit(tr("Cannot load the model on {device}: {error}").format(device=device, error=first))
        return False

    def _stop_value(self):
        """Value the streaming callback returns to stop generation."""
        try:
            import openvino_genai
            return openvino_genai.StreamingStatus.STOP
        except (ImportError, AttributeError):
            return True  # older openvino-genai versions

    def decode(self, audio, beams, kind="sentence", budget=None, prompt=None):
        """Like _decode_once, but if the device fails (e.g. GPU: "could not execute a primitive") it
        reloads the model and tries again, instead of stopping the recording."""
        try:
            return self._decode_once(audio, beams, kind, budget, prompt)
        except Exception as e:  # noqa: BLE001 - any native OpenVINO error
            if self.abort:
                raise
            self.recover(e)
            try:
                return self._decode_once(audio, beams, kind, budget, prompt)
            except Exception as e2:  # noqa: BLE001 - failed again: the second recovery moves to the CPU
                if self.abort or self.o["device"] == "CPU":
                    raise
                self.recover(e2)
                return self._decode_once(audio, beams, kind, budget, prompt)

    def recover(self, err):
        """Throw away the broken pipeline and create a new one. On the second failure within 5 minutes, or if
        the GPU does not reload, continue on the CPU: slower, but the lecture is not interrupted."""
        now = time.time()
        self.recoveries = [t for t in self.recoveries if now - t < 300] + [now]
        reason = first_line(err)
        log.error("model error on %s (%s): recovery #%d", self.o["device"], reason, len(self.recoveries))
        old_device = self.o["device"]
        device = "CPU" if old_device != "CPU" and len(self.recoveries) >= 2 else old_device
        self.status.emit(tr("Reloading the model…"), "warn", True)
        self.pipe = None
        import gc
        gc.collect()  # frees the GPU memory held by the broken pipeline
        try:
            self.o["device"] = device
            self.pipe = self.load(force=True)
        except Exception:  # noqa: BLE001
            if device == "CPU":
                raise
            log.exception("reload on %s failed, switching to the CPU", device)
            device = self.o["device"] = "CPU"
            self.pipe = self.load(force=True)
        if device != old_device:
            self.device_changed.emit(device)
            self.info.emit(tr("⚠ The {device} failed ({reason}): continuing on the CPU, which is slower. "
                              "The audio recorded in the meantime was not lost.").format(device=old_device,
                                                                                        reason=reason))
        else:
            self.info.emit(tr("⚠ Model error on {device} ({reason}): reloaded, transcription continues.")
                           .format(device=device, reason=reason))
        self.idle_status()

    def _decode_once(self, audio, beams, kind="sentence", budget=None, prompt=None):
        """Whisper on `audio`. With greedy decoding generation is followed token by token and stopped
        if it loops, if it exceeds `budget` seconds or if the session is closed.
        `prompt` None = the session prompt, "" = none."""
        cfg = self.pipe.get_generation_config()
        language = whisper_language(self.o["language"])
        if language:
            cfg.language = language
        cfg.task = "transcribe"
        cfg.return_timestamps = False
        cfg.num_beams = beams
        # token cap proportional to the audio: on noise mistaken for speech Whisper can loop and
        # generate up to 448 tokens, blocking transcription for tens of seconds
        cfg.max_new_tokens = int(min(440, 24 + 10 * len(audio) / SR))
        prompt = self.o["prompt"] if prompt is None else prompt
        if prompt:
            cfg.initial_prompt = prompt
        t0 = time.time()
        generated, why = [], []

        def streamer(chunk):
            generated.append(chunk)
            if self.abort:
                why.append("closing")
            elif budget and time.time() - t0 > budget:
                why.append(f"over {budget:.0f} s")
            elif is_looping("".join(generated)):
                why.append("loop")
            return self.stop_value if why else None  # None = continue

        self.busy = (kind, t0)
        try:
            if beams == 1:  # streaming is not supported with beam search
                text = "".join(self.pipe.generate(audio, cfg, streamer=streamer).texts)
            else:
                text = "".join(self.pipe.generate(audio, cfg).texts)
        finally:
            self.busy = None
        log.info("%s: audio %.1f s, beam %d, %.2f s, %d chars%s", kind, len(audio) / SR, beams,
                 time.time() - t0, len(text), f", stopped ({why[0]})" if why else "")
        return text

    def decode_final(self, audio):
        if self.final_beams > 1:
            try:
                return self._decode_once(audio, self.final_beams)
            except RuntimeError as e:
                self.info.emit(tr("Beam search not supported on {device} ({reason}): using fast decoding.")
                               .format(device=self.o["device"], reason=first_line(e, 100)))
                self.final_beams = 1
        return self.decode(audio, 1)

    # --- where to cut ----------------------------------------------------------------
    def find_end(self, speech):
        """Frame where the first complete sentence should be closed, or None if we must wait.
        Considers every pause in the buffer, not just the last one: if the previous transcription was
        slow, the buffer may already hold several sentences."""
        pause_f = max(1, int(self.o["pause"] * FPS))
        long_f, min_seg_f, tail_f = int(LONG_PAUSE_S * FPS), int(MIN_SEGMENT_S * FPS), int(TAIL_S * FPS)
        soft_f, short_f = int(SOFT_SEGMENT_S * FPS), min(pause_f, int(SHORT_PAUSE_S * FPS))
        run = voiced = 0
        for i, is_speech in enumerate(speech[: int(MAX_SEGMENT_S * FPS)]):
            if is_speech:
                run, voiced = 0, voiced + 1
                continue
            if voiced < MIN_SPEECH_FRAMES:
                continue
            run += 1
            start = i - run + 1
            if (run >= pause_f and start >= min_seg_f) or (run >= short_f and start >= soft_f) or run >= long_f:
                return min(len(speech), start + tail_f)
        return None

    @staticmethod
    def find_cut(speech):
        """Cut of a segment that is too long: middle of the last pause ≥ 200 ms after MIN_CUT_S."""
        limit = min(len(speech), int(MAX_SEGMENT_S * FPS))
        gap, run, best = max(1, int(0.2 * FPS)), 0, None
        for i in range(limit):
            run = 0 if speech[i] else run + 1
            if run >= gap and i - run + 1 >= MIN_CUT_S * FPS:
                best = i - run // 2
        return best

    def idle_status(self):
        if self.finishing:
            self.status.emit(tr("Finishing the transcript…"), "accent", True)
        else:
            self.status.emit(tr("Listening"), "ok", True)

    # --- main loop -------------------------------------------------------------------
    def run(self):
        log.info("session: %s", {k: v for k, v in self.o.items() if k != "prompt"})
        self.stop_value = self._stop_value()
        if not self.start_pipeline():
            return
        self.idle_status()
        try:
            self.loop()
        except Exception as e:  # noqa: BLE001
            log.exception("error in the transcription loop")
            self.failed.emit(tr("Error during transcription: {error}").format(error=e))
        log.info("end of session")

    def commit(self, audio, offset, nframes, voiced_s=0.0, preview=0):
        """Final transcription of the first `nframes` frames, then drop them from the buffer.
        `voiced_s` = seconds of speech in the segment, `preview` = characters expected from the preview."""
        seg = audio[: nframes * FRAME]
        self.status.emit(tr("Transcribing the sentence…"), "accent", True)
        t0 = time.time()
        try:
            text = clean_text(self.decode_final(seg))
            echo = bool(self.o["prompt"]) and prompt_echo(text, self.o["prompt"])
            if echo or too_short(text, voiced_s, preview):
                # the final pass lost the audio (copy of the context or truncated sentence): retry like the
                # preview, greedy and without context, which is what the model tends to copy
                log.info("suspicious final text (%s): %r", "copy of the context" if echo else "too short", text)
                retry = clean_text(self.decode(seg, 1, "retry", prompt=""))
                if retry and (echo or len(retry) > len(text)):
                    text = retry
            self.errors = 0
        except Exception as e:  # noqa: BLE001 - one bad segment must not stop the session
            self.errors += 1
            if self.errors >= 3:
                raise
            text = ""
            log.exception("error on a segment")
            self.info.emit(tr("Error on a segment, skipped: {reason}").format(reason=first_line(e, 120)))
        elapsed, duration = time.time() - t0, len(seg) / SR
        if text:
            self.final.emit(text, offset / SR, (offset + len(seg)) / SR)
        self.partial.emit("")
        self.buf.drop_frames(nframes)
        # if transcribing takes almost as long as speaking, the delay grows forever: warn
        self.slow_count = self.slow_count + 1 if elapsed > 0.6 * duration else 0
        msg = tr("Last sentence: {audio} s of audio transcribed in {elapsed} s").format(
            audio=f"{duration:.1f}", elapsed=f"{elapsed:.1f}")
        if self.slow_count >= 2 and self.realtime:
            msg += tr(" — too slow for real time: use a turbo model, Fast precision, the GPU, or turn off "
                      "the preview")
        self.info.emit(msg)
        self.idle_status()

    def loop(self):
        partial_frames, partial_text = 0, ""
        while not self.abort:
            if not self.finishing:
                self.buf.new.wait(0.2)
            self.buf.new.clear()
            offset, audio, probs = self.buf.snapshot()
            nf = len(probs)
            if nf == 0:
                if self.finishing:
                    return
                continue
            speech = speech_mask(probs, self.o["threshold"])
            idx = np.flatnonzero(speech)
            pre_f = int(PRE_ROLL_S * FPS)

            if len(idx) < MIN_SPEECH_FRAMES:  # only silence: keep a moment before the present
                if self.finishing:
                    return
                if nf > 2 * pre_f + 8:
                    self.buf.drop_frames(nf - pre_f)
                    partial_frames, partial_text = 0, ""
                continue
            if idx[0] > pre_f + 4:  # leading silence: cut it and analyze again right away
                self.buf.drop_frames(idx[0] - pre_f)
                partial_frames, partial_text = 0, ""
                self.buf.new.set()
                continue

            end = self.find_end(speech)
            if end is None and nf >= MAX_SEGMENT_S * FPS:
                end = self.find_cut(speech) or int(MAX_SEGMENT_S * FPS)
            if end is None and self.finishing:
                end = min(nf, idx[-1] + 1 + int(TAIL_S * FPS))
            if end:
                # the preview covers partial_frames frames: the part falling in the segment gives the expected chars
                expected = len(partial_text) * min(1.0, end / partial_frames) if partial_frames else 0
                self.commit(audio, offset, end, np.count_nonzero(speech[:end]) / FPS, expected)
                partial_frames, partial_text = 0, ""
                self.buf.new.set()  # more audio arrived in the meantime
                continue

            # preview: at most half of the time, so the final sentence does not queue up
            if self.o["live"] and nf - partial_frames >= PARTIAL_STEP_S * FPS and time.time() >= self.next_partial:
                partial_frames = nf
                t0 = time.time()
                try:
                    text = clean_text(self.decode(audio[: nf * FRAME], 1, "preview", PARTIAL_BUDGET_S))
                except Exception:  # noqa: BLE001
                    if self.abort:
                        return
                    log.exception("error in the preview")
                    text = ""  # the preview is not essential: the final transcription will try again
                self.next_partial = time.time() + (time.time() - t0)
                partial_text = text
                self.partial.emit(text)

def audio_duration(path):
    """Duration in seconds (ffprobe, or the wave module for .wav files); None if it cannot be determined."""
    if shutil.which("ffprobe"):
        try:
            res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                  "default=noprint_wrappers=1:nokey=1", path], capture_output=True, text=True,
                                 timeout=30)
            return float(res.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / w.getframerate()
    except (OSError, wave.Error, EOFError):
        return None

class FileTranscriber(Transcriber):
    """Transcribes an already recorded audio file the way transcribe.py does, not like live recording:
    audio cleanup with ffmpeg and Whisper "long-form" transcription with timestamps, where every 30 s
    window keeps the text of the previous one as context. It is far more accurate than short sentences
    transcribed one by one, which are only needed live to get the text right away.
    The file is read in blocks of BLOCK_S, cut at the quietest point near the limit: memory stays
    bounded, progress is visible and "Stop" acts after at most one block."""
    realtime = False
    BLOCK_S = 120     # audio transcribed at a time: progress updates at every block
    # level that works for (model, device, beams): the following files don't retry the ones that failed
    known_levels = {}
    SEEK_S = 15       # the cut between two blocks is looked for in the last seconds of the block
    READ_S = 10       # audio decoded per read

    def __init__(self, path, opts, cached=None, parent=None):
        super().__init__(AudioBuffer(), opts, cached, parent)
        self.path = path
        self.duration = audio_duration(path)
        self.done = 0.0        # seconds of audio already transcribed
        self.feed_error = None
        self.proc = None
        # as in transcribe.py: from the most accurate configuration to the simplest, if the device
        # does not support beam search or timestamps
        self.levels = list(dict.fromkeys(((opts["beams"], True), (1, True), (1, False))))
        self.level_key = (opts["model"], opts["device"], opts["beams"])
        self.level = self.known_levels.get(self.level_key, 0)

    def cancel(self):
        super().cancel()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    # --- reading -------------------------------------------------------------------------
    def _ffmpeg(self, audio_filter):
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", self.path, "-vn"]
        if audio_filter:
            cmd += ["-af", audio_filter]
        self.proc = subprocess.Popen(cmd + ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        n = int(self.READ_S * SR) * 4
        while not self.abort:
            raw = self.proc.stdout.read(n)
            if not raw:
                break
            yield np.frombuffer(raw[: len(raw) // 4 * 4], np.float32)
        err = self.proc.stderr.read().decode(errors="replace").strip()
        if self.proc.wait() != 0 and not self.abort:
            raise RuntimeError(err.splitlines()[-1] if err else f"ffmpeg exit code {self.proc.returncode}")

    def _wave(self):
        try:
            w = wave.open(self.path, "rb")
        except (wave.Error, EOFError):
            raise RuntimeError(tr("files other than uncompressed .wav need ffmpeg: {hint}")
                               .format(hint=ffmpeg_hint())) from None
        with w:
            if w.getsampwidth() != 2:
                raise RuntimeError(tr("this .wav is not 16-bit: install ffmpeg to read it"))
            ch, resample = w.getnchannels(), Resampler(w.getframerate())
            while not self.abort:
                raw = w.readframes(int(self.READ_S * w.getframerate()))
                if not raw:
                    break
                x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
                if ch > 1:
                    x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
                yield resample(x)

    def _reads(self):
        if not shutil.which("ffmpeg"):
            yield from self._wave()
            return
        audio_filter = CLEANUP_FILTERS["light"]
        started = False
        try:
            for x in self._ffmpeg(audio_filter):
                started = True
                yield x
        except RuntimeError as e:
            if started:
                raise
            # ffmpeg without those filters (minimal builds): transcribe the original audio
            log.warning("audio filters not applicable (%s): original audio", e)
            self.info.emit(tr("Audio cleanup not available with this ffmpeg: transcribing the original audio."))
            yield from self._ffmpeg(None)

    @classmethod
    def _cut(cls, x):
        """Where to close the block: the middle of the quietest half second in the last SEEK_S."""
        win = SR // 2
        seek = x[-int(cls.SEEK_S * SR):]
        n = len(seek) // win
        if n < 2:
            return len(x)
        rms = np.sqrt(np.mean(seek[: n * win].reshape(n, win) ** 2, axis=1))
        return len(x) - len(seek) + int(np.argmin(rms)) * win + win // 2

    def _blocks(self):
        pending, size = [], 0
        for x in self._reads():
            pending.append(x)
            size += len(x)
            if size >= self.BLOCK_S * SR:
                buf = np.concatenate(pending)
                cut = self._cut(buf)
                yield buf[:cut]
                pending, size = [buf[cut:]], len(buf) - cut
        if size and not self.abort:
            yield np.concatenate(pending)

    # --- transcription -------------------------------------------------------------------
    def _generate(self, audio, prompt):
        """Long-form transcription with timestamps; steps down a level if the device lacks support."""
        while True:
            beams, stamps = self.levels[self.level]
            cfg = self.pipe.get_generation_config()
            language = whisper_language(self.o["language"])
            if language:
                cfg.language = language
            cfg.task = "transcribe"
            cfg.return_timestamps = stamps
            cfg.num_beams = beams
            if prompt:
                cfg.initial_prompt = prompt
            t0 = time.time()
            self.busy = ("file", t0)
            try:
                if beams == 1:  # with greedy decoding "Stop" interrupts immediately
                    res = self.pipe.generate(audio, cfg, streamer=lambda _: self.stop_value if self.abort else None)
                else:
                    res = self.pipe.generate(audio, cfg)
            except RuntimeError as e:
                if self.abort or self.level == len(self.levels) - 1:
                    raise
                reason = first_line(e, 120)
                self.level += 1
                self.known_levels[self.level_key] = self.level
                log.warning("setting not supported on %s (%s): level %s", self.o["device"], reason,
                            self.levels[self.level])
                self.info.emit(tr("The {device} does not support {setting} ({reason}): continuing with {fallback}.")
                               .format(device=self.o["device"], setting=self.describe(beams, stamps),
                                       reason=reason, fallback=self.describe(*self.levels[self.level])))
                continue
            finally:
                self.busy = None
            log.info("block: audio %.1f s, beam %d, timestamps %s, %.1f s", len(audio) / SR, beams, stamps,
                     time.time() - t0)
            chunks = [(c.start_ts, c.end_ts, c.text) for c in (res.chunks or [])] if stamps else []
            return chunks or [(0.0, len(audio) / SR, "".join(res.texts))]

    @staticmethod
    def describe(beams, stamps):
        precision = tr("High precision") if beams > 1 else tr("Fast precision")
        return f"{precision} " + (tr("with times") if stamps else tr("without times"))

    def transcribe_block(self, audio):
        """Clean segments (start, end, text) of a block, with times relative to the block."""
        chunks = self._generate(audio, self.o["prompt"])
        segments, removed = clean_segments(chunks)
        total = sum(len(c[2]) for c in chunks)
        if self.o["prompt"] and total and removed / total > 0.3 and not self.abort:
            # the prompt sent the model into a loop (as in transcribe.py): try again without it
            self.info.emit(tr("The model looped with the course terms: transcribing the block again without them."))
            segments, _ = clean_segments(self._generate(audio, ""))
        return segments

    def idle_status(self):
        self.status.emit(tr("Transcribing the file…"), "accent", True)

    def run(self):
        log.info("file: %s (%.0f s)", self.path, self.duration or 0)
        self.stop_value = self._stop_value()
        if not self.start_pipeline():
            return
        self.idle_status()
        offset = 0.0
        try:
            for block in self._blocks():
                try:
                    segments = self.transcribe_block(block)
                except Exception as e:  # noqa: BLE001 - device error: reload and try once more
                    if self.abort:
                        break
                    self.recover(e)
                    segments = self.transcribe_block(block)
                if self.abort and not segments:
                    break
                for start, end, text in segments:
                    self.final.emit(text, offset + start, offset + max(start, end))
                offset += len(block) / SR
                self.done = offset
                if self.abort:
                    break
        except Exception as e:  # noqa: BLE001 - unreadable file, ffmpeg failure or broken model
            log.exception("transcription of %s failed", self.path)
            self.feed_error = first_line(e, 200)
            self.failed.emit(tr("Cannot transcribe {file}: {reason}").format(file=os.path.basename(self.path),
                                                                              reason=self.feed_error))
        log.info("end of file")

# ---------------------------------------------------------------------------
# Notebook: courses, lectures and transcripts stored as folders and text files
# ---------------------------------------------------------------------------
def safe_name(text):
    """Text usable in a file name: no forbidden characters and no '—', which separates the name fields."""
    text = re.sub(r'[/\\:*?"<>|\r\n\t]', " ", text or "").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip(" .")[:80]

def category_names():
    """The suggested categories in the interface language."""
    return [tr(name) for name in CATEGORIES]

def color_for(name, table=None):
    """Fixed color for a name: the predefined one (in any language), otherwise one from the palette chosen
    by the name itself."""
    if table is None:
        table = {**CATEGORIES, **{tr(k): v for k, v in CATEGORIES.items()}}
    return table.get(name) or PALETTE[zlib.crc32(name.lower().encode()) % len(PALETTE)]

def lesson_dirname(number, date, title):
    return f"{tr(LESSON_WORD)} {number:02d} — {date.toString('yyyy-MM-dd')}" + (f" — {title}" if title else "")

def note_filename(number, category, title):
    return f"{number:02d} — {category}" + (f" — {title}" if title else "") + ".txt"

def parse_lesson(path):
    name = os.path.basename(path)
    m = LESSON_RE.match(name)
    if not m:  # folder created by hand: shown anyway, at the bottom
        return {"path": path, "number": 0, "date": QDate(), "title": name}
    return {"path": path, "number": int(m[2]), "date": QDate.fromString(m[3], "yyyy-MM-dd"), "title": m[4] or ""}

def parse_note(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    m = NOTE_RE.match(stem)
    if not m:
        return {"path": path, "number": 0, "category": tr(OTHER_CATEGORY), "title": stem}
    return {"path": path, "number": int(m[1]), "category": m[2].strip(), "title": m[3] or ""}

def subdirs(path):
    try:
        return sorted(e.path for e in os.scandir(path) if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return []

def list_lessons(course):
    return sorted((parse_lesson(p) for p in subdirs(course)),
                  key=lambda l: (l["number"] == 0, l["number"], l["date"].toJulianDay(), l["title"]))

def list_notes(lesson):
    try:
        files = [e.path for e in os.scandir(lesson) if e.is_file() and e.name.endswith(".txt")]
    except OSError:
        return []
    return sorted((parse_note(p) for p in files), key=lambda n: (n["number"] == 0, n["number"], n["path"]))

def next_number(items):
    return max((i["number"] for i in items), default=0) + 1

def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""

def course_meta(course):
    """Course settings: recurring terms (Whisper prompt) and audio folder ("" = the lecture folder)."""
    try:
        with open(os.path.join(course, COURSE_META), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("context", "")
    meta.setdefault("audio", "")
    return meta

def write_course_meta(course, meta):
    with open(os.path.join(course, COURSE_META), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def norm_path(path):
    return os.path.normpath(os.path.abspath(os.path.expanduser(path.strip()))) if path and path.strip() else ""

def pretty_path(path):
    """Path for display: the home folder abbreviated to ~."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path

def long_date(date):
    text = LOCALE.toString(date, QLocale.FormatType.LongFormat) if date.isValid() else ""
    return text[:1].upper() + text[1:]

def move_to_trash(path):
    """System trash, so something deleted by mistake can be recovered."""
    res = QFile.moveToTrash(path)
    return res[0] if isinstance(res, tuple) else bool(res)

dot_icon = kit.dot_icon
alpha = kit.alpha

def icon_button(name, tooltip, slot=None):
    return kit.IconButton(name, tooltip=tooltip, slot=slot)

class Scrim(QWidget, kit.Themed):
    """Semi-transparent veil behind the sidebar opened over the page: a click outside closes it."""

    def __init__(self, parent, on_click):
        super().__init__(parent)
        self.on_click = on_click
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.hide()
        self.follow_theme()

    def restyle(self):
        self.setStyleSheet(f"background: {alpha(C['scrim'], 0.28)};")

    def mousePressEvent(self, event):
        self.on_click()

# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
def button(text, kind=None, slot=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b

def menu_action(menu, text, slot, icon=None):
    a = menu.addAction(icon, text) if icon else menu.addAction(text)
    a.triggered.connect(lambda _=False: slot())
    return a

def label(text="", name=None, wrap=False):
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl

class StatusPill(QLabel, kit.Themed):
    """Recorder status: colored dot that blinks slowly while the model works (steady with reduced motion).
    The color is a token name, so the pill follows theme changes."""
    reduce_motion = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.blink = QTimer(self, interval=650, timeout=self._toggle)
        self.on, self.pulsing = True, False
        self.state = (tr("Ready"), "muted")
        self.follow_theme()

    def set_state(self, text, key, pulsing):
        self.state, self.pulsing, self.on = (text, key), pulsing, True
        self.blink.start() if pulsing and not self.reduce_motion else self.blink.stop()
        self.restyle()

    def restyle(self):
        color = C[self.state[1]]
        self.setStyleSheet(f"QLabel {{ color: {color}; background: {alpha(color, 0.10)}; font-weight: 600; "
                           f"border-radius: 13px; padding: 4px 12px; }}")
        self._render()

    def _toggle(self):
        self.on = not self.on
        self._render()

    def _render(self):
        text, key = self.state
        color = C[key]
        self.setText(f"<span style='color:{color if self.on else alpha(color, 0.25)}'>●</span>&nbsp; {text}")

class Waveform(QWidget):
    """Live microphone level as a scrolling row of bars: accent colored where speech is detected, faint in
    silence. It replaces a plain level meter: you see at a glance that the app hears you, and when."""
    BAR, GAP = 3, 2

    def __init__(self):
        super().__init__()
        self.levels = deque(maxlen=160)
        self.setMinimumWidth(48)
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip(tr("Microphone level: colored where speech is detected"))
        kit.signals.changed.connect(self.update)

    def push(self, level, voice):
        self.levels.append((max(0.0, min(1.0, level)), voice))
        self.update()

    def clear(self):
        self.levels.clear()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        step = self.BAR + self.GAP
        items = list(self.levels)[-max(1, self.width() // step):]
        x = self.width() - len(items) * step
        mid = self.height() / 2
        for level, voice in items:
            h = max(2.0, level * (self.height() - 2))
            p.setBrush(QColor(C["accent"] if voice else C["faint"]))
            p.drawRoundedRect(QRectF(x, mid - h / 2, self.BAR, h), 1.5, 1.5)
            x += step
        if not items:  # idle: a quiet baseline
            p.setBrush(QColor(C["border_strong"]))
            p.drawRoundedRect(QRectF(0, mid - 1, self.width(), 2), 1, 1)

class NoteEdit(QTextEdit):
    """Text editor: a centered column of comfortable reading width that adapts to the window, with airy
    line spacing. While recording the final text can be corrected, but not the preview at the end, which
    the transcriber keeps rewriting: whatever would be typed there goes to the end of the final text."""
    MAX_COLUMN, MIN_SIDE, TOP = 760, 22, 22
    _K = QKeySequence.StandardKey
    DELETE_KEYS = (_K.Cut, _K.Delete, _K.Backspace, _K.DeleteEndOfWord, _K.DeleteStartOfWord,
                   _K.DeleteEndOfLine, _K.DeleteCompleteLine)
    INSERT_KEYS = (_K.Paste, _K.InsertParagraphSeparator, _K.InsertLineSeparator)

    def __init__(self):
        super().__init__()
        self.setObjectName("note")
        self.setAcceptRichText(False)
        self.locked_from = None  # function: position where the preview starts, None if there is none
        self.undo = True         # off while recording: it would also undo the transcribed text
        self.set_text("")

    def set_undo(self, on):
        self.undo = on
        self.document().setUndoRedoEnabled(on)

    def set_reading(self, family, px):
        """Typeface and size of the transcript text (a reading preference, like in e-book readers)."""
        self.setStyleSheet(f"QTextEdit#note {{ font-family: '{family}'; font-size: {px}px; }}")

    def _out_of_preview(self):
        """If the cursor is inside the preview, move it to the end of the final text. Return True if moved."""
        start = self.locked_from() if self.locked_from else None
        cur = self.textCursor()
        if start is None or cur.selectionStart() <= start:
            return False  # a selection overlapping the preview is fine: the preview rewrites itself
        cur.setPosition(start)
        self.setTextCursor(cur)
        return True

    def keyPressEvent(self, event):
        text = event.text()
        if text in ("\b", "\x7f") or any(map(event.matches, self.DELETE_KEYS)):
            if self._out_of_preview():
                return  # deleting inside the preview: only the cursor moves, the final text stays intact
        elif (text and (text.isprintable() or text in "\r\n\t")) or any(map(event.matches, self.INSERT_KEYS)):
            self._out_of_preview()
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):  # paste and drop
        self._out_of_preview()
        super().insertFromMimeData(source)

    def inputMethodEvent(self, event):  # letters composed with dead keys or an input method
        if event.commitString():
            self._out_of_preview()
        super().inputMethodEvent(event)

    def _side(self):
        return max(self.MIN_SIDE, (self.viewport().width() - self.MAX_COLUMN) // 2)

    def setPlaceholderText(self, text):
        """Qt draws the placeholder at the edge of the document, ignoring the margins of the reading column:
        it is drawn here instead, aligned with where the text will start."""
        self._placeholder = text
        super().setPlaceholderText("")
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if getattr(self, "_placeholder", "") and self.document().isEmpty():
            p = QPainter(self.viewport())
            p.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            p.setFont(self.font())
            side = self._side()
            p.drawText(QRectF(side, self.TOP, self.viewport().width() - 2 * side, self.viewport().height() - self.TOP),
                       Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self._placeholder)
            p.end()

    def _apply_margins(self):
        doc = self.document()
        frame = doc.rootFrame().frameFormat()
        side = self._side()
        if frame.leftMargin() == side and frame.topMargin() == self.TOP:
            return
        frame.setLeftMargin(side)
        frame.setRightMargin(side)
        frame.setTopMargin(self.TOP)
        frame.setBottomMargin(self.TOP)
        doc.rootFrame().setFrameFormat(frame)

    def set_text(self, text):
        self.setPlainText(text)
        doc = self.document()
        doc.setUndoRedoEnabled(False)
        doc.setDocumentMargin(0)
        self._apply_margins()
        block = QTextBlockFormat()
        kind = QTextBlockFormat.LineHeightTypes.ProportionalHeight
        block.setLineHeight(155, getattr(kind, "value", kind))
        cur = QTextCursor(doc)
        cur.select(QTextCursor.SelectionType.Document)
        cur.mergeBlockFormat(block)  # new blocks (line breaks, transcribed text) inherit this format
        doc.setUndoRedoEnabled(self.undo)
        self.moveCursor(QTextCursor.MoveOperation.End)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        undo = self.document().isUndoRedoEnabled()
        self.document().setUndoRedoEnabled(False)  # margins are not an edit to undo
        self._apply_margins()
        self.document().setUndoRedoEnabled(undo)

ROLE_KIND = Qt.UserRole        # ("course" | "lesson", path)
ROLE_INFO = Qt.UserRole + 1    # details to display

class TreeDelegate(QStyledItemDelegate):
    """Sidebar rows: course with a colored spine, lecture with number, date and topic."""

    def sizeHint(self, option, index):
        kind = (index.data(ROLE_KIND) or ("course",))[0]
        return QSize(option.rect.width(), 34 if kind == "course" else 50)

    def paint(self, p, option, index):
        kind, _ = index.data(ROLE_KIND)
        info = index.data(ROLE_INFO)
        r = QRectF(option.rect).adjusted(2 if kind == "course" else 14, 2, -6, -2)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["accent_soft"]))
            p.drawRoundedRect(r, 8, 8)
        elif option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["hover"]))
            p.drawRoundedRect(r, 8, 8)
        font = QFont(option.font)
        if kind == "course":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(info["color"]))
            p.drawRoundedRect(QRectF(r.left() + 6, r.center().y() - 8, 8, 16), 2, 2)
            font.setBold(True)
            p.setFont(font)
            count = str(info["count"])
            cw = p.fontMetrics().horizontalAdvance(count) + 10
            text_r = r.adjusted(22, 0, -cw, 0)
            p.setPen(QColor(C["muted"] if info.get("missing") else C["text"]))
            p.drawText(text_r, Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(info["name"], Qt.ElideRight, int(text_r.width())))
            p.setPen(QColor(C["muted"]))
            p.drawText(r.adjusted(0, 0, -6, 0), Qt.AlignVCenter | Qt.AlignRight, count)
        else:
            top = r.adjusted(10, 6, -8, -r.height() / 2)
            bottom = r.adjusted(10, r.height() / 2 - 1, -8, -5)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor(C["accent_text"]))
            head = f"{tr(LESSON_WORD)} {info['number']}" if info["number"] else tr(LESSON_WORD)
            p.drawText(top, Qt.AlignVCenter | Qt.AlignLeft, head)
            font.setBold(False)
            small = QFont(font)
            small.setPointSizeF(max(7.5, font.pointSizeF() - 1))
            p.setFont(small)
            p.setPen(QColor(C["muted"]))
            p.drawText(top, Qt.AlignVCenter | Qt.AlignRight,
                       LOCALE.toString(info["date"], "d MMM yyyy") if info["date"].isValid() else "")
            p.setFont(font)
            p.setPen(QColor(C["text"] if info["title"] else C["muted"]))
            p.drawText(bottom, Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(info["title"] or tr("no topic"), Qt.ElideRight,
                                                  int(bottom.width())))
        p.restore()

class CourseDialog(QDialog):
    """Course name, where to save it (transcripts) and, optionally, a different folder for the audio."""

    def __init__(self, parent, root, path=None, meta=None):
        super().__init__(parent)
        meta = meta or {}
        self.original = path
        self.adopt = False
        self.setWindowTitle(tr("Edit course") if path else tr("New course"))
        self.setMinimumWidth(420)
        self.resize(560, 10)
        form = QFormLayout(self)
        form.setSpacing(12)
        form.setContentsMargins(22, 20, 22, 16)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.ed_name = QLineEdit(os.path.basename(path) if path else "")
        self.ed_name.setPlaceholderText(tr("e.g. Calculus 1"))
        self.ed_parent = QLineEdit(os.path.dirname(path) if path else root)
        self.ed_audio = QLineEdit(meta.get("audio", ""))
        self.ed_audio.setPlaceholderText(tr("In the lecture folder, next to the transcripts"))
        self.ed_audio.setClearButtonEnabled(True)
        self.ed_context = QLineEdit(meta.get("context", ""))
        self.ed_context.setPlaceholderText(tr("e.g. derivatives, Riemann integrals, mean value theorem"))
        self.lbl_target = label("", "hint", True)
        form.addRow(tr("Course name"), self.ed_name)
        form.addRow(tr("Save in"), self._with_browse(self.ed_parent, tr("Where to save the course transcripts")))
        form.addRow("", self.lbl_target)
        form.addRow(tr("Audio (.wav)"), self._with_browse(self.ed_audio, tr("Where to save the course audio")))
        form.addRow(tr("Recurring terms"), self.ed_context)
        form.addRow("", label(tr("Optional: subject, technical terms and proper names help Whisper spell them "
                                 "correctly in every lecture of the course."), "hint", True))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(tr("Save") if path else tr("Create course"))
        buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        for ed in (self.ed_name, self.ed_parent, self.ed_audio):
            ed.textChanged.connect(self._update_target)
        self._update_target()

    def _with_browse(self, edit, title):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(edit, 1)

        def browse():
            start = norm_path(edit.text()) or self.ed_parent.text()
            folder = QFileDialog.getExistingDirectory(self, title, start)
            if folder:
                edit.setText(folder)
        h.addWidget(button(tr("Browse…"), slot=browse))
        return w

    def _update_target(self):
        text = tr("Transcripts in: {path}").format(path=pretty_path(self.target()) or "…")
        if norm_path(self.ed_audio.text()):
            audio = os.path.join(norm_path(self.ed_audio.text()), tr(LESSON_WORD) + " …")
            text += "\n" + tr("Audio in: {path}").format(path=pretty_path(audio))
        self.lbl_target.setText(text)
        self.lbl_target.setMinimumHeight(self.lbl_target.heightForWidth(max(200, self.lbl_target.width())))
        self.resize(max(self.width(), 560), max(self.height(), self.sizeHint().height()))

    def name(self):
        return safe_name(self.ed_name.text())

    def target(self):
        parent = norm_path(self.ed_parent.text())
        return os.path.join(parent, self.name()) if parent and self.name() else ""

    def meta(self):
        return {"context": self.ed_context.text().strip(), "audio": norm_path(self.ed_audio.text())}

    def accept(self):
        title = self.windowTitle()
        if not self.name():
            QMessageBox.warning(self, title, tr("Enter the course name."))
            return
        if not norm_path(self.ed_parent.text()):
            QMessageBox.warning(self, title, tr("Choose where to save the course."))
            return
        target = self.target()
        if target != self.original and os.path.exists(target):
            if self.original or not os.path.isdir(target):
                QMessageBox.warning(self, title, tr("A folder named {path} already exists.").format(path=target))
                return
            # new course in an existing folder (for example after a reinstall): reuse it
            if QMessageBox.question(self, title, tr("The folder {path} already exists.\n\nUse it for this course? "
                                                    "The lectures it contains will show up in the list.")
                                    .format(path=target)) != QMessageBox.Yes:
                return
            self.adopt = True
        super().accept()

class LessonDialog(QDialog):
    def __init__(self, parent, courses, course, lesson=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit lecture") if lesson else tr("New lecture"))
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        form.setSpacing(12)
        form.setContentsMargins(22, 20, 22, 16)
        self.cb_course = QComboBox()
        for path in courses:
            self.cb_course.addItem(os.path.basename(path), path)
        self.cb_course.setCurrentIndex(max(0, self.cb_course.findData(course)))
        self.cb_course.setEnabled(lesson is None)
        self.sp_num = QSpinBox()
        self.sp_num.setRange(1, 999)
        self.ed_date = QDateEdit(lesson["date"] if lesson and lesson["date"].isValid() else QDate.currentDate())
        self.ed_date.setCalendarPopup(True)
        self.ed_date.setLocale(LOCALE)
        self.ed_date.setDisplayFormat(LOCALE.dateFormat(QLocale.FormatType.LongFormat))
        self.ed_title = QLineEdit(lesson["title"] if lesson else "")
        self.ed_title.setPlaceholderText(tr("Optional, e.g. Improper integrals"))
        form.addRow(tr("Course"), self.cb_course)
        form.addRow(tr("Number"), self.sp_num)
        form.addRow(tr("Date"), self.ed_date)
        form.addRow(tr("Topic"), self.ed_title)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(tr("Save") if lesson else tr("Create lecture"))
        buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if lesson:
            self.sp_num.setValue(max(1, lesson["number"]))
        else:  # suggested number: the one after the last lecture of the chosen course
            self.cb_course.currentIndexChanged.connect(self._suggest_number)
            self._suggest_number()
        self.ed_title.setFocus()

    def _suggest_number(self):
        self.sp_num.setValue(next_number(list_lessons(self.cb_course.currentData())))

    def values(self):
        return (self.cb_course.currentData(), self.sp_num.value(), self.ed_date.date(),
                safe_name(self.ed_title.text()))

class SettingsDialog(QDialog):
    """Recording settings: two columns of fields if there is room, one if the window is narrow."""

    def __init__(self, win):
        super().__init__(win)
        self.setWindowTitle(tr("Recording settings"))
        self.setMinimumWidth(380)
        self.resize(780, 300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(14)
        self.pairs, checks = win._build_settings()
        self.labels = [label(text, "muted") for text, _ in self.pairs]
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(10)
        lay.addLayout(self.grid)
        for chk in checks:
            lay.addWidget(chk)
        lay.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.columns = None
        self._arrange(2)

    def _arrange(self, columns):
        if columns == self.columns:
            return
        self.columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for c in range(4):
            self.grid.setColumnStretch(c, 0)
        last = len(self.pairs) - 1
        for i, ((_, field), lbl) in enumerate(zip(self.pairs, self.labels)):
            r, c = (i // 2, (i % 2) * 2) if columns == 2 else (i, 0)
            span = 3 if columns == 2 and i == last and c == 0 else 1  # the last field takes the whole row
            self.grid.addWidget(lbl, r, c)
            self.grid.addWidget(field, r, c + 1, 1, span)
        self.grid.setColumnStretch(1, 1)
        if columns == 2:
            self.grid.setColumnStretch(3, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(2 if event.size().width() >= 640 else 1)

# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class LiveWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · {tr('lecture notes')}")
        self.resize(1240, 840)
        self.setMinimumSize(480, 460)
        self.settings = QSettings(APP_ID, APP_ID)
        # new releases on GitHub: checked at most once a day, in the background (Help menu to turn it off).
        # A fresh QSettings per call: the check writes from a background thread
        self.updates = kit.UpdateChecker(self, APP_VERSION, lambda k, d=None: QSettings(APP_ID, APP_ID).value(k, d),
                                         lambda k, v: QSettings(APP_ID, APP_ID).setValue(k, v))
        self.pipe_cache = None
        self.vad = None
        self.vad_voice = False
        self.worker = self.source = self.io = self.buf = None
        self.txt_file = self.wav = None
        self.txt_path = None
        self.samples = 0
        self.level = 0.0
        self.boundary = None  # QTextCursor at the start of the preview (see partial_pos)
        self.last_end = None
        self.new_session = False
        self.file_empty = True
        self.rec_new_notes = set()   # transcripts created by this recording: deleted if they stay empty
        self.file_queue = None       # audio files still to transcribe (None = no file transcription)
        self.course = self.lesson = self.note = None
        self.notes = []
        self.loading = self.dirty = False
        self.narrow = self.compact = None   # current responsive modes (decided at the first resize)
        self.save_timer = QTimer(self, singleShot=True, interval=800, timeout=self.save_note)

        # technical settings live in a separate window: the main view is the notebook
        self.settings_dlg = self._build_settings_dialog()

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Horizontal)
        self.split.setHandleWidth(1)
        self.split.addWidget(self._build_sidebar())
        self.split.addWidget(self._build_main())
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([280, 960])
        self.split.setCollapsible(0, False)
        self.split.setCollapsible(1, False)
        outer.addWidget(self.split)
        self.scrim = Scrim(root, lambda: self.set_sidebar(False))

        # final text has no explicit color: it follows the palette, so it stays readable when the theme changes
        self.f_text = QTextCharFormat()
        self.f_partial = QTextCharFormat()
        self.f_partial.setFontItalic(True)
        self.f_stamp = QTextCharFormat()
        self._update_formats()
        kit.signals.changed.connect(self._on_theme_changed)

        self.palette_dlg = kit.CommandPalette(self, self._palette_commands, tr("Type a command or a lecture…"),
                                              tr("No results"))
        self._build_menu()
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.clock = QTimer(self, interval=250, timeout=self._tick)
        self.media = QMediaDevices(self)
        self.media.audioInputsChanged.connect(self.refresh_mics)

        self.refresh_mics()
        self.refresh_models()
        self._load_settings()
        self.reload_tree(self.settings.value("lesson") or None)
        QTimer.singleShot(4000, self.updates.check)
        self.update_timer = QTimer(self, interval=6 * 3600 * 1000, timeout=self.updates.check)
        self.update_timer.start()

    # --- building the interface ----------------------------------------------------------
    def _build_menu(self):
        bar = self.menuBar()

        def action(menu, text, slot, shortcut=None):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        m = bar.addMenu(tr("&Notes"))
        action(m, tr("New course…"), self.new_course)
        self.act_lesson = action(m, tr("New lecture…"), self.new_lesson, QKeySequence.New)
        action(m, tr("New transcript"), lambda: self.lesson and self.btn_new_note.showMenu(), "Ctrl+T")
        m.addSeparator()
        self.act_rec = action(m, tr("Start recording"), self.toggle_recording, "Ctrl+R")
        self.act_file = action(m, tr("Transcribe audio files…"), self.transcribe_files, "Ctrl+Shift+R")
        m.addSeparator()
        action(m, tr("Export lecture to Markdown…"), self.export_lesson, "Ctrl+E")
        action(m, tr("Copy transcript text"), self.copy_text, "Ctrl+Shift+C")
        action(m, tr("Open folder"), self.open_folder, "Ctrl+O")
        m.addSeparator()
        action(m, tr("Quit"), self.close, QKeySequence.Quit)

        m = bar.addMenu(tr("&Tools"))
        action(m, tr("Command palette…"), self.palette_dlg.open_palette, "Ctrl+K")
        action(m, tr("Recording settings…"), self.settings_dlg.show, "Ctrl+,")
        action(m, tr("Refresh model list"), self.refresh_models, "F5")
        action(m, tr("Download more models (OVMS Manager)…"), self.open_ovms_manager)
        m.addSeparator()
        look = m.addMenu(tr("Appearance"))
        theme_group = QActionGroup(self)
        current_theme = self.settings.value("theme", "system") or "system"
        for code, name in THEMES:
            a = look.addAction(tr("Theme: {name}").format(name=tr(name)))
            a.setCheckable(True)
            a.setChecked(code == current_theme)
            theme_group.addAction(a)
            a.triggered.connect(lambda _=False, c=code: self.set_theme(c))
        look.addSeparator()
        self.act_motion = look.addAction(tr("Reduce motion"))
        self.act_motion.setCheckable(True)
        self.act_motion.setChecked(StatusPill.reduce_motion)
        self.act_motion.toggled.connect(self.set_reduce_motion)
        lang_menu = m.addMenu(tr("Interface language"))
        group = QActionGroup(self)
        current = self.settings.value("ui_language", "") or ""
        for code, name in UI_LANGUAGES:
            a = lang_menu.addAction(tr(name))
            a.setCheckable(True)
            a.setChecked(code == current)
            group.addAction(a)
            a.triggered.connect(lambda _=False, c=code: self.set_ui_language(c))

        m = bar.addMenu(tr("&Help"))
        action(m, tr("Quick guide"), self.show_help, "F1")
        action(m, tr("Open log file"), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_PATH)))
        m.addSeparator()
        action(m, tr("Check for updates"), self.check_updates_now)
        auto = m.addAction(tr("Check for updates automatically"))
        auto.setCheckable(True)
        auto.setChecked(self.updates.enabled)
        auto.toggled.connect(self.updates.set_enabled)
        m.addSeparator()
        action(m, tr("About {app}").format(app=APP_NAME), self.show_about)

    def check_updates_now(self):
        def done(release):
            self.updates.finished.disconnect(done)
            if release is None:
                self.set_info(tr("{app} {version} is the latest version (or GitHub could not be reached).")
                              .format(app=APP_NAME, version=APP_VERSION))
        self.updates.finished.connect(done)
        self.set_info(tr("Checking for updates…"))
        self.updates.check(force=True)

    def set_theme(self, code):
        self.settings.setValue("theme", code)
        if THEME:
            THEME.set_preference(code)

    def set_reduce_motion(self, on):
        self.settings.setValue("reduce_motion", "true" if on else "false")
        StatusPill.reduce_motion = on
        self.pill.set_state(*self.pill.state, self.pill.pulsing)

    def _update_formats(self):
        self.f_partial.setForeground(QColor(C["muted"]))
        self.f_stamp.setForeground(QColor(C["accent_text"]))

    def _on_theme_changed(self):
        self._update_formats()
        self.tree.viewport().update()
        self._render_info()
        self.empty_icon.setPixmap(kit.icon("notebook", C["accent_text"], 56).pixmap(56, 56))

    def _palette_commands(self, query):
        """Everything the command palette offers: the menu actions, the lectures, new transcripts by category."""
        commands = []

        def walk(menu, section):
            for a in menu.actions():
                if a.menu():
                    walk(a.menu(), section)
                elif a.text() and a.isEnabled() and not a.isSeparator():
                    commands.append(kit.Command(a.text().replace("&", ""), a.trigger, section,
                                                a.shortcut().toString(QKeySequence.NativeText)))
        for top in self.menuBar().actions():
            if top.menu():
                walk(top.menu(), top.text().replace("&", ""))
        if not self.worker:
            for course in self.courses():
                for lesson in list_lessons(course):
                    number = f"{tr(LESSON_WORD)} {lesson['number']}" if lesson["number"] else tr(LESSON_WORD)
                    title = f"{os.path.basename(course)} · {number}" + (f" — {lesson['title']}" if lesson["title"] else "")
                    commands.append(kit.Command(title, lambda p=lesson["path"]: self.reload_tree(p), tr("Go to"),
                                                icon="notebook", keywords=long_date(lesson["date"])))
        if self.lesson:
            for name in self._category_names():
                commands.append(kit.Command(tr("New transcript: {category}").format(category=name),
                                            lambda n=name: self.new_note(n), tr("Transcript"), icon="plus"))
        return commands

    def set_ui_language(self, code):
        self.settings.setValue("ui_language", code)
        QMessageBox.information(self, APP_NAME, tr("The new language will be used the next time you start {app}.")
                                .format(app=APP_NAME))

    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("sidebar")
        side.setMinimumWidth(220)
        side.setMaximumWidth(380)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 20, 12, 14)
        lay.setSpacing(4)
        top = QHBoxLayout()
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(label(APP_NAME, "brand"))
        brand.addWidget(label(tr("your lecture notes"), "brandSub"))
        top.addLayout(brand, 1)
        top.addWidget(icon_button("search", tr("Command palette (Ctrl+K)"), lambda: self.palette_dlg.open_palette()),
                      0, Qt.AlignTop)
        self.btn_hide_side = icon_button("close", tr("Close"), lambda: self.set_sidebar(False))
        top.addWidget(self.btn_hide_side, 0, Qt.AlignTop)
        lay.addLayout(top)
        lay.addSpacing(12)
        self.tree = QTreeWidget()
        self.tree.setObjectName("tree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(0)  # the delegate indents lectures: no arrows or stripes on the left
        self.tree.setRootIsDecorated(False)
        self.tree.setMouseTracking(True)
        self.tree.setItemDelegate(TreeDelegate(self.tree))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        self.tree.itemSelectionChanged.connect(self.on_tree_selection)
        self.tree.itemClicked.connect(self.on_tree_clicked)
        lay.addWidget(self.tree, 1)
        lay.addSpacing(8)
        self.btn_new_lesson = button("＋  " + tr("New lecture"), "primary", self.new_lesson)
        self.btn_new_course = button("＋  " + tr("New course"), "soft", self.new_course)
        lay.addWidget(self.btn_new_lesson)
        lay.addSpacing(8)
        lay.addWidget(self.btn_new_course)
        self.sidebar = side
        return side

    def _build_main(self):
        main = QWidget()
        main.setObjectName("mainArea")
        main.installEventFilter(self)  # to adapt the view to the available width
        self.main_area = main
        lay = QVBoxLayout(main)
        lay.setContentsMargins(28, 20, 28, 16)
        self.main_lay = lay
        self.update_banner = kit.UpdateBanner(tr("{app} {version} is available.").replace("{app}", APP_NAME),
                                              tr("What's new"), tr("Skip this version"), tr("Later"),
                                              self.updates.skip)
        self.updates.found.connect(self.update_banner.show_release)
        lay.addWidget(self.update_banner)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_empty())
        self.stack.addWidget(self._build_lesson())
        lay.addWidget(self.stack, 1)
        return main

    def side_button(self):
        """☰ button to reopen the sidebar when it is hidden (narrow window)."""
        b = icon_button("menu", tr("Show courses and lectures"), lambda: self.set_sidebar(True))
        b.hide()
        self.side_buttons.append(b)
        return b

    def _build_empty(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.side_buttons = []
        top = QHBoxLayout()
        top.addWidget(self.side_button())
        top.addStretch()
        lay.addLayout(top)
        lay.addStretch(2)
        self.empty_icon = QLabel()
        self.empty_icon.setAlignment(Qt.AlignCenter)
        self.empty_icon.setPixmap(kit.icon("notebook", C["accent_text"], 56).pixmap(56, 56))
        lay.addWidget(self.empty_icon)
        lay.addSpacing(10)
        self.empty_title = label("", "emptyTitle", True)
        self.empty_text = label("", "muted", True)
        for w in (self.empty_title, self.empty_text):
            w.setAlignment(Qt.AlignCenter)
            lay.addWidget(w)
        lay.addSpacing(14)
        row = QHBoxLayout()
        row.addStretch()
        self.empty_btn = button("", "primary", lambda: self.empty_action())
        row.addWidget(self.empty_btn)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch(3)
        self.empty_action = self.new_course
        return page

    def _build_lesson(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.side_button(), 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        self.lbl_eyebrow = label("", "eyebrow")
        self.lbl_title = label("", "lessonTitle", True)
        self.lbl_sub = label("", "muted", True)
        for w in (self.lbl_eyebrow, self.lbl_title, self.lbl_sub):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # they wrap, they don't widen the window
            titles.addWidget(w)
        head.addLayout(titles, 1)
        # lecture actions gathered in a menu: they take little room even in a narrow window
        self.btn_lesson_menu = icon_button("dots", tr("Lecture actions"))
        lesson_menu = QMenu(self)
        self.act_edit_lesson = menu_action(lesson_menu, tr("Edit lecture…"), self.edit_lesson)
        menu_action(lesson_menu, tr("Transcribe audio files…"), self.transcribe_files)
        menu_action(lesson_menu, tr("Export to Markdown…"), self.export_lesson)
        menu_action(lesson_menu, tr("Open folder"), self.open_folder)
        self.btn_lesson_menu.setMenu(lesson_menu)
        head.addWidget(self.btn_lesson_menu, 0, Qt.AlignTop)
        lay.addLayout(head)
        lay.addSpacing(16)

        # one tab per transcript, with a dot in the category color
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(6)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setDrawBase(False)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        tabs_row.addWidget(self.tabs, 1)
        self.btn_new_note = kit.IconButton("plus", tr("New transcript"), "soft")
        self.btn_new_note.setToolTip(tr("Create a transcript with a category (Ctrl+T).\n"
                                        "While recording it splits the text: from here on it goes into the new one."))
        self.note_menu = QMenu(self)
        self.note_menu.aboutToShow.connect(self._fill_note_menu)
        self.btn_new_note.setMenu(self.note_menu)
        tabs_row.addWidget(self.btn_new_note, 0)
        lay.addLayout(tabs_row)
        lay.addSpacing(10)

        sheet = QFrame()
        sheet.setObjectName("sheet")
        sl = QVBoxLayout(sheet)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        meta = QHBoxLayout()
        meta.setContentsMargins(16, 10, 10, 10)
        meta.setSpacing(8)
        self.lbl_cat = label(tr("Category"), "muted")
        meta.addWidget(self.lbl_cat)
        self.cb_cat = QComboBox()
        self.cb_cat.setEditable(True)
        self.cb_cat.setInsertPolicy(QComboBox.NoInsert)
        self.cb_cat.setMinimumWidth(120)
        self.cb_cat.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.cb_cat.setMinimumContentsLength(12)
        self.cb_cat.activated.connect(lambda _: self.apply_note_meta())
        self.cb_cat.lineEdit().editingFinished.connect(self.apply_note_meta)
        meta.addWidget(self.cb_cat)
        self.ed_title = QLineEdit()
        self.ed_title.setObjectName("noteTitle")
        self.ed_title.setPlaceholderText(tr("Title (optional)"))
        self.ed_title.setMinimumWidth(60)
        self.ed_title.editingFinished.connect(self.apply_note_meta)
        meta.addWidget(self.ed_title, 1)
        self.btn_reading = kit.IconButton("text_size", tooltip=tr("Reading: text size and typeface"))
        reading_menu = QMenu(self)
        for title, options, key, default in ((tr("Text size"), READING_SIZES, "reading_size", 16),
                                             (tr("Typeface"), READING_FONTS, "reading_font", "sans")):
            if reading_menu.actions():
                reading_menu.addSeparator()
            header = QWidgetAction(reading_menu)
            header.setDefaultWidget(label(title, "menuHeader"))
            reading_menu.addAction(header)
            group = QActionGroup(self)
            for value, name in options:
                a = reading_menu.addAction(tr(name))
                a.setCheckable(True)
                a.setChecked(str(value) == str(self.settings.value(key, default)))
                if key == "reading_font":
                    font = QFont(kit.FONTS["reading" if value == "serif" else "ui"])
                    font.setPointSizeF(self.font().pointSizeF() + 1)
                    a.setFont(font)
                group.addAction(a)
                a.triggered.connect(lambda _=False, k=key, v=value: (self.settings.setValue(k, v), self._apply_reading()))
        self.btn_reading.setMenu(reading_menu)
        meta.addWidget(self.btn_reading)
        self.btn_copy = button(tr("Copy"), "ghost", self.copy_text)
        meta.addWidget(self.btn_copy)
        self.btn_delete_note = button(tr("Delete"), "ghost", self.delete_note)
        meta.addWidget(self.btn_delete_note)
        self.meta_widgets = (self.cb_cat, self.ed_title, self.btn_delete_note)
        sl.addLayout(meta)
        line = QFrame()
        line.setObjectName("sheetLine")
        line.setFixedHeight(1)
        sl.addWidget(line)
        self.text = NoteEdit()
        self.text.locked_from = lambda: self.partial_pos if self.worker else None
        self.text.textChanged.connect(self.on_text_edited)
        sl.addWidget(self.text, 1)
        lay.addWidget(sheet, 1)
        lay.addSpacing(12)
        lay.addWidget(self._build_recorder())
        return page

    def _build_recorder(self):
        """Floating recorder dock: the only element of the page with depth, because it is the one you act on."""
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(6, 4, 6, 10)  # room for the soft shadow
        outer.setSpacing(8)
        dock = QFrame()
        dock.setObjectName("dock")
        kit.elevate(dock, 32, 8)
        row = QHBoxLayout(dock)
        row.setContentsMargins(10, 8, 12, 8)
        row.setSpacing(10)
        self.btn_rec = kit.IconButton("record", tr("Record"), "primary", self.toggle_recording)
        self.btn_rec.setMinimumWidth(116)
        row.addWidget(self.btn_rec)
        self.btn_file = kit.IconButton("file", tr("From file…"), "soft", self.transcribe_files)
        self.btn_file.setToolTip(tr("Transcribe already recorded audio (.wav, .mp3, .m4a, video…) into this "
                                    "lecture (Ctrl+Shift+R).\nEvery file becomes a new transcript."))
        row.addWidget(self.btn_file)
        self.lbl_time = label("00:00:00", "clock")
        self.lbl_time.setFont(kit.tabular(self.lbl_time.font()))
        row.addWidget(self.lbl_time)
        self.wave = Waveform()
        row.addWidget(self.wave, 1)
        self.level_bar = QProgressBar()  # progress of file transcription (the waveform is for the microphone)
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.hide()
        row.addWidget(self.level_bar, 1)
        self.lbl_backlog = label("", "hint")
        self.lbl_backlog.setFont(kit.tabular(self.lbl_backlog.font()))
        self.lbl_backlog.setToolTip(tr("Audio recorded but not yet transcribed for good.\n"
                                       "If it keeps growing, the model is too slow for this device."))
        row.addWidget(self.lbl_backlog)
        self.chip_device = kit.Chip("lock", "", "neutral", tr("Transcription runs on this computer: the audio "
                                                             "never leaves it."))
        row.addWidget(self.chip_device)
        self.pill = StatusPill()
        row.addWidget(self.pill)
        self.btn_settings = icon_button("settings", tr("Recording settings (Ctrl+,)"), self.settings_dlg.show)
        row.addWidget(self.btn_settings)
        outer.addWidget(dock)
        self.lbl_info = label("", "hint", True)
        self.lbl_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_info.setContentsMargins(12, 0, 12, 0)
        outer.addWidget(self.lbl_info)
        self.info_text, self.info_kind = "", None
        return wrap

    def set_info(self, text, kind=None):
        """Message under the dock; kind "warn" or "err" colors it (and an error is not overwritten)."""
        self.info_text, self.info_kind = text, kind
        self._render_info()

    def _render_info(self):
        text = self.info_text
        if self.info_kind in ("warn", "err"):
            text = f"<span style='color:{C[self.info_kind]}'>{text}</span>"
        self.lbl_info.setText(text)

    def _update_device_chip(self, device=None):
        device = device or self.cb_device.currentData()
        self.chip_device.set(tr("Local · {device}").format(device=device))

    def _build_settings_dialog(self):
        dlg = SettingsDialog(self)
        dlg.finished.connect(lambda _: self._save_settings())
        return dlg

    def _build_settings(self):
        """Create the settings fields; return the (label, field) pairs and the check boxes, which
        SettingsDialog lays out in one or two columns depending on the width."""
        def combo(items):
            cb = QComboBox()
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(14)
            for text, data in items:
                cb.addItem(tr(text), data)
            return cb

        def row(*widgets):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            for x, stretch in widgets:
                h.addWidget(x, stretch)
            return w

        self.cb_mic = combo([])
        self.cb_model = combo([])
        self.cb_device = combo(DEVICES)
        self.cb_lang = combo(LANGUAGES)
        self.cb_prec = combo(PRECISIONS)
        self.cb_prec.setToolTip(tr("Decoding of the final text. The preview always uses the fast one.\n"
                                   "“High” is more accurate but every segment arrives a few seconds later."))
        self.cb_sens = combo(SENSITIVITIES)
        self.cb_sens.setToolTip(tr("How loud a sound must be, relative to the background noise, "
                                   "to count as speech."))
        self.sp_pause = QDoubleSpinBox()
        self.sp_pause.setRange(0.3, 3.0)
        self.sp_pause.setSingleStep(0.1)
        self.sp_pause.setSuffix(" s")
        self.sp_pause.setToolTip(tr("Silence after which the sentence is closed and transcribed for good.\n"
                                    "Shorter = final text sooner; longer = more complete sentences."))
        self.ed_prompt = QLineEdit()
        self.ed_prompt.setPlaceholderText(tr("Optional, applies to every course (per-course terms: Edit course)"))
        pairs = [
            (tr("Microphone"), self.cb_mic),
            (tr("Model"), row((self.cb_model, 1), (button("↻", slot=self.refresh_models), 0))),
            (tr("Device"), self.cb_device),
            (tr("Language"), self.cb_lang),
            (tr("Precision"), self.cb_prec),
            (tr("Sensitivity"), row((self.cb_sens, 1), (label(tr("Pause"), "muted"), 0), (self.sp_pause, 0))),
            (tr("Context"), self.ed_prompt),
        ]
        self.chk_live = QCheckBox(tr("Preview while you speak"))
        self.chk_live.setToolTip(tr("Turn it off to use less power (useful on battery): text appears only at "
                                    "every pause."))
        self.chk_ts = QCheckBox(tr("Time [hh:mm:ss] for every sentence"))
        self.chk_wav = QCheckBox(tr("Also save the audio (.wav)"))
        self.chk_wav.setToolTip(tr("The .wav file is saved in the lecture folder: useful to transcribe it again "
                                   "later with a more accurate model (From file… or lectern-transcribe)."))
        checks = (self.chk_live, self.chk_ts, self.chk_wav)
        self.settings_widgets = (self.cb_mic, self.cb_model, self.cb_device, self.cb_lang, self.cb_prec,
                                 self.cb_sens, self.sp_pause, self.ed_prompt) + checks
        return pairs, checks

    # --- adapting to the window width -----------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is getattr(self, "main_area", None) and event.type() == QEvent.Type.Resize:
            self.set_compact(event.size().width() < COMPACT_W)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < NARROW_W
        if narrow != self.narrow:  # only when crossing the threshold: otherwise respect the user's choice
            self.narrow = narrow
            self.set_sidebar(not narrow)
        elif narrow and self.sidebar.isVisible():
            self._place_drawer()

    def set_sidebar(self, visible):
        """Show/hide courses and lectures. With a wide window the bar sits next to the page; with a narrow
        window it opens over the page (as a drawer) and closes as soon as you pick a lecture."""
        root = self.centralWidget()
        if self.narrow and self.sidebar.parent() is not root:
            self.sidebar.setParent(root)          # out of the splitter: the page keeps the full width
        elif not self.narrow and self.sidebar.parent() is not self.split:
            self.split.insertWidget(0, self.sidebar)
            self.split.setSizes([280, max(300, self.width() - 280)])
        self.sidebar.setVisible(visible)
        self.scrim.setVisible(visible and bool(self.narrow))
        if visible and self.narrow:
            self._place_drawer()
        for b in self.side_buttons:
            b.setVisible(not visible)
        self.btn_hide_side.setVisible(bool(self.narrow))

    def _place_drawer(self):
        root = self.centralWidget()
        self.scrim.setGeometry(root.rect())
        self.sidebar.setGeometry(0, 0, min(300, root.width() - 48), root.height())
        self.scrim.raise_()
        self.sidebar.raise_()

    def on_tree_clicked(self, item):
        if self.narrow and item and item.data(0, ROLE_KIND)[0] == "lesson":
            self.set_sidebar(False)

    def set_compact(self, compact):
        """Narrow main area: shorter or hidden labels, so everything stays on one line."""
        if compact == self.compact or not hasattr(self, "btn_settings"):
            return
        self.compact = compact
        self.main_lay.setContentsMargins(*((14, 12, 14, 10) if compact else (28, 20, 28, 16)))
        self.btn_new_note.setText("" if compact else tr("New transcript"))
        self.btn_file.setText("" if compact else tr("From file…"))
        for w in (self.lbl_cat, self.btn_copy, self.lbl_backlog, self.chip_device):
            w.setVisible(not compact)
        self.btn_rec.setMinimumWidth(96 if compact else 116)
        self.wave.setMinimumWidth(48 if compact else 110)

    def open_ovms_manager(self):
        """Start OVMS Manager: the installed one if present, otherwise the copy next to this project."""
        command = shutil.which("ovms-manager")
        if command:
            command = [command]
        else:
            script = os.path.join(os.path.dirname(APP_DIR), "ovms-manager", "ovms_manager.py")
            if not os.path.exists(script):
                QMessageBox.information(self, APP_NAME, tr(
                    "OVMS Manager is not installed: run ovms-manager/install.sh from the project to get it.\n\n"
                    "Alternatively download an OpenVINO Whisper model from huggingface.co/OpenVINO "
                    "(e.g. whisper-large-v3-turbo-int8-ov) into {folder}, then press F5.").format(folder=MODELS_DIR))
                return
            command = [sys.executable, script]
        subprocess.Popen(command, start_new_session=True)

    def show_help(self):
        QMessageBox.information(self, tr("Quick guide"), tr(
            "<b>Organize</b>: create a <i>course</i> for every subject and a <i>lecture</i> for every day "
            "(number and date are suggested automatically). Everything goes into <i>{folder}</i>."
            "<br><br>"
            "<b>Record</b> (Ctrl+R): the grey text is the preview, it becomes final at every pause and is "
            "saved right away. You can correct the final text even while recording. With "
            "<b>＋ New transcript</b> (Ctrl+T) you pick a category: while recording it splits the text, for "
            "example when the lecturer moves from theory to exercises.<br><br>"
            "<b>From file</b> (Ctrl+Shift+R): transcribes already recorded audio (.wav, .mp3, .m4a, video…) "
            "into the open lecture, one transcript per file, like lectern-transcribe (light audio cleanup). "
            "For the best quality: large-v3 model and High precision.<br><br>"
            "<b>Live</b> use a <i>turbo</i> model, the GPU device, Fast precision (Settings).<br>"
            "<b>Distant voice</b>: High sensitivity, pause around 1 s, course terms in Edit course.<br>"
            "<b>Best quality</b>: turn on “Also save the audio” and transcribe the .wav again later with "
            "From file… or <i>lectern-transcribe</i>, large-v3 model and strong cleanup.<br><br>"
            "<b>Export</b> (Ctrl+E) merges the transcripts of the lecture into a single Markdown file.")
            .format(folder=self.notebook_dir()))

    def show_about(self):
        try:
            import openvino_genai
            genai = openvino_genai.__version__
        except (ImportError, AttributeError):
            genai = tr("not available")
        QMessageBox.about(self, tr("About {app}").format(app=APP_NAME), (
            f"<h3>{APP_NAME} {APP_VERSION}</h3>"
            + tr("Real-time transcription from the microphone with Whisper on OpenVINO GenAI, running entirely "
                 "on your computer (CPU, Intel GPU or NPU).") + "<br><br>"
            + f"OpenVINO GenAI: {genai}<br>"
            + tr("Voice detection: Silero VAD (MIT)") + "<br>"
            + tr("Notes: {path}").format(path=self.notebook_dir()) + "<br>"
            + tr("Models: {path}").format(path=MODELS_DIR) + "<br>"
            + tr("Log: {path}").format(path=LOG_PATH) + "<br><br>"
            + tr("License: GNU GPL v3 or later")))

    # --- lists and settings ---------------------------------------------------------------
    def refresh_mics(self):
        current = self.cb_mic.currentText()
        self.mics = list(QMediaDevices.audioInputs())
        self.cb_mic.clear()
        default = QMediaDevices.defaultAudioInput()
        for i, dev in enumerate(self.mics):
            self.cb_mic.addItem(dev.description() + ("  " + tr("(default)") if dev == default else ""), i)
        if not self.mics:
            self.cb_mic.setPlaceholderText(tr("No microphone found"))
        idx = self.cb_mic.findText(current)
        if idx >= 0:
            self.cb_mic.setCurrentIndex(idx)
        elif self.mics and default in self.mics:
            self.cb_mic.setCurrentIndex(self.mics.index(default))

    def refresh_models(self):
        current = self.cb_model.currentData()
        models = find_whisper_models()
        self.cb_model.clear()
        for path in models:
            name = os.path.basename(path)
            note = "  · " + tr("fast, recommended live") if "turbo" in name else ""
            self.cb_model.addItem(name + note, path)
            self.cb_model.setItemData(self.cb_model.count() - 1, path, Qt.ToolTipRole)
        if not models:
            self.cb_model.setPlaceholderText(tr("No Whisper model in {folder} — see Tools menu")
                                             .format(folder=pretty_path(MODELS_DIR)))
            return
        idx = self.cb_model.findData(current)
        if idx < 0:  # live, speed matters: prefer turbo
            idx = next((i for i, p in enumerate(models) if "turbo" in p), 0)
        self.cb_model.setCurrentIndex(idx)

    def _load_settings(self):
        s = self.settings

        def pick(cb, key):
            idx = cb.findData(s.value(key))
            if idx >= 0:
                cb.setCurrentIndex(idx)

        for cb, key in ((self.cb_model, "model"), (self.cb_device, "device"), (self.cb_lang, "language")):
            pick(cb, key)
        if s.value("language") is None:  # first start: transcribe in the interface language if available
            idx = self.cb_lang.findData(LOCALE.name().split("_")[0])
            if idx >= 0:
                self.cb_lang.setCurrentIndex(idx)
        self.cb_prec.setCurrentIndex(int(s.value("precision", 0)))
        self.cb_sens.setCurrentIndex(int(s.value("sensitivity", 1)))
        self.sp_pause.setValue(float(s.value("pause", 0.7)))
        self.ed_prompt.setText(s.value("prompt", ""))
        self.chk_live.setChecked(s.value("live", "true") == "true")
        self.chk_ts.setChecked(s.value("timestamps", "false") == "true")
        self.chk_wav.setChecked(s.value("wav", "false") == "true")
        self._update_device_chip()
        self._apply_reading()
        mic = s.value("mic")
        if mic:
            idx = next((i for i, d in enumerate(self.mics) if d.description() == mic), -1)
            if idx >= 0:
                self.cb_mic.setCurrentIndex(idx)

    def _apply_reading(self):
        size = int(self.settings.value("reading_size", 16))
        serif = self.settings.value("reading_font", "sans") == "serif"
        self.text.set_reading(kit.FONTS["reading"] if serif else kit.FONTS["ui"], size)

    def _save_settings(self):
        s = self.settings
        if self.cb_model.currentData():
            s.setValue("model", self.cb_model.currentData())
        s.setValue("device", self.cb_device.currentData())
        s.setValue("language", self.cb_lang.currentData())
        s.setValue("precision", self.cb_prec.currentIndex())
        s.setValue("sensitivity", self.cb_sens.currentIndex())
        s.setValue("pause", self.sp_pause.value())
        s.setValue("prompt", self.ed_prompt.text())
        s.setValue("live", "true" if self.chk_live.isChecked() else "false")
        s.setValue("timestamps", "true" if self.chk_ts.isChecked() else "false")
        s.setValue("wav", "true" if self.chk_wav.isChecked() else "false")
        if self.mics and self.cb_mic.currentData() is not None:
            s.setValue("mic", self.mics[self.cb_mic.currentData()].description())

    @staticmethod
    def notebook_dir():
        """The notebook folder in Documents. Its name follows the interface language, but an existing notebook
        named in another language is used as is, so switching language never "loses" the notes."""
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        docs = docs or os.path.expanduser("~")
        names = [tr(NOTEBOOK_DIRNAME)] + all_translations(NOTEBOOK_DIRNAME)
        return next((os.path.join(docs, n) for n in names if os.path.isdir(os.path.join(docs, n))),
                    os.path.join(docs, names[0]))

    # --- notebook: courses and lectures -------------------------------------------------------
    def courses(self):
        """Available courses: the folders in the default folder plus the ones registered elsewhere."""
        found = subdirs(self.notebook_dir()) + [p for p in self.external_courses() if os.path.isdir(p)]
        return sorted(set(found), key=lambda p: (os.path.basename(p).lower(), p))

    def external_courses(self):
        try:
            with open(os.path.join(self.notebook_dir(), REGISTRY), encoding="utf-8") as f:
                paths = json.load(f).get("external", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [norm_path(p) for p in paths if isinstance(p, str) and p.strip()]

    def _register(self, path=None, forget=None):
        """Update the list of courses saved outside the default folder."""
        root = self.notebook_dir()
        paths = [p for p in self.external_courses() if p not in (path, forget)]
        if path and os.path.dirname(path) != root:
            paths.append(path)
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, REGISTRY), "w", encoding="utf-8") as f:
            json.dump({"external": paths}, f, ensure_ascii=False, indent=2)

    def forget_course(self, path):
        self._register(forget=path)
        self.course = self.lesson = None
        self.reload_tree()

    def reload_tree(self, select=None):
        """Reload courses and lectures from disk and select `select` (folder of a course or a lecture)."""
        select = select or self.lesson or self.course
        self.tree.blockSignals(True)
        self.tree.clear()
        target = None
        for course in self.courses():
            lessons = list_lessons(course)
            item = QTreeWidgetItem(self.tree)
            name = os.path.basename(course)
            item.setData(0, ROLE_KIND, ("course", course))
            item.setData(0, ROLE_INFO, {"name": name, "count": len(lessons), "color": color_for(name, {}),
                                        "missing": False})
            item.setToolTip(0, course)
            if course == select:
                target = item
            for lesson in lessons:
                child = QTreeWidgetItem(item)
                child.setData(0, ROLE_KIND, ("lesson", lesson["path"]))
                child.setData(0, ROLE_INFO, lesson)
                child.setToolTip(0, os.path.basename(lesson["path"]))
                if lesson["path"] == select:
                    target = child
            item.setExpanded(True)
        # courses in folders that cannot be found right now (external disk unplugged, folder moved…)
        for course in self.external_courses():
            if os.path.isdir(course):
                continue
            item = QTreeWidgetItem(self.tree)
            item.setData(0, ROLE_KIND, ("course", course))
            item.setData(0, ROLE_INFO, {"name": os.path.basename(course), "count": tr("not found"),
                                        "color": C["border"], "missing": True})
            item.setToolTip(0, tr("Folder not found: {path}").format(path=course))
            if course == select:
                target = item
        if target is None and self.tree.topLevelItemCount():  # nothing to reselect: last lecture
            last = self.tree.topLevelItem(0)
            target = last.child(last.childCount() - 1) if last.childCount() else last
        if target:
            self.tree.setCurrentItem(target)
        self.tree.blockSignals(False)
        self.on_tree_selection()

    def on_tree_selection(self):
        item = self.tree.currentItem()
        self.save_note()
        if not item:
            self.course = self.lesson = None
            self.show_empty(tr("Welcome to your notes"),
                            tr("Create a course for every subject: it will hold the lectures, numbered and dated, "
                               "and every lecture holds its transcripts, split by category."),
                            "＋  " + tr("Create your first course"), self.new_course)
            return
        kind, path = item.data(0, ROLE_KIND)
        if kind == "course" and item.data(0, ROLE_INFO).get("missing"):
            if os.path.isdir(path):  # it came back in the meantime (disk plugged in again)
                self.reload_tree(path)
                return
            self.course = self.lesson = None
            self.show_empty(os.path.basename(path),
                            tr("Folder not found:\n{path}\n\nIf it is on an external disk or in a synced folder, "
                               "connect it and select the course again. Otherwise you can remove it from the "
                               "list: no file is deleted.").format(path=pretty_path(path)),
                            tr("Remove from list"), lambda: self.forget_course(path))
            return
        if kind == "course":
            self.course, self.lesson = path, None
            self.show_empty(os.path.basename(path), tr("No lecture selected. Create a new one: number and date "
                                                       "are already suggested."),
                            "＋  " + tr("New lecture"), self.new_lesson)
            return
        self.course, self.lesson = os.path.dirname(path), path
        self.settings.setValue("lesson", path)
        self.stack.setCurrentIndex(1)
        self.reload_notes()

    def show_empty(self, title, text, action_text, action):
        self.stack.setCurrentIndex(0)
        self.show_note(None)
        self.notes = []
        self.empty_title.setText(title)
        self.empty_text.setText(text)
        self.empty_btn.setText(action_text)
        self.empty_action = action

    def update_header(self):
        info = parse_lesson(self.lesson)
        course = os.path.basename(self.course)
        number = f"{tr(LESSON_WORD)} {info['number']}" if info["number"] else tr(LESSON_WORD)
        self.lbl_eyebrow.setText(f"{course.upper()}  ·  {number.upper()}")
        self.lbl_title.setText(info["title"] or number)
        n = len(self.notes)
        count = tr("no transcripts") if not n else tr("1 transcript") if n == 1 else \
            tr("{n} transcripts").format(n=n)
        self.lbl_sub.setText(" · ".join(filter(None, (long_date(info["date"]), count))))

    def tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item or self.worker:
            return
        kind, path = item.data(0, ROLE_KIND)
        self.tree.setCurrentItem(item)
        menu = QMenu(self)
        if kind == "course" and item.data(0, ROLE_INFO).get("missing"):
            menu_action(menu, tr("Remove from list"), lambda: self.forget_course(path))
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if kind == "course":
            menu_action(menu, tr("New lecture…"), self.new_lesson)
            menu_action(menu, tr("Edit course…"), lambda: self.edit_course(path))
        else:
            menu_action(menu, tr("Edit lecture…"), self.edit_lesson)
            menu_action(menu, tr("Export to Markdown…"), self.export_lesson)
        menu_action(menu, tr("Open folder"), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        menu.addSeparator()
        menu_action(menu, tr("Move to trash…"), lambda: self.trash_folder(kind, path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def new_course(self):
        if self.worker:
            return
        dlg = CourseDialog(self, self.notebook_dir())
        if dlg.exec() != QDialog.Accepted:
            return
        path = dlg.target()
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, tr("New course"), tr("Cannot create {path}:\n{error}").format(path=path, error=e))
            return
        meta = course_meta(path) if dlg.adopt else {}
        meta.update({k: v for k, v in dlg.meta().items() if v or not dlg.adopt})
        write_course_meta(path, meta)
        self._register(path)
        self.reload_tree(path)

    def edit_course(self, path):
        dlg = CourseDialog(self, self.notebook_dir(), path, course_meta(path))
        if dlg.exec() != QDialog.Accepted:
            return
        new_path = dlg.target()
        if new_path != path:
            if os.path.dirname(new_path) != os.path.dirname(path) and QMessageBox.question(
                    self, tr("Edit course"), tr("Move the course with all its lectures to\n{path}?")
                    .format(path=new_path)) != QMessageBox.Yes:
                return
            self.save_note()
            try:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                shutil.move(path, new_path)  # also across disks
            except OSError as e:
                QMessageBox.warning(self, tr("Edit course"), tr("Cannot move the course:\n{error}").format(error=e))
                return
            self._register(new_path, forget=path)
            # stay on the same lecture, now in the new folder
            reopen = new_path + self.lesson[len(path):] if self.lesson and self.course == path else new_path
            self.course = self.lesson = self.note = None
        else:
            reopen = self.lesson or new_path
        write_course_meta(new_path, dlg.meta())
        self.reload_tree(reopen)

    @staticmethod
    def course_context(course):
        return course_meta(course)["context"] if course else ""

    def audio_dir(self):
        """Where to save the audio of the open lecture: the course audio folder, if set, otherwise the
        lecture folder."""
        base = course_meta(self.course)["audio"] if self.course else ""
        return os.path.join(base, os.path.basename(self.lesson)) if base else self.lesson

    def new_lesson(self):
        if self.worker:
            return
        courses = self.courses()
        if not courses:
            QMessageBox.information(self, tr("New lecture"), tr("Create a course first: lectures live inside a "
                                                                "course."))
            self.new_course()
            return
        dlg = LessonDialog(self, courses, self.course)
        if dlg.exec() != QDialog.Accepted:
            return
        course, number, date, title = dlg.values()
        path = os.path.join(course, lesson_dirname(number, date, title))
        if os.path.exists(path):
            QMessageBox.information(self, tr("New lecture"), tr("This lecture already exists: opening it."))
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, tr("New lecture"), tr("Cannot create {path}:\n{error}").format(path=path, error=e))
            return
        self.reload_tree(path)

    def edit_lesson(self):
        if not self.lesson or self.worker:
            return
        dlg = LessonDialog(self, self.courses(), self.course, parse_lesson(self.lesson))
        if dlg.exec() != QDialog.Accepted:
            return
        _, number, date, title = dlg.values()
        new_path = os.path.join(self.course, lesson_dirname(number, date, title))
        if new_path != self.lesson:
            if os.path.exists(new_path):
                QMessageBox.warning(self, tr("Edit lecture"), tr("A lecture with these details already exists."))
                return
            self.save_note()
            old_audio = self.audio_dir()
            try:
                os.rename(self.lesson, new_path)
                if old_audio != self.lesson and os.path.isdir(old_audio):  # the audio folder follows the lecture
                    new_audio = os.path.join(os.path.dirname(old_audio), os.path.basename(new_path))
                    if not os.path.exists(new_audio):
                        os.rename(old_audio, new_audio)
            except OSError as e:
                QMessageBox.warning(self, tr("Edit lecture"), tr("Cannot rename the lecture:\n{error}").format(error=e))
                self.reload_tree(self.lesson if os.path.isdir(self.lesson) else new_path)
                return
            self.note = None  # the transcript paths have changed
            self.lesson = new_path
        self.reload_tree(new_path)

    def trash_folder(self, kind, path):
        name = os.path.basename(path)
        question = (tr("Move the course “{name}” with all its lectures to the trash?") if kind == "course" else
                    tr("Move the lecture “{name}” with its transcripts to the trash?")).format(name=name)
        if QMessageBox.question(self, tr("Move to trash"), question + "\n"
                                + tr("You can restore it from the system trash.")) != QMessageBox.Yes:
            return
        self.save_note()
        self.note = None
        if not move_to_trash(path):
            QMessageBox.warning(self, tr("Move to trash"), tr("It could not be moved to the trash."))
            return
        if kind == "course":
            self._register(forget=path)
        self.course = os.path.dirname(path) if kind == "lesson" else None
        self.lesson = None
        self.reload_tree(self.course)

    # --- transcripts of the lecture ---------------------------------------------------------
    def reload_notes(self, select=None):
        """Reread the lecture transcripts and open `select` (or the one already open, or the last one)."""
        self.notes = list_notes(self.lesson) if self.lesson else []
        select = select or (self.note["path"] if self.note else None)
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        for note in self.notes:
            i = self.tabs.addTab("")
            self._update_tab(i, note)
        idx = next((i for i, n in enumerate(self.notes) if n["path"] == select), len(self.notes) - 1)
        self.tabs.setCurrentIndex(idx)
        self.tabs.blockSignals(False)
        self._fill_categories()
        self.show_note(self.notes[idx] if idx >= 0 else None)
        self.update_header()

    def _update_tab(self, i, note):
        text = f"{note['number']} · {note['category']}" if note["number"] else note["category"]
        if note["title"]:
            text += f" — {note['title']}"
        self.tabs.setTabText(i, text if len(text) <= 38 else text[:36] + "…")
        self.tabs.setTabIcon(i, dot_icon(color_for(note["category"])))
        self.tabs.setTabToolTip(i, os.path.basename(note["path"]))

    def _category_names(self):
        names = category_names()
        return names + sorted({n["category"] for n in self.notes} - set(names))

    def _fill_categories(self):
        current = self.cb_cat.currentText()
        self.cb_cat.blockSignals(True)
        self.cb_cat.clear()
        for name in self._category_names():
            self.cb_cat.addItem(dot_icon(color_for(name)), name)
        self._set_category(current)
        self.cb_cat.blockSignals(False)

    def _set_category(self, name):
        """Select the entry (so the colored dot shows too) or, if it is not listed, type the text."""
        idx = self.cb_cat.findText(name)
        if idx >= 0:
            self.cb_cat.setCurrentIndex(idx)
        else:
            self.cb_cat.setEditText(name)

    def _fill_note_menu(self):
        self.note_menu.clear()
        for name in self._category_names():
            menu_action(self.note_menu, name, lambda n=name: self.new_note(n), dot_icon(color_for(name)))
        self.note_menu.addSeparator()
        menu_action(self.note_menu, tr("Other category…"), self._new_note_custom)

    def _new_note_custom(self):
        name, ok = QInputDialog.getText(self, tr("New transcript"), tr("Category:"))
        if ok and safe_name(name):
            self.new_note(safe_name(name))

    def _default_category(self):
        return self.settings.value("category") or tr(DEFAULT_CATEGORY)

    def new_note(self, category):
        """Create an empty transcript in the lecture and open it. While recording, from here on the text
        goes into the new transcript: a way to split the lecture by topic without stopping the microphone."""
        if not self.lesson:
            return None
        self.save_note()
        if self.worker:
            self._close_note_file()
        number = next_number(list_notes(self.lesson))
        path = os.path.join(self.lesson, note_filename(number, category, ""))
        open(path, "a", encoding="utf-8").close()
        self.settings.setValue("category", category)
        self.reload_notes(path)
        if self.worker:
            self.rec_new_notes.add(path)
            self._open_note_file()
            self.partial_pos, self.last_end, self.new_session = 0, None, False
            self.set_info(tr("From here on the recording continues in “{category}”.").format(category=category))
        return self.note

    def on_tab_changed(self, i):
        if 0 <= i < len(self.notes):
            self.show_note(self.notes[i])

    def show_note(self, note):
        """Open a transcript in the sheet (None = empty sheet, no transcript)."""
        if note and self.note and note["path"] == self.note["path"]:
            self.note = note  # same transcript: keep the text (and the preview in progress)
        else:
            self.save_note()
            self.note = note
            self.loading = True
            self.text.set_text(read_text(note["path"]) if note else "")
            self.loading = False
            self.dirty = False
        recording = self.worker is not None
        self.text.setReadOnly(note is None)
        self.text.setPlaceholderText(
            tr("Press “Record” (Ctrl+R) and the text will appear here, line after line.") if note or not self.lesson
            else tr("No transcript in this lecture yet: press “Record” or “＋ New transcript”."))
        for w in self.meta_widgets:
            w.setEnabled(note is not None)
        self.btn_delete_note.setEnabled(note is not None and not recording)
        self.cb_cat.blockSignals(True)
        self._set_category(note["category"] if note else "")
        self.cb_cat.blockSignals(False)
        self.ed_title.setText(note["title"] if note else "")

    def on_text_edited(self):
        if self.loading or not self.note:
            return
        self.dirty = True
        self.save_timer.start()

    def save_note(self):
        """Autosave of manual edits. While recording, the file, which normally only receives new sentences
        at the end, is rewritten with the whole final text (preview excluded). Return True if it wrote."""
        self.save_timer.stop()
        if not self.dirty or not self.note:
            return False
        if self.worker:
            if not self.txt_file or self.txt_path != self.note["path"]:
                return False
            cur = QTextCursor(self.text.document())
            cur.setPosition(self.partial_pos, QTextCursor.MoveMode.KeepAnchor)
            text = cur.selectedText().replace(" ", "\n").replace(" ", "\n")
        else:
            text = self.text.toPlainText()
        tmp = self.note["path"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.note["path"])
        if self.worker:  # the file open for appending was replaced: reopen the new one
            self.txt_file.close()
            self._open_note_file()
        self.dirty = False
        return True

    def apply_note_meta(self):
        """Category or title changed: rename the file (works while recording too)."""
        note = self.note
        if not note:
            return
        category = safe_name(self.cb_cat.currentText()) or tr(OTHER_CATEGORY)
        title = safe_name(self.ed_title.text())
        if (category, title) == (note["category"], note["title"]):
            return
        number = note["number"] or next_number(self.notes)
        new_path = os.path.join(self.lesson, note_filename(number, category, title))
        if os.path.exists(new_path):
            QMessageBox.warning(self, tr("Transcript"), tr("A transcript with this name already exists."))
            self.show_note(note)
            return
        self.save_note()
        os.rename(note["path"], new_path)
        if self.txt_path == note["path"]:
            self.txt_path = new_path  # the file open for recording stays valid even if renamed
        if note["path"] in self.rec_new_notes:
            self.rec_new_notes.discard(note["path"])
            self.rec_new_notes.add(new_path)
        note.update(path=new_path, number=number, category=category, title=title)
        self.settings.setValue("category", category)
        self._update_tab(self.notes.index(note), note)
        self._fill_categories()
        self._set_category(category)

    def delete_note(self):
        if not self.note or self.worker:
            return
        name = os.path.basename(self.note["path"])
        title = tr("Delete transcript")
        if QMessageBox.question(self, title, tr("Move “{name}” to the trash?").format(name=name)) != QMessageBox.Yes:
            return
        if not move_to_trash(self.note["path"]):
            if QMessageBox.question(self, title, tr("The system trash is not available.\n\nDelete “{name}” "
                                                    "permanently?").format(name=name)) != QMessageBox.Yes:
                return
            os.remove(self.note["path"])
        self.dirty = False
        self.note = None
        self.reload_notes()

    def export_lesson(self):
        if not self.lesson:
            return
        self.save_note()
        info = parse_lesson(self.lesson)
        number = f"{tr(LESSON_WORD)} {info['number']}" if info["number"] else tr(LESSON_WORD)
        lines = [f"# {os.path.basename(self.course)} — {number}" + (f": {info['title']}" if info["title"] else ""),
                 ""]
        if info["date"].isValid():
            lines += [f"*{long_date(info['date'])}*", ""]
        for note in list_notes(self.lesson):
            lines += [f"## {note['category']}" + (f" — {note['title']}" if note["title"] else ""), "",
                      read_text(note["path"]).strip() or tr("*(empty)*"), ""]
        path, _ = QFileDialog.getSaveFileName(self, tr("Export lecture"),
                                              os.path.join(self.course, os.path.basename(self.lesson) + ".md"),
                                              tr("Markdown (*.md);;Text (*.txt)"))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines).rstrip() + "\n")
            self.set_info(tr("Lecture exported to {path}").format(path=path))

    # --- recording -------------------------------------------------------------------------
    def toggle_recording(self):
        if self.file_queue is not None:
            self.stop_file_job()
        elif self.source:
            self.stop_recording()
        elif not self.worker:
            self.start_recording()

    def _check_model(self):
        if self.cb_model.currentData():
            return True
        QMessageBox.warning(self, tr("No model"), tr(
            "No Whisper model found in {folder}.\n\n"
            "Download one with OVMS Manager → Explore (e.g. whisper-large-v3-turbo-int8-ov), or from "
            "huggingface.co/OpenVINO into that folder, then press F5.").format(folder=MODELS_DIR))
        return False

    def start_recording(self):
        if not self.lesson:
            QMessageBox.information(self, tr("Record"), tr("Choose or create a lecture first: the transcript will "
                                                           "be saved inside it."))
            return
        if not self._check_model():
            return
        model = self.cb_model.currentData()
        if not self.mics:
            QMessageBox.warning(self, tr("No microphone"), tr("No audio input device available."))
            return
        mic = self.mics[self.cb_mic.currentData()]
        self._save_settings()

        fmt = QAudioFormat()
        fmt.setSampleRate(SR)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not mic.isFormatSupported(fmt):
            fmt = mic.preferredFormat()
        self.fmt, self.resampler, self.leftover = fmt, Resampler(fmt.sampleRate()), b""

        threshold, factor = self.cb_sens.currentData()
        if not isinstance(self.vad, SileroVAD):
            try:
                self.set_info(tr("Preparing voice detection (Silero VAD)…"))
                QApplication.processEvents()
                self.vad = SileroVAD()
            except Exception as e:  # noqa: BLE001 - offline on first start: use the volume threshold
                self.vad = EnergyVAD(factor)
                QMessageBox.warning(self, tr("Voice detection"), tr(
                    "Silero VAD is not available ({error}).\nUsing a volume threshold, which is less reliable: "
                    "if it loses words try Sensitivity “High”.").format(error=e))
        if isinstance(self.vad, EnergyVAD):
            self.vad.factor, threshold = factor, 0.5
        self.vad.reset()

        # record into the open transcript (text is appended at the end) or into a new one
        self.save_note()
        self.rec_new_notes = set()
        if not self.note:
            self.new_note(self._default_category())
            self.rec_new_notes.add(self.note["path"])
        self.txt_path = self.note["path"]
        self._open_note_file()
        self.wav = None
        if self.chk_wav.isChecked():
            try:
                os.makedirs(self.audio_dir(), exist_ok=True)
                name = f"{safe_name(tr('Recording'))} {datetime.now().strftime('%H-%M-%S')}.wav"
                self.wav_path = os.path.join(self.audio_dir(), name)
                self.wav = wave.open(self.wav_path, "wb")
            except OSError as e:
                if self._close_files():  # the transcript just created was empty: remove its tab too
                    self.reload_notes()
                QMessageBox.warning(self, tr("Audio"), tr(
                    "Cannot save the audio in {path}:\n{error}\n\nCheck the course audio folder (Edit course) or "
                    "turn off “Also save the audio”.").format(path=self.audio_dir(), error=e))
                return
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(SR)

        self.buf = AudioBuffer()
        self.samples = 0
        self.mic, self.mic_restarts, self.silent_frames = mic, 0, 0
        self.wave_time = 0.0
        self.wave.clear()
        if not self._open_source():
            if self._close_files():
                self.reload_notes()
            QMessageBox.warning(self, tr("Microphone"), tr("Cannot open {device}.").format(device=mic.description()))
            return

        # the existing text stays: the new session starts with a new paragraph
        self.partial_pos = self.text.document().characterCount() - 1
        self.last_end = None
        self.new_session = self.partial_pos > 0

        self._start_worker(Transcriber(self.buf, self._session_opts(model, threshold), self.pipe_cache, self))
        self._set_recording_ui(True)
        self.clock.start()
        msg = tr("Recording into “{category}” · voice detected with {vad}").format(category=self.note["category"],
                                                                                   vad=tr(self.vad.name))
        if self.wav:
            msg += " · " + tr("audio in {path}").format(path=self.audio_dir())
        if "turbo" not in os.path.basename(model):
            msg += " · " + tr("Note: non-turbo models are ~4 times slower and can fall behind when used live.")
        self.set_info(msg)

    def _session_opts(self, model, threshold):
        prompt = ". ".join(filter(None, (self.course_context(self.course), self.ed_prompt.text().strip())))
        return {"model": model, "device": self.cb_device.currentData(), "language": self.cb_lang.currentData(),
                "beams": self.cb_prec.currentData(), "threshold": threshold,
                "pause": self.sp_pause.value(), "prompt": prompt, "live": self.chk_live.isChecked()}

    def _start_worker(self, worker):
        self.worker = worker
        worker.status.connect(self.pill.set_state)
        worker.partial.connect(self.on_partial)
        worker.final.connect(self.on_final)
        worker.info.connect(self.set_info)
        worker.device_changed.connect(self._update_device_chip)
        worker.failed.connect(self.on_failed)
        worker.pipe_ready.connect(lambda key, pipe: setattr(self, "pipe_cache", (key, pipe)))
        worker.finished.connect(self.on_worker_done)
        worker.start()

    # --- transcribing already recorded files -------------------------------------------------
    def transcribe_files(self):
        if self.worker:
            return
        title = tr("Transcribe audio files")
        if not self.lesson:
            QMessageBox.information(self, title, tr("Choose or create a lecture first: the transcripts will be "
                                                    "saved inside it."))
            return
        if not self._check_model():
            return
        start = next((d for d in (self.audio_dir(), self.lesson) if os.path.isdir(d)), self.lesson)
        exts = " ".join(f"*.{e}" for e in ("wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "wma", "webm",
                                            "mp4", "mkv", "mov"))
        paths, _ = QFileDialog.getOpenFileNames(self, title, start,
                                                tr("Audio and video ({patterns});;All files (*)").format(patterns=exts))
        if not paths:
            return
        if not shutil.which("ffmpeg") and any(not p.lower().endswith(".wav") for p in paths):
            QMessageBox.warning(self, title, tr("Files other than .wav need ffmpeg: {hint}.").format(hint=ffmpeg_hint()))
            return
        self._save_settings()
        self.file_queue = sorted(paths)
        self.file_total, self.file_done, self.stopping_file = len(paths), [], False
        self._next_file()

    def _next_file(self):
        """Transcribe the next queued file into a new transcript of the lecture."""
        path = self.file_queue.pop(0)
        self.save_note()
        category = self._default_category()
        number = next_number(list_notes(self.lesson))
        title = safe_name(os.path.splitext(os.path.basename(path))[0])
        note_path = os.path.join(self.lesson, note_filename(number, category, title))
        open(note_path, "a", encoding="utf-8").close()
        self.reload_notes(note_path)
        self.rec_new_notes = {note_path}  # if no text comes out of the file, the empty transcript is deleted
        self._open_note_file()
        self.partial_pos, self.last_end, self.new_session = 0, None, False
        self.samples = 0
        self.buf = None
        self.wave.hide()  # the waveform is for the microphone: a file shows its progress instead
        self.level_bar.show()
        self._start_worker(FileTranscriber(path, self._session_opts(self.cb_model.currentData(), 0.5),
                                           self.pipe_cache, self))
        self._set_recording_ui(True)
        self.act_rec.setText(tr("Stop transcribing the file"))
        self.clock.start()
        n = self.file_total - len(self.file_queue)
        which = " " + tr("({n} of {total})").format(n=n, total=self.file_total) if self.file_total > 1 else ""
        self.set_info(tr("Transcribing {file}{which} into “{category}”…").format(
            file=os.path.basename(path), which=which, category=category))

    def stop_file_job(self):
        """Stop the current file (text already transcribed stays) and discard the queued ones."""
        self.file_queue, self.stopping_file = [], True
        if self.worker:
            self.worker.cancel()
            self.btn_rec.setEnabled(False)
            self.btn_rec.setText(tr("Stopping…"))

    def _file_finished(self, worker, removed):
        """Called by on_worker_done when a file ends: move to the next one or summarize."""
        self.wave.show()
        self.level_bar.hide()
        failed = self.info_kind == "err"
        stopped = self.stopping_file and not failed
        if worker.feed_error:
            outcome = tr("error ({reason})").format(reason=worker.feed_error)
        else:
            outcome = tr("error") if failed else tr("stopped") if stopped else \
                tr("no speech found") if removed else tr("done")
        self.file_done.append((os.path.basename(worker.path), outcome, failed or bool(worker.feed_error)))
        # an unreadable file does not stop the others; a model error does (it would repeat on all of them)
        if self.file_queue and not stopped and (not failed or worker.feed_error):
            self._next_file()
            return
        self.file_queue = None
        if failed and not worker.feed_error:
            return  # keep the model error message visible
        summary = (tr("Stopped.") if stopped else tr("File transcription finished.")) + " " + \
            " · ".join(f"{name}: {outcome}" for name, outcome, _ in self.file_done)
        errors = any(error for _, _, error in self.file_done)
        self.set_info(summary, "err" if errors else None)

    def _set_recording_ui(self, recording):
        """While recording you stay in the lecture: no switching lecture or tab, but you can create new
        transcripts (to split the text) and change category and title."""
        for w in self.settings_widgets + (self.tree, self.tabs, self.btn_new_lesson, self.btn_new_course,
                                          self.act_edit_lesson, self.btn_delete_note, self.act_lesson,
                                          self.btn_file, self.act_file):
            w.setEnabled(not recording)
        if not recording:
            self.btn_delete_note.setEnabled(self.note is not None)
        self.text.setReadOnly(self.note is None)
        self.text.set_undo(not recording)
        self.btn_rec.setEnabled(True)
        self.btn_rec.setText(tr("Stop") if recording else tr("Record"))
        self.act_rec.setText(tr("Stop recording") if recording else tr("Start recording"))
        self.btn_rec.setObjectName("danger" if recording else "primary")
        self.btn_rec.set_icon_name("stop" if recording else "record")
        self.btn_rec.setStyle(self.btn_rec.style())  # re-apply the style after changing objectName

    def _open_note_file(self):
        self.txt_path = self.note["path"]
        self.txt_file = open(self.txt_path, "a", encoding="utf-8")
        self.file_empty = os.path.getsize(self.txt_path) == 0

    def _close_note_file(self):
        """Close the file being recorded into; if it was created for this recording and stayed empty,
        delete it. Return True if it was deleted."""
        if not self.txt_file:
            return False
        self.txt_file.close()
        self.txt_file = None
        if self.file_empty and self.txt_path in self.rec_new_notes and os.path.exists(self.txt_path):
            os.remove(self.txt_path)
            if self.note and self.note["path"] == self.txt_path:
                self.note = None
            return True
        return False

    def _open_source(self):
        """Open (or reopen) the microphone stream. Return False on failure."""
        if self.source:
            self.source.stop()
            self.source.deleteLater()
        self.source = QAudioSource(self.mic, self.fmt, self)
        # 2 s buffer: if the interface freezes for a moment no audio is lost
        self.source.setBufferSize(int(2 * self.fmt.sampleRate() * self.fmt.bytesPerFrame()))
        self.io = self.source.start()
        if self.io is None or self.source.error().name != "NoError":
            self.source = self.io = None
            return False
        self.io.readyRead.connect(self.on_audio)
        self.last_audio = time.time()
        return True

    def _watchdog(self):
        """Some systems (PulseAudio/PipeWire, device suspend) stop sending audio without reporting an
        error: if nothing arrives for a few seconds, the microphone is reopened."""
        if not self.source or time.time() - self.last_audio < MIC_STALL_S:
            return
        state = self.source.state().name
        self.mic_restarts += 1
        log.info("microphone silent for %.1f s (state %s): reopening it", time.time() - self.last_audio, state)
        self.leftover = b""
        if self._open_source():
            msg = tr("The microphone had stopped sending audio (state: {state}): reopened automatically "
                     "({count}×).").format(state=state, count=self.mic_restarts)
            self.set_info(msg, "warn")
        else:
            msg = tr("The microphone stopped sending audio and cannot be reopened: press Stop and start "
                     "recording again.")
            self.set_info(msg, "err")
            self.stop_recording()

    def stop_recording(self):
        if self.source:
            self.source.stop()
            self.source = self.io = None
        self.level = 0
        self.vad_voice = False
        if self.worker and self.worker.isRunning():
            self.worker.finish()
            self.btn_rec.setEnabled(False)
            self.btn_rec.setText(tr("Finishing…"))
        else:
            self.on_worker_done()

    def on_audio(self):
        if not self.io:
            return
        raw = self.leftover + bytes(self.io.readAll())
        usable = len(raw) // self.fmt.bytesPerFrame() * self.fmt.bytesPerFrame()
        self.leftover = raw[usable:]
        x = self.resampler(to_float32(raw[:usable], self.fmt))
        if not len(x):
            return
        self.last_audio = time.time()
        probs = self.vad(x)
        self.buf.append(x, probs)
        self.samples += len(x)
        if len(probs):
            self.vad_voice = bool(probs[-3:].max() >= 0.5)
            self.silent_frames = self.silent_frames + len(probs) if probs.max() < 0.2 else 0
            if self.silent_frames >= VAD_RESET_S * FPS:  # long silence: start "clean" again
                self.vad.reset_state()
                self.silent_frames = 0
        if self.wav:
            self.wav.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        db = 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
        self.level = max(min(100.0, max(0.0, (db + 60) / 60 * 100)), self.level - 6)
        now = time.time()
        if now - self.wave_time >= 0.05:  # ~20 bars per second, whatever the size of the audio chunks
            self.wave.push(self.level / 100, self.vad_voice)
            self.wave_time = now

    def _tick(self):
        self._watchdog()
        self.lbl_time.setText(timestamp(self.samples / SR))
        if isinstance(self.worker, FileTranscriber):
            done, total = self.worker.done, self.worker.duration
            self.lbl_time.setText(timestamp(done))
            text = f"/ {timestamp(total)} · {min(100, 100 * done / total):.0f}%" if total else ""
            busy = self.worker.busy
            if busy:  # a block can take minutes: the timer in the pill shows it is working
                elapsed = int(time.time() - busy[1])
                self.pill.state = (tr("Transcribing… {time}").format(time=f"{elapsed // 60}:{elapsed % 60:02d}"),
                                   self.pill.state[1])
                self.pill._render()
            self.level_bar.setValue(int(min(100, 100 * done / total)) if total else 0)
            self.lbl_backlog.setText(f"<span style='color:{C['muted']}'>{text}</span>")
        elif self.worker and self.buf:
            backlog = (self.samples - self.buf.offset) / SR
            color = C["warn"] if backlog > MAX_SEGMENT_S + 5 else C["muted"]
            text = tr("to transcribe: {seconds} s").format(seconds=f"{backlog:.0f}")
            busy = self.worker.busy
            if busy and time.time() - busy[1] > 3:  # the model is stuck on an operation: say so
                text += " · " + tr("model busy for {seconds} s ({task})").format(
                    seconds=f"{time.time() - busy[1]:.0f}", task=busy[0])
                color = C["warn"]
            self.lbl_backlog.setText(f"<span style='color:{color}'>{text}</span>")
        else:
            self.lbl_backlog.setText("")

    def on_failed(self, msg):
        self.set_info(msg, "err")
        if self.source:
            self.source.stop()
            self.source = self.io = None

    def on_worker_done(self):
        if self.source:  # the thread ended by itself (error): stop the microphone too
            self.source.stop()
            self.source = self.io = None
        self.on_partial("")
        self.save_note()  # corrections made while recording and not yet saved
        worker, self.worker = self.worker, None
        self.clock.stop()
        self._tick()
        removed = self._close_files()
        self._update_device_chip()
        self._set_recording_ui(False)
        self.pill.set_state(tr("Ready"), "muted", False)
        if removed:
            self.reload_notes()
        else:
            self.update_header()
        if isinstance(worker, FileTranscriber) and self.file_queue is not None:
            self._file_finished(worker, removed)

    def _close_files(self):
        if self.wav:
            self.wav.close()
            self.wav = None
        if not self.txt_file:
            return False
        removed = self._close_note_file()
        if self.info_kind != "err":  # don't hide an error message
            self.set_info(tr("Recording finished.") if removed else
                          tr("Transcript saved in {path}").format(path=self.txt_path))
        return removed

    # --- text ----------------------------------------------------------------------------------
    def _separator(self, start=None):
        if self.partial_pos == 0:
            return ""
        if self.new_session:
            return "\n\n"
        if self.chk_ts.isChecked():
            return "\n"
        if start is not None and self.last_end is not None and start - self.last_end > PARAGRAPH_GAP_S:
            return "\n\n"
        return " "

    @property
    def partial_pos(self):
        """Where the final text ends and the preview begins. It is a QTextCursor and not a number: if the
        text before it is corrected by hand, Qt moves it accordingly."""
        return self.boundary.position() if self.boundary else 0

    @partial_pos.setter
    def partial_pos(self, pos):
        doc = self.text.document()
        if self.boundary is None or self.boundary.document() is not doc:
            self.boundary = QTextCursor(doc)
        self.boundary.setPosition(min(pos, doc.characterCount() - 1))

    def _partial_cursor(self):
        doc = self.text.document()
        cur = QTextCursor(doc)
        cur.setPosition(min(self.partial_pos, doc.characterCount() - 1))
        cur.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        return cur

    def _keep_scrolled(self, fn):
        """Update the transcribed text staying at the bottom if it was being followed. It is not a manual
        edit: it must not be saved, and it must not move the cursor of whoever is correcting."""
        bar = self.text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.loading = True
        try:
            fn()
        finally:
            self.loading = False
        if at_bottom:
            bar.setValue(bar.maximum())

    def on_partial(self, text):
        def update():
            start = self.partial_pos
            cur = self._partial_cursor()
            cur.removeSelectedText()
            if text:
                cur.insertText(self._separator() + text, self.f_partial)
            self.partial_pos = start  # Qt would have moved the boundary after the inserted text
        self._keep_scrolled(update)

    def on_final(self, text, start, end):
        sep = self._separator(start)
        stamp = f"[{timestamp(start)}] " if self.chk_ts.isChecked() else ""

        def update():
            cur = self._partial_cursor()
            cur.removeSelectedText()
            cur.insertText(sep, self.f_text)
            if stamp:
                cur.insertText(stamp, self.f_stamp)
            cur.insertText(text, self.f_text)
            self.partial_pos = cur.position()
        self._keep_scrolled(update)
        self.last_end, self.new_session = end, False

        saved = self.dirty and self.save_note()  # edited by hand: rewrite the whole file, new sentence included
        if self.txt_file and not saved:  # written right away: if the app crashes the text is already there
            file_sep = "" if self.file_empty else ("\n\n" if sep == "\n\n" else sep)
            self.txt_file.write(file_sep + stamp + text)
            self.txt_file.flush()
            self.file_empty = False

    def copy_text(self):
        QApplication.clipboard().setText(self.text.toPlainText())
        self.set_info(tr("Text copied to the clipboard."))

    def open_folder(self):
        path = self.lesson or self.course or self.notebook_dir()
        os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def closeEvent(self, event):
        if self.worker:
            ok = QMessageBox.question(self, tr("Recording in progress"),
                                      tr("A recording is in progress: the final text is already saved, the last "
                                         "sentence will be lost. Quit?"), QMessageBox.Yes | QMessageBox.No)
            if ok != QMessageBox.Yes:
                event.ignore()
                return
            if self.source:
                self.source.stop()
            self.worker.cancel()
            self.worker.wait(5000)
            self.save_note()  # while the worker exists it saves only the final text, without the preview
        self._close_files()
        self.save_note()
        self.settings.setValue("geometry", self.saveGeometry())
        event.accept()
        if self.worker and self.worker.isRunning():
            os._exit(0)  # a generate() running in native code cannot be interrupted

def app_qss(t):
    """Lectern's own rules, on top of the kit's base style. Calm layout: the sidebar is a notch dimmer than the
    page and separated only by color; the sheet is a plain surface; the recorder dock is the only floating
    element."""
    r = kit.RADIUS
    return kit.base_qss(t) + f"""
QMainWindow, #root, #mainArea, QDialog {{ background: {t['bg']}; }}
#sidebar {{ background: {t['sidebar']}; border: none; }}
#brand {{ font-size: 20px; font-weight: 750; }}
#brandSub {{ color: {t['muted']}; font-size: 12px; }}
QTreeWidget#tree {{ background: transparent; border: none; outline: none; }}
#eyebrow {{ color: {t['accent_text']}; font-size: 11px; font-weight: 700; letter-spacing: 0.6px; }}
#lessonTitle {{ font-size: 26px; font-weight: 750; }}
#emptyTitle {{ font-size: 22px; font-weight: 750; }}
#menuHeader {{ color: {t['muted']}; font-size: 11px; font-weight: 700; padding: 6px 12px 2px 12px; }}
#clock {{ font-size: 15px; font-weight: 650; }}
#sheet {{ background: {t['surface']}; border: none; border-radius: {r['xl']}px; }}
#sheetLine {{ background: {t['border']}; margin: 0 16px; }}
#dock {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 20px; }}
QTextEdit#note {{ background: transparent; border: none; selection-background-color: {t['accent_soft']};
    selection-color: {t['text']}; }}
QLineEdit#noteTitle {{ font-size: 14px; font-weight: 600; border-color: transparent; background: transparent; }}
QLineEdit#noteTitle:hover {{ border-color: {t['border']}; }}
QLineEdit#noteTitle:focus {{ border-color: {t['focus']}; background: {t['surface']}; }}
QTabBar::tab {{ background: transparent; border: none; border-radius: 15px; padding: 6px 14px; margin-right: 4px;
    color: {t['muted']}; }}
QTabBar::tab:selected {{ background: {t['surface']}; color: {t['text']}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ background: {t['hover']}; color: {t['text']}; }}
QTabBar::tab:disabled {{ color: {t['faint']}; }}
QTabBar::tab:selected:disabled {{ color: {t['text']}; }}
QTabBar QToolButton {{ background: {t['bg']}; border: none; }}
QSplitter::handle {{ background: {t['sidebar']}; }}
"""


def single_instance(app):
    """If the app is already open, bring it to the front and return None; otherwise return the server
    that will receive the requests of later instances."""
    name = f"{APP_ID}-{os.getuid()}"
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return None
    QLocalServer.removeServer(name)  # leftover of an instance that crashed
    server = QLocalServer(app)
    server.listen(name)
    return server

def missing_dependencies():
    missing = []
    for module, package in (("openvino_genai", "openvino-genai"), ("openvino", "openvino")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing

def init_theme(app):
    """Fusion style, bundled fonts and the light/dark theme chosen in the settings (default: follow the system)."""
    global THEME
    settings = QSettings(APP_ID, APP_ID)
    app.setStyle("Fusion")
    kit.load_fonts(app)
    motion = settings.value("reduce_motion")
    StatusPill.reduce_motion = kit.reduce_motion_default() if motion is None else motion == "true"
    THEME = kit.ThemeManager(app, ACCENT, settings.value("theme", "system") or "system", app_qss)
    return THEME


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName(APP_ID)
    setup_language(app, QSettings(APP_ID, APP_ID).value("ui_language", "") or "")
    app.setWindowIcon(QIcon.fromTheme(APP_ID, QIcon(os.path.join(APP_DIR, f"{APP_ID}.svg"))))
    init_theme(app)

    server = single_instance(app)
    if server is None:
        sys.exit(0)
    missing = missing_dependencies()
    if missing:
        QMessageBox.critical(None, APP_NAME, tr(
            "Missing Python packages: {packages}.\n\nRun {installer} again to repair the environment, or:\n"
            "{python} -m pip install {pip_args}").format(packages=", ".join(missing),
                                                         installer=os.path.join(APP_DIR, "install.sh"),
                                                         python=sys.executable, pip_args=" ".join(missing)))
        sys.exit(1)

    win = LiveWindow()

    def bring_to_front():
        conn = server.nextPendingConnection()
        if conn:
            conn.close()
        win.setWindowState(win.windowState() & ~Qt.WindowMinimized)
        win.show()
        win.raise_()
        win.activateWindow()

    server.newConnection.connect(bring_to_front)
    win.show()
    sys.exit(app.exec())

# Required guard: openvino-genai uses multiprocessing (see transcribe.py)
if __name__ == "__main__":
    main()
