#!/usr/bin/env python3
"""Static i18n checker for the Voice Atelier web UI.

No JavaScript execution, no third-party dependencies. It parses:
  * web/js/i18n.js          -> the ru/en dictionaries (keys, placeholders)
  * web/index.html          -> data-i18n / data-i18n-title attributes
  * web/app.js, web/js/*.js -> every t('literal') key, every dotted string that
                               starts with a known i18n prefix (dynamic maps such
                               as TAB_TITLES, JOB_KIND_LABEL, ERROR_KEYS,
                               DESIGN_PRESETS), and the documented template keys.

Exit code 0 when every used key exists in both locales and ru/en key sets match.
Exit code 1 otherwise, printing an actionable inventory.

Usage: python verification/check_i18n.py [--root <repo>] [--quiet]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PREFIXES = (
    "a11y",
    "banner",
    "brand",
    "common",
    "design",
    "errors",
    "guide",
    "jobs",
    "models",
    "nav",
    "status",
    "studio",
    "take",
    "toast",
    "voices",
)

# Keys produced through template literals in the UI. Kept explicit so a missing
# dictionary entry still fails when the code path is dynamic.
DYNAMIC_KEYS = {
    "jobs.${": [
        "jobs.queued",
        "jobs.running",
        "jobs.done",
        "jobs.failed",
        "jobs.canceled",
    ],
    "take.warn.${": [
        "take.warn.possible_truncation_heuristic",
        "take.warn.possible_runaway_repetition",
        "take.warn.engine_sample_rate",
    ],
    "models.desc.${": [
        "models.desc.qwen-base-1.7b",
        "models.desc.qwen-base-0.6b",
        "models.desc.qwen-design-1.7b",
    ],
    "studio.mode.${": ["studio.mode.clone", "studio.mode.design"],
}

KEY_LINE = re.compile(r"'([a-z][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9_-]+)+)'\s*:\s*")
DOTTED = re.compile(r"[a-z][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9_-]+)+")
STRING = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_locales(source: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Returns (ru, en) mapping key -> placeholder names."""
    ru_at = source.find("ru: {")
    en_at = source.find("en: {", ru_at + 1)
    if ru_at < 0 or en_at < 0:
        raise SystemExit("i18n.js: could not locate the ru/en dictionaries")

    def collect(block: str) -> dict[str, set[str]]:
        matches = list(KEY_LINE.finditer(block))
        out: dict[str, set[str]] = {}
        for index, match in enumerate(matches):
            key = match.group(1)
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
            out[key] = set(PLACEHOLDER.findall(block[start:end]))
        return out

    return collect(source[ru_at:en_at]), collect(source[en_at:])


def collect_used(root: Path) -> set[str]:
    used: set[str] = set()
    files = [root / "web" / "app.js"]
    files += sorted((root / "web" / "js").glob("*.js"))
    files = [path for path in files if path.name != "i18n.js" and path.exists()]

    index = root / "web" / "index.html"
    if index.exists():
        html = read(index)
        used.update(re.findall(r'data-i18n(?:-title)?="([^"]+)"', html))

    combined = ""
    for path in files:
        text = read(path)
        combined += text
        for match in STRING.finditer(text):
            value = match.group(1)
            if "." not in value:
                continue
            if value.split(".", 1)[0] in PREFIXES and DOTTED.fullmatch(value):
                used.add(value)

    for marker, keys in DYNAMIC_KEYS.items():
        if marker in combined:
            used.update(keys)

    # Keys referenced by the stageLabel() helper inside i18n.js itself.
    i18n_source = read(root / "web" / "js" / "i18n.js")
    block = re.search(r"const STAGE_KEYS = \{(.*?)\};", i18n_source, re.S)
    if block:
        for value in re.findall(r"'([^']+)'", block.group(1)):
            if "." in value:
                used.add(value)

    return used


def main() -> int:
    parser = argparse.ArgumentParser(description="Static i18n coverage check.")
    parser.add_argument("--root", default=None, help="repository root (default: parent of verification/)")
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    i18n_path = root / "web" / "js" / "i18n.js"
    if not i18n_path.exists():
        print(f"FAIL: {i18n_path} not found")
        return 1

    ru, en = parse_locales(read(i18n_path))
    used = collect_used(root)

    missing_ru = sorted(used - ru.keys())
    missing_en = sorted(used - en.keys())
    only_ru = sorted(ru.keys() - en.keys())
    only_en = sorted(en.keys() - ru.keys())

    placeholder_issues = []
    for key in sorted(ru.keys() & en.keys()):
        if ru[key] != en[key]:
            placeholder_issues.append((key, sorted(ru[key]), sorted(en[key])))

    problems = bool(missing_ru or missing_en or only_ru or only_en or placeholder_issues)

    if not args.quiet or problems:
        print(f"i18n: {len(used)} used keys, {len(ru)} ru, {len(en)} en  ({root})")

    def dump(title: str, items, formatter=str) -> None:
        if not items:
            return
        print(f"\n{title} ({len(items)}):")
        for item in items:
            print(f"  - {formatter(item)}")

    dump("MISSING in ru", missing_ru)
    dump("MISSING in en", missing_en)
    dump("present only in RU (parity)", only_ru)
    dump("present only in EN (parity)", only_en)
    dump(
        "placeholder mismatch",
        placeholder_issues,
        lambda row: f"{row[0]}: ru={row[1]} en={row[2]}",
    )

    if problems:
        print("\nFAIL: fix the keys above, then re-run this checker.")
        return 1

    if not args.quiet:
        print("OK: all used keys exist in RU and EN, locales are in parity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
