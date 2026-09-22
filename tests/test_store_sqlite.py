"""The sqlite backend end to end -- the path that works with the cable unplugged."""

from __future__ import annotations

import os

import pytest

from conftest import make_capsule, make_gene
from evomap_genes import Capsule, Event, EvomapGenesError, Gene, Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(backend="sqlite", db_path=str(tmp_path / "genes.db"), author="tester")


def test_default_backend_is_sqlite(tmp_path) -> None:
    """No credentials in, nothing remote out (contract 4, authorization boundary)."""
    plain = Store(db_path=str(tmp_path / "a.db"))
    assert plain.stats()["resolved_backend"] == "sqlite"
    assert plain.stats()["remote_enabled"] is False


def test_auto_picks_evomap_only_with_a_node_secret(tmp_path) -> None:
    with_secret = Store(db_path=str(tmp_path / "b.db"), api_key="f" * 64)
    assert with_secret.stats()["resolved_backend"] == "evomap"
    assert with_secret.stats()["remote_enabled"] is True


def test_full_round_trip(store: Store) -> None:
    gene, capsule = make_gene(), make_capsule()
    assert store.store(gene) == gene.id
    assert store.store(capsule) == capsule.id
    assert store.get(gene.id) == gene
    assert store.get(capsule.id) == capsule
    assert store.get("nope") is None


def test_store_overwrites_by_id(store: Store) -> None:
    store.store(make_gene(title="first"))
    store.store(make_gene(title="second"))
    got = store.get("gene_retry")
    assert isinstance(got, Gene) and got.title == "second"
    assert store.stats()["assets"] == 1


def test_store_rejects_foreign_objects(store: Store) -> None:
    with pytest.raises(EvomapGenesError, match="takes a Gene or a Capsule"):
        store.store({"id": "x"})  # type: ignore[arg-type]


def test_record_and_count_events(store: Store) -> None:
    store.record(Event(id="e1", t=1.0, actor="a", intent="repair", subject_id="caps_retry"))
    store.record(Event(id="e2", t=2.0, actor="a", intent="optimize", outcome="success"))
    assert store.stats()["events"] == 2
    with pytest.raises(EvomapGenesError, match="takes an Event"):
        store.record("not an event")  # type: ignore[arg-type]


def test_search_returns_scored_hits_with_reasons(store: Store) -> None:
    store.store(make_gene())
    store.store(make_capsule())
    hits = store.search("timeout retry")
    assert [h.source for h in hits] == ["sqlite", "sqlite"]
    assert all(0.0 < h.score <= 1.0 for h in hits)
    assert all(h.why for h in hits)
    assert hits == sorted(hits, key=lambda h: (-h.score, h.asset.id))


def test_search_filters_by_kind(store: Store) -> None:
    store.store(make_gene())
    store.store(make_capsule())
    assert all(isinstance(h.asset, Gene) for h in store.search("timeout", kind="gene"))
    assert all(isinstance(h.asset, Capsule) for h in store.search("timeout", kind="capsule"))


def test_search_honours_k(store: Store) -> None:
    for i in range(5):
        store.store(make_gene(id=f"g{i}", title=f"timeout fix number {i}"))
    assert len(store.search("timeout", k=2)) == 2
    assert len(store.search("timeout", k=99)) == 5


def test_search_context_range_changes_the_ranking(store: Store) -> None:
    store.store(make_gene(id="flat", title="timeout fix", preconditions=[]))
    store.store(make_gene(id="ranged", title="timeout fix", preconditions=["0 <= tilt_deg <= 30"]))
    upright = store.search("timeout", context={"tilt_deg": 10})
    assert upright[0].asset.id == "ranged"
    tipped = store.search("timeout", context={"tilt_deg": 80})
    assert tipped[0].asset.id == "flat"


def test_search_misses_return_empty(store: Store) -> None:
    store.store(make_gene())
    assert store.search("nothing whatsoever about this") == []


def test_search_argument_validation(store: Store) -> None:
    with pytest.raises(EvomapGenesError, match="k must be an int"):
        store.search("x", k=0)
    with pytest.raises(EvomapGenesError, match="kind must be"):
        store.search("x", kind="genes")  # type: ignore[arg-type]
    with pytest.raises(EvomapGenesError, match="query must be a str"):
        store.search(42)  # type: ignore[arg-type]


def test_search_is_deterministic(store: Store) -> None:
    store.store(make_gene())
    store.store(make_capsule())
    first = [(h.asset.id, h.score, h.why) for h in store.search("timeout retry")]
    for _ in range(3):
        assert [(h.asset.id, h.score, h.why) for h in store.search("timeout retry")] == first


