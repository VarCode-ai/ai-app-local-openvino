# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [1.0.0] — 2026-10-23

First public release.

### Apps

- **OVMS Manager**: a desktop front end for OpenVINO Model Server.
  - Browse the OpenVINO models on Hugging Face and download them.
  - Serve them through an OpenAI-compatible API on the Intel CPU, GPU or NPU.
  - Convert other models with Optimum.
  - Runs the server with Podman or Docker.
- **Lectern**: a lecture notebook with live speech-to-text (Whisper on OpenVINO GenAI).
  - Organized by course, lecture and category, and saved as plain text files.
  - Transcribes already recorded audio and video files.
  - Exports a lecture to Markdown.
- **lectern-transcribe**: batch transcription of a folder from the terminal, with resume, audio cleanup and
  automatic model download.

### Design

- Shared UI kit for the whole ecosystem (`common/ui_kit.py`).
- Light and dark themes that follow the system and switch live.
- Command palette (Ctrl+K) in both apps.
- Bundled Inter and Newsreader fonts.
- Live waveform and local-processing indicator in Lectern.
- RAM fit indicator in OVMS Manager.
- WCAG 2.2 AA contrast in both themes, visible focus rings, and a *Reduce motion* option.

### Platform

- English and Italian interface, following the system language.
- Per-user installers for every major glibc distribution: Debian/Ubuntu, Fedora (also Atomic), Arch and
  openSUSE. They check for missing packages and do not require root.
- Automatic fallback from GPU or NPU to the CPU when an accelerator cannot be used.
- Update notifications based on the GitHub releases: checked at most once a day, and they can be turned off.

[1.0.0]: https://github.com/VarCode-ai/ai-app-local-openvino/releases/tag/v1.0.0
