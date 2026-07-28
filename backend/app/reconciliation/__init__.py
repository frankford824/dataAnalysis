"""Independent reconciliation bounded context.

Importing this package registers its SQLAlchemy models on the shared metadata
without coupling the domain to the legacy ingestion and analytics models.
"""

from . import models

__all__ = ["models"]
