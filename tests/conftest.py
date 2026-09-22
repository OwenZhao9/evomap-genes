"""Shared fixtures.

Two guarantees enforced here:

* no test ever opens a real socket (``no_real_network`` is autouse);
* the fake hub records every request so tests can assert on what was sent --
  and, just as importantly, on what was *not* sent.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from evomap_genes import Capsule, Gene


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard stop: any attempt at a real connection fails the test."""

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("tests must not touch the real network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeHub:
    """A stand-in for https://evomap.ai driven entirely from memory."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.routes: dict[str, Any] = {}
        self.offline = False
        self.node_secret = "a" * 64

    def route(self, path: str, handler: Any) -> None:
        self.routes[path] = handler

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeHub:
        monkeypatch.setattr(urllib.request, "urlopen", self.urlopen)
        return self

    @property
    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]

    def urlopen(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        full = request.full_url
        path = urllib.parse.unquote(
            full.split("?", 1)[0]
            .replace("https://evomap.ai", "")
            .replace("https://hub.example", "")
        )
        query = full.split("?", 1)[1] if "?" in full else ""
        body = json.loads(request.data.decode()) if request.data else None
        self.calls.append(
            {
                "method": request.get_method(),
                "path": path,
                "query": query,
                "body": body,
                "auth": request.headers.get("Authorization"),
                "timeout": timeout,
            }
        )
        if self.offline:
            raise urllib.error.URLError("network is unreachable")
        handler = self.routes.get(path)
        if handler is None:
            raise urllib.error.HTTPError(full, 404, "not found", {}, None)  # type: ignore[arg-type]
        result = handler(self.calls[-1]) if callable(handler) else handler
        if isinstance(result, urllib.error.HTTPError):
            raise result
        return _FakeResponse(json.dumps(result).encode())


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> FakeHub:
    fake = FakeHub().install(monkeypatch)
    fake.route(
        "/a2a/hello",
        lambda call: {
            "status": "acknowledged",
            "your_node_id": call["body"]["sender_id"],
            "hub_node_id": "hub_test",
            "node_secret": fake.node_secret,
            "claim_code": "REEF-0000",
            "credit_balance": 100,
        },
    )
    fake.route(
        "/a2a/publish",
        lambda call: {
            "protocol": "gep-a2a",
            "message_type": "decision",
            "payload": {"decision": "accepted", "bundle_id": "bundle_1"},
        },
    )
    fake.route("/a2a/assets/search", lambda call: {"assets": []})
    return fake


def make_gene(**over: Any) -> Gene:
    kwargs: dict[str, Any] = {
        "id": "gene_retry",
        "title": "Retry with exponential backoff on timeout",
        "kind": "repair",
        "preconditions": ["tilt_deg <= 30"],
        "constraints": ["no sudo"],
        "validation": ["pytest -q tests/test_retry.py"],
        "strategy": {"steps": ["catch", "sleep", "retry"]},
        "tags": ["timeout", "network"],
        "author": "tester",
        "created_at": 100.0,
        "updated_at": 101.0,
    }
    kwargs.update(over)
    return Gene(**kwargs)


def make_capsule(**over: Any) -> Capsule:
    kwargs: dict[str, Any] = {
        "id": "caps_retry",
        "title": "Bounded retry plus connection pooling",
        "trigger_signals": ["TimeoutError", "ECONNREFUSED"],
        "confidence": 0.88,
        "blast_radius": "single-file",
        "environment": {"platform": "linux", "battery_v": [11.0, 12.6]},
        "strategy_steps": ["add retry", "pool connections"],
        "content": "diff --git a/client.py b/client.py\n+    retry(attempts=3, backoff=2.0)\n",
        "verified_by": [{"who": "ci", "t": 120.0, "how": "pytest -q"}],
        "tags": ["timeout"],
        "author": "tester",
        "created_at": 110.0,
        "extra": {"gene_id": "gene_retry"},
    }
    kwargs.update(over)
    return Capsule(**kwargs)
