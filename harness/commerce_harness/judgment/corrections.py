from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CorrectionEntry:
    correction_id: str
    suggestion_id: str
    residual_id: str
    category: str
    metric: str
    period: str
    shop: str
    model_action: str
    human_action: str
    human_reason: str
    decided_by: str

    def __post_init__(self) -> None:
        for name in (
            "correction_id",
            "suggestion_id",
            "residual_id",
            "category",
            "metric",
            "period",
            "shop",
            "model_action",
            "human_action",
            "human_reason",
            "decided_by",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.model_action == self.human_action:
            raise ValueError("a correction requires a human decision that differs from the model")


class CorrectionBook:
    """结构化错题本，可选 JSONL 持久化；不触碰正式账本。"""

    def __init__(
        self,
        entries: Iterable[CorrectionEntry] = (),
        *,
        path: str | Path | None = None,
    ) -> None:
        self._entries = list(entries)
        self.path = Path(path) if path is not None else None

    @property
    def entries(self) -> tuple[CorrectionEntry, ...]:
        return tuple(self._entries)

    @classmethod
    def load(cls, path: str | Path) -> CorrectionBook:
        target = Path(path)
        if not target.exists():
            return cls(path=target)
        with target.open(encoding="utf-8") as handle:
            entries = [CorrectionEntry(**json.loads(line)) for line in handle if line.strip()]
        return cls(entries, path=target)

    def append(self, entry: CorrectionEntry) -> None:
        if any(existing.correction_id == entry.correction_id for existing in self._entries):
            raise ValueError(f"duplicate correction_id: {entry.correction_id}")
        self._entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False, sort_keys=True) + "\n")

    def search(
        self,
        *,
        category: str,
        metric: str | None = None,
        limit: int = 5,
    ) -> tuple[CorrectionEntry, ...]:
        matches = [
            item
            for item in reversed(self._entries)
            if item.category == category and (metric is None or item.metric == metric)
        ]
        return tuple(matches[: max(0, limit)])

    def overturned_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for entry in self._entries:
            result[entry.category] = result.get(entry.category, 0) + 1
        return result

