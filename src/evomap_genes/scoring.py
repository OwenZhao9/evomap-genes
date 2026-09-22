"""Keyword + context scoring for the sqlite backend.

Pure functions, no IO, no clock: the same asset and the same query always
produce the same score and the same ``why`` string.
"""

from __future__ import annotations

import math
import re
from typing import Any, TypeGuard

from .models import Capsule, Gene

__all__ = ["context_ranges", "score_asset", "tokenize"]

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CMP_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*(?P<op><=|>=|<|>|==|=)\s*(?P<num>-?\d+(?:\.\d+)?)\s*$"
)
_BETWEEN_RE = re.compile(
    r"^\s*(?P<lo>-?\d+(?:\.\d+)?)\s*(?P<op1><=|<)\s*(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*"
    r"(?P<op2><=|<)\s*(?P<hi>-?\d+(?:\.\d+)?)\s*$"
)

# Weights per match zone.  Tuned so that a title hit always outranks a body hit.
W_TITLE = 3.0
W_TAG = 2.0
W_SIGNAL = 2.0
W_BODY = 1.0
W_CONTEXT_KEY = 0.5
W_CONTEXT_VALUE = 1.5
W_CONTEXT_IN_RANGE = 2.5
W_CONTEXT_OUT_OF_RANGE = -3.0

#: ``score = raw / (raw + _SATURATION)`` keeps the published score inside 0..1
#: while staying strictly monotonic in the raw score.
_SATURATION = 6.0


def tokenize(text: str) -> list[str]:
    """Lower-case alphanumeric tokens of length >= 2, order preserved, deduped."""
    seen: dict[str, None] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) >= 2:
            seen.setdefault(tok, None)
    return list(seen)


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _range_from_value(value: Any) -> tuple[float, float] | None:
    if _is_number(value):
        return (float(value), float(value))
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(_is_number(v) for v in value):
        lo, hi = float(value[0]), float(value[1])
        return (lo, hi) if lo <= hi else (hi, lo)
    if isinstance(value, dict):
        lo_raw = value.get("min", value.get("lo", value.get("low")))
        hi_raw = value.get("max", value.get("hi", value.get("high")))
        if _is_number(lo_raw) or _is_number(hi_raw):
            return (
                float(lo_raw) if _is_number(lo_raw) else -math.inf,
                float(hi_raw) if _is_number(hi_raw) else math.inf,
            )
    return None


def _merge(ranges: dict[str, tuple[float, float]], name: str, lo: float, hi: float) -> None:
    old = ranges.get(name)
    if old is None:
        ranges[name] = (lo, hi)
    else:
        ranges[name] = (max(old[0], lo), min(old[1], hi))


def _parse_constraint(text: str, ranges: dict[str, tuple[float, float]]) -> None:
    m = _BETWEEN_RE.match(text)
    if m:
        _merge(ranges, m.group("name"), float(m.group("lo")), float(m.group("hi")))
        return
    m = _CMP_RE.match(text)
    if not m:
        return
    name, op, num = m.group("name"), m.group("op"), float(m.group("num"))
    if op in ("<=", "<"):
        _merge(ranges, name, -math.inf, num)
    elif op in (">=", ">"):
        _merge(ranges, name, num, math.inf)
    else:
        _merge(ranges, name, num, num)


def context_ranges(asset: Gene | Capsule) -> dict[str, tuple[float, float]]:
    """Numeric ranges an asset says it applies to.

    Read from, in order (later sources narrow earlier ones):

    1. ``extra["context"]`` -- ``{"tilt_deg": [0, 30]}`` / ``{"min": .., "max": ..}``
    2. ``Gene.strategy["context"]`` or ``Capsule.environment``
    3. free-text ``preconditions`` / ``constraints`` such as ``"tilt_deg <= 30"``
       or ``"0 <= tilt_deg <= 30"``
    """
    ranges: dict[str, tuple[float, float]] = {}
    sources: list[Any] = [asset.extra.get("context")]
    if isinstance(asset, Gene):
        sources.append(asset.strategy.get("context"))
    else:
        sources.append(asset.environment)
    for source in sources:
        if isinstance(source, dict):
            for key, value in source.items():
                parsed = _range_from_value(value)
                if parsed is not None:
                    _merge(ranges, str(key), parsed[0], parsed[1])
    if isinstance(asset, Gene):
        for text in (*asset.preconditions, *asset.constraints):
            _parse_constraint(text, ranges)
    return ranges


