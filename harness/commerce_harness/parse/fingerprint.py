from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FormatFingerprint:
    digest: str
    canonical: str

    @classmethod
    def from_structure(cls, structure: dict[str, Any]) -> FormatFingerprint:
        canonical = json.dumps(
            structure,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            canonical=canonical,
        )
