from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import polars as pl


@dataclass(frozen=True)
class RecomputeSpec:
    business_key: str
    amount_columns: tuple[str, ...]
    date_column: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RecomputeSpec:
        business_key = str(payload["business_key"])
        amount_columns = tuple(str(item) for item in payload["amount_columns"])
        if not amount_columns:
            raise ValueError("amount_columns 不能为空")
        return cls(
            business_key=business_key,
            amount_columns=amount_columns,
            date_column=(
                str(payload["date_column"]) if payload.get("date_column") else None
            ),
        )


def deterministic_recompute(
    source_path: Path,
    spec: RecomputeSpec,
    output_path: Path,
) -> dict[str, Any]:
    suffix = source_path.suffix.casefold()
    if suffix == ".csv":
        frame = pl.read_csv(
            source_path,
            infer_schema_length=1000,
            try_parse_dates=False,
            ignore_errors=False,
        )
    elif suffix in {".xlsx", ".xlsm"}:
        import openpyxl

        workbook = openpyxl.load_workbook(
            source_path, read_only=True, data_only=True, keep_links=False
        )
        try:
            sheet = next(
                item for item in workbook.worksheets if item.sheet_state == "visible"
            )
            rows = sheet.iter_rows(values_only=True)
            headers = [str(value or "").strip() for value in next(rows)]
            data = list(rows)
            frame = pl.DataFrame(
                {
                    header: [row[index] if index < len(row) else None for row in data]
                    for index, header in enumerate(headers)
                    if header
                },
                strict=False,
            )
        finally:
            workbook.close()
    else:
        raise ValueError("重计算仅支持 CSV/XLSX")

    required = {spec.business_key, *spec.amount_columns}
    if spec.date_column:
        required.add(spec.date_column)
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"缺少重计算字段: {', '.join(missing)}")

    normalized = frame.select(
        [
            pl.col(spec.business_key).cast(pl.String).str.strip_chars().alias("business_key"),
            *[
                pl.col(column)
                .cast(pl.String)
                .str.replace_all(",", "")
                .cast(pl.Decimal(20, 4), strict=True)
                .alias(column)
                for column in spec.amount_columns
            ],
            *(
                [pl.col(spec.date_column).cast(pl.String).alias("business_date")]
                if spec.date_column
                else []
            ),
        ]
    ).filter(pl.col("business_key").is_not_null() & (pl.col("business_key") != ""))

    # 业务键跨文件/跨 run 去重由控制面提供已认证键集合；单任务内固定保留首行。
    normalized = normalized.unique(subset=["business_key"], keep="first", maintain_order=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized.write_parquet(output_path, compression="zstd")
    connection = duckdb.connect(database=":memory:")
    try:
        select_parts = [
            "COUNT(*)::BIGINT AS business_key_count",
            *[
                f'COALESCE(SUM("{column}"), 0)::DECIMAL(20,4) AS "{column}"'
                for column in spec.amount_columns
            ],
        ]
        row = connection.execute(
            f"SELECT {', '.join(select_parts)} FROM read_parquet(?)",
            [str(output_path)],
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise RuntimeError("确定性汇总未返回结果")

    output_hash = _sha256(output_path)
    totals = {
        column: format(Decimal(str(row[index + 1])), ".4f")
        for index, column in enumerate(spec.amount_columns)
    }
    result = {
        "business_key_count": int(row[0]),
        "totals": totals,
        "normalized_sha256": output_hash,
        "normalized_path": str(output_path),
        "engine": {
            "name": "finance_agent_deterministic",
            "version": 1,
            "spec_checksum": hashlib.sha256(
                json.dumps(
                    {
                        "business_key": spec.business_key,
                        "amount_columns": spec.amount_columns,
                        "date_column": spec.date_column,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
    }
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
