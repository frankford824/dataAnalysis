"""Pure, deterministic accounting kernel.

The package deliberately has no database, network, spreadsheet-library, or LLM
dependency.  Callers must supply immutable source evidence and explicit rule
versions.
"""

from .money import DECIMAL38_4_MAX, MONEY_QUANTUM, MoneyValue, parse_money

__all__ = [
    "DECIMAL38_4_MAX",
    "MONEY_QUANTUM",
    "MoneyValue",
    "parse_money",
]
