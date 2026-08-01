"""Tolerant JSON recovery from LLM output.

Models wrap JSON in fences even under ``response_format=json_object``, and
reasoning models sometimes prepend a sentence. Three escalating strategies, then
give up loudly — a silent ``{}`` reads downstream as "no findings", which is the
failure mode the playbook calls "silent zeros read as truth".
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$", re.MULTILINE)


class JsonRecoveryError(ValueError):
    """The model's output could not be parsed as JSON by any strategy."""


def parse_json_loose(text: str) -> Any:
    if not text or not text.strip():
        raise JsonRecoveryError("empty model output")

    stripped = _FENCE.sub("", text.strip())

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Widest balanced object or array in the response.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue

    # Trailing commas are the most common remaining defect.
    repaired = re.sub(r",\s*([}\]])", r"\1", stripped)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        raise JsonRecoveryError(
            f"unparseable model output ({exc}); first 300 chars: {stripped[:300]!r}"
        ) from exc


def coerce_dict(value: Any, *, context: str = "") -> dict:
    """Validate that a parsed payload is an object, with a useful error if not."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    raise JsonRecoveryError(
        f"expected a JSON object{' for ' + context if context else ''}, got {type(value).__name__}"
    )
