"""60 seconds with evomap-genes. Runs offline, writes to a temp file, no network.

uv run python examples/quickstart.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from evomap_genes import Capsule, Event, Gene, Store


def main() -> None:
    db = Path(tempfile.mkdtemp()) / "genes.db"
    store = Store(db_path=str(db), author="quickstart")  # backend="auto" -> sqlite

    gene = Gene(
        id="gene_retry_backoff",
        title="Retry with exponential backoff on transient timeouts",
        kind="repair",
        preconditions=["0 <= tilt_deg <= 30", "battery_v >= 11.0"],
        constraints=["never retry a non-idempotent write"],
        validation=["pytest -q tests/test_retry.py"],
        strategy={"steps": ["catch TimeoutError", "sleep 2**n", "retry <= 3"]},
        tags=["timeout", "network", "retry"],
        author="quickstart",
        created_at=1_700_000_000.0,
        updated_at=1_700_000_000.0,
    )
    capsule = Capsule(
        id="caps_retry_backoff",
        title="Bounded retry plus a connection pool on the telemetry client",
        trigger_signals=["TimeoutError", "ECONNREFUSED"],
        confidence=0.88,
        blast_radius="single-file",
        environment={"platform": "linux", "python": "3.11", "battery_v": [11.0, 12.6]},
        strategy_steps=["wrap the call in retry(3)", "share one pooled session"],
        content=(
            "diff --git a/telemetry.py b/telemetry.py\n"
            "-    resp = requests.get(url, timeout=1)\n"
            "+    resp = session.get(url, timeout=1)  # pooled + retried\n"
        ),
        verified_by=[{"who": "ci", "t": 1_700_000_100.0, "how": "pytest -q"}],
        tags=["timeout", "retry"],
        author="quickstart",
        created_at=1_700_000_100.0,
        extra={"gene_id": "gene_retry_backoff"},  # links the pair for GEP publishing
    )

    store.store(gene)
    store.store(capsule)
    store.record(
        Event(
            id="ev_001",
            t=1_700_000_200.0,
            actor="quickstart",
            intent="stop the telemetry client from timing out",
            mutations=[{"tried": "raise timeout"}, {"tried": "retry + pool"}],
            outcome="success",
            subject_id="caps_retry_backoff",
        )
    )

    print("# what the agent inherits when it wakes up upright")
    for hit in store.search("timeout retry", k=5, context={"tilt_deg": 12, "battery_v": 11.8}):
        print(f"{hit.score:.2f}  [{hit.source}]  {hit.asset.title}")
        print(f"        why: {hit.why}")

    print("\n# same query while the machine is tipped over")
    for hit in store.search("timeout retry", k=5, context={"tilt_deg": 85}):
        print(f"{hit.score:.2f}  [{hit.source}]  {hit.asset.title}")

    stats = store.stats()
    print(
        f"\n# backend={stats['resolved_backend']} assets={stats['assets']} "
        f"unsynced={stats['unsynced']} events={stats['events']}"
    )
    print(f"# db: {db}")


if __name__ == "__main__":
    main()
