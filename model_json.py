"""Conservative decoding for untrusted model decisions, never stored state."""
from __future__ import annotations

import json
import math
import re


class ModelResponseError(RuntimeError):
    """An unusable model response; callers may retry the original task."""


class ModelJSONError(ModelResponseError, ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelJSONError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ModelJSONError("non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ModelJSONError("non-finite JSON number")
    return number


def parse_model_object(raw: str) -> dict:
    """Accept one complete object, optional fence/BOM and redundant closers.

    Never search inside prose/arrays, take the first of several answers, repair
    missing text, or collapse duplicate keys (including nested verdict fields).
    Only wrappers that cannot add a decision are stripped.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ModelJSONError("empty or non-text response")
    text = raw.strip().removeprefix("\ufeff").strip()
    if text.startswith("```"):
        fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\n?```", text,
                             flags=re.DOTALL | re.IGNORECASE)
        if not fence:
            raise ModelJSONError("incomplete or ambiguous JSON fence")
        text = fence.group(1).strip()
    if not text.startswith("{"):
        raise ModelJSONError("response must be one top-level object")
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant, parse_float=_finite_float)
    try:
        value, consumed = decoder.raw_decode(text)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ModelJSONError):
            raise
        raise ModelJSONError("invalid or incomplete JSON object") from exc
    if not isinstance(value, dict):
        raise ModelJSONError("response must be one top-level object")
    suffix = text[consumed:]
    if not re.fullmatch(r"[\s}\]]*", suffix):
        raise ModelJSONError("extra data after JSON object")
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            try:
                item.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise ModelJSONError("invalid Unicode string") from exc
    return value


def json_object_or_none(raw: str) -> dict | None:
    try:
        return parse_model_object(raw)
    except ModelJSONError:
        return None
