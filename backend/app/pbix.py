from __future__ import annotations

from typing import Any


def parse_pbix(data: bytes) -> tuple[str, dict[str, Any], str | None]:
    """Best-effort metadata parser; PBIX failure never blocks manual registration."""
    if not data[:2] == b"PK":
        return "manual_required", {}, "File is not a supported PBIX package; register metadata manually"
    try:
        from pbixray import PBIXRay  # type: ignore[import-not-found]

        parser = PBIXRay(data)
        metadata = {
            "tables": list(parser.tables),
            "relationships": list(parser.relationships),
            "measures": list(parser.measures),
        }
        return "parsed", metadata, None
    except ImportError:
        return "manual_required", {}, "PBIXRay is not installed; manual metadata registration is available"
    except Exception as exc:  # corrupt, encrypted, or thin reports are expected customer inputs
        return "manual_required", {}, f"PBIX metadata parser could not read this file: {type(exc).__name__}"
