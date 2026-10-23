#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the WCAG 2.2 contrast of the design tokens in common/ui_kit.py.

For every theme (light, dark) and every app accent, each foreground/background pair actually used by the apps
must reach 4.5:1 (text, WCAG 1.4.3) or 3:1 (focus rings and graphical indicators, WCAG 1.4.11).
Exit code 1 if any pair fails.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common"))
import ui_kit  # noqa: E402

TEXT, UI = 4.5, 3.0
PAIRS = [
    # (foreground, background, minimum, what)
    ("text", "bg", TEXT, "body text"), ("text", "surface", TEXT, "text on cards"),
    ("text", "sidebar", TEXT, "sidebar text"), ("text", "accent_soft", TEXT, "text on selections"),
    ("text", "hover", TEXT, "text on hovered rows"),
    ("muted", "bg", TEXT, "secondary text"), ("muted", "surface", TEXT, "secondary text on cards"),
    ("muted", "sidebar", TEXT, "secondary sidebar text"),
    ("accent_text", "surface", TEXT, "accent text"), ("accent_text", "bg", TEXT, "accent text on background"),
    ("accent_text", "accent_soft", TEXT, "soft buttons and checked chips"),
    ("accent_fg", "accent", TEXT, "primary buttons"), ("accent_fg", "accent_hover", TEXT, "hovered primary buttons"),
    ("ok", "surface", TEXT, "success messages"), ("warn", "surface", TEXT, "warnings"),
    ("err", "surface", TEXT, "errors"), ("err", "bg", TEXT, "errors on background"),
    ("accent", "surface", UI, "focus ring on cards"), ("accent", "bg", UI, "focus ring on background"),
    ("accent_fg", "accent", UI, "focus ring inside primary buttons"),
    ("console_text", "console_bg", TEXT, "console"), ("console_muted", "console_bg", TEXT, "console timestamps"),
    ("console_ok", "console_bg", TEXT, "console ok"), ("console_warn", "console_bg", TEXT, "console warnings"),
    ("console_err", "console_bg", TEXT, "console errors"), ("console_cmd", "console_bg", TEXT, "console commands"),
]


def luminance(hex_color):
    channels = [int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main():
    failed = 0
    for accent in ui_kit.ACCENTS:
        for scheme in ui_kit.NEUTRALS:
            t = ui_kit.tokens(scheme, accent)
            worst = min(((contrast(t[fg], t[bg]), fg, bg) for fg, bg, _, _ in PAIRS))
            print(f"{accent}/{scheme}: lowest ratio {worst[0]:.2f} ({worst[1]} on {worst[2]})")
            for fg, bg, minimum, what in PAIRS:
                ratio = contrast(t[fg], t[bg])
                if ratio < minimum:
                    failed += 1
                    print(f"  FAIL {what}: {fg} {t[fg]} on {bg} {t[bg]} = {ratio:.2f} (needs {minimum})")
    print("all pairs pass" if not failed else f"{failed} pairs fail")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
