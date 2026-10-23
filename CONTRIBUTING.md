# Contributing

Thanks for helping! Bug reports, fixes, translations of the documentation and new features are all
welcome.

## Reporting a bug

Open an issue with:

- your distribution and version, and your hardware (CPU, GPU, NPU);
- what you did, what you expected, and what happened;
- the relevant log: the console of OVMS Manager, or `~/.cache/lectern/lectern.log` for Lectern.

## Running from a clone

The installers have a development mode: the menu entry and the commands run the files in your clone, so
every change is live at the next start.

```bash
./ovms-manager/install.sh --dev
./lectern/install.sh --dev
```

You can also start the apps from a terminal to see their output:

```bash
~/.local/share/ovms-manager/venv/bin/python ovms-manager/ovms_manager.py
./lectern/lectern.sh
```

## Code style

- Python 3.10+, standard library plus the packages in the `requirements.txt` files. Keep each app a
  single file that runs without being installed as a package.
- Follow the style of the surrounding code: 120-character lines, short comments that explain *why*.
- User-visible text is written in English and wrapped in `tr()` (see [Translations](#translations)).
- Shell scripts use `bash` with `set -euo pipefail`, must work on every distribution listed in the
  README, and must not require root.

## Building a new app of the ecosystem

New apps reuse the shared kit in [common/ui_kit.py](common/ui_kit.py), so the whole ecosystem looks and
behaves the same:

```python
import ui_kit as kit
C = kit.T                                    # colors of the current theme
kit.load_fonts(app)                          # Inter for the interface, Newsreader for reading
theme = kit.ThemeManager(app, "ember", "system", lambda t: kit.base_qss(t) + my_qss(t))
button = kit.IconButton("download", tr("Download"), "primary", slot)
palette = kit.CommandPalette(window, provider, tr("Type a command…"), tr("No results"))
```

- Pick an accent in `ACCENTS`, or add a new one and check it with `python tools/check_contrast.py`.
- Draw with the theme tokens (`C["text"]`, `C["accent_soft"]`…), never with fixed colors. Painted
  widgets read them at paint time; anything else connects to `kit.signals.changed` (or uses
  `kit.Themed`) so it follows live theme switches.
- Use `kit.icon()` instead of emoji, which some distributions cannot display.
- Add the kit to the installer as in the existing `install.sh` (`COMMON_FILES`).

## Translations

User-visible text is written in English in the code and wrapped in `tr("…")`. Text defined before the
language is known, such as tables of constants, is marked with `N_("…")` and passed to `tr()` where it is
shown. Use named placeholders and `.format()`, never f-strings, inside `tr()`:

```python
tr("Cannot create {path}:\n{error}").format(path=path, error=e)
```

Each app keeps its translations in `locale/<language>.json`, a map from the English text to the
translation. To add a language, for example German:

1. Copy `lectern/locale/it.json` to `lectern/locale/de.json` (and the same for `ovms-manager`) and
   translate the values. Keep the `{placeholders}` unchanged.
2. Add `("de", "Deutsch")` to `UI_LANGUAGES` in `lectern.py` and `ovms_manager.py`.
3. Add `locale/de.json` to `FILES` in both `install.sh`.
4. In the menu entry written by each `install.sh`, add `GenericName[de]=`, `Comment[de]=` and
   `Keywords[de]=` next to the Italian ones: desktops show them when the system language is German.
5. Run `python tools/check_translations.py`.

When you change or add a text in the code, update every catalog. `python tools/check_translations.py
--missing` prints the texts still to translate.

## Checks before a pull request

```bash
pip install pyflakes shellcheck-py
python -m pyflakes common/*.py ovms-manager/*.py lectern/*.py tools/*.py
shellcheck ovms-manager/*.sh lectern/*.sh
python tools/check_translations.py
python tools/check_contrast.py
```

Then test what you changed by hand. For transcription changes, a short recording and a file with
`lectern-transcribe` (for example with `OpenVINO/whisper-tiny-int8-ov` on the CPU) cover most paths.

## Releasing a new version

The apps compare their version with the latest GitHub release to show the update banner, so a release
needs three steps:

1. Raise `APP_VERSION` in `lectern/lectern.py` and `ovms-manager/ovms_manager.py`, using the same
   [semantic version](https://semver.org/) for both, and add the changes to `CHANGELOG.md`.
2. Commit, then tag with a `v` prefix and push the tag: `git tag -a v1.1.0 -m "1.1.0"` and
   `git push origin v1.1.0`.
3. The *Release binaries* workflow starts when the tag arrives. It builds the AppImages and attaches them,
   with `SHA256SUMS`, to the release of the tag, creating the release if needed. Then edit the release on
   GitHub and paste the CHANGELOG section as its notes. It must be published, not a draft or a
   pre-release: the apps only see the latest published release.

To build the AppImages on your machine (about 1 GB of downloads the first time):

```bash
packaging/build-appimage.sh            # both apps into dist/
packaging/build-appimage.sh lectern    # only one
```

For a tag that was released before the workflow existed, run *Release binaries* by hand from the
Actions tab and type the tag.

## License

By contributing you agree that your contributions are licensed under the
[GNU GPL v3.0 or later](LICENSE), the license of the project.
