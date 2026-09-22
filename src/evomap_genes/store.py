"""``Store`` -- the one object callers touch.

Offline-first: every write lands in sqlite before anything is attempted over the
network, and a failed network attempt is a ``synced=False`` row, never an
exception.

.. warning::
   Every method here blocks (up to ``timeout_s``).  Do **not** call it from a
   real-time control loop -- run it on a worker thread or at a task boundary.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Mapping
from typing import Any, Literal

from .adapters import _bundle_payload, from_remote
from .local import LocalStore
from .models import Capsule, Event, EvomapGenesError, Gene, SearchHit
from .remote import DEFAULT_URL, RemoteClient, RemoteUnavailable, dig
from .scoring import score_asset, tokenize

__all__ = ["Store"]

log = logging.getLogger(__name__)

BACKENDS = ("auto", "evomap", "sqlite")

#: A hit the hub returned but our local scorer finds no words for still means
#: something -- the hub matched it on signals we cannot see.  Floor, not a fake score.
_REMOTE_FLOOR = 0.25


def _monotonic() -> float:
    """Deadline clock.  Only measures elapsed time, never used as a data value."""
    return time.monotonic()


class Store:
    """Experience assets in, experience assets out -- with or without a network.

    Args:
        backend: ``"sqlite"`` (local only), ``"evomap"`` (local + hub), or
            ``"auto"``, which picks ``evomap`` when a ``node_secret`` is present
            and ``sqlite`` otherwise.
        url: hub base URL, default ``https://evomap.ai``.
        api_key: the GEP **node_secret** (64-hex) used as ``Authorization:
            Bearer``.  Passed in by the caller; this library never writes it to
            disk and never obtains one without an explicit :meth:`register` call.
        db_path: sqlite file. Its directory must already exist.
        timeout_s: upper bound on how long any single call blocks.
        author: stamped onto nothing automatically; reported by :meth:`stats`
            and used as the default ``actor`` label in log messages.
    """

    def __init__(
        self,
        backend: Literal["auto", "evomap", "sqlite"] = "auto",
        *,
        url: str | None = None,
        api_key: str | None = None,
        db_path: str = "genes.db",
        timeout_s: float = 3.0,
        author: str = "",
    ) -> None:
        if backend not in BACKENDS:
            raise EvomapGenesError(f"backend must be one of {BACKENDS}, got {backend!r}")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            raise EvomapGenesError("timeout_s must be a number")
        if timeout_s <= 0:
            raise EvomapGenesError(f"timeout_s must be > 0, got {timeout_s}")
        if not isinstance(db_path, str) or not db_path:
            raise EvomapGenesError("db_path must be a non-empty str")
        if not isinstance(author, str):
            raise EvomapGenesError("author must be a str")

        parent = os.path.dirname(os.path.abspath(db_path))
        if not os.path.isdir(parent):
            raise EvomapGenesError(f"db_path directory does not exist: {parent}")

        self.backend = backend
        self.timeout_s = float(timeout_s)
        self.author = author
        self.url = (url or DEFAULT_URL).rstrip("/")
        self._node_secret = api_key or None  # in memory only, never persisted

        try:
            self._local = LocalStore(db_path)
        except sqlite3.Error as exc:
            raise EvomapGenesError(f"cannot open sqlite database {db_path!r}: {exc}") from exc

        self._node_id = self._local.meta_get("node_id")
        self._last_error: str | None = None
        self._remote_skipped = 0
        self._remote_calls = 0
        self._remote_failures = 0

    # ------------------------------------------------------------- internals

    @property
    def _remote_enabled(self) -> bool:
        """Remote work happens only with an explicit secret and a non-sqlite backend."""
        return self.backend != "sqlite" and bool(self._node_secret)

    def _client(self, *, timeout_s: float | None = None) -> RemoteClient:
        return RemoteClient(
            url=self.url,
            node_secret=self._node_secret,
            node_id=self._node_id,
            timeout_s=self.timeout_s if timeout_s is None else timeout_s,
        )

    def _note_failure(self, what: str, exc: Exception) -> None:
        self._remote_failures += 1
        self._last_error = f"{what}: {exc}"
        log.warning("evomap-genes: %s -- staying local (%s)", what, exc)

    # -------------------------------------------------------------- register

    def register(
        self,
        *,
        node_id: str,
        model: str | None = None,
        capabilities: Mapping[str, Any] | None = None,
        env_fingerprint: Mapping[str, Any] | None = None,
        identity_doc: str = "",
        constitution: str = "",
    ) -> dict[str, Any]:
        """Register this node with the hub -- ``POST /a2a/hello``. Explicit only.

        Nothing in this library calls it for you: not import, not ``__init__``,
        not ``store()``.  Reading the EvoMap docs does not authorize registration;
        you calling this method does.

        What it does, exactly, and nothing else:

        1. one HTTPS POST to ``<url>/a2a/hello`` carrying ``node_id``, the
           optional fields you pass, and your local gene/capsule counts;
        2. keeps the returned ``node_secret`` **in memory** on this instance so
           later calls can authenticate;
        3. persists only the non-secret ``node_id`` in the local sqlite file.

        It does **not** write the secret to disk, start a heartbeat, claim tasks
        or publish anything.  Storing the secret is your decision -- it is in the
        returned dict under ``"node_secret"``.

        Returns:
            ``{"ok": bool, "node_id": str, "node_secret": str | None,
            "response": dict, "error": str}``.  Never raises on network trouble.
        """
        if not isinstance(node_id, str) or not node_id.strip():
            raise EvomapGenesError("node_id must be a non-empty str, e.g. 'node_myagent'")
        result: dict[str, Any] = {
            "ok": False,
            "node_id": node_id,
            "node_secret": None,
            "response": {},
            "error": "",
        }
        if self.backend == "sqlite":
            result["error"] = (
                "backend='sqlite': registration is a remote action; use 'auto'/'evomap'"
            )
            return result

        counts = self._local.counts()
        payload: dict[str, Any] = {
            "capabilities": dict(capabilities or {}),
            "gene_count": counts["genes"],
            "capsule_count": counts["capsules"],
        }
        if model:
            payload["model"] = model
        if env_fingerprint:
            payload["env_fingerprint"] = dict(env_fingerprint)
        if identity_doc:
            payload["identity_doc"] = identity_doc
        if constitution:
            payload["constitution"] = constitution

        self._remote_calls += 1
        try:
            body = RemoteClient(url=self.url, timeout_s=self.timeout_s).hello(
                node_id=node_id, payload=payload
            )
        except RemoteUnavailable as exc:
            self._note_failure("register()", exc)
            result["error"] = str(exc)
            return result

        secret = dig(body, "node_secret")
        self._node_id = str(dig(body, "your_node_id") or node_id)
        self._local.meta_set("node_id", self._node_id)
        if isinstance(secret, str) and secret:
            self._node_secret = secret
            result["node_secret"] = secret
        result["ok"] = True
        result["node_id"] = self._node_id
        result["response"] = body
        return result

    # ---------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        kind: Literal["gene", "capsule"] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Find experience worth reusing.  Blocks; never raises on network trouble.

        Scoring (sqlite): keyword hits weighted by where they land -- title 3.0,
        tags 2.0, signals/preconditions 2.0, body 1.0 -- plus ``context``
        handling: a numeric context value inside a range the asset declares adds
        2.5, outside it subtracts 3.0.  ``why`` on every hit spells out which
        zone matched and by how much.

        With the ``evomap`` backend the hub's public search runs too and its
        results are merged in; if the hub is unreachable you silently get the
        local results.
        """
        if not isinstance(query, str):
            raise EvomapGenesError("query must be a str")
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise EvomapGenesError(f"k must be an int >= 1, got {k!r}")
        if kind not in (None, "gene", "capsule"):
            raise EvomapGenesError(f"kind must be 'gene', 'capsule' or None, got {kind!r}")
        ctx = dict(context) if context else None

        hits: dict[str, SearchHit] = {}
        for asset, _synced in self._local.iter_assets(kind):
            score, why = score_asset(asset, query, ctx)
            if score > 0.0:
                hits[asset.id] = SearchHit(asset=asset, score=score, why=why, source="sqlite")

        if self._remote_enabled:
            for hit in self._remote_search(query, k=k, kind=kind, context=ctx):
                existing = hits.get(hit.asset.id)
                if existing is None or hit.score > existing.score:
                    hits[hit.asset.id] = hit

        ordered = sorted(hits.values(), key=lambda h: (-h.score, h.asset.id))
        return ordered[:k]

    def _remote_search(
        self,
        query: str,
        *,
        k: int,
        kind: str | None,
        context: dict[str, Any] | None,
    ) -> list[SearchHit]:
        deadline = _monotonic() + self.timeout_s
        signals = tokenize(query)
        asset_type = {"gene": "Gene", "capsule": "Capsule"}.get(kind or "")
        client = self._client()
        self._remote_calls += 1
        try:
            raw = client.search_assets(
                signals=signals,
                limit=k,
                asset_type=asset_type,
                timeout_s=max(0.05, deadline - _monotonic()),
            )
        except RemoteUnavailable as exc:
            self._note_failure("search()", exc)
            return []

        out: list[SearchHit] = []
        for rank, payload in enumerate(raw, start=1):
            asset = self._materialize(payload, client, deadline)
            if asset is None:
                self._remote_skipped += 1
                continue
            local_score, why = score_asset(asset, query, context)
            score = local_score if local_score > 0.0 else _REMOTE_FLOOR
            note = f"hub /a2a/assets/search matched signals '{','.join(signals)}' (rank {rank})"
            out.append(
                SearchHit(
                    asset=asset,
                    score=score,
                    why=f"{why}; {note}" if why else note,
                    source="evomap",
                )
            )
        return out

    def _materialize(
        self, payload: dict[str, Any], client: RemoteClient, deadline: float
    ) -> Gene | Capsule | None:
        """Turn a hub payload into an asset, fetching detail once if the summary is thin."""
        try:
            return from_remote(payload)
        except EvomapGenesError as exc:
            log.debug("evomap-genes: summary not usable (%s), trying detail", exc)
        asset_id = payload.get("asset_id") or payload.get("id")
        if not isinstance(asset_id, str) or _monotonic() >= deadline:
            return None
        try:
            detail = client.get_asset(asset_id, timeout_s=max(0.05, deadline - _monotonic()))
            return from_remote(detail)
        except (RemoteUnavailable, EvomapGenesError) as exc:
            log.debug("evomap-genes: dropping hub asset %s (%s)", asset_id, exc)
            return None

    # ------------------------------------------------------------------- get

    def get(self, id: str) -> Gene | Capsule | None:
        """Local first.  If the hub has it and we do not, cache it locally and return it."""
        if not isinstance(id, str) or not id:
            raise EvomapGenesError("id must be a non-empty str")
        asset = self._local.get(id)
        if asset is not None:
            return asset
        if not self._remote_enabled:
            return None
        self._remote_calls += 1
        try:
            payload = self._client().get_asset(id)
            remote_asset = from_remote(payload)
        except (RemoteUnavailable, EvomapGenesError) as exc:
            self._note_failure(f"get({id!r})", exc)
            return None
        self._local.upsert(remote_asset, synced=True, remote_id=id)
        return remote_asset

    # ----------------------------------------------------------------- store

    def store(self, asset: Gene | Capsule) -> str:
        """Write an asset.  sqlite first, hub second, offline always fine.

        The local write happens before any network attempt and is the one that
        decides the return value.  If the hub attempt fails -- or there is no
        network at all -- the row stays ``synced=False`` and :meth:`sync` picks
        it up later.  Returns the asset id.
        """
        if not isinstance(asset, (Gene, Capsule)):
            raise EvomapGenesError(f"store() takes a Gene or a Capsule, got {type(asset).__name__}")

        self._local.upsert(asset, synced=False)  # step 1: local, always

        if self._remote_enabled:
            bundle = self._pair(asset)
            if bundle is None:
                log.debug(
                    "evomap-genes: %s has no Gene+Capsule partner yet; GEP publishes bundles "
                    "only, leaving it synced=False",
                    asset.id,
                )
            else:
                self._push(bundle[0], bundle[1], deadline=_monotonic() + self.timeout_s)
        return asset.id

    def _pair(self, asset: Gene | Capsule) -> tuple[Gene, Capsule] | None:
        """Find the Gene+Capsule pair GEP requires, using ``extra['gene_id']``."""
        if isinstance(asset, Capsule):
            ref = asset.extra.get("gene_id") or asset.extra.get("gene")
            if not isinstance(ref, str) or not ref:
                return None
            gene = self._local.get(ref)
            return (gene, asset) if isinstance(gene, Gene) else None
        partners = [
            c
            for c, synced in self._local.iter_assets("capsule")
            if isinstance(c, Capsule)
            and (c.extra.get("gene_id") or c.extra.get("gene")) == asset.id
            and not synced
        ]
        return (asset, partners[0]) if partners else None

    def _push(self, gene: Gene, capsule: Capsule, *, deadline: float) -> bool:
        """One publish attempt.  Returns success; never raises."""
        event = self._local.unsynced_event_for((capsule.id, gene.id))
        payload = _bundle_payload(gene, capsule, event)
        client = self._client(timeout_s=max(0.05, deadline - _monotonic()))
        self._remote_calls += 1
        try:
            body = client.publish(payload)
        except RemoteUnavailable as exc:
            self._note_failure(f"publish({gene.id}+{capsule.id})", exc)
            return False
        decision = dig(body, "decision")
        if isinstance(decision, str) and decision.lower() in ("rejected", "error", "quarantined"):
            self._note_failure(
                f"publish({gene.id}+{capsule.id})", RemoteUnavailable(f"hub said {decision!r}")
            )
            return False
        self._local.mark_synced(gene.id, remote_id=payload[0].get("asset_id"))
        self._local.mark_synced(capsule.id, remote_id=payload[1].get("asset_id"))
        if event is not None:
            self._local.mark_event_synced(event.id)
        return True

    # ---------------------------------------------------------------- record

    def record(self, ev: Event) -> None:
        """Log an evolution event locally.

        Stays local: GEP carries an ``EvolutionEvent`` only as the optional third
        element of a Gene+Capsule bundle, so events ride along on the next
        successful publish of the asset they point at (``subject_id``).
        """
        if not isinstance(ev, Event):
            raise EvomapGenesError(f"record() takes an Event, got {type(ev).__name__}")
        self._local.record(ev, synced=False)

    # ------------------------------------------------------------------ sync

    def sync(self) -> dict[str, Any]:
        """Push everything local that the hub has not seen.  Bounded by ``timeout_s``.

        Returns a report: how many bundles went up, how many assets are still
        unpaired (a Gene with no Capsule cannot be published under GEP), whether
        the deadline cut the run short, and the last error seen.
        """
        counts = self._local.counts()
        report: dict[str, Any] = {
            "backend": self._resolved_backend,
            "ok": False,
            "pushed_bundles": 0,
            "pushed_assets": 0,
            "failed_bundles": 0,
            "unpaired": 0,
            "pending_before": counts["unsynced"],
            "pending_after": counts["unsynced"],
            "deadline_reached": False,
            "error": "",
        }
        if not self._remote_enabled:
            report["error"] = (
                "remote disabled: backend='sqlite' or no node_secret "
                "(pass api_key=... or call register())"
            )
            return report

        deadline = _monotonic() + self.timeout_s
        seen: set[str] = set()
        for asset in self._local.unsynced():
            if asset.id in seen:
                continue
            if _monotonic() >= deadline:
                report["deadline_reached"] = True
                break
            pair = self._pair(asset)
            if pair is None:
                report["unpaired"] += 1
                continue
            gene, capsule = pair
            if gene.id in seen or capsule.id in seen:
                continue
            seen.update({gene.id, capsule.id})  # one attempt per bundle per run
            if self._push(gene, capsule, deadline=deadline):
                report["pushed_bundles"] += 1
                report["pushed_assets"] += 2
            else:
                report["failed_bundles"] += 1
        report["ok"] = report["failed_bundles"] == 0 and not report["deadline_reached"]
        report["pending_after"] = self._local.counts()["unsynced"]
        report["error"] = self._last_error or ""
        return report

    # ----------------------------------------------------------------- stats

    @property
    def _resolved_backend(self) -> str:
        """``backend`` with ``"auto"`` resolved: evomap when a node_secret exists.

        Internal: the contract freezes the public surface, so callers read this
        through ``stats()["resolved_backend"]``.
        """
        if self.backend != "auto":
            return self.backend
        return "evomap" if self._node_secret else "sqlite"

    def stats(self) -> dict[str, Any]:
        """Counters, no network access."""
        counts = self._local.counts()
        return {
            **counts,
            "backend": self.backend,
            "resolved_backend": self._resolved_backend,
            "remote_enabled": self._remote_enabled,
            "has_node_secret": bool(self._node_secret),
            "node_id": self._node_id,
            "url": self.url,
            "db_path": self._local.db_path,
            "timeout_s": self.timeout_s,
            "author": self.author,
            "remote_calls": self._remote_calls,
            "remote_failures": self._remote_failures,
            "remote_skipped": self._remote_skipped,
            "last_error": self._last_error or "",
        }

    # -------------------------------------------------------- export /import

    def export(self, path: str) -> int:
        """Write every asset and event to a JSONL file.  Returns the record count."""
        if not isinstance(path, str) or not path:
            raise EvomapGenesError("path must be a non-empty str")
        written = 0
        with open(path, "w", encoding="utf-8") as fh:
            for record in self._local.raw_docs():
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                written += 1
        return written

    def import_(self, path: str) -> int:
        """Read a JSONL file written by :meth:`export`.  Returns the record count.

        Existing ids are overwritten, so importing the same file twice is a
        no-op.  A malformed record raises :class:`EvomapGenesError` rather than
        silently dropping somebody's experience.
        """
        if not isinstance(path, str) or not path:
            raise EvomapGenesError("path must be a non-empty str")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError as exc:
            raise EvomapGenesError(f"cannot read {path!r}: {exc}") from exc

        loaded = 0
        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                kind = record["record"]
                doc = record["doc"]
                synced = bool(record.get("synced", False))
                if kind == "gene":
                    self._local.upsert(Gene.from_dict(doc), synced=synced)
                elif kind == "capsule":
                    self._local.upsert(Capsule.from_dict(doc), synced=synced)
                elif kind == "event":
                    self._local.record(Event.from_dict(doc), synced=synced)
                else:
                    raise EvomapGenesError(f"unknown record type {kind!r}")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise EvomapGenesError(
                    f"{path}:{lineno} is not a valid export record: {exc}"
                ) from exc
            loaded += 1
        return loaded
