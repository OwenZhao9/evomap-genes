"""A small, deliberately incomplete GEP ``gep-a2a v1.0.0`` HTTP client.

Standard library only (``urllib``).  Four endpoints are wired -- see the README
for which, and for the long list of endpoints this library refuses to touch.

Nothing here runs on its own: every method is reached from an explicit public
``Store`` call.  There is no heartbeat, no task claiming, no background thread.
"""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

__all__ = ["RemoteClient", "RemoteUnavailable"]

log = logging.getLogger(__name__)

PROTOCOL = "gep-a2a"
PROTOCOL_VERSION = "1.0.0"
DEFAULT_URL = "https://evomap.ai"
USER_AGENT = "evomap-genes/0.1.0 (+https://github.com/OwenZhao9/evomap-genes)"


class RemoteUnavailable(Exception):
    """The hub could not be reached, or answered something unusable.

    Never escapes the public API: ``Store`` catches it and degrades.
    """


def _now() -> float:
    """Wall clock, used *only* to stamp outgoing protocol envelopes.

    No decision in this library depends on it (contract 0.2).  Tests replace it.
    """
    import time

    return time.time()


def _envelope(message_type: str, sender_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    return {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "message_type": message_type,
        "message_id": f"msg_{int(now * 1000)}_{uuid.uuid4().hex[:8]}",
        "sender_id": sender_id,
        "timestamp": datetime.fromtimestamp(now, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "payload": payload,
    }


def dig(body: Any, key: str) -> Any:
    """Read ``key`` from the body or from its ``payload`` envelope."""
    if not isinstance(body, dict):
        return None
    if key in body:
        return body[key]
    payload = body.get("payload")
    if isinstance(payload, dict):
        return payload.get(key)
    return None


def extract_assets(body: Any) -> list[dict[str, Any]]:
    """Pull an asset list out of whatever shape the hub answered with."""
    candidates: list[Any] = [body]
    if isinstance(body, dict):
        payload = body.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload)
    for candidate in candidates:
        if isinstance(candidate, list):
            return [a for a in candidate if isinstance(a, dict)]
        if isinstance(candidate, dict):
            for key in ("assets", "results", "items", "data", "capsules", "genes"):
                value = candidate.get(key)
                if isinstance(value, list):
                    return [a for a in value if isinstance(a, dict)]
            if "asset" in candidate and isinstance(candidate["asset"], dict):
                return [candidate["asset"]]
    return []


class RemoteClient:
    """One hub, one node identity.  Blocking; bounded by ``timeout_s``."""

    def __init__(
        self,
        *,
        url: str | None = None,
        node_secret: str | None = None,
        node_id: str | None = None,
        timeout_s: float = 3.0,
    ) -> None:
        self.base_url = (url or DEFAULT_URL).rstrip("/")
        self.node_secret = node_secret or None
        self.node_id = node_id or None
        self.timeout_s = timeout_s

    # ------------------------------------------------------------- transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = False,
        timeout_s: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = None
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.node_secret:
                raise RemoteUnavailable("no node_secret: this endpoint needs an explicit secret")
            headers["Authorization"] = f"Bearer {self.node_secret}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:  # non-2xx
            detail = ""
            with contextlib.suppress(Exception):  # pragma: no cover - body may be consumed
                detail = exc.read().decode("utf-8", "replace")[:400]
            raise RemoteUnavailable(f"HTTP {exc.code} from {path}: {detail}") from exc
        except Exception as exc:
            # Anything the transport can throw -- DNS failure, timeout, a proxy
            # object that blows up, no network stack at all -- degrades to
            # "hub unavailable".  A store() call must never fail because of it.
            raise RemoteUnavailable(f"{path} unreachable: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteUnavailable(f"{path} returned non-JSON: {exc}") from exc

    # ------------------------------------------------------------- endpoints

    def hello(self, *, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /a2a/hello`` -- node registration.

        Only ever called from :meth:`evomap_genes.Store.register`.  Reading the
        protocol docs is not authorization to call this; a human/operator asking
        for it is.
        """
        body = self._request("POST", "/a2a/hello", body=_envelope("hello", node_id, payload))
        if not isinstance(body, dict):
            raise RemoteUnavailable("/a2a/hello returned an unexpected shape")
        return body

    def publish(
        self, assets: list[dict[str, Any]], *, chain_id: str | None = None
    ) -> dict[str, Any]:
        """``POST /a2a/publish`` -- a Gene + Capsule bundle (+ optional event)."""
        if not self.node_id:
            raise RemoteUnavailable("no node_id: call Store.register(node_id=...) first")
        payload: dict[str, Any] = {"assets": assets}
        if chain_id:
            payload["chain_id"] = chain_id
        body = self._request(
            "POST",
            "/a2a/publish",
            body=_envelope("publish", self.node_id, payload),
            auth=True,
        )
        if not isinstance(body, dict):
            raise RemoteUnavailable("/a2a/publish returned an unexpected shape")
        return body

    def search_assets(
        self,
        *,
        signals: list[str],
        limit: int,
        asset_type: str | None = None,
        timeout_s: float | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /a2a/assets/search`` -- public, read-only, no credits spent."""
        params: dict[str, Any] = {"limit": max(1, int(limit))}
        if signals:
            params["signals"] = ",".join(signals)
        if asset_type:
            params["type"] = asset_type
        body = self._request("GET", "/a2a/assets/search", params=params, timeout_s=timeout_s)
        return extract_assets(body)

    def get_asset(self, asset_id: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        """``GET /a2a/assets/:id?detailed=true`` -- public, read-only."""
        body = self._request(
            "GET",
            f"/a2a/assets/{urllib.parse.quote(asset_id, safe='')}",
            params={"detailed": "true"},
            timeout_s=timeout_s,
        )
        assets = extract_assets(body)
        if assets:
            return assets[0]
        if isinstance(body, dict) and (body.get("type") or body.get("asset_id")):
            return body
        raise RemoteUnavailable(f"/a2a/assets/{asset_id} returned no asset")
