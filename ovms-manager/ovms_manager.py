#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""OVMS Manager — a desktop front end for OpenVINO Model Server, the Hugging Face OpenVINO catalog and Optimum."""
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone

import requests
from PySide6.QtCore import (QAbstractTableModel, QLibraryInfo, QLocale, QModelIndex, QPropertyAnimation, QRectF, QSize,
                            QSortFilterProxyModel, Qt, QThread, QTimer, QTranslator, QUrl, Signal, Property,
                            QEasingCurve)
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate,
                               QTableView, QVBoxLayout, QWidget, QListWidget, QListWidgetItem, QDialog,
                               QScrollArea)

APP_ID = "ovms-manager"  # = name of the .desktop file created by install.sh
APP_VERSION = "1.0.0"    # compared with the latest GitHub release for update notifications
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(APP_DIR, "ui_kit.py")):  # running from the project: the kit is in ../common
    sys.path.insert(0, os.path.join(os.path.dirname(APP_DIR), "common"))
import ui_kit as kit  # noqa: E402  (shared UI kit of the ecosystem: themes, fonts, icons, command palette)
# Folder holding the OpenVINO models (shared with Lectern). Override with $OPENVINO_MODELS_DIR.
MODELS_DIR = os.path.abspath(os.path.expanduser(os.environ.get("OPENVINO_MODELS_DIR") or "~/models"))
_HOME = os.path.expanduser("~")
# shown in the UI with the home folder abbreviated to ~
MODELS_LABEL = "~" + MODELS_DIR[len(_HOME):] if MODELS_DIR.startswith(_HOME + os.sep) else MODELS_DIR
DATA_DIR = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ovms_manager")
CACHE_FILE = os.path.join(CACHE_DIR, "hf_catalog.json")
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
CACHE_TTL = 6 * 3600
HF_API = "https://huggingface.co/api/models"
HF_EXPAND = ["downloads", "likes", "lastModified", "pipeline_tag", "tags", "library_name"]

OVMS_IMAGE = "docker.io/openvino/model_server:latest-gpu"
CONTAINER_NAME = "ovms-server"
FALLBACK_MODEL_NAME = "ov_model"   # served name when the folder name gives nothing usable
OVMS_PORT = 8000
# 127.0.0.1 and not "localhost": localhost resolves to ::1 (IPv6) first, and rootless podman's port
# forwarding resets IPv6 connections ("connection reset"), while IPv4 works.
OVMS_HOST = "127.0.0.1"
OVMS_URL = f"http://{OVMS_HOST}:{OVMS_PORT}"
OPEN_WEBUI_URL = os.environ.get("OPEN_WEBUI_URL") or "http://localhost:3000"


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------
# User-visible text is written in English and translated through locale/<language>.json, a plain
# {"English text": "translation"} map. N_() only marks text defined before the language is known; it is
# translated with tr() where it is shown. tools/check_translations.py checks that the catalogs are complete.
def N_(text):
    return text


_catalog = {}


def tr(text):
    return _catalog.get(text, text)


def setup_language(app, choice):
    """Load the catalog for `choice` ('' = system language) and Qt's own translations (standard buttons and
    dialogs). Return the language code in use."""
    global _catalog
    lang = choice or QLocale.system().name().split("_")[0]
    try:
        with open(os.path.join(APP_DIR, "locale", f"{lang}.json"), encoding="utf-8") as f:
            _catalog = json.load(f)
    except (OSError, ValueError):
        _catalog, lang = {}, "en"
    QLocale.setDefault(QLocale(lang) if choice else QLocale.system())
    qt = QTranslator(app)
    if lang != "en" and qt.load(f"qtbase_{lang}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt)
    return lang


UI_LANGUAGES = [("", N_("System language")), ("en", "English"), ("it", "Italiano")]


# ---------------------------------------------------------------------------
# Container runtime: Podman or Docker
# ---------------------------------------------------------------------------
def detect_runtime():
    """'podman' or 'docker' ($OVMS_CONTAINER_RUNTIME wins, then Podman, then Docker); None if neither."""
    forced = os.environ.get("OVMS_CONTAINER_RUNTIME", "").strip()
    for name in ([forced] if forced else ["podman", "docker"]):
        if shutil.which(name):
            return name
    return None


RUNTIME = detect_runtime()
RUNTIME_NAME = (RUNTIME or "podman").capitalize()
# Hostname under which a containerized Open WebUI reaches this machine. With Docker on Linux, Open WebUI
# must be started with --add-host=host.docker.internal:host-gateway (as its own docs suggest).
HOST_ALIAS = "host.docker.internal" if RUNTIME == "docker" else "host.containers.internal"
CONTAINER_URL = f"http://{HOST_ALIAS}:{OVMS_PORT}/v3"
# Extra `run` options for every OVMS container, e.g. "--group-add keep-groups" for GPU access with rootless
# Podman on distributions where the render node is only accessible through a supplementary group
EXTRA_RUN_ARGS = shlex.split(os.environ.get("OVMS_EXTRA_RUN_ARGS", ""))
RUNTIME_HINT = N_("Neither Podman nor Docker was found. Install one of them, e.g.  sudo apt install podman  ·  "
                   "sudo dnf install podman  ·  sudo pacman -S podman  ·  sudo zypper install podman")


def runtime_cmd(*args):
    return [RUNTIME or "podman", *args]


def device_groups(flags):
    """--group-add options for the groups owning the device nodes in `flags` (render, video, accel…).
    Needed with Docker, where the container runs with the user's uid but not with its groups."""
    gids = set()
    for i, flag in enumerate(flags[:-1]):
        if flag != "--device":
            continue
        node = flags[i + 1].split(":")[0]
        for path in (glob.glob(os.path.join(node, "*")) if os.path.isdir(node) else [node]):
            try:
                gids.add(os.stat(path).st_gid)
            except OSError:
                pass
    return [x for gid in sorted(gids) for x in ("--group-add", str(gid))]


def container_run(*args, devices=(), detach=False, name=None, ports=(), as_owner=False):
    """`<runtime> run` command for the OVMS image with ~/models mounted on /models.
    Volumes use the shared SELinux label (:z): with the private one (:Z) every new container would
    relabel ~/models and take access away from the server container that is already running.
    With Docker the container runs as the current user, so the files it writes in ~/models stay yours
    (rootless Podman already maps them to you)."""
    cmd = runtime_cmd("run", "-d" if detach else "--rm")
    if name:
        cmd += ["--name", name]
    for port in ports:
        cmd += ["-p", port]
    cmd += ["-v", f"{MODELS_DIR}:/models:z", *devices]
    if RUNTIME == "docker":
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}", *device_groups(list(devices))]
    elif as_owner:
        # rootless Podman: root in the container is your user on the host, so it can rewrite config.json whoever
        # created it (you, or the container user of an earlier run); the image's own user could not
        cmd += ["--user", "0:0"]
    return cmd + EXTRA_RUN_ARGS + [OVMS_IMAGE, *args]


# ---------------------------------------------------------------------------
# Optimum (model conversion)
# ---------------------------------------------------------------------------
# Runs optimum-cli removing from sys.path the entries that point to the same folder. In Fedora's virtual
# environments lib64/ is a link to lib/ but both end up in sys.path: optimum finds
# optimum/commands/register/register_openvino.py twice and fails with "conflicting subparser: openvino".
OPTIMUM_WRAPPER = ("import os,sys;seen=set();"
                   "sys.path[:]=[p for p in sys.path if not((r:=os.path.realpath(p)) in seen or seen.add(r))];"
                   "from optimum.commands.optimum_cli import main;sys.argv=['optimum-cli',*sys.argv[1:]];main()")
OPTIMUM_CANDIDATES = (
    os.path.join(os.path.dirname(sys.executable), "optimum-cli"),               # same environment as the app
    os.path.join(DATA_DIR, APP_ID, "optimum-venv", "bin", "optimum-cli"),       # install.sh --with-optimum
)


def find_optimum():
    return next((p for p in OPTIMUM_CANDIDATES if os.access(p, os.X_OK)), None) or shutil.which("optimum-cli")


def optimum_command(args):
    """Command running `optimum-cli <args>` with the Python of its environment and the sys.path fix."""
    optimum_bin = find_optimum()
    if not optimum_bin:
        return ["optimum-cli", *args]  # fails with "command not found", shown in the console
    # in a virtual environment the right Python sits next to optimum-cli (more reliable than the shebang,
    # which pip replaces with "#!/bin/sh" when the path is very long)
    bin_dir = os.path.dirname(optimum_bin)
    for name in ("python", "python3"):
        python = os.path.join(bin_dir, name)
        if os.access(python, os.X_OK):
            return [python, "-c", OPTIMUM_WRAPPER, *args]
    return [optimum_bin, *args]


NAV_PAGES = (N_("Server"), N_("Explore Hugging Face"), N_("Local library"), N_("Converter"))

HARDWARE_MODES = {
    "GPU": {
        "title": N_("Intel GPU"), "icon": "gpu", "desc": N_("Fastest generation"),
        "device": "GPU",
        "flags": ["--device", "/dev/dri"],
        "probe": "gpu",
    },
    "NPU": {
        "title": N_("Intel NPU"), "icon": "spark", "desc": N_("Cool and quiet, ideal on battery"),
        "device": "NPU",
        "flags": ["--device", "/dev/accel:/dev/accel", "--device", "/dev/dri"],
        "probe": "npu",
    },
    "MULTI": {
        "title": N_("Hybrid GPU + CPU"), "icon": "layers", "desc": N_("Parallel MULTI throughput"),
        "device": "MULTI:GPU,CPU",
        "flags": ["--device", "/dev/dri"],
        "probe": "gpu",
    },
    "CPU": {
        "title": N_("CPU only"), "icon": "chip", "desc": N_("Works everywhere"),
        "device": "CPU",
        "flags": [],
        "probe": None,
    },
}


