#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the translation catalogs of every app.

Each app keeps its translations in <app>/locale/<language>.json, a {"English text": "translation"} map.
This script collects the English texts passed as literals to tr() and N_() in the app's Python files and
reports, for each catalog:
  - missing:      texts without a translation (shown in English);
  - unused:       translations whose text no longer exists in the code;
  - placeholders: translations whose {name} placeholders differ from the English text.
It also rejects tr()/N_() calls whose argument is not a plain string (e.g. an f-string), which could never
be found in a catalog.

Usage:
    python tools/check_translations.py            # check, exit code 1 on problems
    python tools/check_translations.py --missing  # print the missing texts as JSON, ready to translate
"""
import ast
import glob
import json
import os
import string
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKERS = {"tr", "N_"}


def texts_in(path):
    """(texts marked in the file, problems found)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    texts, problems = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in MARKERS:
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                texts.add(arg.value)
            elif not isinstance(arg, (ast.Name, ast.Attribute, ast.Subscript)):  # variables hold N_() texts
                problems.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}: {node.func.id}() needs a plain "
                                "string, not an expression or f-string")
    return texts, problems


def placeholders(text):
    return sorted({name for _, name, _, _ in string.Formatter().parse(text) if name is not None})


def main():
    dump_missing = "--missing" in sys.argv
    failed = False
    for locale_dir in sorted(glob.glob(os.path.join(ROOT, "*", "locale"))):
        app_dir = os.path.dirname(locale_dir)
        app = os.path.basename(app_dir)
        texts, problems = set(), []
        for path in glob.glob(os.path.join(app_dir, "*.py")):
            t, p = texts_in(path)
            texts |= t
            problems += p
        for catalog_path in sorted(glob.glob(os.path.join(locale_dir, "*.json"))):
            with open(catalog_path, encoding="utf-8") as f:
                catalog = json.load(f)
            lang = os.path.splitext(os.path.basename(catalog_path))[0]
            missing = sorted(texts - catalog.keys())
            unused = sorted(catalog.keys() - texts)
            bad = [k for k in catalog if k in texts and placeholders(k) != placeholders(catalog[k])]
            empty = [k for k, v in catalog.items() if not isinstance(v, str) or not v.strip()]
            if dump_missing:
                print(json.dumps({k: "" for k in missing}, ensure_ascii=False, indent=2))
                continue
            print(f"{app}/{lang}: {len(texts)} texts, {len(missing)} missing, {len(unused)} unused, "
                  f"{len(bad)} with wrong placeholders")
            for title, items in (("missing", missing), ("unused", unused), ("wrong placeholders", bad),
                                 ("empty", empty)):
                for item in items:
                    print(f"  {title}: {item!r}")
            failed |= bool(missing or bad or empty or unused)
        for problem in problems:
            print("  " + problem)
        failed |= bool(problems)
    sys.exit(1 if failed and not dump_missing else 0)


if __name__ == "__main__":
    main()
