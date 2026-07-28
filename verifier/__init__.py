"""Independent verifier for certification reports.

ZERO imports from commerce_harness — by design. This package can be
run in a separate process, machine, or audit context to verify that a
certification report is internally consistent.
"""

from verifier.core import verify

__all__ = ["verify"]
