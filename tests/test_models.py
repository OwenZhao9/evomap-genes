"""Construction-time rules and JSON round trips."""

from __future__ import annotations

import pytest

from conftest import make_capsule, make_gene
from evomap_genes import Capsule, Event, EvomapGenesError, Gene, SearchHit


def test_gene_json_round_trip() -> None:
    gene = make_gene()
    assert Gene.from_dict(gene.to_dict()) == gene


def test_capsule_json_round_trip() -> None:
    capsule = make_capsule()
    assert Capsule.from_dict(capsule.to_dict()) == capsule


def test_event_json_round_trip() -> None:
    event = Event(
        id="ev1",
        t=12.5,
        actor="agent-a",
        intent="repair the timeout",
        mutations=[{"tried": "increase timeout"}, {"tried": "retry"}],
        outcome="success",
        subject_id="caps_retry",
        payload={"note": "worked on the second attempt"},
    )
    assert Event.from_dict(event.to_dict()) == event


def test_search_hit_round_trip() -> None:
    for asset in (make_gene(), make_capsule()):
        hit = SearchHit(asset=asset, score=0.5, why="because", source="sqlite")
        assert SearchHit.from_dict(hit.to_dict()) == hit


def test_capsule_rejects_thin_content() -> None:
    with pytest.raises(ValueError, match="at least 50 characters"):
        make_capsule(content="too short")


def test_capsule_accepts_exactly_fifty_chars() -> None:
    assert len(make_capsule(content="x" * 50).content) == 50


def test_capsule_error_is_a_value_error() -> None:
    assert issubclass(EvomapGenesError, ValueError)
    assert issubclass(EvomapGenesError, Exception)
    with pytest.raises(EvomapGenesError):
        make_capsule(content="")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": "not-a-kind"}, "kind must be one of"),
        ({"id": ""}, "non-empty"),
        ({"version": 0}, "version must be an int"),
        ({"tags": "timeout"}, "sequence of str"),
        ({"preconditions": [1, 2]}, "only str"),
        ({"strategy": []}, "must be a dict"),
    ],
)
def test_gene_construction_errors(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make_gene(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"confidence": 1.5}, "within 0..1"),
        ({"confidence": -0.1}, "within 0..1"),
        ({"blast_radius": ""}, "non-empty str"),
        ({"verified_by": ["not a dict"]}, "only dict"),
    ],
)
def test_capsule_construction_errors(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make_capsule(**kwargs)


def test_event_outcome_is_checked() -> None:
    with pytest.raises(ValueError, match="outcome must be one of"):
        Event(id="e", t=0.0, actor="a", intent="i", outcome="maybe")  # type: ignore[arg-type]


def test_search_hit_score_bounds() -> None:
    with pytest.raises(ValueError, match=r"within 0\.\.1"):
        SearchHit(asset=make_gene(), score=1.2, why="", source="sqlite")
    with pytest.raises(ValueError, match="source must be"):
        SearchHit(asset=make_gene(), score=0.2, why="", source="redis")


def test_sequence_defaults_normalize_to_lists() -> None:
    """The contract writes ``tags: list[str] = ()``; instances still hold lists."""
    gene = Gene(
        id="g",
        title="t",
        kind="explore",
        preconditions=[],
        constraints=[],
        validation=[],
        strategy={},
    )
    assert gene.tags == [] and isinstance(gene.tags, list)
    assert Gene.from_dict(gene.to_dict()) == gene


def test_frozen() -> None:
    gene = make_gene()
    with pytest.raises(Exception):  # noqa: B017 - dataclasses raises FrozenInstanceError
        gene.title = "nope"  # type: ignore[misc]
