"""The only place that knows what the remote hub's JSON looks like.

Two pure functions:

* :func:`to_remote` -- local :class:`~evomap_genes.models.Gene` /
  :class:`~evomap_genes.models.Capsule` -> a hub asset dict.
* :func:`from_remote` -- a hub asset dict -> a local Gene / Capsule.

Point them at a different hub by editing these two functions; the public API of
the library does not move.  Every key the hub sends that this mapping does not
model is preserved in ``asset.extra`` and written back out on the next
:func:`to_remote`, so a round trip never drops information.

The mapping below targets GEP ``gep-a2a v1.0.0`` as documented at
https://evomap.ai/docs/en/05-a2a-protocol.md ("Bundle Structure").
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import GENE_KINDS, Capsule, Event, EvomapGenesError, Gene

__all__ = ["from_remote", "to_remote"]

SCHEMA_VERSION = "1.5.0"

#: Top-level keys this adapter maps onto real fields.  Anything else -> ``extra``.
_GENE_KNOWN = frozenset(
    {
        "type",
        "schema_version",
        "asset_id",
        "category",
        "summary",
        "signals_match",
        "validation",
        "strategy",
        "preconditions",
        "constraints",
        "metadata",
    }
)
_CAPSULE_KNOWN = frozenset(
    {
        "type",
        "schema_version",
        "asset_id",
        "trigger",
        "summary",
        "confidence",
        "blast_radius",
        "env_fingerprint",
        "strategy",
        "content",
        "verified_by",
        "metadata",
    }
)
_KNOWN_META = frozenset({"tags", "author", "version", "local_id", "created_at", "updated_at"})

#: Excluded from the SHA-256 preimage: ``asset_id`` itself (per the protocol doc)
#: and ``model_name`` (documented as metadata that is not hashed).
_UNHASHED = ("asset_id", "model_name")


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _asset_id(payload: dict[str, Any]) -> str:
    """``sha256(canonical_json(asset_without_asset_id))`` as documented by the Hub."""
    body = {k: v for k, v in payload.items() if k not in _UNHASHED}
    digest = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _meta(asset: Gene | Capsule) -> dict[str, Any]:
    meta: dict[str, Any] = dict(asset.extra.get("metadata") or {})
    meta["tags"] = list(asset.tags)
    meta["author"] = asset.author
    meta["local_id"] = asset.id
    meta["created_at"] = asset.created_at
    if isinstance(asset, Gene):
        meta["version"] = asset.version
        meta["updated_at"] = asset.updated_at
    return meta


def _passthrough(asset: Gene | Capsule) -> dict[str, Any]:
    """Remote-only fields kept in ``extra``, minus the ones we rebuild ourselves."""
    return {
        k: v
        for k, v in asset.extra.items()
        if k not in ("metadata", "asset_id", "blast_radius_raw", "category_raw")
    }


def to_remote(asset: Gene | Capsule) -> dict[str, Any]:
    """Render a local asset as a hub asset dict, ``asset_id`` included.

    Pure: no IO, no clock, no globals.  ``asset_id`` is recomputed from the
    payload being sent, which is exactly what the Hub re-derives and checks.
    """
    if isinstance(asset, Gene):
        payload: dict[str, Any] = _passthrough(asset)
        payload.update(
            {
                "type": "Gene",
                "schema_version": SCHEMA_VERSION,
                "category": asset.extra.get("category_raw") or asset.kind,
                "summary": asset.title,
                "signals_match": list(asset.tags),
                "validation": list(asset.validation),
                "strategy": dict(asset.strategy),
                "preconditions": list(asset.preconditions),
                "constraints": list(asset.constraints),
                "metadata": _meta(asset),
            }
        )
    elif isinstance(asset, Capsule):
        blast: Any = asset.extra.get("blast_radius_raw")
        if blast is None:
            blast = asset.blast_radius
        payload = _passthrough(asset)
        payload.update(
            {
                "type": "Capsule",
                "schema_version": SCHEMA_VERSION,
                "trigger": list(asset.trigger_signals),
                "summary": asset.title,
                "confidence": asset.confidence,
                "blast_radius": blast,
                "env_fingerprint": dict(asset.environment),
                "strategy": list(asset.strategy_steps),
                "content": asset.content,
                "verified_by": [dict(v) for v in asset.verified_by],
                "metadata": _meta(asset),
            }
        )
    else:  # pragma: no cover - guarded by the type checker and by callers
        raise EvomapGenesError(f"to_remote() takes a Gene or a Capsule, got {type(asset).__name__}")

    payload["asset_id"] = _asset_id(payload)
    return payload


def _remote_type(d: dict[str, Any]) -> str:
    declared = str(d.get("type") or d.get("asset_type") or "").strip().lower()
    if declared in ("gene", "capsule"):
        return declared
    if "content" in d or "trigger" in d or "confidence" in d:
        return "capsule"
    if "category" in d or "signals_match" in d:
        return "gene"
    raise EvomapGenesError("cannot tell whether this remote payload is a Gene or a Capsule")


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _local_id(d: dict[str, Any], meta: dict[str, Any]) -> str:
    for candidate in (meta.get("local_id"), d.get("id"), d.get("asset_id")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    raise EvomapGenesError("remote payload has no usable id (metadata.local_id / id / asset_id)")


def _blast_radius_str(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and value:
        return ",".join(f"{k}={value[k]}" for k in sorted(value))
    return "unknown"


def from_remote(d: dict[str, Any]) -> Gene | Capsule:
    """Build a local asset from a hub asset dict.

    Unknown keys land in ``extra`` and are written back by :func:`to_remote`.
    Raises :class:`EvomapGenesError` when the payload cannot become a valid
    asset (for example a Capsule whose ``content`` is missing because the hub
    returned metadata only); callers treat that as "skip this hit", never as a
    crash.
    """
    if not isinstance(d, dict):
        raise EvomapGenesError("from_remote() takes a dict")

    kind = _remote_type(d)
    known = _GENE_KNOWN if kind == "gene" else _CAPSULE_KNOWN
    meta = dict(d.get("metadata") or {})
    extra: dict[str, Any] = {k: v for k, v in d.items() if k not in known}
    leftover_meta = {k: v for k, v in meta.items() if k not in _KNOWN_META}
    if leftover_meta:
        extra["metadata"] = leftover_meta
    if isinstance(d.get("asset_id"), str):
        extra["asset_id"] = d["asset_id"]

    tags = _str_list(meta.get("tags"))
    author = str(meta.get("author") or "")
    local_id = _local_id(d, meta)

    if kind == "gene":
        category = str(d.get("category") or "explore")
        if category not in GENE_KINDS:
            extra["category_raw"] = category
            category = "explore"
        strategy = d.get("strategy")
        if not isinstance(strategy, dict):
            if strategy is not None:
                extra["strategy_raw"] = strategy
            strategy = {}
        preconditions = d.get("preconditions")
        if preconditions is None:
            preconditions = strategy.get("preconditions")
        constraints = d.get("constraints")
        if constraints is None:
            constraints = strategy.get("constraints")
        return Gene(
            id=local_id,
            title=str(d.get("summary") or ""),
            kind=category,  # type: ignore[arg-type]
            preconditions=_str_list(preconditions),
            constraints=_str_list(constraints),
            validation=_str_list(d.get("validation")),
            strategy=strategy,
            tags=tags or _str_list(d.get("signals_match")),
            author=author,
            version=int(meta.get("version") or 1),
            created_at=float(meta.get("created_at") or 0.0),
            updated_at=float(meta.get("updated_at") or 0.0),
            extra=extra,
        )

    raw_blast = d.get("blast_radius")
    if isinstance(raw_blast, dict):
        extra["blast_radius_raw"] = raw_blast
    steps = d.get("strategy")
    if isinstance(steps, dict):
        extra["strategy_raw"] = steps
        steps = []
    return Capsule(
        id=local_id,
        title=str(d.get("summary") or ""),
        trigger_signals=_str_list(d.get("trigger")),
        confidence=float(d.get("confidence") or 0.0),
        blast_radius=_blast_radius_str(raw_blast),
        environment=dict(d.get("env_fingerprint") or {}),
        strategy_steps=_str_list(steps),
        content=str(d.get("content") or ""),
        verified_by=[dict(v) for v in (d.get("verified_by") or []) if isinstance(v, dict)],
        tags=tags,
        author=author,
        created_at=float(meta.get("created_at") or 0.0),
        extra=extra,
    )


def _event_to_remote(ev: Event) -> dict[str, Any]:
    """The optional third element of a publish bundle (GEP ``EvolutionEvent``).

    Internal: ``Event`` is not an "asset" in the contract's sense, so it is not
    part of the public adapter surface -- but the schema mapping still lives here.
    """
    payload: dict[str, Any] = {
        "type": "EvolutionEvent",
        "schema_version": SCHEMA_VERSION,
        "intent": ev.intent,
        "outcome": {"status": ev.outcome},
        "mutations_tried": len(ev.mutations),
        "metadata": {
            "local_id": ev.id,
            "t": ev.t,
            "actor": ev.actor,
            "mutations": [dict(m) for m in ev.mutations],
            "subject_id": ev.subject_id,
            "payload": ev.payload,
        },
    }
    payload["asset_id"] = _asset_id(payload)
    return payload


def _bundle_payload(
    gene: Gene, capsule: Capsule, event: Event | None = None
) -> list[dict[str, Any]]:
    """Build the ``payload.assets`` array for ``POST /a2a/publish``.

    Internal, and deliberately in this file: linking a Capsule to its Gene via
    the Gene's ``asset_id`` is schema knowledge, so it lives with the rest of it.
    GEP rejects a lone asset -- Gene and Capsule always travel together.
    """
    gene_payload = to_remote(gene)
    capsule_payload = to_remote(capsule)
    capsule_payload["gene"] = gene_payload["asset_id"]
    capsule_payload["asset_id"] = _asset_id(capsule_payload)
    assets = [gene_payload, capsule_payload]
    if event is not None:
        assets.append(_event_to_remote(event))
    return assets
