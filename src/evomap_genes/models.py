"""Public data structures for :mod:`evomap_genes`.

Field names follow the EvoMap GEP v1.0.0 concepts (see https://evomap.ai/llms.txt).
Every structure is a frozen dataclass with a lossless ``to_dict()`` / ``from_dict()``
JSON round-trip, so callers can drop them into logs, dashboards or a remote hub.

Anything the remote hub sends that this library does not model is preserved in
``extra`` -- nothing is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

__all__ = [
    "Capsule",
    "Event",
    "EvomapGenesError",
    "Gene",
    "SearchHit",
]

GeneKind = Literal["repair", "optimize", "innovate", "regulatory", "explore"]
Outcome = Literal["success", "failure", "partial", "aborted"]

GENE_KINDS: tuple[str, ...] = get_args(GeneKind)
OUTCOMES: tuple[str, ...] = get_args(Outcome)

#: GEP requires a Capsule to carry substance (``diff``/``strategy``/``content``/
#: ``code_snippet`` >= 50 chars).  We enforce it on ``content`` at construction time.
MIN_CONTENT_CHARS = 50


class EvomapGenesError(ValueError):
    """Construction-time error for this library.

    Subclasses :class:`ValueError` so that ``except ValueError`` -- the error model
    every library in this family follows -- keeps working.  Runtime problems
    (timeouts, offline hub, bad HTTP status) never raise: they degrade and are
    reported through return values instead.
    """


def _as_str_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raise EvomapGenesError(f"{field_name} must be a sequence of str, got a bare str")
    try:
        items = list(value)
    except TypeError as exc:  # pragma: no cover - defensive
        raise EvomapGenesError(f"{field_name} must be a sequence of str") from exc
    for item in items:
        if not isinstance(item, str):
            raise EvomapGenesError(f"{field_name} must contain only str, got {type(item).__name__}")
    return items


def _as_dict_list(value: Any, *, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    items = list(value)
    for item in items:
        if not isinstance(item, dict):
            raise EvomapGenesError(
                f"{field_name} must contain only dict, got {type(item).__name__}"
            )
    return [dict(item) for item in items]


def _as_dict(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EvomapGenesError(f"{field_name} must be a dict, got {type(value).__name__}")
    return dict(value)


def _require_id(value: Any, *, field_name: str = "id") -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvomapGenesError(f"{field_name} must be a non-empty str")
    return value


def _as_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvomapGenesError(f"{field_name} must be a number, got {type(value).__name__}")
    return float(value)


@dataclass(frozen=True)
class Gene:
    """A reusable strategy template.

    Mirrors the GEP ``Gene`` asset: a *kind* of fix plus the preconditions,
    constraints and validation commands that decide whether it may be applied.

    Units: ``created_at`` / ``updated_at`` are seconds since the Unix epoch and are
    supplied by the caller -- this library never reads the wall clock to make a
    decision.
    """

    id: str
    title: str
    kind: GeneKind
    preconditions: list[str]
    constraints: list[str]
    validation: list[str]
    strategy: dict[str, Any]
    tags: list[str] = ()  # type: ignore[assignment]
    author: str = ""
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "id", _require_id(self.id))
        if not isinstance(self.title, str):
            raise EvomapGenesError("title must be a str")
        if self.kind not in GENE_KINDS:
            raise EvomapGenesError(f"kind must be one of {GENE_KINDS}, got {self.kind!r}")
        set_(self, "preconditions", _as_str_list(self.preconditions, field_name="preconditions"))
        set_(self, "constraints", _as_str_list(self.constraints, field_name="constraints"))
        set_(self, "validation", _as_str_list(self.validation, field_name="validation"))
        set_(self, "strategy", _as_dict(self.strategy, field_name="strategy"))
        set_(self, "tags", _as_str_list(self.tags, field_name="tags"))
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise EvomapGenesError("version must be an int >= 1")
        set_(self, "created_at", _as_float(self.created_at, field_name="created_at"))
        set_(self, "updated_at", _as_float(self.updated_at, field_name="updated_at"))
        set_(self, "extra", _as_dict(self.extra, field_name="extra"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "preconditions": list(self.preconditions),
            "constraints": list(self.constraints),
            "validation": list(self.validation),
            "strategy": dict(self.strategy),
            "tags": list(self.tags),
            "author": self.author,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Gene:
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            kind=d.get("kind", "explore"),
            preconditions=d.get("preconditions") or [],
            constraints=d.get("constraints") or [],
            validation=d.get("validation") or [],
            strategy=d.get("strategy") or {},
            tags=d.get("tags") or [],
            author=d.get("author", ""),
            version=d.get("version", 1),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            extra=d.get("extra") or {},
        )


@dataclass(frozen=True)
class Capsule:
    """A validated fix, with the audit chain that proves it was validated.

    Construction-time rule from GEP: ``content`` shorter than
    :data:`MIN_CONTENT_CHARS` characters raises :class:`EvomapGenesError`
    (a :class:`ValueError`) -- a Capsule without substance is not a Capsule.
    """

    id: str
    title: str
    trigger_signals: list[str]
    confidence: float
    blast_radius: str
    environment: dict[str, Any]
    strategy_steps: list[str]
    content: str
    verified_by: list[dict[str, Any]] = ()  # type: ignore[assignment]
    tags: list[str] = ()  # type: ignore[assignment]
    author: str = ""
    created_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "id", _require_id(self.id))
        if not isinstance(self.title, str):
            raise EvomapGenesError("title must be a str")
        set_(
            self,
            "trigger_signals",
            _as_str_list(self.trigger_signals, field_name="trigger_signals"),
        )
        confidence = _as_float(self.confidence, field_name="confidence")
        if not 0.0 <= confidence <= 1.0:
            raise EvomapGenesError(f"confidence must be within 0..1, got {confidence}")
        set_(self, "confidence", confidence)
        if not isinstance(self.blast_radius, str) or not self.blast_radius:
            raise EvomapGenesError("blast_radius must be a non-empty str, e.g. 'single-file'")
        set_(self, "environment", _as_dict(self.environment, field_name="environment"))
        set_(self, "strategy_steps", _as_str_list(self.strategy_steps, field_name="strategy_steps"))
        if not isinstance(self.content, str):
            raise EvomapGenesError("content must be a str")
        if len(self.content) < MIN_CONTENT_CHARS:
            raise EvomapGenesError(
                f"content must be at least {MIN_CONTENT_CHARS} characters "
                f"(GEP requires a Capsule to carry substance), got {len(self.content)}"
            )
        set_(self, "verified_by", _as_dict_list(self.verified_by, field_name="verified_by"))
        set_(self, "tags", _as_str_list(self.tags, field_name="tags"))
        set_(self, "created_at", _as_float(self.created_at, field_name="created_at"))
        set_(self, "extra", _as_dict(self.extra, field_name="extra"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "trigger_signals": list(self.trigger_signals),
            "confidence": self.confidence,
            "blast_radius": self.blast_radius,
            "environment": dict(self.environment),
            "strategy_steps": list(self.strategy_steps),
            "content": self.content,
            "verified_by": [dict(v) for v in self.verified_by],
            "tags": list(self.tags),
            "author": self.author,
            "created_at": self.created_at,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Capsule:
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            trigger_signals=d.get("trigger_signals") or [],
            confidence=d.get("confidence", 0.0),
            blast_radius=d.get("blast_radius") or "unknown",
            environment=d.get("environment") or {},
            strategy_steps=d.get("strategy_steps") or [],
            content=d.get("content", ""),
            verified_by=d.get("verified_by") or [],
            tags=d.get("tags") or [],
            author=d.get("author", ""),
            created_at=d.get("created_at", 0.0),
            extra=d.get("extra") or {},
        )


@dataclass(frozen=True)
class Event:
    """A GEP ``EvolutionEvent``: what was intended, what was tried, what happened.

    ``t`` is seconds since the Unix epoch and is supplied by the caller.
    """

    id: str
    t: float
    actor: str
    intent: str
    mutations: list[dict[str, Any]] = ()  # type: ignore[assignment]
    outcome: Outcome = "partial"
    subject_id: str | None = None
    payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "id", _require_id(self.id))
        set_(self, "t", _as_float(self.t, field_name="t"))
        if not isinstance(self.actor, str):
            raise EvomapGenesError("actor must be a str")
        if not isinstance(self.intent, str):
            raise EvomapGenesError("intent must be a str")
        set_(self, "mutations", _as_dict_list(self.mutations, field_name="mutations"))
        if self.outcome not in OUTCOMES:
            raise EvomapGenesError(f"outcome must be one of {OUTCOMES}, got {self.outcome!r}")
        if self.subject_id is not None and not isinstance(self.subject_id, str):
            raise EvomapGenesError("subject_id must be a str or None")
        if self.payload is not None:
            set_(self, "payload", _as_dict(self.payload, field_name="payload"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "t": self.t,
            "actor": self.actor,
            "intent": self.intent,
            "mutations": [dict(m) for m in self.mutations],
            "outcome": self.outcome,
            "subject_id": self.subject_id,
            "payload": None if self.payload is None else dict(self.payload),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        return cls(
            id=d["id"],
            t=d.get("t", 0.0),
            actor=d.get("actor", ""),
            intent=d.get("intent", ""),
            mutations=d.get("mutations") or [],
            outcome=d.get("outcome", "partial"),
            subject_id=d.get("subject_id"),
            payload=d.get("payload"),
        )


@dataclass(frozen=True)
class SearchHit:
    """One search result: the asset, how well it matched, and why."""

    asset: Gene | Capsule
    score: float
    why: str
    source: str  # "evomap" | "sqlite"

    def __post_init__(self) -> None:
        if not isinstance(self.asset, (Gene, Capsule)):
            raise EvomapGenesError("asset must be a Gene or a Capsule")
        score = _as_float(self.score, field_name="score")
        if not 0.0 <= score <= 1.0:
            raise EvomapGenesError(f"score must be within 0..1, got {score}")
        object.__setattr__(self, "score", score)
        if not isinstance(self.why, str):
            raise EvomapGenesError("why must be a str")
        if self.source not in ("evomap", "sqlite"):
            raise EvomapGenesError(f"source must be 'evomap' or 'sqlite', got {self.source!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_type": "gene" if isinstance(self.asset, Gene) else "capsule",
            "asset": self.asset.to_dict(),
            "score": self.score,
            "why": self.why,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SearchHit:
        asset_type = d.get("asset_type", "gene")
        asset: Gene | Capsule = (
            Gene.from_dict(d["asset"]) if asset_type == "gene" else Capsule.from_dict(d["asset"])
        )
        return cls(
            asset=asset, score=d["score"], why=d.get("why", ""), source=d.get("source", "sqlite")
        )
