#!/usr/bin/env python3
"""Validate an eo-services.json discovery document against its JSON Schema.

The schema (``schemas/eo-services.schema.json``) mirrors the eosdk discovery
models and SPEC §6.2. This validator adds a few semantic checks the plain
schema cannot express (real calendar dates, a usable ``/realms/`` split, and a
warning for ``replacement`` pointing at a missing sibling strategy).

Usage (from anywhere)::

    python scripts/validate_eo_services.py                 # served discovery doc
    python scripts/validate_eo_services.py path/to/doc.json [more.json ...]

Exit code is 0 when every file is valid, 1 otherwise.

Requires ``jsonschema`` (``uv pip install jsonschema`` or ``pip install
jsonschema``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
    from jsonschema.validators import extend
except ModuleNotFoundError:  # pragma: no cover - dependency hint
    sys.stderr.write(
        "error: the 'jsonschema' package is required.\n"
        "       install it with: uv pip install jsonschema\n"
    )
    raise SystemExit(2) from None

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "eo-services.schema.json"
DEFAULT_DOCUMENT = REPO_ROOT / "docker" / "discovery" / "eo-services.json"


def _pointer(path: object) -> str:
    """Render a jsonschema error path as a readable location."""
    parts = list(path)  # type: ignore[call-overload]
    return "$" + "".join(f"[{p!r}]" if isinstance(p, str) else f"[{p}]" for p in parts)


def _semantic_errors(document: dict) -> list[str]:
    """Checks beyond the schema's reach, mirroring the SDK's parsing."""
    errors: list[str] = []

    services = document.get("services", {})
    if not isinstance(services, dict):
        return errors

    # auth.issuer must split into a non-empty realm (SDK: partition on '/realms/').
    auth = services.get("auth")
    if isinstance(auth, dict) and isinstance(auth.get("issuer"), str):
        base, sep, realm = auth["issuer"].partition("/realms/")
        if not sep or not base or not realm.strip("/"):
            errors.append(
                f"$['services']['auth']['issuer']: {auth['issuer']!r} does not yield a "
                "'/realms/<realm>' split usable by the SDK"
            )

    # sunset must be a real calendar date; collect every strategy in the document.
    http = services.get("data_access", {})
    http = http.get("http", {}) if isinstance(http, dict) else {}
    strategies: dict[str, dict] = {}
    if isinstance(http, dict):
        strategies = {k: v for k, v in http.items() if isinstance(v, dict)}

    import datetime as dt

    for name, strat in strategies.items():
        sunset = strat.get("sunset")
        if isinstance(sunset, str):
            try:
                dt.date.fromisoformat(sunset)
            except ValueError:
                errors.append(
                    f"$['services']['data_access']['http'][{name!r}]['sunset']: "
                    f"{sunset!r} is not a valid calendar date"
                )
        replacement = strat.get("replacement")
        if isinstance(replacement, str) and replacement not in strategies:
            errors.append(
                f"$['services']['data_access']['http'][{name!r}]['replacement']: "
                f"{replacement!r} is not another strategy under data_access.http"
            )

    return errors


def validate_file(path: Path, validator: Draft202012Validator) -> bool:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"✗ {path}: cannot read file ({exc})")
        return False
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"✗ {path}: invalid JSON ({exc})")
        return False

    problems: list[str] = [
        f"{_pointer(error.path)}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    ]

    if isinstance(document, dict):
        problems.extend(_semantic_errors(document))

    if problems:
        print(f"✗ {path}: {len(problems)} problem(s)")
        for problem in problems:
            print(f"    {problem}")
        return False

    print(f"✓ {path}: valid")
    return True


def main(argv: list[str]) -> int:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        sys.stderr.write(f"error: cannot read schema {SCHEMA_PATH}: {exc}\n")
        return 2

    Draft202012Validator.check_schema(schema)
    # Enable format assertions (uri, date) rather than leaving them as annotations.
    validator_cls = extend(Draft202012Validator)
    validator = validator_cls(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)

    paths = [Path(a) for a in argv[1:]] or [DEFAULT_DOCUMENT]
    ok = all(validate_file(p, validator) for p in paths)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
