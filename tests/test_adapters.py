"""The field adaptation layer: nothing dropped, both directions."""

from __future__ import annotations

import hashlib
import json

import pytest

from conftest import make_capsule, make_gene
from evomap_genes import Capsule, EvomapGenesError, Gene, from_remote, to_remote
from evomap_genes.adapters import _bundle_payload


def test_gene_maps_onto_gep_field_names() -> None:
    payload = to_remote(make_gene())
    assert payload["type"] == "Gene"
    assert payload["category"] == "repair"
    assert payload["summary"] == "Retry with exponential backoff on timeout"
    assert payload["signals_match"] == ["timeout", "network"]
    assert payload["validation"] == ["pytest -q tests/test_retry.py"]
    assert payload["metadata"]["tags"] == ["timeout", "network"]
    assert payload["metadata"]["local_id"] == "gene_retry"


def test_capsule_maps_onto_gep_field_names() -> None:
    payload = to_remote(make_capsule())
    assert payload["type"] == "Capsule"
    assert payload["trigger"] == ["TimeoutError", "ECONNREFUSED"]
    assert payload["confidence"] == 0.88
    assert payload["env_fingerprint"]["platform"] == "linux"
    assert payload["strategy"] == ["add retry", "pool connections"]
    assert payload["content"].startswith("diff --git")
    assert payload["verified_by"][0]["who"] == "ci"


def test_asset_id_is_the_documented_sha256_of_the_canonical_json() -> None:
    payload = to_remote(make_gene())
    body = {k: v for k, v in payload.items() if k not in ("asset_id", "model_name")}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert payload["asset_id"] == f"sha256:{expected}"


def test_asset_id_is_stable_and_content_addressed() -> None:
    assert to_remote(make_gene())["asset_id"] == to_remote(make_gene())["asset_id"]
    assert to_remote(make_gene())["asset_id"] != to_remote(make_gene(title="other"))["asset_id"]


@pytest.mark.parametrize("asset", [make_gene(), make_capsule()])
def test_round_trip_only_gains_the_hub_asset_id(asset) -> None:
    back = from_remote(to_remote(asset))
    assert set(back.extra) - set(asset.extra) == {"asset_id"}
    assert back.to_dict() | {"extra": asset.extra} == asset.to_dict()


@pytest.mark.parametrize("asset", [make_gene(), make_capsule()])
def test_round_trip_is_stable_after_the_first_pass(asset) -> None:
    once = from_remote(to_remote(asset))
    assert from_remote(to_remote(once)) == once


def test_unknown_hub_fields_survive_a_full_round_trip() -> None:
    foreign = {
        "type": "Gene",
        "asset_id": "sha256:deadbeef",
        "category": "repair",
        "summary": "Foreign gene",
        "signals_match": ["s1"],
        "validation": [],
        "strategy": {"k": 1},
        "metadata": {"tags": ["t"], "author": "them", "mystery_meta": 7},
        "gdi_score": 0.91,
        "trust_tier": "featured",
        "domain": "software_engineering",
        "chain_id": "chain_x",
    }
    gene = from_remote(foreign)
    assert gene.extra["gdi_score"] == 0.91
    assert gene.extra["trust_tier"] == "featured"
    assert gene.extra["metadata"]["mystery_meta"] == 7
    assert gene.extra["asset_id"] == "sha256:deadbeef"

    back = to_remote(gene)
    for key in ("gdi_score", "trust_tier", "domain", "chain_id"):
        assert back[key] == foreign[key]
    assert back["metadata"]["mystery_meta"] == 7


def test_an_unknown_category_is_kept_verbatim() -> None:
    gene = from_remote(
        {"type": "Gene", "asset_id": "sha256:x", "category": "quantum", "summary": "s"}
    )
    assert gene.kind == "explore"  # conservative fallback, still a valid Literal
    assert gene.extra["category_raw"] == "quantum"
    assert to_remote(gene)["category"] == "quantum"


def test_a_dict_blast_radius_becomes_readable_and_comes_back_as_a_dict() -> None:
    capsule = from_remote(
        {
            "type": "Capsule",
            "asset_id": "sha256:y",
            "summary": "s",
            "trigger": ["T"],
            "confidence": 0.7,
            "blast_radius": {"files": 2, "lines": 40},
            "content": "z" * 60,
        }
    )
    assert capsule.blast_radius == "files=2,lines=40"
    assert to_remote(capsule)["blast_radius"] == {"files": 2, "lines": 40}


def test_type_is_inferred_when_the_hub_omits_it() -> None:
    assert isinstance(
        from_remote({"asset_id": "sha256:a", "category": "repair", "summary": "s"}), Gene
    )
    assert isinstance(
        from_remote({"asset_id": "sha256:b", "summary": "s", "content": "c" * 60}), Capsule
    )


def test_an_undecidable_payload_is_rejected_not_guessed() -> None:
    with pytest.raises(EvomapGenesError, match="Gene or a Capsule"):
        from_remote({"asset_id": "sha256:c", "summary": "s"})


def test_a_metadata_only_capsule_is_rejected() -> None:
    with pytest.raises(EvomapGenesError, match="at least 50 characters"):
        from_remote({"type": "Capsule", "asset_id": "sha256:d", "summary": "s", "trigger": []})


def test_an_id_less_payload_is_rejected() -> None:
    with pytest.raises(EvomapGenesError, match="no usable id"):
        from_remote({"type": "Gene", "summary": "s"})


def test_from_remote_needs_a_dict() -> None:
    with pytest.raises(EvomapGenesError, match="takes a dict"):
        from_remote("[]")  # type: ignore[arg-type]


def test_to_remote_needs_an_asset() -> None:
    with pytest.raises(EvomapGenesError, match="Gene or a Capsule"):
        to_remote({"id": "x"})  # type: ignore[arg-type]


def test_bundle_links_the_capsule_to_its_gene() -> None:
    gene, capsule = make_gene(), make_capsule()
    assets = _bundle_payload(gene, capsule)
    assert [a["type"] for a in assets] == ["Gene", "Capsule"]
    assert assets[1]["gene"] == assets[0]["asset_id"]
    body = {k: v for k, v in assets[1].items() if k not in ("asset_id", "model_name")}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert assets[1]["asset_id"] == f"sha256:{expected}"  # recomputed after linking


def test_bundle_can_carry_an_evolution_event() -> None:
    from evomap_genes import Event

    event = Event(
        id="ev1", t=1.0, actor="a", intent="repair", outcome="success", mutations=[{"tried": "x"}]
    )
    assets = _bundle_payload(make_gene(), make_capsule(), event)
    assert assets[2]["type"] == "EvolutionEvent"
    assert assets[2]["outcome"] == {"status": "success"}
    assert assets[2]["mutations_tried"] == 1
    assert assets[2]["metadata"]["local_id"] == "ev1"


def test_adapters_are_pure() -> None:
    """Same input, same output; the input is untouched."""
    gene = make_gene()
    before = gene.to_dict()
    assert to_remote(gene) == to_remote(gene)
    assert gene.to_dict() == before
