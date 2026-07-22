"""Layer 2 — modular, deterministic compute over published primitives.

``REGISTRY`` lists the enabled compute modules. Adding a metric means adding a module and
appending it here; nothing in the ingestion path changes.
"""

from .base import ComputeModule, ComputeStore
from .cl_profile import ClProfileModule

REGISTRY: tuple[ComputeModule, ...] = (ClProfileModule(),)

__all__ = ["REGISTRY", "ClProfileModule", "ComputeModule", "ComputeStore"]
