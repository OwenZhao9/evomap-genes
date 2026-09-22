"""The evomap backend, driven entirely against a fake hub.

No test here opens a socket; ``conftest.no_real_network`` makes sure of it.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from conftest import FakeHub, make_capsule, make_gene
from evomap_genes import Capsule, Gene, Store
from evomap_genes.adapters import to_remote


@pytest.fixture
def online(tmp_path, hub: FakeHub) -> Store:
    store = Store(db_path=str(tmp_path / "genes.db"), api_key=hub.node_secret)
    store._node_id = "node_tester"  # normally set by register(); see test_register_*
    return store


# ----------------------------------------------------------------- register


def test_register_calls_hello_once_and_returns_the_secret(tmp_path, hub: FakeHub) -> None:
    store = Store(db_path=str(tmp_path / "g.db"))
    assert hub.calls == []  # constructing a Store talks to nobody

    result = store.register(node_id="node_tester", model="claude-opus-5")

    assert hub.paths == ["/a2a/hello"]
    call = hub.calls[0]
    assert call["method"] == "POST"
    assert call["auth"] is None  # hello is the one endpoint that needs no secret
    assert call["body"]["protocol"] == "gep-a2a"
    assert call["body"]["protocol_version"] == "1.0.0"
    assert call["body"]["message_type"] == "hello"
    assert call["body"]["sender_id"] == "node_tester"
    assert call["body"]["payload"]["model"] == "claude-opus-5"
    assert result["ok"] is True
    assert result["node_secret"] == hub.node_secret
    assert store.stats()["resolved_backend"] == "evomap"


def test_register_never_writes_the_secret_to_disk(tmp_path, hub: FakeHub) -> None:
    db = tmp_path / "g.db"
    store = Store(db_path=str(db))
    store.register(node_id="node_tester")
    assert hub.node_secret.encode() not in db.read_bytes()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert hub.node_secret.encode() not in path.read_bytes()


def test_register_persists_only_the_node_id(tmp_path, hub: FakeHub) -> None:
    db = str(tmp_path / "g.db")
    Store(db_path=db).register(node_id="node_tester")
    reopened = Store(db_path=db, api_key=hub.node_secret)
    assert reopened.stats()["node_id"] == "node_tester"


def test_register_reports_failure_instead_of_raising(tmp_path, hub: FakeHub) -> None:
    hub.offline = True
    result = Store(db_path=str(tmp_path / "g.db")).register(node_id="node_tester")
    assert result["ok"] is False
    assert result["node_secret"] is None
    assert "unreachable" in result["error"]


def test_register_refused_on_an_explicitly_local_store(tmp_path, hub: FakeHub) -> None:
    store = Store(backend="sqlite", db_path=str(tmp_path / "g.db"))
    result = store.register(node_id="node_tester")
    assert result["ok"] is False
    assert hub.calls == []


def test_register_validates_its_argument(tmp_path, hub: FakeHub) -> None:
    with pytest.raises(ValueError, match="node_id must be"):
        Store(db_path=str(tmp_path / "g.db")).register(node_id="")


# -------------------------------------------------------------------- store


def test_double_write_marks_the_bundle_synced(online: Store, hub: FakeHub) -> None:
    online.store(make_gene())
    # A lone Gene is not publishable under GEP -- it waits for its Capsule.
    assert online.stats()["synced"] == 0
    assert "/a2a/publish" not in hub.paths

    online.store(make_capsule())
    assert online.stats() == {**online.stats(), "synced": 2, "unsynced": 0}
    assert hub.paths.count("/a2a/publish") == 1


def test_publish_sends_a_gene_capsule_bundle_with_bearer_auth(online: Store, hub: FakeHub) -> None:
    online.store(make_gene())
    online.store(make_capsule())
    call = next(c for c in hub.calls if c["path"] == "/a2a/publish")
    assert call["auth"] == f"Bearer {hub.node_secret}"
    assets = call["body"]["payload"]["assets"]
    assert [a["type"] for a in assets] == ["Gene", "Capsule"]
    assert assets[1]["gene"] == assets[0]["asset_id"]
    assert all(a["asset_id"].startswith("sha256:") for a in assets)


def test_a_recorded_event_rides_along_with_the_bundle(online: Store, hub: FakeHub) -> None:
    from evomap_genes import Event

    online.record(
        Event(
            id="ev1",
            t=5.0,
            actor="a",
            intent="repair",
            subject_id="caps_retry",
            outcome="success",
            mutations=[{"tried": "sleep"}, {"tried": "retry"}],
        )
    )
    online.store(make_gene())
    online.store(make_capsule())
    assets = next(c for c in hub.calls if c["path"] == "/a2a/publish")["body"]["payload"]["assets"]
    assert [a["type"] for a in assets] == ["Gene", "Capsule", "EvolutionEvent"]
    assert assets[2]["intent"] == "repair"
    assert assets[2]["mutations_tried"] == 2
    assert online.stats()["events_unsynced"] == 0


def test_a_rejected_publish_leaves_the_row_unsynced(online: Store, hub: FakeHub) -> None:
    hub.route(
        "/a2a/publish",
        lambda call: {"payload": {"decision": "rejected", "reason": "hash mismatch"}},
    )
    online.store(make_gene())
    online.store(make_capsule())
    assert online.stats()["unsynced"] == 2
    assert "rejected" in online.stats()["last_error"]


def test_an_http_error_leaves_the_row_unsynced(online: Store, hub: FakeHub) -> None:
    hub.route(
        "/a2a/publish",
        lambda call: urllib.error.HTTPError("u", 429, "too many", {}, None),  # type: ignore[arg-type]
    )
    online.store(make_gene())
    online.store(make_capsule())
    assert online.stats()["unsynced"] == 2
    assert "HTTP 429" in online.stats()["last_error"]


def test_store_without_a_node_id_stays_local(tmp_path, hub: FakeHub) -> None:
    store = Store(db_path=str(tmp_path / "g.db"), api_key=hub.node_secret)
    store.store(make_gene())
    store.store(make_capsule())
    assert store.stats()["unsynced"] == 2
    assert "/a2a/publish" not in hub.paths


# --------------------------------------------------------------------- sync


def test_sync_pushes_what_store_could_not(tmp_path, hub: FakeHub) -> None:
    store = Store(db_path=str(tmp_path / "g.db"), api_key=hub.node_secret)
    store._node_id = "node_tester"
    hub.offline = True
    store.store(make_gene())
    store.store(make_capsule())
    assert store.stats()["unsynced"] == 2

    hub.offline = False
    report = store.sync()
    assert report["ok"] is True
    assert report["pushed_bundles"] == 1
    assert report["pushed_assets"] == 2
    assert report["pending_before"] == 2
    assert report["pending_after"] == 0
    assert store.stats()["synced"] == 2


def test_sync_is_a_no_op_when_everything_is_synced(online: Store, hub: FakeHub) -> None:
    online.store(make_gene())
    online.store(make_capsule())
    before = hub.paths.count("/a2a/publish")
    report = online.sync()
    assert report["pushed_bundles"] == 0
    assert hub.paths.count("/a2a/publish") == before


def test_sync_counts_unpaired_assets(online: Store, hub: FakeHub) -> None:
    online.store(make_gene(id="lonely_gene"))
    report = online.sync()
    assert report["unpaired"] == 1
    assert report["pushed_bundles"] == 0
    assert report["ok"] is True


def test_sync_reports_failures_without_raising(online: Store, hub: FakeHub) -> None:
    hub.offline = True
    online.store(make_gene())
    online.store(make_capsule())
    report = online.sync()
    assert report["ok"] is False
    assert report["failed_bundles"] == 1
    assert report["pending_after"] == 2


def test_sync_without_a_secret_explains_itself(tmp_path) -> None:
    report = Store(backend="sqlite", db_path=str(tmp_path / "g.db")).sync()
    assert report["ok"] is False
    assert "node_secret" in report["error"]
    assert report["pushed_bundles"] == 0


# ------------------------------------------------------------------- search


def _hub_gene() -> dict:
    return to_remote(make_gene(id="hub_gene", title="Hub side timeout remedy"))


def _hub_capsule() -> dict:
    return to_remote(make_capsule(id="hub_caps", title="Hub side retry capsule", extra={}))


def test_search_merges_hub_results(online: Store, hub: FakeHub) -> None:
    hub.route("/a2a/assets/search", lambda call: {"assets": [_hub_gene(), _hub_capsule()]})
    online.store(make_gene())
    hits = online.search("timeout retry", k=5)
    by_id = {h.asset.id: h for h in hits}
    assert by_id["gene_retry"].source == "sqlite"
    assert by_id["hub_gene"].source == "evomap"
    assert "hub /a2a/assets/search matched signals" in by_id["hub_gene"].why
    call = next(c for c in hub.calls if c["path"] == "/a2a/assets/search")
    assert call["method"] == "GET"
    assert "signals=timeout%2Cretry" in call["query"]


def test_search_passes_the_kind_filter_to_the_hub(online: Store, hub: FakeHub) -> None:
    hub.route("/a2a/assets/search", lambda call: {"assets": []})
    online.search("timeout", kind="capsule")
    assert "type=Capsule" in hub.calls[-1]["query"]


def test_search_tolerates_other_response_shapes(online: Store, hub: FakeHub) -> None:
    hub.route("/a2a/assets/search", lambda call: {"payload": {"results": [_hub_gene()]}})
    assert any(h.asset.id == "hub_gene" for h in online.search("timeout"))
    hub.route("/a2a/assets/search", lambda call: [_hub_gene()])
    assert any(h.asset.id == "hub_gene" for h in online.search("timeout"))


def test_search_falls_back_to_a_detail_fetch_for_thin_capsules(online: Store, hub: FakeHub) -> None:
    thin = {k: v for k, v in _hub_capsule().items() if k != "content"}
    hub.route("/a2a/assets/search", lambda call: {"assets": [thin]})
    hub.route(f"/a2a/assets/{thin['asset_id']}", lambda call: _hub_capsule())
    hits = online.search("retry")
    assert [h.asset.id for h in hits] == ["hub_caps"]
    assert "detailed=true" in hub.calls[-1]["query"]


def test_search_drops_a_hub_asset_it_cannot_complete(online: Store, hub: FakeHub) -> None:
    thin = {k: v for k, v in _hub_capsule().items() if k != "content"}
    hub.route("/a2a/assets/search", lambda call: {"assets": [thin]})
    assert online.search("retry") == []
    assert online.stats()["remote_skipped"] == 1


def test_search_degrades_to_local_when_the_hub_is_down(online: Store, hub: FakeHub) -> None:
    online.store(make_gene())
    hub.offline = True
    hits = online.search("timeout")
    assert [h.source for h in hits] == ["sqlite"]
    assert "unreachable" in online.stats()["last_error"]


def test_search_prefers_the_better_score_for_a_duplicate_id(online: Store, hub: FakeHub) -> None:
    local = make_gene(id="dup", title="unrelated words", tags=[], strategy={}, constraints=[])
    online.store(local)
    hub.route(
        "/a2a/assets/search",
        lambda call: {"assets": [to_remote(make_gene(id="dup", title="timeout retry fix"))]},
    )
    hits = online.search("timeout retry")
    assert len(hits) == 1
    assert hits[0].source == "evomap"


# ---------------------------------------------------------------------- get


def test_get_falls_through_to_the_hub_and_caches(online: Store, hub: FakeHub) -> None:
    payload = _hub_gene()
    hub.route("/a2a/assets/hub_gene", lambda call: payload)
    got = online.get("hub_gene")
    assert isinstance(got, Gene) and got.title == "Hub side timeout remedy"
    assert online.stats()["assets"] == 1
    assert online.stats()["synced"] == 1
    before = len(hub.calls)
    assert online.get("hub_gene") == got  # served locally the second time
    assert len(hub.calls) == before


def test_get_returns_none_when_the_hub_does_not_have_it(online: Store, hub: FakeHub) -> None:
    assert online.get("missing") is None
    assert online.stats()["assets"] == 0


def test_get_never_reaches_the_hub_on_the_sqlite_backend(tmp_path, hub: FakeHub) -> None:
    store = Store(backend="sqlite", db_path=str(tmp_path / "g.db"))
    assert store.get("anything") is None
    assert hub.calls == []


def test_get_prefers_a_local_capsule(online: Store, hub: FakeHub) -> None:
    capsule = make_capsule()
    online.store(capsule)
    before = len(hub.calls)
    assert isinstance(online.get(capsule.id), Capsule)
    assert len(hub.calls) == before


# ---------------------------------------------------------------- transport


def test_custom_hub_url_is_honoured(tmp_path, hub: FakeHub) -> None:
    store = Store(db_path=str(tmp_path / "g.db"), url="https://hub.example/")
    store.register(node_id="node_tester")
    assert hub.calls[0]["path"] == "/a2a/hello"
    assert store.stats()["url"] == "https://hub.example"


def test_timeout_is_passed_down_to_urlopen(tmp_path, hub: FakeHub) -> None:
    store = Store(db_path=str(tmp_path / "g.db"), timeout_s=0.25)
    store.register(node_id="node_tester")
    assert hub.calls[0]["timeout"] == pytest.approx(0.25)


def test_non_json_response_is_survivable(online: Store, hub: FakeHub, monkeypatch) -> None:
    class Garbage:
        def read(self) -> bytes:
            return b"<html>503</html>"

        def __enter__(self) -> Garbage:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Garbage())
    assert online.search("timeout") == []
    assert "non-JSON" in online.stats()["last_error"]


def test_envelope_timestamp_is_iso8601_utc(online: Store, hub: FakeHub) -> None:
    online.store(make_gene())
    online.store(make_capsule())
    envelope = next(c for c in hub.calls if c["path"] == "/a2a/publish")["body"]
    assert envelope["timestamp"].endswith("Z")
    assert envelope["message_id"].startswith("msg_")
    assert json.dumps(envelope)  # the whole envelope is JSON-serialisable