def test_everything_survives_a_reopen(tmp_path) -> None:
    path = str(tmp_path / "persist.db")
    first = Store(backend="sqlite", db_path=path)
    first.store(make_gene())
    first.record(Event(id="e1", t=1.0, actor="a", intent="repair"))
    del first
    second = Store(backend="sqlite", db_path=path)
    assert second.get("gene_retry") == make_gene()
    assert second.stats()["events"] == 1


def test_stats_shape(store: Store) -> None:
    store.store(make_gene())
    store.store(make_capsule())
    store.record(Event(id="e1", t=1.0, actor="a", intent="repair"))
    stats = store.stats()
    assert stats["assets"] == 2
    assert stats["genes"] == 1 and stats["capsules"] == 1
    assert stats["unsynced"] == 2 and stats["synced"] == 0
    assert stats["events"] == 1
    assert stats["backend"] == "sqlite" and stats["resolved_backend"] == "sqlite"
    assert stats["author"] == "tester"


# --------------------------------------------------------------- export/import


def test_export_import_round_trip(tmp_path) -> None:
    src = Store(backend="sqlite", db_path=str(tmp_path / "src.db"))
    gene, capsule = make_gene(), make_capsule()
    event = Event(
        id="e1",
        t=9.0,
        actor="a",
        intent="repair",
        subject_id="caps_retry",
        mutations=[{"tried": "sleep"}],
        outcome="success",
        payload={"k": 1},
    )
    src.store(gene)
    src.store(capsule)
    src.record(event)

    path = str(tmp_path / "dump.jsonl")
    assert src.export(path) == 3

    dst = Store(backend="sqlite", db_path=str(tmp_path / "dst.db"))
    assert dst.import_(path) == 3
    assert dst.get(gene.id) == gene
    assert dst.get(capsule.id) == capsule
    assert dst.stats()["events"] == 1
    assert dst.export(str(tmp_path / "again.jsonl")) == 3
    with open(path, encoding="utf-8") as a, open(tmp_path / "again.jsonl", encoding="utf-8") as b:
        assert a.read() == b.read()


def test_import_is_idempotent(tmp_path) -> None:
    src = Store(backend="sqlite", db_path=str(tmp_path / "s.db"))
    src.store(make_gene())
    path = str(tmp_path / "d.jsonl")
    src.export(path)
    dst = Store(backend="sqlite", db_path=str(tmp_path / "d.db"))
    dst.import_(path)
    dst.import_(path)
    assert dst.stats()["assets"] == 1


def test_export_preserves_the_synced_flag(tmp_path, hub) -> None:
    src = Store(db_path=str(tmp_path / "s.db"))
    src.register(node_id="node_tester")
    src.store(make_gene())
    src.store(make_capsule())
    assert src.stats()["synced"] == 2
    path = str(tmp_path / "d.jsonl")
    src.export(path)
    dst = Store(backend="sqlite", db_path=str(tmp_path / "d.db"))
    dst.import_(path)
    assert dst.stats()["synced"] == 2


def test_import_rejects_garbage(tmp_path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"record": "gene", "doc": {"no_id": true}}\n', encoding="utf-8")
    store = Store(backend="sqlite", db_path=str(tmp_path / "x.db"))
    with pytest.raises(EvomapGenesError, match="not a valid export record"):
        store.import_(str(bad))


def test_import_skips_blank_lines(tmp_path) -> None:
    src = Store(backend="sqlite", db_path=str(tmp_path / "s.db"))
    src.store(make_gene())
    path = tmp_path / "d.jsonl"
    src.export(str(path))
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    dst = Store(backend="sqlite", db_path=str(tmp_path / "d.db"))
    assert dst.import_(str(path)) == 1


def test_import_missing_file(tmp_path) -> None:
    store = Store(backend="sqlite", db_path=str(tmp_path / "x.db"))
    with pytest.raises(EvomapGenesError, match="cannot read"):
        store.import_(str(tmp_path / "absent.jsonl"))


# ------------------------------------------------------------- constructor


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"backend": "postgres"}, "backend must be one of"),
        ({"timeout_s": 0}, "timeout_s must be > 0"),
        ({"timeout_s": -1.0}, "timeout_s must be > 0"),
        ({"timeout_s": "fast"}, "timeout_s must be a number"),
        ({"db_path": ""}, "db_path must be a non-empty str"),
        ({"author": 7}, "author must be a str"),
    ],
)
def test_constructor_errors(tmp_path, kwargs: dict, match: str) -> None:
    kwargs.setdefault("db_path", str(tmp_path / "x.db"))
    with pytest.raises(ValueError, match=match):
        Store(**kwargs)


def test_missing_directory_is_a_construction_error(tmp_path) -> None:
    with pytest.raises(EvomapGenesError, match="directory does not exist"):
        Store(db_path=os.path.join(str(tmp_path), "nope", "x.db"))
