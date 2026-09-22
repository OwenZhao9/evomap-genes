"""evomap-genes -- experience assets for agents, offline first.

``Gene`` is a reusable strategy, ``Capsule`` is a validated fix with its audit
chain, ``Event`` is what happened while getting there.  ``Store`` keeps all of it
in sqlite so the agent can look things up with the network unplugged, and
optionally mirrors it to an EvoMap GEP hub.

Field names follow EvoMap GEP v1.0.0 (https://evomap.ai/llms.txt).

.. warning::
   ``Store`` methods block (up to ``timeout_s``) and instances are **not**
   thread-safe.  Never call them from a real-time control loop.
"""

from .adapters import from_remote, to_remote
from .models import Capsule, Event, EvomapGenesError, Gene, SearchHit
from .store import Store

__all__ = [
    "Capsule",
    "Event",
    "EvomapGenesError",
    "Gene",
    "SearchHit",
    "Store",
    "from_remote",
    "to_remote",
]

__version__ = "0.1.0"
