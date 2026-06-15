#!/usr/bin/env python3
"""Validate data/commands.json against data/vendors.json.

Run from anywhere:  python3 tools/validate.py
Exits 0 when clean, 1 when any error is found. Warnings never fail the build.

Checks:
  - JSON parses; commands.json is a list of entry objects
  - required fields present: id, task, category, commands
  - no duplicate ids
  - commands keys are known CLI vendor ids ("mist" must use mist_note instead)
  - command values are non-empty strings
  - verify is either true or a list of known vendor ids (mist allowed)
  - tags is a list of strings (warning if missing/empty)
  - {{placeholders}} are limited to interface / vlan / ip / hostname
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Loose scan so malformed tokens ({{ interface }}, {{vlan2}}) are CAUGHT, not skipped;
# only the exact tight forms below render in the app (see app.js PLACEHOLDER_ALL).
PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
ALLOWED_TOKENS = {"{{interface}}", "{{vlan}}", "{{ip}}", "{{hostname}}"}
REQUIRED_FIELDS = ("id", "task", "category", "commands")
KNOWN_FIELDS = set(REQUIRED_FIELDS) | {"tags", "mist_note", "notes", "verify"}


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        vendors_doc = json.loads((ROOT / "data" / "vendors.json").read_text())
        entries = json.loads((ROOT / "data" / "commands.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}")
        return 1

    vendor_ids = {v["id"] for v in vendors_doc.get("vendors", [])}
    cli_vendor_ids = vendor_ids - {"mist"}

    if not isinstance(entries, list):
        print("FATAL: commands.json must be a top-level JSON array of entries")
        return 1

    seen_ids: set[str] = set()
    for i, entry in enumerate(entries):
        where = f"entry[{i}] ({entry.get('id', '?')})" if isinstance(entry, dict) else f"entry[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: not an object")
            continue

        for field in REQUIRED_FIELDS:
            if field not in entry:
                errors.append(f"{where}: missing required field '{field}'")
        for field in ("id", "task", "category"):
            if field in entry and (not isinstance(entry[field], str) or not entry[field].strip()):
                errors.append(f"{where}: '{field}' must be a non-empty string")

        eid = entry.get("id")
        if isinstance(eid, str):
            if eid in seen_ids:
                errors.append(f"{where}: duplicate id '{eid}'")
            seen_ids.add(eid)

        for field in entry:
            if field not in KNOWN_FIELDS:
                warnings.append(f"{where}: unknown field '{field}'")

        commands = entry.get("commands")
        if commands is not None:
            if not isinstance(commands, dict):
                errors.append(f"{where}: 'commands' must be an object")
            else:
                if "mist" in commands:
                    errors.append(f"{where}: 'mist' belongs in 'mist_note', not 'commands'")
                for vid, cmd in commands.items():
                    if vid not in cli_vendor_ids and vid != "mist":
                        errors.append(f"{where}: unknown vendor key '{vid}'")
                    if not isinstance(cmd, str) or not cmd.strip():
                        errors.append(f"{where}: command for '{vid}' must be a non-empty string")
                if not commands and not entry.get("mist_note"):
                    warnings.append(f"{where}: no commands and no mist_note — entry renders empty")

        tags = entry.get("tags")
        if tags is None or tags == []:
            warnings.append(f"{where}: no tags — entry only findable by task name")
        elif not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            errors.append(f"{where}: 'tags' must be a list of strings")

        verify = entry.get("verify")
        if verify is not None and verify is not True:
            if not isinstance(verify, list) or not all(isinstance(v, str) for v in verify):
                errors.append(f"{where}: 'verify' must be true or a list of vendor ids")
            else:
                for vid in verify:
                    if vid not in vendor_ids:
                        errors.append(f"{where}: 'verify' references unknown vendor '{vid}'")

        for field in ("mist_note", "notes"):
            val = entry.get(field)
            if val is not None and (not isinstance(val, str) or not val.strip()):
                errors.append(f"{where}: '{field}' must be a non-empty string when present")

        texts = list(commands.values()) if isinstance(commands, dict) else []
        for field in ("mist_note", "notes"):
            if isinstance(entry.get(field), str):
                texts.append(entry[field])
        for text in texts:
            if not isinstance(text, str):
                continue
            for match in PLACEHOLDER_RE.finditer(text):
                if match.group(0) not in ALLOWED_TOKENS:
                    errors.append(
                        f"{where}: bad placeholder '{match.group(0)}'"
                        " (allowed, no spaces: " + ", ".join(sorted(ALLOWED_TOKENS)) + ")"
                    )

    for warning in warnings:
        print(f"  warn: {warning}")
    for error in errors:
        print(f" ERROR: {error}")
    print(
        f"\n{len(entries)} entries · {len(vendor_ids)} vendors · "
        f"{len(errors)} errors · {len(warnings)} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
