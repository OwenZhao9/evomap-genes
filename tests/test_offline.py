"""The whole point of the library: it keeps working with the cable pulled.

``no_real_network`` (autouse, in conftest) already makes every socket call fail.
These tests do not install the fake hub, so the transport really has nowhere to go.
"""

from __future__ import annotations

import urllib.error

import pytest

from conftest import FakeHub, make_capsule, make_gene
from evomap_genes import Event, Store


@pytest.fixture
def offline_store(tmp_path) -> Store:
    """Configured for the hub, with no hub reachable."""
    store = Store(db_path=str(tmp_path / "genes.db"), api_key="f" * 64)
    store._node_id = "node_tester"
    return store


def test_store_succeeds_with_no_network_at_all(offline_store: Store) -> None:
    gene, capsule = make_gene(), make_capsule()
    assert offline_store.store(gene) == gene.id
    assert offline_store.store(capsule) == capsule.id
    assert offline_store.get(gene.id) == gene
    assert offline_store.get(capsule.id) == capsule
    assert offline_store.stats()["unsynced"] == 2
    assert offline_store.stats()["synced"] == 0


def test_store_does_not_raise_on_a_dns_failure(tmp_path, monkeypatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("[Errno 8] nodename nor servname provided")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    store = Store(db_path=str(tmp_path / "g.db"), api_key="f" * 64)
    store._node_id = "node_tester"
    store.store(make_gene())
    assert store.store(make_capsule()) == "caps_retry"
    assert store.stats()["unsynced"] == 2


def test_search_works_offline(offline_store: Store) -> None:
    offline_store.store(make_gene())
    offline_store.store(make_capsule())
    hits = offline_store.search("timeout retry", context={"battery_v": 11.8})
    assert [h.source for h in hits] == ["sqlite", "sqlite"]
    assert len(hits) == 2


def test_get_and_record_and_stats_work_offline(offline_store: Store) -> None:
    offline_store.record(Event(id="e1", t=1.0, actor="a", intent="repair"))
    assert offline_store.get("nothing") is None
    assert offline_store.stats()["events"] == 1


def test_export_import_work_offline(offline_store: Store, tmp_path) -> None:
    offline_store.store(make_gene())
    path = str(tmp_path / "dump.jsonl")
    assert offline_store.export(path) == 1
    other = Store(backend="sqlite", db_path=str(tmp_path / "other.db"))
    assert other.import_(path) == 1


def test_sync_reports_the_outage_instead_of_raising(offline_store: Store) -> None:
    offline_store.store(make_gene())
    offline_store.store(make_capsule())
    report = offline_store.sync()
    assert report["ok"] is False
    assert report["pending_after"] == 2
    assert report["error"]


def test_everything_catches_up_once_the_hub_returns(tmp_path, monkeypatch) -> None:
    """Offline writes, then one sync when the network comes back."""
    store = Store(db_path=str(tmp_path / "g.db"), api_key="f" * 64)
    store._node_id = "node_tester"
    store.store(make_gene())
    store.store(make_capsule())
    assert store.stats()["unsynced"] == 2

    hub = FakeHub().install(monkeypatch)
    hub.route("/a2a/publish", lambda call: {"payload": {"decision": "accepted"}})
    report = store.sync()
    assert report["pushed_bundles"] == 1
    assert store.stats()["unsynced"] == 0
