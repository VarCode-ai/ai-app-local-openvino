# Troubleshooting

Problems common to both apps, grouped by area. App-specific tips are in the
[OVMS Manager](../ovms-manager/README.md#troubleshooting) and [Lectern](../lectern/README.md#tips-and-troubleshooting)
READMEs.

## Installation

### `Cannot create a Python virtual environment`

Debian, Ubuntu and their derivatives ship the `venv` module in a separate package:

```bash
sudo apt install python3-venv
```

### `pip could not install` / `No matching distribution found for openvino-genai`

OpenVINO and PySide6 publish wheels for specific Python versions. A distribution that has just moved to a
new Python may be ahead of them, and an old one (openSUSE Leap ships Python 3.6 as `python3`) is too far
behind. Install a supported Python next to the system one and point the installer to it:

```bash
sudo zypper install python311        # openSUSE Leap
sudo dnf install python3.12          # Fedora
PYTHON=python3.12 ./lectern/install.sh
```

The installer recreates the environment when the Python version changes.

### The app is not in the menu, or the command is not found

- Some desktops refresh the menu only after you log out and back in.
- `~/.local/bin` is not on the `PATH` of every distribution (for example Arch). Add it to your shell
  profile:

  ```bash
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
  ```

## Starting the apps

### `Could not load the Qt platform plugin "xcb"`

On X11 sessions Qt 6 needs `libxcb-cursor`:

| Distribution | Command |
| --- | --- |
| Debian, Ubuntu | `sudo apt install libxcb-cursor0` |
| Fedora | `sudo dnf install xcb-util-cursor` |
| Arch | `sudo pacman -S xcb-util-cursor` |
| openSUSE | `sudo zypper install libxcb-cursor0` |

To see which library is missing, start the app from a terminal with `QT_DEBUG_PLUGINS=1 lectern`. On a
Wayland session you can also force the native backend with `QT_QPA_PLATFORM=wayland`.

### The app starts but shows no window

Lectern runs a single instance: starting it again brings the existing window to the front. If an old
process hangs, close it with `pkill -f lectern.py`.

## Hardware acceleration

### Intel GPU not used by Lectern

Lectern runs Whisper directly on your system, which needs the Intel GPU compute runtime:

| Distribution | Command |
| --- | --- |
| Debian, Ubuntu | `sudo apt install intel-opencl-icd` |
| Fedora | `sudo dnf install intel-compute-runtime` |
| Arch | `sudo pacman -S intel-compute-runtime` |
| openSUSE | `sudo zypper install intel-opencl` |

The packages in older LTS releases (for example Ubuntu 22.04) may not support recent GPUs such as Arc or
Core Ultra graphics. In that case install the packages from the
[Intel compute-runtime releases](https://github.com/intel/compute-runtime/releases).

Your user must also be able to open `/dev/dri/renderD*`. On most desktops this is granted automatically
to the logged-in user. If it isn't, add yourself to the `render` group and log in again:

```bash
sudo usermod -aG render "$USER"
```

To check what OpenVINO sees:

```bash
~/.local/share/lectern/venv/bin/python -c "import openvino as ov; print(ov.Core().available_devices)"
```

If the GPU is missing from the list, the problem is the driver, not Lectern. Lectern falls back to the CPU
automatically and says so in the status line.

### Intel NPU not detected

The NPU needs the [Linux NPU driver](https://github.com/intel/linux-npu-driver) (packaged as
`intel-npu-driver` in some distributions) and a recent kernel with the `intel_vpu` module (the newer the
processor, the newer the kernel it needs). After installing it, `/dev/accel/accel0` must exist and be accessible to your user (usually
through the `render` group).

Not every model runs on the NPU. For LLMs prefer the `INT4 CW` variants. If Whisper fails on the NPU, use
the GPU or the CPU.

### The GPU is from AMD or NVIDIA

OpenVINO supports Intel GPUs and NPUs only. On other hardware use the CPU mode. OVMS Manager marks
non-Intel devices as *not Intel · unsupported*.

## Containers (OVMS Manager)

### Docker: `permission denied while trying to connect to the Docker daemon socket`

Your user may not use Docker. Either add it to the `docker` group (this grants root-equivalent access)
and log in again, or use rootless Docker or Podman:

```bash
sudo usermod -aG docker "$USER"
```

### GPU not available inside the container with rootless Podman

On some distributions the render node is only accessible through a supplementary group, which rootless
containers drop by default. Keep your groups in the container:

```bash
OVMS_EXTRA_RUN_ARGS="--group-add keep-groups" ovms-manager
```

To make it permanent, add `export OVMS_EXTRA_RUN_ARGS="--group-add keep-groups"` to `~/.profile` and log
in again: the menu entry inherits the variables of your session.

### SELinux denials

The models folder is mounted with the shared SELinux label (`:z`), which works on Fedora and RHEL. If you
moved `OPENVINO_MODELS_DIR` to a location with a special label (another disk, an NFS share), relabel it
or check `sudo ausearch -m avc -ts recent`.

### Files in `~/models` that you cannot delete

With rootless Podman, files written by the container belong to a sub-UID of your user. The **Delete**
button of the Local library page handles this. From a terminal use:

```bash
podman unshare rm -rf ~/models/<folder>
```

## Audio (Lectern)

### No microphone, or the recording stays silent

- Check that the microphone is not muted and is selected as input in your system sound settings.
- Lectern uses Qt Multimedia, which talks to PulseAudio or to PipeWire through its PulseAudio
  compatibility layer. On minimal installations install `pipewire-pulse` (or `pulseaudio`) and the
  `libpulse` library.
- If the microphone stops sending audio during a lecture, Lectern reopens it automatically and shows a
  warning.

### `ffmpeg not found` (Fedora)

Fedora's `ffmpeg-free` package is enough for common formats. For formats with patent restrictions (for
example some AAC or WMA files) install `ffmpeg` from [RPM Fusion](https://rpmfusion.org).

## Getting help

When you open an issue, include your distribution and version, the output of the installer, and the
relevant log:

- Lectern: **Help → Open log file** (`~/.cache/lectern/lectern.log`).
- OVMS Manager: the console at the bottom of the window, and **Container log**.
