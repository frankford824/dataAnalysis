"""Which side of the cloud boundary this process runs on.

``edge`` sits next to the customer's files and may read them. ``core`` holds the
kernel, the workbench and the model orchestration, and must never touch customer
storage directly — everything reaches it over HTTP as content-addressed uploads.
Keeping this in one place means the boundary is a single check, not a habit.
"""

from __future__ import annotations

import os

ROLE_ENV = "FA_ROLE"
# Transitional escape hatch: existing single-machine installs still let core
# dial finance-win over SSH. It must be turned off once files arrive through
# edge uploads, and the boundary check refuses the cloud shape while it is on.
LEGACY_CORE_SOURCE_READ_ENV = "FA_ALLOW_CORE_SOURCE_READ"
CORE = "core"
EDGE = "edge"
_TRUTHY = {"1", "true", "yes", "on"}


def current_role() -> str:
    return os.environ.get(ROLE_ENV, "").strip().lower() or EDGE


def legacy_core_source_read() -> bool:
    return (
        os.environ.get(LEGACY_CORE_SOURCE_READ_ENV, "").strip().lower() in _TRUTHY
    )


def reads_customer_sources() -> bool:
    """True when this process is allowed to read customer files directly."""
    return current_role() != CORE or legacy_core_source_read()