def _fmt(value: float) -> str:
    if value == math.inf:
        return "+inf"
    if value == -math.inf:
        return "-inf"
    return f"{value:g}"


def _zones(asset: Gene | Capsule) -> tuple[str, list[str], list[str], str]:
    """(title, tags, signals, body) with everything lower-cased for matching."""
    if isinstance(asset, Gene):
        signals = [*asset.preconditions, asset.kind]
        body_parts = [
            *asset.constraints,
            *asset.validation,
            _flatten(asset.strategy),
            asset.author,
        ]
    else:
        signals = list(asset.trigger_signals)
        body_parts = [
            *asset.strategy_steps,
            asset.content,
            asset.blast_radius,
            _flatten(asset.environment),
            asset.author,
        ]
    return (
        asset.title.lower(),
        [t.lower() for t in asset.tags],
        [s.lower() for s in signals],
        " ".join(body_parts).lower(),
    )


def _flatten(obj: Any) -> str:
    if isinstance(obj, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return " ".join(_flatten(v) for v in obj)
    return str(obj)


def score_asset(
    asset: Gene | Capsule,
    query: str,
    context: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """Return ``(score in 0..1, why)``.

    ``why`` lists every zone that contributed, with its weight, e.g.::

        title~'retry,backoff' (+6.0); tags~'timeout' (+2.0);
        context battery_v=11.8 in [11,12.6] (+2.5)

    A score of ``0.0`` means "no evidence" -- callers drop those hits.
    """
    title, tags, signals, body = _zones(asset)
    tokens = tokenize(query)
    raw = 0.0
    reasons: list[str] = []

    def hit(zone_text: str, toks: list[str]) -> list[str]:
        return [t for t in toks if t in zone_text]

    joined_tags = " ".join(tags)
    joined_signals = " ".join(signals)

    for zone_name, zone_text, weight in (
        ("title", title, W_TITLE),
        ("tags", joined_tags, W_TAG),
        ("signals", joined_signals, W_SIGNAL),
        ("body", body, W_BODY),
    ):
        matched = hit(zone_text, tokens)
        if matched:
            gain = weight * len(matched)
            raw += gain
            reasons.append(f"{zone_name}~'{','.join(matched)}' (+{gain:.1f})")

    if context:
        ranges = context_ranges(asset)
        haystack = " ".join((title, joined_tags, joined_signals, body))
        for key in sorted(context):
            value = context[key]
            key_l = str(key).lower()
            rng = ranges.get(str(key))
            if rng is not None and _is_number(value):
                lo, hi = rng
                if lo <= float(value) <= hi:
                    raw += W_CONTEXT_IN_RANGE
                    reasons.append(
                        f"context {key}={value:g} in [{_fmt(lo)},{_fmt(hi)}] "
                        f"(+{W_CONTEXT_IN_RANGE:.1f})"
                    )
                else:
                    raw += W_CONTEXT_OUT_OF_RANGE
                    reasons.append(
                        f"context {key}={value:g} outside [{_fmt(lo)},{_fmt(hi)}] "
                        f"({W_CONTEXT_OUT_OF_RANGE:.1f})"
                    )
                continue
            if not _is_number(value) and str(value).lower() in haystack and str(value):
                raw += W_CONTEXT_VALUE
                reasons.append(f"context {key}={value!r} mentioned (+{W_CONTEXT_VALUE:.1f})")
            elif key_l in haystack:
                raw += W_CONTEXT_KEY
                reasons.append(f"context key {key} mentioned (+{W_CONTEXT_KEY:.1f})")

    if raw <= 0.0:
        why = "; ".join(reasons) if reasons else "no keyword or context evidence"
        return 0.0, why
    return raw / (raw + _SATURATION), "; ".join(reasons)