def served_name_for(model_rel_path):
    """Name the model shows up with in Open WebUI: the folder name without the '-ov' suffix.
    e.g. 'OpenVINO/Qwen3.5-4B-int8-ov' -> 'Qwen3.5-4B-int8'"""
    name = os.path.basename(model_rel_path.rstrip("/"))
    name = re.sub(r"-ov$", "", name, flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or FALLBACK_MODEL_NAME


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(**values):
    state = load_state()
    state.update(values)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def _drop_named(node, name):
    """Recursively remove from lists the items named `name` (also inside {"config": {...}})."""
    if isinstance(node, dict):
        return {k: _drop_named(v, name) for k, v in node.items()}
    if isinstance(node, list):
        return [_drop_named(e, name) for e in node
                if not (isinstance(e, dict) and name in (e.get("name"), (e.get("config") or {}).get("name")))]
    return node


def config_has_model(path, name):
    """True if config.json has an entry with that name (or if the file exists but cannot be read)."""
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return True
    return _drop_named(cfg, name) != cfg


def remove_model_from_config(path, name):
    """Rewrite config.json in place, without the entry `name`. In place on purpose: replacing the file would
    change its owner and permissions, and the OVMS container could no longer write it."""
    with open(path, "r+") as f:
        cfg = json.load(f)
        f.seek(0)
        f.truncate()
        json.dump(_drop_named(cfg, name), f, indent=2)


# PCI classes (lspci -n): 03xx = display controller, 1200 = processing accelerator (NPU)
PCI_CLASSES = {"gpu": ("03",), "npu": ("1200",)}
DEVICE_NODES = {"gpu": "/dev/dri", "npu": "/dev/accel"}
INTEL_VENDOR = "8086"


def pci_devices():
    """(vendor, class) of every PCI device, e.g. ('8086', '0300'); None if they cannot be listed."""
    pci = []
    try:
        for dev in os.listdir("/sys/bus/pci/devices"):
            base = f"/sys/bus/pci/devices/{dev}"
            with open(f"{base}/vendor") as fv, open(f"{base}/class") as fc:
                pci.append((fv.read().strip()[2:].lower(), fc.read().strip()[2:6].lower()))
        return pci
    except OSError:
        pass
    try:
        out = subprocess.run(["lspci", "-n"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        m = re.match(r"\S+ ([0-9a-f]{4}): ([0-9a-f]{4}):", line)
        if m:
            pci.append((m.group(2), m.group(1)))
    return pci or None


def detect_accelerators():
    """Detect Intel GPU/NPU. Return {kind: 'ok' | 'hidden' | 'other' | 'missing'}.

    'hidden' = the Intel hardware is on the PCI bus but its /dev node is not visible to this process
    (typical when the app runs in a sandbox such as bwrap/flatpak): the container cannot get it.
    'other' = there is a /dev node but no Intel device of that kind (e.g. an AMD or NVIDIA GPU, which
    OpenVINO does not support).
    """
    pci = pci_devices()
    result = {}
    for kind, prefixes in PCI_CLASSES.items():
        node = DEVICE_NODES[kind]
        has_node = os.path.isdir(node) and bool(os.listdir(node))
        intel = None if pci is None else any(v == INTEL_VENDOR and c.startswith(prefixes) for v, c in pci)
        if has_node:
            result[kind] = "other" if intel is False else "ok"
        else:
            result[kind] = "hidden" if intel else "missing"
    return result


# Category -> (label, task for `ovms --pull`, HF pipeline_tags)
CATEGORIES = {
    "llm": (N_("LLM / Chat"), "text_generation", {"text-generation"}),
    "vlm": (N_("Vision-Language"), "text_generation", {"image-text-to-text"}),
    "embed": (N_("Embeddings"), "embeddings", {"feature-extraction", "sentence-similarity"}),
    "rerank": (N_("Rerank"), "rerank", {"text-ranking"}),
    "image": (N_("Image generation"), "image_generation", {"text-to-image", "image-to-image"}),
    "asr": (N_("Speech-to-Text"), "speech2text", {"automatic-speech-recognition"}),
    "tts": (N_("Text-to-Speech"), "text2speech", {"text-to-speech"}),
    "cv": (N_("Computer Vision"), None, {"image-classification", "object-detection", "image-segmentation"}),
    "other": (N_("Other"), None, set()),
}

QUANT_COLORS = {
    "INT4": "#22c55e", "INT8": "#38bdf8", "FP16": "#a78bfa", "FP32": "#f472b6",
    "NF4": "#facc15", "MXFP4": "#fb923c", "W8A16": "#2dd4bf", "—": "#64748b",
}

# Explanations shown in the guide and in tooltips. Sizes are indicative for a 7-billion-parameter model.
QUANT_INFO = {
    "FP16": (N_("16 bit · ≈ 2 bytes/parameter · 7B ≈ 14 GB"),
             N_("Half-precision floating-point weights: quality practically identical to the original, "
             "but it takes the most memory and is the slowest to generate.")),
    "INT8": (N_("8 bit · ≈ 1 byte/parameter · 7B ≈ 7–8 GB"),
             N_("Weights compressed to 8-bit integers: half the memory of FP16 with almost no quality loss. "
             "A good compromise when memory is enough.")),
    "INT4": (N_("4 bit · ≈ 0.5 bytes/parameter · 7B ≈ 4–5 GB"),
             N_("4-bit weights: a quarter of the memory of FP16 and faster generation on Intel GPUs and NPUs, "
             "because LLMs are limited by memory bandwidth. Slight quality loss, "
             "more noticeable on small models (< 3B).")),
    "OTHER": ("NF4 · MXFP4 · W8A16 · BF16 · FP32",
              N_("NF4/MXFP4: alternative 4-bit formats. W8A16: 8-bit weights, 16-bit compute. "
              "BF16/FP32: full precision, large files. “—”: quantization not stated in the repo name.")),
}
QUANT_VARIANT_INFO = {
    "CW": N_("channel-wise: one scale per channel instead of per group of weights. The variant meant for the NPU."),
    "GQ": N_("group quantization: weights are quantized in groups (e.g. 128) with one scale each. More accurate."),
    "SYM": N_("symmetric: range centered on zero. Simpler and faster to compute."),
    "ASYM": N_("asymmetric: range with an offset. A bit more accurate, a bit more expensive."),
}


def quant_tooltip(quant, variant=""):
    key = quant if quant in QUANT_INFO else "OTHER"
    size, text = QUANT_INFO[key]
    label = tr("Other") if quant == "OTHER" else quant
    tip = f"<b>{label}</b> — {tr(size)}<br>{tr(text)}"
    if variant in QUANT_VARIANT_INFO:
        tip += f"<br><br><b>{variant}</b>: {tr(QUANT_VARIANT_INFO[variant])}"
    return f"<div style='max-width:340px'>{tip}</div>"


# ---------------------------------------------------------------------------
# Palette & style
# ---------------------------------------------------------------------------
ACCENT = "electric"   # OVMS Manager's accent: luminous electric blue
C = kit.T             # tokens of the current theme (light or dark), updated in place by the kit
THEME = None          # kit.ThemeManager, created in main()
THEMES = [("system", N_("System")), ("light", N_("Light")), ("dark", N_("Dark"))]


def app_qss(t):
    """OVMS Manager's own rules on top of the kit's base style. Calm layout: the sidebar is a notch dimmer, cards
    are plain surfaces without heavy borders, the console stays a dark terminal in both themes."""
    r = kit.RADIUS
    return kit.base_qss(t) + f"""
QMainWindow, #root, QDialog {{ background: {t['bg']}; }}
#sidebar {{ background: {t['sidebar']}; border: none; }}
#brand {{ font-size: 16px; font-weight: 750; }}
#brandSub {{ color: {t['muted']}; font-size: 11px; }}
#navList {{ background: transparent; border: none; outline: none; }}
#navList::item {{ padding: 9px 12px; margin: 2px 8px; border-radius: {r['md']}px; color: {t['muted']}; }}
#navList::item:hover {{ background: {t['hover']}; color: {t['text']}; }}
#navList::item:selected {{ background: {t['accent_soft']}; color: {t['accent_text']}; font-weight: 600; }}
#pageTitle {{ font-size: 24px; font-weight: 750; }}
#pageSub {{ color: {t['muted']}; font-size: 13px; }}
#card {{ background: {t['surface']}; border: none; border-radius: {r['xl']}px; }}
#cardTitle {{ font-size: 14px; font-weight: 650; }}
QPushButton#hwCard {{ text-align: left; padding: 14px; border-radius: {r['lg']}px; background: {t['surface2']};
    border: 1px solid {t['border']}; }}
QPushButton#hwCard:hover {{ border-color: {t['border_strong']}; }}
QPushButton#hwCard:checked {{ border: 2px solid {t['accent']}; background: {t['accent_soft']}; }}
QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: {t['border_strong']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; }}
QTableView {{ background: {t['surface']}; border: none; border-radius: {r['xl']}px; gridline-color: transparent;
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']};
    alternate-background-color: {t['surface2']}; outline: none; }}
QTableView::item {{ padding: 6px; border: none; }}
QHeaderView::section {{ background: {t['surface']}; color: {t['muted']}; border: none;
    border-bottom: 1px solid {t['border']}; padding: 10px 8px; font-weight: 650; font-size: 11px; }}
QListWidget#lib {{ background: transparent; border: none; outline: none; }}
QListWidget#lib::item {{ background: {t['surface']}; border: 1px solid transparent; border-radius: {r['lg']}px;
    padding: 14px; margin: 3px 0; }}
QListWidget#lib::item:hover {{ border-color: {t['border']}; }}
QListWidget#lib::item:selected {{ border-color: {t['accent']}; background: {t['accent_soft']}; color: {t['text']}; }}
#consoleWrap {{ background: {t['console_bg']}; border-radius: {r['lg']}px; }}
#consoleTitle {{ color: {t['console_text']}; font-weight: 650; }}
#consoleLast {{ color: {t['console_muted']}; }}
QPlainTextEdit#console {{ background: {t['console_bg']}; border: none; color: {t['console_text']};
    font-family: "{kit.FONTS['mono']}"; font-size: 12px; padding: 2px 6px; }}
#consoleWrap QPushButton#ghost {{ color: {t['console_muted']}; }}
#consoleWrap QPushButton#ghost:hover {{ background: #24221f; color: {t['console_text']}; }}
#consoleWrap QPushButton#icon:hover {{ background: #24221f; }}
QSplitter::handle {{ background: transparent; }}
#statusBar {{ background: {t['sidebar']}; border-top: 1px solid {t['border']}; }}
"""


class ThemedLabel(QLabel, kit.Themed):
    """Label whose rich text depends on theme colors: `render()` is called again at every theme change."""

    def __init__(self, render, name=None):
        super().__init__()
        self.render = render
        if name:
            self.setObjectName(name)
        self.follow_theme()

    def restyle(self):
        self.setText(self.render())


# ---------------------------------------------------------------------------
# Hugging Face logic
# ---------------------------------------------------------------------------
QUANT_RE = re.compile(r"(?<![a-z0-9])(int4|int8|fp16|fp32|bf16|nf4|mxfp4|w8a16|w4a16)(?![a-z0-9])", re.I)
QUANT_VARIANT_RE = re.compile(r"(?<![a-z0-9])(cw|gq|sym|asym|awq)(?![a-z0-9])", re.I)


def detect_quant(repo_id, tags=()):
    """Infer the quantization from the repo name (e.g. '...-int4-cw-ov' -> ('INT4', 'CW'))."""
    name = repo_id.split("/")[-1]
    m = QUANT_RE.search(name)
    if m:
        variant = QUANT_VARIANT_RE.search(name, m.end())
        return m.group(1).upper(), (variant.group(1).upper() if variant else "")
    for t in tags:
        if QUANT_RE.fullmatch(t):
            return t.upper(), ""
    return "—", ""


def detect_category(repo_id, pipeline_tag):
    for key, (_, _, tags) in CATEGORIES.items():
        if pipeline_tag in tags:
            return key
    n = repo_id.lower()
    if re.search(r"diffusion|dreamshaper|flux|sdxl|lcm_|sana", n):
        return "image"
    if re.search(r"whisper", n):
        return "asr"
    if re.search(r"embed|bge-|e5-|minilm", n):
        return "embed"
    if re.search(r"rerank", n):
        return "rerank"
    if re.search(r"bert|resnet|yolo|vit-|mobilenet|detr", n):
        return "cv"
    if pipeline_tag is None:
        return "llm"  # most OpenVINO repos without a tag are LLMs
    return "other"


def normalize(raw, official):
    repo_id = raw["id"]
    quant, variant = detect_quant(repo_id, raw.get("tags", []))
    base_model = next((t.split(":", 1)[1] for t in raw.get("tags", []) if t.startswith("base_model:")
                       and not t.startswith("base_model:quantized:")), "")
    return {
        "id": repo_id,
        "author": repo_id.split("/")[0],
        "name": repo_id.split("/")[-1],
        "quant": quant,
        "variant": variant,
        "category": detect_category(repo_id, raw.get("pipeline_tag")),
        "pipeline": raw.get("pipeline_tag") or "",
        "downloads": raw.get("downloads", 0) or 0,
        "likes": raw.get("likes", 0) or 0,
        "updated": raw.get("lastModified") or raw.get("createdAt") or "",
        "base_model": base_model,
        "official": official,
    }


def hf_get(params):
    query = list(params.items()) + [("expand[]", e) for e in HF_EXPAND]
    r = requests.get(HF_API, params=query, timeout=30)
    r.raise_for_status()
    return r.json()


def read_cached_catalog():
    """Catalog saved by the last successful download; None if missing or unreadable."""
    try:
        with open(CACHE_FILE) as f:
            models = json.load(f)
        return models if isinstance(models, list) else None
    except (OSError, ValueError):
        return None


def fetch_official(force=False):
    if not force and os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < CACHE_TTL:
        cached = read_cached_catalog()
        if cached is not None:
            return cached, True
    raw = hf_get({"author": "OpenVINO", "sort": "downloads", "direction": "-1", "limit": "1000"})
    models = [normalize(m, True) for m in raw]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(models, f)
    return models, False


def fetch_community(search):
    params = {"filter": "openvino", "sort": "downloads", "direction": "-1", "limit": "300"}
    if search:
        params["search"] = search
    return [normalize(m, False) for m in hf_get(params) if not m["id"].startswith("OpenVINO/")]


def human(n):
    for unit, div in (("M", 1_000_000), ("k", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(n)


def human_size(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
        b /= 1024
    return f"{b:.1f} PB"


def rel_time(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    days = (datetime.now(timezone.utc) - dt).days
    if days < 1:
        return tr("today")
    if days < 30:
        return tr("yesterday") if days == 1 else tr("{n} days ago").format(n=days)
    if days < 365:
        return tr("1 month ago") if days < 60 else tr("{n} months ago").format(n=days // 30)
    return tr("1 year ago") if days < 730 else tr("{n} years ago").format(n=days // 365)


# ---------------------------------------------------------------------------
# Worker threads
# ---------------------------------------------------------------------------
class Task(QThread):
    """Runs `fn(task)` in the background. `fn` uses task.log() / task.run_cmd() — the UI updates only via signals."""
    line = Signal(str, str)          # text, level
    progress = Signal(int, str)      # percentage (-1 = indeterminate), text
    result = Signal(object)
    failed = Signal(str)

    PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.output_tail = []

    def log(self, text, level="info"):
        self.line.emit(text, level)

    def run_cmd(self, cmd, display=None):
        """Run cmd showing its output in the console; `display` replaces the command shown in the log.
        The last lines stay in self.output_tail to recognize known errors."""
        self.log("$ " + (display or " ".join(cmd)), "cmd")
        self.output_tail = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            self.log(tr("Command not found: {command}").format(command=cmd[0]), "err")
            return 127
        last_pct = None
        for raw in iter(proc.stdout.readline, ""):
            text = raw.strip()
            if not text:
                continue
            self.output_tail = (self.output_tail + [text])[-200:]
            m = self.PCT_RE.search(text)
            if m:
                pct = min(100, int(float(m.group(1))))
                self.progress.emit(pct, text[:120])
                if pct == last_pct or pct % 10:
                    continue  # don't flood the log with progress bars
                last_pct = pct
            self.log(text)
        proc.stdout.close()
        proc.wait()
        return proc.returncode

    def run(self):
        try:
            self.result.emit(self.fn(self))
        except Exception as e:  # noqa: BLE001 - show any error in the UI
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------------
class StatusPill(QWidget):
    """Status indicator with a pulsing halo (steady with reduced motion). The color is a token name ("ok",
    "warn"…), resolved at paint time, so the pill follows theme changes."""
    reduce_motion = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glow = 0.0
        self.key = "muted"
        kit.signals.changed.connect(self.update)
        self.text = tr("Checking…")
        self.setFixedHeight(34)
        self.setMinimumWidth(170)
        self.anim = QPropertyAnimation(self, b"glow", self)
        self.anim.setDuration(1400)
        self.anim.setStartValue(0.0)
        self.anim.setKeyValueAt(0.5, 1.0)
        self.anim.setEndValue(0.0)
        self.anim.setEasingCurve(QEasingCurve.InOutSine)
        self.anim.setLoopCount(-1)

    def get_glow(self):
        return self._glow

    def set_glow(self, v):
        self._glow = v
        self.update()

    glow = Property(float, get_glow, set_glow)

    @property
    def color(self):
        return QColor(C[self.key])

    def set_state(self, text, key, pulsing):
        self.text, self.key = text, key
        bold = QFont(self.font())
        bold.setBold(True)
        self.setMinimumWidth(max(170, QFontMetrics(bold).horizontalAdvance(text) + 52))  # dot + padding
        pulsing = pulsing and not self.reduce_motion
        if pulsing and self.anim.state() != QPropertyAnimation.Running:
            self.anim.start()
        elif not pulsing:
            self.anim.stop()
            self._glow = 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        bg = QColor(self.color)
        bg.setAlphaF(0.12)
        p.setPen(QPen(QColor(self.color.red(), self.color.green(), self.color.blue(), 110), 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        cx, cy = r.left() + 18, r.center().y()
        halo = QColor(self.color)
        halo.setAlphaF(0.35 * self._glow)
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        rad = 5 + 5 * self._glow
        p.drawEllipse(QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad))
        p.setBrush(self.color)
        p.drawEllipse(QRectF(cx - 5, cy - 5, 10, 10))
        p.setPen(self.color.lighter(130))
        f = self.font()
        f.setBold(True)
        p.setFont(f)
        p.drawText(r.adjusted(34, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text)


class BadgeDelegate(QStyledItemDelegate):
    """Draws the quantization as a colored badge."""

    def paint(self, painter, option, index):
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(C["accent_soft"]))
        text = index.data(Qt.DisplayRole) or ""
        if not text:
            return
        base = text.split()[0]
        color = QColor(QUANT_COLORS.get(base, "#64748b"))
        light = C["scheme"] == "light"
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        f = QFont(option.font)
        f.setBold(True)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1.5))
        painter.setFont(f)
        w = painter.fontMetrics().horizontalAdvance(text) + 18
        h = 22
        r = QRectF(option.rect.left() + 8, option.rect.center().y() - h / 2, w, h)
        bg = QColor(color)
        bg.setAlphaF(0.16)
        painter.setBrush(bg)
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 140), 1))
        painter.drawRoundedRect(r, 11, 11)
        painter.setPen(color.darker(170) if light else color.lighter(115))
        painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()


def card(title=None, subtitle=None):
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 18, 20, 20)
    lay.setSpacing(12)
    if title:
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("muted")
        s.setWordWrap(True)
        lay.addWidget(s)
    return frame, lay


def quant_guide():
    """Card explaining quantization."""
    frame, lay = card(tr("What is quantization?"),
                      tr("An LLM is made of billions of numbers (the “weights”). Quantizing means storing them with "
                         "fewer bits: the model takes less memory and generates faster, in exchange for a small loss "
                         "of precision. In OpenVINO the weights are compressed, while computation stays "
                         "floating-point."))
    grid = QGridLayout()
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(10)
    grid.setColumnStretch(2, 1)
    for r, (q, (size, text)) in enumerate(QUANT_INFO.items()):
        color = QColor(QUANT_COLORS.get(q, C["muted"]))
        color = (color.darker(170) if C["scheme"] == "light" else color).name()
        badge = QLabel(tr("Other") if q == "OTHER" else q)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedWidth(64)
        badge.setStyleSheet(f"color:{color}; border:1px solid {color}; border-radius:10px; "
                            f"padding:2px 6px; font-weight:700; font-size:11px;")
        sz = QLabel(tr(size))
        sz.setObjectName("hint")
        desc = QLabel(tr(text))
        desc.setWordWrap(True)
        grid.addWidget(badge, r, 0, Qt.AlignTop)
        grid.addWidget(sz, r, 1, Qt.AlignTop)
        grid.addWidget(desc, r, 2)
    lay.addLayout(grid)
    variants = QLabel(f"<b>{tr('Name suffixes')}</b> &nbsp;·&nbsp; " + " &nbsp;·&nbsp; ".join(
        f"<b>{k}</b> {tr(v).split(':')[0]}" for k, v in QUANT_VARIANT_INFO.items()))
    variants.setObjectName("hint")
    variants.setWordWrap(True)
    lay.addWidget(variants)
    tip = QLabel(f"<span style='color:{C['accent_text']}'><b>{tr('Tip:')}</b></span> " + tr(
        "for chat on an Intel GPU or NPU start from <b>INT4</b> (on the NPU prefer the <b>INT4 CW</b> variants). "
        "Move to <b>INT8</b> if answers seem less accurate and you have enough memory; <b>FP16</b> is only worth "
        "it for maximum fidelity."))
    tip.setWordWrap(True)
    lay.addWidget(tip)
    return frame


def show_quant_guide(parent):
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("Quantization guide"))
    dlg.setStyleSheet(f"QDialog {{ background: {C['bg']}; }}")
    dlg.resize(760, 600)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(20, 20, 20, 16)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
    scroll.setWidget(quant_guide())
    lay.addWidget(scroll, 1)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(button(tr("Got it"), "primary", dlg.accept))
    lay.addLayout(row)
    dlg.exec()


def guide_button(parent):
    return kit.IconButton("info", tr("What is quantization?"), "chip", lambda: show_quant_guide(parent), size=16)


def page_header(title, subtitle):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(4)
    t = QLabel(title)
    t.setObjectName("pageTitle")
    s = QLabel(subtitle)
    s.setObjectName("pageSub")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def scrollable(page):
    """Page that scrolls when the window is too short, instead of squeezing its cards."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
    area.setWidget(page)
    return area


def button(text, kind=None, slot=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


# ---------------------------------------------------------------------------
# Catalog table model
# ---------------------------------------------------------------------------
class CatalogModel(QAbstractTableModel):
    HEADERS = [N_("Model"), N_("Quantization"), N_("Type"), N_("Author"), N_("Downloads"), N_("Likes"),
               N_("Updated")]

    def __init__(self):
        super().__init__()
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return tr(self.HEADERS[section])
        return None

    def data(self, index, role=Qt.DisplayRole):
        m = self.rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return [
                m["name"],
                f"{m['quant']} {m['variant']}".strip() if m["quant"] != "—" else "—",
                tr(CATEGORIES[m["category"]][0]),
                ("✓ " if m["official"] else "") + m["author"],
                "⬇ " + human(m["downloads"]),
                "♥ " + human(m["likes"]),
                rel_time(m["updated"]),
            ][col]
        if role == Qt.UserRole:  # sort key
            return [m["name"].lower(), m["quant"], m["category"], m["author"].lower(),
                    m["downloads"], m["likes"], m["updated"]][col]
        if role == Qt.UserRole + 1:
            return m
        if role == Qt.ForegroundRole:
            if col == 3 and m["official"]:
                return QBrush(QColor(C["accent"]))
            if col >= 4:
                return QBrush(QColor(C["muted"]))
        if role == Qt.FontRole and col == 0:
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ToolTipRole:
            return quant_tooltip(m["quant"], m["variant"]) if col == 1 else m["id"]
        return None


class CatalogFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.search = ""
        self.quants = set()      # empty = all
        self.category = "llm"    # None = all
        self.setSortRole(Qt.UserRole)

    def filterAcceptsRow(self, row, parent):
        m = self.sourceModel().rows[row]
        if self.category and m["category"] != self.category:
            return False
        return self.matches(m)

    def matches(self, m):
        """Search + quantization, without the type filter (also used for the counts)."""
        if self.quants:
            q = m["quant"] if m["quant"] in ("INT4", "INT8", "FP16") else "OTHER"
            if q not in self.quants:
                return False
        if self.search:
            hay = f"{m['id']} {m['base_model']}".lower()
            return all(tok in hay for tok in self.search.lower().split())
        return True


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class OVMSManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OVMS Manager · OpenVINO Model Server")
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)
        self.tasks = set()   # every live thread (for shutdown)
        self.jobs = set()    # operations visible to the user (progress bar)
        self.official, self.community = [], []
        self.selected_model = None
        self.stop_wait = threading.Event()
        state = load_state()
        self.served_name = state.get("served_name")
        self.local_only = bool(state.get("local_only", False))
        # new releases on GitHub: checked at most once a day, in the background (command palette to turn it off)
        self.updates = kit.UpdateChecker(self, APP_VERSION, lambda k, d=None: load_state().get(k, d),
                                         lambda k, v: save_state(**{k: v}))
        self.palette_dlg = kit.CommandPalette(self, self._palette_commands, tr("Type a command or a model…"),
                                              tr("No results"))
        QShortcut(QKeySequence("Ctrl+K"), self, self.palette_dlg.open_palette)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addLayout(body, 1)
        body.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(28, 24, 28, 14)
        right.setSpacing(12)
        body.addLayout(right, 1)
        self.update_banner = kit.UpdateBanner(tr("{app} {version} is available.").replace("{app}", "OVMS Manager"),
                                              tr("What's new"), tr("Skip this version"), tr("Later"),
                                              self.updates.skip)
        self.updates.found.connect(self.update_banner.show_release)
        right.addWidget(self.update_banner)

        self.stack = QStackedWidget()
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.stack)
        self.splitter.addWidget(self._build_console())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setChildrenCollapsible(False)
        self.toggle_console(False)  # progressive disclosure: only the last line until it is needed
        right.addWidget(self.splitter, 1)

        self.stack.addWidget(scrollable(self._build_server_page()))
        self.stack.addWidget(self._build_explore_page())
        self.stack.addWidget(self._build_library_page())
        self.stack.addWidget(scrollable(self._build_converter_page()))
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        outer.addWidget(self._build_statusbar())

        self.refresh_local_models()
        if RUNTIME is None:
            self.log(tr(RUNTIME_HINT), "err")
        for kind, status in detect_accelerators().items():
            if status == "hidden":
                self.log(tr("Intel {device} present but {node} is not accessible: the app seems to run in a sandbox, "
                            "the container will not be able to use it.").format(device=kind.upper(),
                                                                                node=DEVICE_NODES[kind]), "warn")
        self.status_timer = QTimer(self, interval=4000, timeout=self.check_server_status)
        self.status_timer.start()
        self.check_server_status()
        self.load_catalog()
        QTimer.singleShot(4000, self.updates.check)
        self.update_timer = QTimer(self, interval=6 * 3600 * 1000, timeout=self.updates.check)
        self.update_timer.start()

    def check_updates_now(self):
        def done(release):
            self.updates.finished.disconnect(done)
            if release is None:
                self.log(tr("{app} {version} is the latest version (or GitHub could not be reached).")
                         .format(app="OVMS Manager", version=APP_VERSION), "ok")
            else:
                self.log(tr("{app} {version} is available.").replace("{app}", "OVMS Manager")
                         .format(version=release.version) + f" {release.url}", "ok")
        self.updates.finished.connect(done)
        self.updates.check(force=True)

    def toggle_update_checks(self):
        self.updates.set_enabled(not self.updates.enabled)
        self.log(tr("Automatic update checks: {state}.").format(
            state=tr("on") if self.updates.enabled else tr("off")), "ok")

    # --- Layout ----------------------------------------------------------------
    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(248)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(8, 20, 8, 14)
        lay.setSpacing(4)

        brand = QHBoxLayout()
        brand.setContentsMargins(12, 0, 8, 16)
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(QIcon(os.path.join(APP_DIR, f"{APP_ID}.svg")).pixmap(30, 30))
        brand.addWidget(logo)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        b1 = QLabel("OVMS Manager")
        b1.setObjectName("brand")
        b2 = QLabel("OpenVINO · Optimum · HF")
        b2.setObjectName("brandSub")
        txt.addWidget(b1)
        txt.addWidget(b2)
        brand.addLayout(txt)
        brand.addStretch()
        lay.addLayout(brand)
        search = kit.IconButton("search", tr("Search…"), "ghost", lambda: self.palette_dlg.open_palette(), size=16)
        search.setToolTip(tr("Command palette (Ctrl+K)"))
        search.setStyleSheet("text-align: left; padding-left: 21px;")
        lay.addWidget(search)
        lay.addSpacing(4)

        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setCursor(Qt.PointingHandCursor)
        self.nav.setIconSize(QSize(18, 18))
        self.nav_icons = ("server", "explore", "library", "converter")
        for label in NAV_PAGES:
            self.nav.addItem(QListWidgetItem(tr(label)))
        kit.signals.changed.connect(self._refresh_nav_icons)
        self._refresh_nav_icons()
        lay.addWidget(self.nav, 1)

        for icon_name, tip, combo in (("globe", tr("Interface language"), "cb_ui_lang"),
                                      ("theme", tr("Theme"), "cb_theme")):
            row = QHBoxLayout()
            row.setContentsMargins(10, 0, 10, 6)
            row.setSpacing(6)
            pic = QLabel()

            def draw(pic=pic, n=icon_name):
                pic.setPixmap(kit.icon(n, C["muted"], 16).pixmap(16, 16))
            draw()
            kit.signals.changed.connect(draw)
            pic.setToolTip(tip)
            row.addWidget(pic)
            cb = QComboBox()
            cb.setToolTip(tip)
            row.addWidget(cb, 1)
            setattr(self, combo, cb)
            lay.addLayout(row)
        for code, name in UI_LANGUAGES:
            self.cb_ui_lang.addItem(tr(name), code)
        self.cb_ui_lang.setCurrentIndex(max(0, self.cb_ui_lang.findData(load_state().get("ui_language", ""))))
        self.cb_ui_lang.currentIndexChanged.connect(self._on_ui_language)
        for code, name in THEMES:
            self.cb_theme.addItem(tr(name), code)
        self.cb_theme.setCurrentIndex(max(0, self.cb_theme.findData(load_state().get("theme", "system"))))
        self.cb_theme.currentIndexChanged.connect(self._on_theme)
        lay.addSpacing(6)

        foot, flay = card()
        flay.setContentsMargins(14, 12, 10, 12)
        flay.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(QLabel(f"<b>{tr('Endpoint')}</b>"))
        top.addStretch()
        top.addWidget(kit.IconButton("copy", tooltip=tr("Copy the endpoint URL"), size=16,
                                     slot=lambda: self._copy(f"{OVMS_URL}/v3")))
        flay.addLayout(top)
        ep = ThemedLabel(lambda: f"<span style='color:{C['accent_text']}'>{OVMS_HOST}:{OVMS_PORT}/v3</span>")
        ep.setTextInteractionFlags(Qt.TextSelectableByMouse)
        flay.addWidget(ep)
        flay.addWidget(kit.Chip("lock", tr("Local · OpenAI API"), "neutral",
                                tr("The models run on this computer. Runtime: {runtime}").format(runtime=RUNTIME_NAME)))
        lay.addWidget(foot)
        return side

    def _refresh_nav_icons(self):
        for i, name in enumerate(self.nav_icons):
            self.nav.item(i).setIcon(kit.icon(name, C["muted"], 18))

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.lbl_activity.setText(tr("Copied: {text}").format(text=text))

    def _on_ui_language(self, _index):
        save_state(ui_language=self.cb_ui_lang.currentData())
        QMessageBox.information(self, "OVMS Manager", tr("The new language will be used the next time you start "
                                                         "{app}.").format(app="OVMS Manager"))

    def _on_theme(self, _index):
        save_state(theme=self.cb_theme.currentData())
        if THEME:
            THEME.set_preference(self.cb_theme.currentData())

    def _build_console(self):
        """Console as progressive disclosure: collapsed it shows only the last line; it opens with a click, and
        by itself when an error arrives."""
        wrap = QFrame()
        wrap.setObjectName("consoleWrap")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.btn_console = kit.IconButton("chevron_up", tooltip=tr("Show or hide the console"), size=16,
                                          color_key="console_muted", slot=lambda: self.toggle_console())
        head.addWidget(self.btn_console)
        t = QLabel(tr("Console"))
        t.setObjectName("consoleTitle")
        head.addWidget(t)
        self.console_last = QLabel("")
        self.console_last.setObjectName("consoleLast")
        self.console_last.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        head.addWidget(self.console_last, 1)
        head.addWidget(button(tr("Clear"), "ghost", lambda: (self.console.clear(), self.console_last.clear())))
        lay.addLayout(head)
        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        lay.addWidget(self.console, 1)
        self.console_open = True
        return wrap

    def toggle_console(self, show=None):
        show = not self.console_open if show is None else show
        self.console_open = show
        self.console.setVisible(show)
        self.console_last.clear()  # open, the console shows everything; closed, only the last line
        self.btn_console.set_icon_name("chevron_down" if show else "chevron_up")
        total = sum(self.splitter.sizes()) or 800
        self.splitter.setSizes([total - 220, 220] if show else [total - 44, 44])

    def _build_statusbar(self):
        bar = QFrame()
        bar.setObjectName("statusBar")
        bar.setFixedHeight(34)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        self.lbl_activity = QLabel(tr("Ready"))
        self.lbl_activity.setObjectName("hint")
        lay.addWidget(self.lbl_activity, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.setTextVisible(False)
        self.progress.hide()
        lay.addWidget(self.progress)
        return bar

    # --- Server page -----------------------------------------------------------
    def _build_server_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        head = QHBoxLayout()
        head.addWidget(page_header(tr("OVMS server"), tr("Start OpenVINO Model Server with the model and the "
                                                         "accelerator of your choice.")), 1)
        self.chip_model = kit.Chip("server", tr("No model served"), "neutral", tr("Model exposed by the server"))
        head.addWidget(self.chip_model, 0, Qt.AlignTop)
        head.addSpacing(6)
        self.pill = StatusPill()
        head.addWidget(self.pill, 0, Qt.AlignTop)
        lay.addLayout(head)

        c1, l1 = card(tr("Model"), tr("OpenVINO IR models found in {folder}").format(folder=MODELS_LABEL))
        row = QHBoxLayout()
        self.cb_models = QComboBox()
        self.cb_models.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.cb_models, 1)
        row.addWidget(kit.IconButton("refresh", tr("Refresh"), slot=self.refresh_local_models))
        row.addWidget(kit.IconButton("explore", tr("Find models"), slot=lambda: self.nav.setCurrentRow(1)))
        l1.addLayout(row)
        lay.addWidget(c1)

        c2, l2 = card(tr("Hardware accelerator"))
        grid = QGridLayout()
        grid.setSpacing(12)
        self.hw_group = QButtonGroup(self)
        self.hw_group.setExclusive(True)
        accel = detect_accelerators()
        first_ok = None
        for i, (key, hw) in enumerate(HARDWARE_MODES.items()):
            status = "ok" if hw["probe"] is None else accel[hw["probe"]]
            state, color_key = {"ok": (tr("detected"), "ok"),
                                "hidden": (tr("present · /dev not accessible"), "warn"),
                                "other": (tr("not Intel · unsupported"), "warn"),
                                "missing": (tr("not detected"), "muted")}[status]
            b = QPushButton()
            b.setObjectName("hwCard")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(92)
            inner = QVBoxLayout(b)
            inner.setContentsMargins(14, 10, 14, 10)
            top = ThemedLabel(lambda hw=hw: f"<b>{tr(hw['title'])}</b>")
            pic = QLabel()

            def draw(pic=pic, name=hw["icon"]):
                pic.setPixmap(kit.icon(name, C["accent_text"], 22).pixmap(22, 22))
            draw()
            kit.signals.changed.connect(draw)
            title_row = QHBoxLayout()
            title_row.setSpacing(8)
            title_row.addWidget(pic)
            title_row.addWidget(top, 1)
            desc = QLabel(tr(hw["desc"]))
            desc.setObjectName("hint")
            st = ThemedLabel(lambda k=color_key, state=state, hw=hw: f"<span style='color:{C[k]}'>● {state}</span> · "
                                                                    f"<span style='color:{C['muted']}'>{hw['device']}</span>")
            st.setStyleSheet("font-size: 11px;")
            inner.addLayout(title_row)
            for w in (pic, top, desc, st):
                w.setAttribute(Qt.WA_TransparentForMouseEvents)
            inner.addWidget(desc)
            inner.addWidget(st)
            if status == "hidden":
                node = DEVICE_NODES[hw["probe"]]
                b.setToolTip(tr("The hardware is present, but {node} is not visible to this process (is the app "
                                "running in a sandbox?).\nThe container will not get it: start the app from a "
                                "regular terminal or expose the device to the sandbox.").format(node=node))
            elif status == "other":
                b.setToolTip(tr("OpenVINO supports Intel GPUs and NPUs only: use the CPU mode on this machine."))
            b.setProperty("hw", key)
            self.hw_group.addButton(b)
            grid.addWidget(b, 0, i)
            if first_ok is None and status == "ok":
                first_ok = b
        (first_ok or self.hw_group.buttons()[-1]).setChecked(True)
        l2.addLayout(grid)
        lay.addWidget(c2)

        c3, l3 = card(tr("Controls"))
        ctrl = QHBoxLayout()
        self.btn_start = kit.IconButton("play", tr("Start server"), "primary", self.start_server)
        self.btn_stop = kit.IconButton("stop", tr("Stop"), "danger", self.stop_server)
        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_stop)
        self.chk_debug = QCheckBox(tr("Verbose log"))
        self.chk_debug.setToolTip(tr("Start OVMS with --log_level DEBUG: useful to see where loading gets stuck"))
        ctrl.addWidget(self.chk_debug)
        self.chk_local = QCheckBox(tr("Localhost only"))
        self.chk_local.setChecked(self.local_only)
        self.chk_local.setToolTip(tr("Publish port {port} on 127.0.0.1 only, so other computers on the network "
                                     "cannot use the API (it has no authentication).\nLeave it off if Open WebUI "
                                     "runs in a container and reaches the server through {host}.")
                                  .format(port=OVMS_PORT, host=HOST_ALIAS))
        self.chk_local.toggled.connect(lambda on: save_state(local_only=on))
        ctrl.addWidget(self.chk_local)
        ctrl.addStretch()
        l3.addLayout(ctrl)
        tools = QHBoxLayout()  # second row: the page stays usable in narrow windows
        tools.addWidget(kit.IconButton("globe", "Open WebUI", slot=lambda: webbrowser.open(OPEN_WEBUI_URL)))
        tools.addWidget(kit.IconButton("flask", tr("Test API"), slot=self.test_api))
        tools.addWidget(kit.IconButton("log", tr("Container log"), slot=self.show_container_logs))
        tools.addStretch()
        l3.addLayout(tools)
        lay.addWidget(c3)

        c4, l4 = card(tr("Connect Open WebUI"))
        guide = ThemedLabel(lambda: tr(
            "In Open WebUI open <b>Admin Settings → Connections → OpenAI API</b>, press <b>+</b> and enter:"
            "<br>• URL: {url} &nbsp;{container_note}"
            "<br>• API key: any text (e.g. <i>none</i>)"
            "<br>• The model shows up with the name of its folder (e.g. <b>Qwen3.5-4B-int8</b>). "
            "If it does not, add it by hand in the “Model IDs” field of the connection.").format(
            url=f"<span style='color:{C['accent_text']}'>{OVMS_URL}/v3</span>",
            container_note=f"<span style='color:{C['muted']}'>" + tr("(if Open WebUI runs in a container: {url})")
            .format(url=f"<span style='color:{C['accent_text']}'>{CONTAINER_URL}</span>") + "</span>"))
        guide.setWordWrap(True)
        guide.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l4.addWidget(guide)
        lay.addWidget(c4)
        lay.addStretch()
        return page

    # --- Explore page ----------------------------------------------------------
    def _build_explore_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(page_header(tr("Explore Hugging Face"),
                                   tr("Models already converted to OpenVINO IR and quantized, ready to download.")), 1)
        self.btn_refresh_hf = kit.IconButton("refresh", tr("Refresh catalog"), slot=lambda: self.load_catalog(force=True))
        head.addWidget(self.btn_refresh_hf, 0, Qt.AlignTop)
        lay.addLayout(head)

        # Filter bar
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search by name or base model (e.g. qwen 7b, llama, phi)…"))
        search_action = self.search.addAction(kit.icon("search"), QLineEdit.LeadingPosition)
        kit.signals.changed.connect(lambda: search_action.setIcon(kit.icon("search")))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        bar.addWidget(self.search, 1)
        self.cb_category = QComboBox()
        self.cb_category.addItem(tr("All types"), None)
        for key, (label, _, _) in CATEGORIES.items():
            self.cb_category.addItem(tr(label), key)
        self.cb_category.setCurrentIndex(1)
        self.cb_category.currentIndexChanged.connect(lambda _: self._apply_filters())
        bar.addWidget(self.cb_category)
        self.chk_community = QCheckBox(tr("Community"))
        self.chk_community.setToolTip(tr("Include community repos tagged 'openvino'"))
        self.chk_community.toggled.connect(self._on_community_toggled)
        bar.addWidget(self.chk_community)
        lay.addLayout(bar)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        lbl = QLabel(tr("Quantization"))
        lbl.setObjectName("hint")
        chips.addWidget(lbl)
        self.quant_chips = {}
        for q in ("INT4", "INT8", "FP16", "OTHER"):
            b = button(tr("Other") if q == "OTHER" else q, "chip")
            b.setCheckable(True)
            b.setToolTip(quant_tooltip(q))
            b.toggled.connect(lambda _: self._apply_filters(auto=True))
            self.quant_chips[q] = b
            chips.addWidget(b)
        chips.addWidget(guide_button(self))
        chips.addStretch()
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("hint")
        chips.addWidget(self.lbl_count)
        lay.addLayout(chips)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(12)

        self.catalog = CatalogModel()
        self.proxy = CatalogFilter()
        self.proxy.setSourceModel(self.catalog)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setFont(kit.tabular(self.table.font()))  # numbers line up in the columns
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(4, Qt.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setItemDelegateForColumn(1, BadgeDelegate(self.table))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        for col, w in ((1, 118), (2, 128), (3, 130), (4, 90), (5, 62), (6, 110)):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            hh.resizeSection(col, w)
        hh.setHighlightSections(False)
        hh.setMinimumSectionSize(56)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.viewport().installEventFilter(self)
        self.table.selectionModel().currentRowChanged.connect(self._on_select)
        self.table.doubleClicked.connect(lambda _: self.download_selected())
        self.empty_hint = QLabel(self.table.viewport())
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setObjectName("muted")
        self.empty_hint.hide()
        split.addWidget(self.table)
        split.addWidget(self._build_detail_panel())
        split.setStretchFactor(0, 1)
        split.setSizes([800, 300])
        lay.addWidget(split, 1)

        custom = QHBoxLayout()
        custom.setSpacing(10)
        lbl = QLabel(tr("Custom repo"))
        lbl.setObjectName("hint")
        custom.addWidget(lbl)
        self.entry_custom_dl = QLineEdit()
        self.entry_custom_dl.setPlaceholderText(tr("e.g. {repo}").format(repo="OpenVINO/Qwen2.5-7B-Instruct-int4-ov"))
        self.entry_custom_dl.returnPressed.connect(self.download_custom)
        custom.addWidget(self.entry_custom_dl, 1)
        self.btn_download_custom = kit.IconButton("download", tr("Download"), slot=self.download_custom)
        custom.addWidget(self.btn_download_custom)
        lay.addLayout(custom)
        return page

    def _build_detail_panel(self):
        panel, lay = card()
        panel.setMinimumWidth(280)
        panel.setMaximumWidth(340)
        self.d_title = QLabel(tr("Select a model"))
        self.d_title.setObjectName("cardTitle")
        self.d_title.setWordWrap(True)
        self.d_title.setStyleSheet("font-size: 16px;")
        self.d_repo = QLabel("")
        self.d_repo.setObjectName("hint")
        self.d_repo.setWordWrap(True)
        self.d_repo.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.d_badges = QLabel("")
        self.d_info = QLabel(tr("Pick a model in the table to see its details.\nDouble-click to download it."))
        self.d_info.setObjectName("muted")
        self.d_info.setWordWrap(True)
        self.d_grid = QGridLayout()
        self.d_grid.setHorizontalSpacing(14)
        self.d_grid.setVerticalSpacing(8)
        self.d_grid.setColumnStretch(1, 1)
        self.chip_ram = kit.Chip("memory", "", "neutral", tr("Estimate from the repository size: the model needs "
                                                            "about this much memory, plus some room for the context."))
        self.chip_ram.hide()
        for w in (self.d_title, self.d_repo, self.d_badges, self.d_info):
            lay.addWidget(w)
        lay.addLayout(self.d_grid)
        lay.addWidget(self.chip_ram, 0, Qt.AlignLeft)
        lay.addStretch()

        self.btn_download = kit.IconButton("download", tr("Download with OVMS"), "primary", self.download_selected)
        self.btn_download.setEnabled(False)
        self.btn_hf = kit.IconButton("external", tr("Open on Hugging Face"), slot=self.open_on_hf)
        self.btn_hf.setEnabled(False)
        lay.addWidget(self.btn_download)
        lay.addWidget(self.btn_hf)

        return panel

    # --- Library page ----------------------------------------------------------
    def _build_library_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(page_header(tr("Local library"), tr("Models in {folder}").format(folder=MODELS_LABEL)), 1)
        head.addWidget(kit.IconButton("refresh", tr("Refresh"), slot=self.refresh_local_models), 0, Qt.AlignTop)
        lay.addLayout(head)

        self.lib_list = QListWidget()
        self.lib_list.setObjectName("lib")
        self.lib_list.setSpacing(2)
        self.lib_list.itemDoubleClicked.connect(self._use_library_model)
        lay.addWidget(self.lib_list, 1)

        actions = QHBoxLayout()
        self.lbl_lib_total = QLabel("")
        self.lbl_lib_total.setObjectName("hint")
        actions.addWidget(self.lbl_lib_total)
        actions.addStretch()
        actions.addWidget(kit.IconButton("folder", tr("Open folder"), slot=self.open_library_folder))
        actions.addWidget(kit.IconButton("play", tr("Use in server"), "primary", slot=
                                 lambda: self._use_library_model(self.lib_list.currentItem())))
        actions.addWidget(kit.IconButton("delete", tr("Delete"), "danger", self.delete_library_model))
        lay.addLayout(actions)
        return page

    # --- Converter page --------------------------------------------------------
    def _build_converter_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(page_header(tr("Optimum converter"),
                                  tr("Export and quantize a Hugging Face PyTorch model to OpenVINO IR.")))

        c, l = card(tr("Source"))
        l.addWidget(QLabel(tr("PyTorch model ID (Hugging Face)")))
        self.entry_hf_id = QLineEdit("Qwen/Qwen2.5-7B-Instruct")
        self.entry_hf_id.textChanged.connect(self._suggest_output_name)
        l.addWidget(self.entry_hf_id)
        lay.addWidget(c)

        c, l = card(tr("Weight precision"))
        seg = QHBoxLayout()
        seg.setSpacing(8)
        self.prec_group = QButtonGroup(self)
        for prec, desc in (("int4", N_("Recommended for GPU/NPU")), ("int8", N_("Balanced")),
                           ("fp16", N_("Best quality"))):
            b = button(f"{prec.upper()}  ·  {tr(desc)}", "chip")
            b.setCheckable(True)
            b.setProperty("prec", prec)
            b.setMinimumHeight(38)
            self.prec_group.addButton(b)
            seg.addWidget(b)
            if prec == "int4":
                b.setChecked(True)
        self.prec_group.buttonToggled.connect(lambda *_: self._suggest_output_name())
        for b in self.prec_group.buttons():
            b.setToolTip(quant_tooltip(b.property("prec").upper()))
        seg.addStretch()
        seg.addWidget(guide_button(self))
        l.addLayout(seg)
        lay.addWidget(c)

        c, l = card(tr("Advanced options"))
        self.chk_trust = QCheckBox(tr("Allow custom code from the repository (--trust-remote-code)"))
        self.chk_trust.setToolTip(tr("Some models with new architectures ship Python code that must run to load "
                                     "them.\nOnly enable it for repositories you trust: that code runs on your "
                                     "computer."))
        l.addWidget(self.chk_trust)
        hint = QLabel(tr("Only needed for some models with non-standard architectures. The code is downloaded "
                         "from the repository and executed on your computer: enable it only if you trust the "
                         "author."))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        l.addWidget(hint)
        lay.addWidget(c)

        c, l = card(tr("Destination"))
        l.addWidget(QLabel(tr("Folder name in {folder}").format(folder=MODELS_LABEL)))
        self.entry_output_name = QLineEdit()
        l.addWidget(self.entry_output_name)
        self._suggest_output_name()
        lay.addWidget(c)

        row = QHBoxLayout()
        if not find_optimum():
            missing = QLabel(tr("optimum-cli not found: run <b>install.sh --with-optimum</b> to enable "
                                "conversion."))
            missing.setObjectName("hint")
            missing.setWordWrap(True)
            row.addWidget(missing, 1)
        row.addStretch()
        self.btn_convert = kit.IconButton("converter", tr("Convert with Optimum"), "primary", self.start_conversion)
        self.btn_convert.setMinimumWidth(240)
        row.addWidget(self.btn_convert)
        lay.addLayout(row)
        lay.addStretch()
        return page

    # --- UI helpers ------------------------------------------------------------
    def log(self, text, level="info"):
        colors = {"info": C["console_text"], "ok": C["console_ok"], "err": C["console_err"],
                  "warn": C["console_warn"], "cmd": C["console_cmd"], "head": C["console_head"]}
        ts = datetime.now().strftime("%H:%M:%S")
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if hasattr(self, "console_last") and not self.console_open:
            self.console_last.setText(f"<span style='color:{colors.get(level, colors['info'])}'>{safe}</span>")
            if level == "err" and not self.console_open:
                self.toggle_console(True)
        weight = "font-weight:700;" if level in ("head", "ok", "err") else ""
        self.console.appendHtml(f"<span style='color:{C['console_muted']}'>{ts}</span>&nbsp; "
                                f"<span style='color:{colors.get(level, colors['info'])};{weight}'>{safe}</span>")

    def run_task(self, fn, label, buttons=(), on_done=None):
        for b in buttons:
            b.setEnabled(False)
        task = Task(fn, self)
        self.tasks.add(task)
        self.jobs.add(task)
        self.lbl_activity.setText(f"⏳ {label}…")
        self.progress.setRange(0, 0)
        self.progress.show()

        def on_progress(pct, text):
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
            self.lbl_activity.setText(f"⏳ {label} — {pct}%")

        def finish():
            self.tasks.discard(task)
            self.jobs.discard(task)
            for b in buttons:
                b.setEnabled(True)
            if not self.jobs:
                self.progress.hide()
                self.lbl_activity.setText(tr("Ready"))
            task.deleteLater()

        task.line.connect(self.log)
        task.progress.connect(on_progress)
        task.failed.connect(lambda msg: self.log(tr("Error: {error}").format(error=msg), "err"))
        if on_done:
            task.result.connect(on_done)
        task.finished.connect(finish)
        task.start()
        return task

    def require_runtime(self):
        """False (with an explanation in the console) if neither Podman nor Docker is installed."""
        if RUNTIME:
            return True
        self.log(tr(RUNTIME_HINT), "err")
        return False

    # --- Local models ----------------------------------------------------------
    def scan_local_models(self):
        found = []
        for root, dirs, files in os.walk(MODELS_DIR):
            dirs.sort()
            if root == MODELS_DIR:
                continue  # the repository root only holds config.json and the model folders
            if any(f.endswith(".xml") for f in files) or "graph.pbtxt" in files or "model_index.json" in files:
                found.append(os.path.relpath(root, MODELS_DIR))
                dirs[:] = []  # don't descend into subfolders (e.g. unet/, text_encoder/)
        return found

    def refresh_local_models(self):
        current = self.cb_models.currentText()
        found = self.scan_local_models()
        self.cb_models.clear()
        self.cb_models.addItems(found)
        if current in found:
            self.cb_models.setCurrentText(current)
        if not found:
            self.cb_models.setPlaceholderText(tr("No models — download one from Explore Hugging Face"))

        self.lib_list.clear()
        total = 0
        for rel in found:
            path = os.path.join(MODELS_DIR, rel)
            size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs
                       if not os.path.islink(os.path.join(r, f)))
            total += size
            quant, variant = detect_quant(rel)
            badge = f"{quant} {variant}".strip()
            item = QListWidgetItem(f"{rel}\n{human_size(size)}   ·   {badge}")
            item.setData(Qt.UserRole, rel)
            item.setToolTip(path)
            self.lib_list.addItem(item)
        self.lbl_lib_total.setText(tr("{n} models · {size} used").format(n=len(found), size=human_size(total)))

    def _use_library_model(self, item):
        if not item:
            return
        self.cb_models.setCurrentText(item.data(Qt.UserRole))
        self.nav.setCurrentRow(0)

    def open_library_folder(self):
        item = self.lib_list.currentItem()
        path = os.path.join(MODELS_DIR, item.data(Qt.UserRole)) if item else MODELS_DIR
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def delete_library_model(self):
        item = self.lib_list.currentItem()
        if not item:
            QMessageBox.information(self, tr("Delete model"), tr("Select a model in the list first."))
            return
        rel = item.data(Qt.UserRole)
        path = os.path.realpath(os.path.join(MODELS_DIR, rel))
        if not path.startswith(os.path.realpath(MODELS_DIR) + os.sep):
            QMessageBox.warning(self, tr("Delete model"), tr("Invalid path, outside {folder}:\n{path}")
                                .format(folder=MODELS_DIR, path=path))
            return
        ok = QMessageBox.question(self, tr("Delete model"), tr("Permanently delete\n{path}?").format(path=path),
                                  QMessageBox.Yes | QMessageBox.No)
        if ok != QMessageBox.Yes:
            return

        def job(t):
            t.log(tr("Deleting {model}").format(model=rel), "head")
            errors = []

            def on_error(_func, failed_path, exc):
                exc = exc[1] if isinstance(exc, tuple) else exc  # onerror passes a tuple, onexc the exception
                errors.append((failed_path, exc))

            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=on_error)
            else:
                shutil.rmtree(path, onerror=on_error)
            if not os.path.exists(path):
                return []

            # Files written by a container may belong to the container user: with rootless Podman they show
            # up on the host with a "sub" UID (e.g. 100999) and your user cannot delete them; with Docker
            # (older downloads) they may belong to another UID. Delete them from inside a container instead.
            if any(isinstance(e, PermissionError) for _, e in errors) and RUNTIME:
                t.log(tr("Some files were created by the container: deleting them through it…"), "warn")
                if RUNTIME == "podman":
                    t.run_cmd(["podman", "unshare", "rm", "-rf", path])
                else:
                    parent, name = os.path.split(path)
                    t.run_cmd(["docker", "run", "--rm", "--user", "0", "-v", f"{parent}:/target:z",
                               "--entrypoint", "rm", OVMS_IMAGE, "-rf", f"/target/{name}"])
                if not os.path.exists(path):
                    return []
            return errors or [(path, OSError(tr("the folder still exists")))]

        def done(errors):
            self.refresh_local_models()
            if not errors:
                self.log(tr("Model deleted: {model}").format(model=rel), "ok")
                return
            for failed_path, exc in errors:
                self.log(tr("Cannot delete {path}: {error}").format(
                    path=failed_path, error=getattr(exc, "strerror", None) or exc), "err")
            details = "\n".join(f"• {p}: {getattr(e, 'strerror', None) or e}" for p, e in errors[:5])
            if len(errors) > 5:
                details += "\n" + tr("… and {n} more (see the console)").format(n=len(errors) - 5)
            hint = (f"podman unshare rm -rf \"{path}\"" if RUNTIME == "podman" else f"sudo rm -rf \"{path}\"")
            QMessageBox.warning(self, tr("Deletion incomplete"), tr(
                "{model} could not be deleted completely ({n} errors).\n\n{details}\n\nFrom a terminal you can "
                "try:\n{command}").format(model=rel, n=len(errors), details=details, command=hint))

        self.run_task(job, tr("Deleting {model}").format(model=os.path.basename(rel)), on_done=done)

    # --- Server ----------------------------------------------------------------
    def check_server_status(self):
        if any(getattr(t, "is_status", False) for t in self.tasks):
            return
        if RUNTIME is None:
            self.pill.set_state(tr("Podman/Docker missing"), "warn", False)
            return

        def probe(_task):
            try:
                res = subprocess.run(runtime_cmd("ps", "--filter", f"name={CONTAINER_NAME}", "--format",
                                                 "{{.Status}}"),
                                     capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if res.returncode != 0:
                return "error"
            return res.stdout.strip()

        def apply(status):
            if status is None:
                self.pill.set_state(tr("{runtime} not responding").format(runtime=RUNTIME_NAME), "warn", False)
            elif status == "error":
                self.pill.set_state(tr("{runtime} error").format(runtime=RUNTIME_NAME), "warn", False)
                self.pill.setToolTip(tr("`{command}` failed. With Docker, make sure your user may use it (docker "
                                        "group or rootless Docker).").format(command=f"{RUNTIME} ps"))
            elif status.startswith("Up"):
                self.pill.set_state(tr("RUNNING · port {port}").format(port=OVMS_PORT), "ok", True)
                self.chip_model.set(self.served_name or "—", kind="accent")
                return
            else:
                self.pill.set_state(tr("STOPPED"), "err", False)
            self.chip_model.set(tr("No model served"), kind="neutral")

        task = Task(probe, self)
        task.is_status = True
        self.tasks.add(task)
        task.result.connect(apply)
        task.finished.connect(lambda: (self.tasks.discard(task), task.deleteLater()))
        task.start()

    def start_server(self):
        if not self.require_runtime():
            return
        model_rel_path = self.cb_models.currentText()
        if not model_rel_path:
            self.log(tr("No model selected. Download one from the Explore section."), "err")
            return
        hw = HARDWARE_MODES[self.hw_group.checkedButton().property("hw")]
        target_device = hw["device"]
        debug = self.chk_debug.isChecked()
        bind = f"{OVMS_HOST}:" if self.chk_local.isChecked() else ""
        served_name = served_name_for(model_rel_path)
        category = detect_category(model_rel_path, None)
        if category not in ("llm", "vlm"):
            self.log(tr("Note: {model} looks like a “{type}” model, not a chat model: Open WebUI will not list it "
                        "among the chat models.").format(model=model_rel_path, type=tr(CATEGORIES[category][0])),
                     "warn")

        def job(t):
            t.log(tr("Starting server · {model} on {device} · name: {name}").format(
                model=model_rel_path, device=target_device, name=served_name), "head")
            t.log(tr("Stopping any existing container…"))
            subprocess.run(runtime_cmd("stop", CONTAINER_NAME), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(runtime_cmd("rm", CONTAINER_NAME), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            config_path = os.path.join(MODELS_DIR, "config.json")
            # entries registered by previous starts must go, otherwise OVMS would still serve the old
            # model, or refuse the new entry ("name already occupied")
            stale = [n for n in dict.fromkeys([served_name, load_state().get("served_name"), FALLBACK_MODEL_NAME])
                     if n and config_has_model(config_path, n)]
            for name in stale:
                t.log(tr("Removing the previous entry “{name}” from config.json…").format(name=name))
                try:
                    remove_model_from_config(config_path, name)
                except (OSError, ValueError) as e:
                    t.log(tr("Cannot edit config.json directly ({error}): letting OVMS do it.").format(error=e),
                          "warn")
                if config_has_model(config_path, name):
                    t.run_cmd(container_run("--remove_from_config", "--config_path", "/models/config.json",
                                            "--model_name", name, as_owner=True))
                if config_has_model(config_path, name):
                    t.log(tr("The entry “{name}” is still in {path}: fix the file permissions or delete it, then "
                             "try again.").format(name=name, path=config_path), "err")
                    return

            t.log(tr("Registering the model in the config for {device}…").format(device=target_device))
            ret = t.run_cmd(container_run("--add_to_config", "--config_path", "/models/config.json",
                                          "--model_path", model_rel_path, "--model_name", served_name,
                                          "--target_device", target_device, as_owner=True))
            if ret != 0:
                t.log(tr("Registration in the config failed (exit code {code}): server not started.")
                      .format(code=ret), "err")
                if any("cannot open" in line for line in t.output_tail):
                    t.log(tr("The container cannot write {path}: check its owner and permissions (ls -l), or delete "
                             "it and start the server again.").format(path=config_path), "err")
                return
            save_state(served_name=served_name)
            self.served_name = served_name

            t.log(tr("Starting the OVMS container…"))
            args = ["--rest_port", str(OVMS_PORT), "--config_path", "/models/config.json"]
            if debug:
                args += ["--log_level", "DEBUG"]
            cmd = container_run(*args, devices=hw["flags"], detach=True, name=CONTAINER_NAME,
                                ports=[f"{bind}{OVMS_PORT}:{OVMS_PORT}"])
            if t.run_cmd(cmd) != 0:
                t.log(tr("Error starting the container."), "err")
                return
            t.log(tr("Container started. Waiting for OVMS to load the model (it can take a few minutes)…"))
            self._wait_until_ready(t, served_name)

        self.run_task(job, tr("Starting server"), [self.btn_start], lambda _: self.check_server_status())

    def _wait_until_ready(self, t, name, timeout=600):
        """Follow the container logs until the model answers on /v3/models, or the container dies."""
        printed = 0
        last_status = 0
        start = time.time()
        self.stop_wait.clear()
        while time.time() - start < timeout:
            # state first, then logs: if the container just died, the logs read afterwards are complete
            try:
                state = subprocess.run(runtime_cmd("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}",
                                                   CONTAINER_NAME),
                                       capture_output=True, text=True, timeout=30).stdout.split()
                # `logs` writes to stderr what the container writes to stderr: merge them
                logs = subprocess.run(runtime_cmd("logs", CONTAINER_NAME), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, timeout=30).stdout
            except subprocess.TimeoutExpired:
                state, logs = ["true"], ""
            lines = [line for line in logs.splitlines() if line.strip()]
            for line in lines[printed:]:
                low = line.lower()
                level = "err" if any(k in low for k in ("[error]", "[critical]", "error:", "exception")) else "info"
                t.log("  " + line.strip(), level)
            printed = max(printed, len(lines))

            if not state or state[0] != "true":
                code = state[1] if len(state) > 1 else "?"
                t.log(tr("The OVMS container stopped (exit code {code}): check the errors in the log above.")
                      .format(code=code), "err")
                return

            rest_up, ready, ids = self._probe_ovms(name)
            if ready:
                t.log(tr("Model ready in {seconds} s! OpenAI endpoint: {url} · model name: {name}").format(
                    seconds=int(time.time() - start), url=f"{OVMS_URL}/v3", name=name), "ok")
                t.log(tr("In Open WebUI: Admin Settings → Connections → OpenAI API → “+”, URL {url} (if Open WebUI "
                         "runs in a container: {container_url}), any API key.").format(
                    url=f"{OVMS_URL}/v3", container_url=CONTAINER_URL), "ok")
                return

            elapsed = int(time.time() - start)
            if elapsed - last_status >= 15:
                last_status = elapsed
                if not rest_up:
                    what = tr("the REST server on port {port} is not answering yet").format(port=OVMS_PORT)
                else:
                    what = tr("the server answers, but “{name}” is not ready yet").format(name=name)
                    if ids:
                        what += " " + tr("(exposed models: {models})").format(models=", ".join(ids))
                t.log(tr("⏳ {seconds} s · {status}. You can interrupt with “Stop”.").format(seconds=elapsed,
                                                                                         status=what), "warn")

            if self.stop_wait.wait(3):
                t.log(tr("Wait interrupted."), "warn")
                return
        t.log(tr("The model did not become available within {minutes} minutes: check the log.")
              .format(minutes=timeout // 60), "err")

    def _probe_ovms(self, name):
        """Return (REST server reachable, model ready, ids exposed on /v3/models)."""
        rest_up, ids = False, []
        try:
            r = requests.get(f"{OVMS_URL}/v3/models", timeout=3)
            rest_up = True
            if r.ok:
                ids = [m.get("id") for m in r.json().get("data", [])]
        except (requests.RequestException, ValueError):
            pass
        if name in ids:
            return True, True, ids
        try:
            # some model types (e.g. speech2text) may not show up in /v3/models
            r = requests.get(f"{OVMS_URL}/v2/models/{name}/ready", timeout=3)
            return True, r.status_code == 200, ids
        except requests.RequestException:
            return rest_up, False, ids

    def show_container_logs(self):
        if not self.require_runtime():
            return

        def job(t):
            t.log(tr("Last lines of the {container} log").format(container=CONTAINER_NAME), "head")
            res = subprocess.run(runtime_cmd("logs", "--tail", "80", CONTAINER_NAME),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in res.stdout.splitlines():
                if line.strip():
                    t.log("  " + line.strip(), "err" if "[error]" in line.lower() else "info")

        self.run_task(job, tr("Reading container log"))

    def stop_server(self):
        if not self.require_runtime():
            return
        self.stop_wait.set()  # interrupts a wait for the model in progress

        def job(t):
            t.log(tr("Stopping the server…"), "head")
            subprocess.run(runtime_cmd("stop", CONTAINER_NAME), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(runtime_cmd("rm", CONTAINER_NAME), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            t.log(tr("Server stopped."), "ok")

        self.run_task(job, tr("Stopping server"), [self.btn_start, self.btn_stop], lambda _: self.check_server_status())

    def test_api(self):
        """Test the API the way Open WebUI would: model list + a real chat request."""
        base = f"{OVMS_URL}/v3"

        def job(t):
            t.log(tr("OVMS OpenAI API test"), "head")
            try:
                r = requests.get(f"{base}/models", timeout=5)
            except requests.RequestException as e:
                t.log("1/2 ✗ " + tr("{url} unreachable: {error}").format(url=f"{base}/models", error=e), "err")
                t.log(tr("The server is not listening: start it or check “Container log”."), "err")
                return
            if not r.ok:
                t.log(f"1/2 ✗ {base}/models → HTTP {r.status_code}: {r.text[:300]}", "err")
                return
            ids = [m.get("id") for m in r.json().get("data", [])]
            t.log(f"1/2 ✓ {base}/models → {', '.join(ids) or tr('no models')}", "ok" if ids else "warn")
            if not ids:
                t.log(tr("No model exposed: Open WebUI will not see anything."), "err")
                return
            name = self.served_name if self.served_name in ids else ids[0]

            payload = {"model": name, "max_tokens": 512,
                       "messages": [{"role": "user", "content": tr("Hi! Answer with a single sentence.")}]}
            t.log("2/2 " + tr("Sending a chat request to “{name}” (the first one can be slow)…").format(name=name))
            started = time.time()
            try:
                r = requests.post(f"{base}/chat/completions", json=payload, timeout=300)
            except requests.RequestException as e:
                t.log(f"2/2 ✗ /chat/completions: {e}", "err")
                return
            if not r.ok:
                t.log(f"2/2 ✗ /chat/completions → HTTP {r.status_code}: {r.text[:500]}", "err")
                return
            try:
                data = r.json()
                message = data["choices"][0]["message"]
                answer = message.get("content") or ""
                if not answer.strip() and message.get("reasoning_content"):
                    answer = tr("(reasoning only, the answer has not started yet)") + " " + message["reasoning_content"]
                usage = data.get("usage", {})
            except (ValueError, KeyError, IndexError):
                t.log("2/2 ✗ " + tr("unexpected answer: {text}").format(text=r.text[:500]), "err")
                return
            t.log("2/2 ✓ " + tr("answer in {seconds} s ({tokens} tokens): {answer}").format(
                seconds=f"{time.time() - started:.1f}", tokens=usage.get("completion_tokens", "?"),
                answer=answer.strip()[:300]), "ok")
            t.log(tr("The API works. In Open WebUI use {url}, or {container_url} if Open WebUI runs in a container.")
                  .format(url=f"{OVMS_URL}/v3", container_url=CONTAINER_URL), "ok")

        self.run_task(job, tr("API test"))

    # --- HF catalog ------------------------------------------------------------
    def load_catalog(self, force=False):
        def job(_t):
            return fetch_official(force)

        def done(res):
            self.official, cached = res
            src = tr("cache") if cached else "Hugging Face"
            self.log(tr("OpenVINO catalog loaded from {source}: {n} models.").format(source=src, n=len(self.official)),
                     "ok")
            self._rebuild_catalog()

        def fallback(_msg):
            cached = read_cached_catalog()
            if cached is not None:
                self.official = cached
                self.log(tr("Network not available, using the cached catalog."), "warn")
            else:
                self.log(tr("Cannot load the Hugging Face catalog (offline?). Press “Refresh catalog” to retry."),
                         "warn")
            self._rebuild_catalog()

        task = self.run_task(job, tr("Loading the Hugging Face catalog"), [self.btn_refresh_hf], done)
        task.failed.connect(fallback)
        if self.chk_community.isChecked():
            self._fetch_community()

    def _fetch_community(self):
        query = self.search.text().strip()

        def done(rows):
            self.community = rows
            self._rebuild_catalog()
            found = (tr("Community: {n} OpenVINO repos found for “{query}”") if query else
                     tr("Community: {n} OpenVINO repos found")).format(n=len(rows), query=query)
            self.log(found + " · " + tr("{n} visible with the current filters.").format(n=self.proxy.rowCount()),
                     "ok")

        self.run_task(lambda _t: fetch_community(query), tr("Searching community repos"), on_done=done)

    def _rebuild_catalog(self):
        rows = self.official + (self.community if self.chk_community.isChecked() else [])
        self.catalog_loaded = True
        self.catalog.set_rows(rows)
        self._apply_filters(auto=True)

    def _on_community_toggled(self, on):
        if on:
            self._fetch_community()
        else:
            self._rebuild_catalog()

    def _on_search(self, text):
        self.proxy.search = text.strip()
        self._apply_filters(auto=True)
        if self.chk_community.isChecked():
            # debounce: query HF only when the user stops typing
            if not hasattr(self, "_search_timer"):
                self._search_timer = QTimer(self, singleShot=True, interval=600, timeout=self._fetch_community)
            self._search_timer.start()

    def _apply_filters(self, auto=False):
        """auto=True when search/data change: if the selected type has no results
        but other types do, switch to "All types" automatically."""
        self.proxy.quants = {q for q, b in self.quant_chips.items() if b.isChecked()}
        counts = {}
        for m in self.catalog.rows:
            if self.proxy.matches(m):
                counts[m["category"]] = counts.get(m["category"], 0) + 1
        total = sum(counts.values())
        category = self.cb_category.currentData()
        if auto and category and not counts.get(category) and total:
            self.cb_category.blockSignals(True)
            self.cb_category.setCurrentIndex(0)
            self.cb_category.blockSignals(False)
            category = None
        for i in range(self.cb_category.count()):
            key = self.cb_category.itemData(i)
            label = tr(CATEGORIES[key][0]) if key else tr("All types")
            self.cb_category.setItemText(i, f"{label}  ({counts.get(key, 0) if key else total})")
        self.proxy.category = category
        self.proxy.invalidate()
        visible = self.proxy.rowCount()
        if visible == 0:
            if total:
                self.empty_hint.setText(tr("No results in “{type}”.\n{n} models match in other types: choose "
                                           "“All types”.").format(type=tr(CATEGORIES[category][0]), n=total))
            elif self.catalog.rowCount():
                self.empty_hint.setText(tr("No model matches the search or the filters."))
            elif getattr(self, "catalog_loaded", False):
                self.empty_hint.setText(tr("The catalog could not be loaded. Check your connection and press "
                                           "“Refresh catalog”."))
            else:
                self.empty_hint.setText(tr("Loading the catalog…"))
            self.empty_hint.setGeometry(self.table.viewport().rect().adjusted(20, 20, -20, -20))
            self.empty_hint.show()
        else:
            self.empty_hint.hide()
        # redundant columns hidden to leave room for the model name
        self.table.setColumnHidden(2, self.proxy.category is not None)
        self.table.setColumnHidden(3, not self.chk_community.isChecked())
        self._fit_name_column()
        self.lbl_count.setText(tr("{n} of {total} models").format(n=self.proxy.rowCount(),
                                                                   total=self.catalog.rowCount()))

    def eventFilter(self, obj, event):
        if obj is self.table.viewport() and event.type() == event.Type.Resize:
            self._fit_name_column()
            self.empty_hint.setGeometry(obj.rect().adjusted(20, 20, -20, -20))
        return super().eventFilter(obj, event)

    def _fit_name_column(self):
        """The Model column takes the free space, but never less than 260px (then the scrollbar appears)."""
        hh = self.table.horizontalHeader()
        others = sum(hh.sectionSize(c) for c in range(1, self.catalog.columnCount()) if not hh.isSectionHidden(c))
        hh.resizeSection(0, max(260, self.table.viewport().width() - others))

    def _palette_commands(self, query):
        """What the command palette offers: pages, server actions, local models and a direct catalog search."""
        C_ = kit.Command
        go = tr("Go to")
        commands = [C_(tr(label), lambda i=i: self.nav.setCurrentRow(i), go, icon=self.nav_icons[i])
                    for i, label in enumerate(NAV_PAGES)]
        server = tr("Server")
        help_ = tr("Help")
        commands += [
            C_(tr("Check for updates"), self.check_updates_now, help_, icon="download"),
            C_(tr("Turn automatic update checks off") if self.updates.enabled else
               tr("Turn automatic update checks on"), self.toggle_update_checks, help_, icon="settings"),
            C_(tr("Start server"), self.start_server, server, icon="play"),
            C_(tr("Stop"), self.stop_server, server, icon="stop", keywords="server"),
            C_(tr("Test API"), self.test_api, server, icon="flask"),
            C_(tr("Container log"), lambda: (self.toggle_console(True), self.show_container_logs()), server, icon="log"),
            C_(tr("Copy the endpoint URL"), lambda: self._copy(f"{OVMS_URL}/v3"), server, icon="copy"),
            C_(tr("Show or hide the console"), self.toggle_console, tr("Console"), icon="chevron_up"),
            C_(tr("Refresh catalog"), lambda: self.load_catalog(force=True), tr("Explore Hugging Face"), icon="refresh"),
            C_(tr("Refresh"), self.refresh_local_models, tr("Local library"), icon="refresh"),
        ]
        for code, name in THEMES:
            commands.append(C_(tr("Theme: {name}").format(name=tr(name)),
                               lambda c=code: self.cb_theme.setCurrentIndex(self.cb_theme.findData(c)),
                               tr("Theme"), icon="theme"))
        for i in range(self.cb_models.count()):
            rel = self.cb_models.itemText(i)
            commands.append(C_(tr("Use in server: {model}").format(model=rel),
                               lambda r=rel: (self.cb_models.setCurrentText(r), self.nav.setCurrentRow(0)),
                               tr("Local library"), icon="server"))
        if query:
            def search(q=query):
                self.nav.setCurrentRow(1)
                self.search.setText(q)
            commands.append(C_(tr("Search the catalog: {query}").format(query=query), search,
                               tr("Explore Hugging Face"), icon="search", score=0.5))
        return commands

    def _on_select(self, current, _prev):
        self.chip_ram.hide()
        if not current.isValid():
            return
        m = self.proxy.data(current, Qt.UserRole + 1)
        self.selected_model = m
        self.d_title.setText(m["name"])
        self.d_repo.setText(m["id"])
        quant = f"{m['quant']} {m['variant']}".strip()
        qc = QUANT_COLORS.get(m["quant"], "#64748b")
        official = (f"<span style='color:{C['accent']}'>✓ {tr('Official OpenVINO')}</span>" if m["official"]
                    else f"<span style='color:{C['warn']}'>{tr('Community')}</span>")
        self.d_badges.setText(f"<span style='color:{qc};font-weight:700'>■ {quant}</span>"
                              f"&nbsp;&nbsp;·&nbsp;&nbsp;{official}")
        label, task, _ = CATEGORIES[m["category"]]
        rows = [
            (tr("Type"), tr(label) + (f" <span style='color:{C['muted']}'>({m['pipeline']})</span>"
                                      if m["pipeline"] else "")),
            (tr("OVMS task"), task or f"<span style='color:{C['warn']}'>{tr('not supported by --pull')}</span>"),
            (tr("Base model"), m["base_model"] or "—"),
            (tr("Updated"), rel_time(m["updated"])),
            (tr("Size"), tr("computing…")),
        ]
        self._detail_rows = rows
        self._render_detail()
        self.btn_download.setEnabled(task is not None)
        self.btn_hf.setEnabled(True)
        self._fetch_size(m["id"])

    def _show_ram_fit(self, size):
        """At a glance: does the model fit in this computer's memory?"""
        try:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            ram = 0
        if not size or not ram:
            self.chip_ram.hide()
            return
        ratio = size / ram
        text, kind = ((tr("Fits in your {ram} of RAM"), "ok") if ratio <= 0.6 else
                      (tr("Tight for your {ram} of RAM"), "warn") if ratio <= 0.9 else
                      (tr("Larger than your {ram} of RAM"), "err"))
        self.chip_ram.set(text.format(ram=human_size(ram)), kind=kind)
        self.chip_ram.show()

    def _render_detail(self):
        self.d_info.hide()
        while self.d_grid.count():
            self.d_grid.takeAt(0).widget().deleteLater()
        for r, (k, v) in enumerate(self._detail_rows):
            key = QLabel(k)
            key.setObjectName("muted")
            val = QLabel(v)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.d_grid.addWidget(key, r, 0, Qt.AlignTop)
            self.d_grid.addWidget(val, r, 1)

    def _fetch_size(self, repo_id):
        def job(_t):
            r = requests.get(f"{HF_API}/{repo_id}", params=[("expand[]", "usedStorage")], timeout=15)
            r.raise_for_status()
            return repo_id, r.json().get("usedStorage")

        def done(res):
            rid, size = res
            if self.selected_model and self.selected_model["id"] == rid:
                self._detail_rows[-1] = (tr("Size"), f"≈ {human_size(size)}" if size else "—")
                self._render_detail()
                self._show_ram_fit(size)

        task = Task(job, self)
        self.tasks.add(task)
        task.result.connect(done)
        task.finished.connect(lambda: (self.tasks.discard(task), task.deleteLater()))
        task.start()

    def open_on_hf(self):
        if self.selected_model:
            webbrowser.open(f"https://huggingface.co/{self.selected_model['id']}")

    def download_selected(self):
        m = self.selected_model
        if not m:
            return
        task = CATEGORIES[m["category"]][1]
        if not task:
            self.log(tr("{model}: this model type cannot be downloaded with ovms --pull.").format(model=m["id"]),
                     "warn")
            return
        self._download(m["id"], task)

    def download_custom(self):
        repo = self.entry_custom_dl.text().strip()
        if not repo:
            return
        task = CATEGORIES[detect_category(repo, None)][1] or "text_generation"
        self._download(repo, task)

    def _download(self, model_id, ovms_task):
        if not self.require_runtime():
            return

        def job(t):
            t.log(tr("Download · {model}").format(model=model_id), "head")
            t.log(tr("Task: {task} · Destination: {folder}").format(task=ovms_task, folder=MODELS_DIR))
            ret = t.run_cmd(container_run("--pull", "--source_model", model_id,
                                          "--model_repository_path", "/models", "--task", ovms_task))
            if ret == 0:
                t.log(tr("Download complete! The model is in {folder}.").format(folder=MODELS_DIR), "ok")
            else:
                t.log(tr("Download error (exit code {code}).").format(code=ret), "err")
            return ret

        self.run_task(job, tr("Downloading {model}").format(model=model_id.split("/")[-1]),
                      [self.btn_download, self.btn_download_custom], lambda _: self.refresh_local_models())

    # --- Converter -------------------------------------------------------------
    def _precision(self):
        return self.prec_group.checkedButton().property("prec")

    def _suggest_output_name(self):
        if not hasattr(self, "entry_output_name"):
            return
        base = self.entry_hf_id.text().strip().split("/")[-1].lower()
        if base:
            self.entry_output_name.setText(f"{base}-{self._precision()}-ov")

    def start_conversion(self):
        hf_id = self.entry_hf_id.text().strip()
        prec = self._precision()
        out_name = self.entry_output_name.text().strip()
        if not hf_id or not out_name:
            self.log(tr("Fill in every field before starting the conversion."), "err")
            return
        dest_path = os.path.normpath(os.path.join(MODELS_DIR, out_name))
        if not dest_path.startswith(MODELS_DIR + os.sep):
            self.log(tr("The destination must be a folder inside {folder}.").format(folder=MODELS_DIR), "err")
            return
        if not find_optimum():
            self.log(tr("optimum-cli not found. Run install.sh --with-optimum (or install optimum[openvino] in the "
                        "app's environment) and restart OVMS Manager."), "err")
            return
        trust = self.chk_trust.isChecked()
        cli_args = ["export", "openvino", "--model", hf_id, "--weight-format", prec]
        if trust:
            cli_args.append("--trust-remote-code")
        cli_args.append(dest_path)
        cmd = optimum_command(cli_args)

        def job(t):
            t.log(tr("Optimum conversion · {model} → {precision}").format(model=hf_id, precision=prec.upper()),
                  "head")
            t.log(tr("Destination: {path}").format(path=dest_path))
            if trust:
                t.log(tr("Custom code from the repository allowed (--trust-remote-code)."), "warn")
            t.log(f"Python: {cmd[0]}")
            ret = t.run_cmd(cmd, display="optimum-cli " + " ".join(cli_args))
            if ret == 0:
                t.log(tr("Conversion completed successfully!"), "ok")
                return
            output = "\n".join(t.output_tail)
            if "trust_remote_code" in output and not trust:
                t.log(tr("Conversion failed: {model} contains custom Python code. If you trust the repository, "
                         "enable “Allow custom code” in Advanced options and try again.").format(model=hf_id), "err")
            elif re.search(r"not supported|unsupported|is not (yet )?supported|Unrecognized configuration", output,
                           re.IGNORECASE):
                t.log(tr("Conversion failed: this architecture is probably not supported by optimum-intel. "
                         "Look for an already converted version of the model in the Explore section."), "err")
            else:
                t.log(tr("Conversion failed (exit code {code}): check the error in the log above.").format(code=ret),
                      "err")

        self.run_task(job, tr("Optimum conversion"), [self.btn_convert], lambda _: self.refresh_local_models())

    def closeEvent(self, event):
        running = [t for t in self.jobs if t.isRunning()]
        if running:
            ok = QMessageBox.question(self, tr("Operations in progress"),
                                      tr("Some operations are still running (download/conversion). Quit anyway?"),
                                      QMessageBox.Yes | QMessageBox.No)
            if ok != QMessageBox.Yes:
                event.ignore()
                return
        self.status_timer.stop()
        for t in list(self.tasks):
            t.wait(3000)
        event.accept()
        if any(t.isRunning() for t in self.tasks):
            os._exit(0)  # threads blocked on a subprocess cannot be interrupted cleanly


def init_theme(app):
    """Fusion style, bundled fonts and the light/dark theme saved in state.json (default: follow the system)."""
    global THEME
    state = load_state()
    app.setStyle("Fusion")
    kit.load_fonts(app)
    StatusPill.reduce_motion = state.get("reduce_motion", kit.reduce_motion_default())
    THEME = kit.ThemeManager(app, ACCENT, state.get("theme", "system"), app_qss)
    return THEME


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("OVMS Manager")
    # must match the name of the .desktop file created by install.sh: that way GNOME/KDE associate
    # the window with the menu entry (right icon in the dock)
    app.setDesktopFileName(APP_ID)
    setup_language(app, load_state().get("ui_language", ""))
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{APP_ID}.svg")
    app.setWindowIcon(QIcon.fromTheme(APP_ID, QIcon(icon_path)))
    init_theme(app)
    win = OVMSManager()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
