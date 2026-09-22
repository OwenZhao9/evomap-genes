"""The authorization boundary EvoMap states at the top of its own llms.txt.

Reading a protocol document does not authorize registration, credential
storage, heartbeat loops, task claiming or publishing.  These tests are how the
library proves it keeps to that.
"""

from __future__ import annotations

import importlib
import inspect

import evomap_genes
from conftest import FakeHub, make_capsule, make_gene
from evomap_genes import Store


def test_importing_the_package_makes_no_http_call(monkeypatch) -> None:
    hub = FakeHub().install(monkeypatch)
    importlib.reload(evomap_genes)
    assert hub.calls == []


def test_constructing_a_store_makes_no_http_call(tmp_path, hub: FakeHub) -> None:
    for backend in ("auto", "sqlite", "evomap"):
        Store(backend=backend, db_path=str(tmp_path / f"{backend}.db"), api_key="f" * 64)
    assert hub.calls == []


def test_nothing_registers_by_itself(tmp_path, hub: FakeHub) -> None:
    """Only an explicit register() may hit /a2a/hello."""
    store = Store(db_path=str(tmp_path / "g.db"), api_key="f" * 64)
    store._node_id = "node_tester"
    store.store(make_gene())
    store.store(make_capsule())
    store.search("timeout")
    store.get("whatever")
    store.sync()
    store.stats()
    store.export(str(tmp_path / "d.jsonl"))
    assert "/a2a/hello" not in hub.paths


def test_the_secret_is_never_persisted(tmp_path, hub: FakeHub) -> None:
    store = Store(db_path=str(tmp_path / "g.db"))
    store.register(node_id="node_tester")
    store.store(make_gene())
    store.store(make_capsule())
    store.export(str(tmp_path / "dump.jsonl"))
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert hub.node_secret not in path.read_text(encoding="utf-8", errors="ignore")


def test_no_remote_traffic_without_an_explicit_secret(tmp_path, hub: FakeHub) -> None:
    store = Store(backend="evomap", db_path=str(tmp_path / "g.db"))  # asked for, but no secret
    store.store(make_gene())
    store.store(make_capsule())
    store.search("timeout")
    store.get("x")
    assert store.stats()["remote_enabled"] is False
    assert hub.calls == []


def test_no_heartbeat_no_tasks_no_auto_publish_endpoints_exist() -> None:
    """Endpoints the contract forbids are simply not implemented."""
    from evomap_genes import remote

    source = inspect.getsource(remote)
    for forbidden in (
        "/a2a/heartbeat",
        "/a2a/task",
        "/a2a/work",
        "/a2a/report",
        "/a2a/validator/stake",
        "/a2a/council",
        "/a2a/dm",
        "/a2a/ask",
        "/a2a/session",
    ):
        assert forbidden not in source


def test_no_background_threads_or_timers_anywhere() -> None:
    import pkgutil

    for module_info in pkgutil.iter_modules(evomap_genes.__path__):
        module = importlib.import_module(f"evomap_genes.{module_info.name}")
        source = inspect.getsource(module)
        for forbidden in ("threading", "asyncio", "atexit", "signal.", "subprocess"):
            assert forbidden not in source, f"{module_info.name} mentions {forbidden}"


def test_no_locks_in_the_library() -> None:
    """Contract 0.3: instances are single-threaded and must not lock."""
    import pkgutil

    for module_info in pkgutil.iter_modules(evomap_genes.__path__):
        module = importlib.import_module(f"evomap_genes.{module_info.name}")
        source = inspect.getsource(module)
        assert "Lock(" not in source
        assert "RLock(" not in source


def test_the_library_never_prints() -> None:
    import pkgutil

    for module_info in pkgutil.iter_modules(evomap_genes.__path__):
        module = importlib.import_module(f"evomap_genes.{module_info.name}")
        source = inspect.getsource(module)
        assert "print(" not in source


def test_public_surface_matches_the_contract() -> None:
    assert sorted(evomap_genes.__all__) == [
        "Capsule",
        "Event",
        "EvomapGenesError",
        "Gene",
        "SearchHit",
        "Store",
        "from_remote",
        "to_remote",
    ]
    public_methods = sorted(name for name in vars(Store) if not name.startswith("_"))
    assert public_methods == [
        "export",
        "get",
        "import_",
        "record",
        "register",
        "search",
        "stats",
        "store",
        "sync",
    ]


def test_store_signature_matches_the_contract() -> None:
    params = inspect.signature(Store.__init__).parameters
    assert list(params) == [
        "self",
        "backend",
        "url",
        "api_key",
        "db_path",
        "timeout_s",
        "author",
    ]
    assert params["backend"].default == "auto"
    assert params["db_path"].default == "genes.db"
    assert params["timeout_s"].default == 3.0


def test_no_hardware_or_serial_imports() -> None:
    import pkgutil

    for module_info in pkgutil.iter_modules(evomap_genes.__path__):
        module = importlib.import_module(f"evomap_genes.{module_info.name}")
        source = inspect.getsource(module)
        for forbidden in ("serial", "usb", "smbus", "RPi", "gpio"):
            assert forbidden not in source.lower(), f"{module_info.name} mentions {forbidden}"


def test_only_the_standard_library_is_required() -> None:
    import pathlib
    import tomllib

    data = tomllib.loads(
        (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert data["project"]["dependencies"] == []
    assert data["project"]["requires-python"] == ">=3.11"
