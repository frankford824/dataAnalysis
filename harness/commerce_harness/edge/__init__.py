"""Customer-side edge: read local/remote source files and upload to core.

Edge never shares a filesystem or DuckDB with core. The only allowed
crossing is HTTP (and object-store PUT when configured).
"""

from .client import CoreUploadClient
from .server import create_edge_app, run_edge

__all__ = ["CoreUploadClient", "create_edge_app", "run_edge"]
