from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from fastapi import HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .audit import record_audit
from .models import (
    CertifiedAggregate,
    Enterprise,
    IngestionRun,
    MetricDefinition,
    NormalizedRecord,
    Problem,
    ReviewQueueItem,
    SemanticModelVersion,
    SourceBinding,
    SourceDefinition,
    Store,
    utcnow,
)
from .security import RequestContext
from .storage import ObjectStorage
from .config import get_settings


CANONICAL_NUMERIC = ["revenue", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost"]
ALLOWED_FILE_TYPES = {".csv", ".xlsx", ".xls", ".zip"}


def recognize_source_candidates(db: Session, ctx: RequestContext, filename: str, data: bytes, store_id: str | None) -> list[SourceDefinition]:
    if not ctx.enterprise_id:
        return []
    frame = parse_tabular(filename, data)
    headers = {str(value).strip().lower() for value in frame.columns}
    suffix = Path(filename).suffix.lower().lstrip(".")
    now = utcnow()
    sources = db.scalars(
        select(SourceDefinition).where(
            SourceDefinition.enterprise_id == ctx.enterprise_id,
            SourceDefinition.archived_at.is_(None),
            SourceDefinition.status.in_(["active", "approved", "published"]),
            or_(SourceDefinition.effective_from.is_(None), SourceDefinition.effective_from <= now),
            or_(SourceDefinition.effective_to.is_(None), SourceDefinition.effective_to > now),
        )
    ).all()
    matches: list[SourceDefinition] = []
    for source in sources:
        file_types = {str(value).lower().lstrip(".") for value in source.file_types}
        if file_types and suffix not in file_types:
            continue
        recognition = source.recognition or {}
        required = {str(value).strip().lower() for value in recognition.get("required_headers", [])}
        alias_map = {
            str(key).strip().lower(): {str(alias).strip().lower() for alias in ([values] if isinstance(values, str) else values)}
            for key, values in (source.field_aliases or {}).items()
        }
        if required and not all(field in headers or bool(alias_map.get(field, set()).intersection(headers)) for field in required):
            continue
        filename_pattern = recognition.get("filename_pattern")
        if filename_pattern and not re.search(filename_pattern, Path(filename).name, re.IGNORECASE):
            continue
        if store_id:
            direct = set(db.scalars(select(SourceBinding.scope_id).where(SourceBinding.source_definition_id == source.id, SourceBinding.scope_type == "store", SourceBinding.archived_at.is_(None))).all())
            if direct and store_id not in direct:
                requested_logical = db.scalar(select(Store.logical_id).where(Store.id == store_id, Store.enterprise_id == ctx.enterprise_id))
                bound_logical = set(db.scalars(select(Store.logical_id).where(Store.id.in_(direct), Store.enterprise_id == ctx.enterprise_id)).all())
                if not requested_logical or requested_logical not in bound_logical:
                    continue
        matches.append(source)
    return matches


def create_review_problem(db: Session, ctx: RequestContext, kind: str, message: str, details: dict[str, Any], ingestion_run_id: str | None = None) -> Problem:
    problem = Problem(enterprise_id=ctx.enterprise_id, ingestion_run_id=ingestion_run_id, kind=kind, user_message=message, technical_detail=details, created_by=ctx.user_id)
    db.add(problem)
    db.flush()
    db.add(ReviewQueueItem(enterprise_id=ctx.enterprise_id, problem_id=problem.id, priority=80 if kind == "source_not_recognized" else 60))
    record_audit(db, ctx, "queue_review", "problem", problem.id, {"kind": kind})
    db.commit()
    return problem


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _read_single(name: str, data: bytes) -> pl.DataFrame:
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        frame = pl.read_csv(io.BytesIO(data), try_parse_dates=True, infer_schema_length=5000)
    if suffix in {".xlsx", ".xls"}:
        frame = pl.read_excel(io.BytesIO(data), infer_schema_length=5000)
    if suffix not in {".csv", ".xlsx", ".xls"}:
        raise HTTPException(status_code=422, detail=f"unsupported file type: {suffix}")
    if frame.height > get_settings().max_input_rows:
        raise HTTPException(status_code=413, detail="file exceeds the configured row limit")
    return frame


def parse_tabular(filename: str, data: bytes) -> pl.DataFrame:
    settings = get_settings()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file exceeds the configured upload limit")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_FILE_TYPES:
        raise HTTPException(status_code=422, detail="only CSV, XLSX, XLS, and ZIP are supported")
    if suffix != ".zip":
        return _read_single(filename, data)
    frames: list[pl.DataFrame] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            total_size = sum(info.file_size for info in archive.infolist() if not info.is_dir())
            if total_size > settings.max_uncompressed_bytes:
                raise HTTPException(status_code=413, detail="ZIP exceeds the configured uncompressed limit")
            for info in archive.infolist():
                if ".." in Path(info.filename).parts or Path(info.filename).is_absolute():
                    raise HTTPException(status_code=422, detail="ZIP contains an unsafe path")
                if info.compress_size and info.file_size / info.compress_size > 200:
                    raise HTTPException(status_code=422, detail="ZIP compression ratio is unsafe")
                if info.is_dir() or Path(info.filename).suffix.lower() not in {".csv", ".xlsx", ".xls"}:
                    continue
                frames.append(_read_single(info.filename, archive.read(info)))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=422, detail="invalid ZIP file") from exc
    if not frames:
        raise HTTPException(status_code=422, detail="ZIP contains no supported data files")
    try:
        combined = pl.concat(frames, how="diagonal_relaxed")
        if combined.height > settings.max_input_rows:
            raise HTTPException(status_code=413, detail="ZIP exceeds the configured row limit")
        return combined
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="ZIP tables could not be combined") from exc


def _rename_aliases(frame: pl.DataFrame, source: SourceDefinition) -> pl.DataFrame:
    aliases: dict[str, Any] = source.field_aliases or {}
    normalized_lookup = {str(column).strip().lower(): column for column in frame.columns}
    rename: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        choices = [candidates] if isinstance(candidates, str) else list(candidates)
        choices.insert(0, canonical)
        for candidate in choices:
            actual = normalized_lookup.get(str(candidate).strip().lower())
            if actual:
                rename[actual] = canonical
                break
    coverage_candidate = normalized_lookup.get(source.coverage_time_field.strip().lower())
    if "occurred_at" not in rename.values() and coverage_candidate:
        rename[coverage_candidate] = "occurred_at"
    return frame.rename(rename)


def _normalise(frame: pl.DataFrame, source: SourceDefinition, store_id: str | None) -> tuple[pl.DataFrame, int, dict[str, int]]:
    frame = _rename_aliases(frame, source)
    if "occurred_at" not in frame.columns:
        raise HTTPException(status_code=422, detail="coverage time field was not found")
    frame = frame.with_columns(
        pl.col("occurred_at").cast(pl.String).str.to_datetime(strict=False, time_zone="UTC").alias("occurred_at")
    )
    if frame["occurred_at"].null_count():
        raise HTTPException(status_code=422, detail="coverage time field contains invalid values")
    numeric_errors: dict[str, int] = {}
    for column in CANONICAL_NUMERIC:
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(Decimal("0.0000"), dtype=pl.Decimal(20, 4)).alias(column))
            numeric_errors[column] = 0
        else:
            text_value = pl.col(column).cast(pl.String, strict=False).str.strip_chars().str.replace_all(",", "")
            parsed = text_value.cast(pl.Decimal(20, 4), strict=False)
            numeric_errors[column] = int(frame.select((text_value.is_not_null() & (text_value != "") & parsed.is_null()).sum()).item())
            frame = frame.with_columns(parsed.fill_null(Decimal("0.0000")).alias(column))
    if "order_id" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("order_id"))
    else:
        frame = frame.with_columns(pl.col("order_id").cast(pl.String, strict=False))
    if "event_type" not in frame.columns:
        frame = frame.with_columns(pl.lit("sale").alias("event_type"))
    if "store_id" not in frame.columns:
        frame = frame.with_columns(pl.lit(store_id).cast(pl.String).alias("store_id"))
    elif store_id:
        frame = frame.with_columns(pl.col("store_id").cast(pl.String).fill_null(store_id))
    dedupe_keys = [key for key in source.dedupe_keys if key in frame.columns]
    original_rows = frame.height
    if dedupe_keys:
        frame = frame.unique(subset=dedupe_keys, keep="first", maintain_order=True)
    return frame, original_rows - frame.height, numeric_errors


def _validate_stores(db: Session, source: SourceDefinition, frame: pl.DataFrame, supplied_store_id: str | None, ctx: RequestContext) -> set[str]:
    tenant = source.enterprise_id
    store_ids = {value for value in frame["store_id"].drop_nulls().cast(pl.String).to_list() if value}
    if supplied_store_id:
        store_ids.add(supplied_store_id)
    if not store_ids:
        raise HTTPException(status_code=422, detail="a store context is required")
    valid = set(db.scalars(select(Store.id).where(Store.enterprise_id == tenant, Store.id.in_(store_ids))).all())
    if valid != store_ids:
        raise HTTPException(status_code=422, detail="file contains unknown or cross-tenant store identifiers")
    if ctx.store_ids is not None:
        logical_ids = set(db.scalars(select(Store.logical_id).where(Store.id.in_(store_ids))).all())
        if not store_ids.issubset(ctx.store_ids) and not logical_ids.issubset(ctx.store_ids):
            raise HTTPException(status_code=403, detail="file includes a store outside the account scope")
    direct_bindings = set(
        db.scalars(
            select(SourceBinding.scope_id).where(
                SourceBinding.enterprise_id == tenant,
                SourceBinding.source_definition_id == source.id,
                SourceBinding.scope_type == "store",
                SourceBinding.archived_at.is_(None),
            )
        ).all()
    )
    if direct_bindings and not store_ids.issubset(direct_bindings):
        supplied_logical = set(db.scalars(select(Store.logical_id).where(Store.id.in_(store_ids), Store.enterprise_id == tenant)).all())
        bound_logical = set(db.scalars(select(Store.logical_id).where(Store.id.in_(direct_bindings), Store.enterprise_id == tenant)).all())
        if not supplied_logical.issubset(bound_logical):
            raise HTTPException(status_code=422, detail="file includes a store outside the source binding")
    return store_ids


def _duckdb_summary(frame: pl.DataFrame, duplicate_rows: int) -> dict[str, Any]:
    connection = duckdb.connect(":memory:")
    try:
        connection.register("input_rows", frame.to_arrow())
        row = connection.execute(
            """SELECT count(*) AS rows, count(DISTINCT order_id) AS orders,
            coalesce(sum(revenue), 0) AS revenue, coalesce(sum(refund), 0) AS refund,
            coalesce(sum(platform_fee), 0), coalesce(sum(advertising_fee), 0),
            coalesce(sum(shipping_fee), 0), coalesce(sum(product_cost), 0),
            coalesce(sum(revenue-refund-platform_fee-advertising_fee-shipping_fee-product_cost), 0)
            FROM input_rows"""
        ).fetchone()
    finally:
        connection.close()
    return {
        "row_count": int(row[0]),
        "order_count": int(row[1]),
        "revenue": str(Decimal(str(row[2])).quantize(Decimal("0.0001"))),
        "refund": str(Decimal(str(row[3])).quantize(Decimal("0.0001"))),
        "platform_fee": str(Decimal(str(row[4])).quantize(Decimal("0.0001"))),
        "advertising_fee": str(Decimal(str(row[5])).quantize(Decimal("0.0001"))),
        "shipping_fee": str(Decimal(str(row[6])).quantize(Decimal("0.0001"))),
        "fees": str(sum(Decimal(str(value)) for value in row[4:7]).quantize(Decimal("0.0001"))),
        "product_cost": str(Decimal(str(row[7])).quantize(Decimal("0.0001"))),
        "profit": str(Decimal(str(row[8])).quantize(Decimal("0.0001"))),
        "duplicate_rows_removed": duplicate_rows,
    }


def _quality(source: SourceDefinition, frame: pl.DataFrame, summary: dict[str, Any], numeric_errors: dict[str, int], dropped_before_activation: int, store_ids: set[str]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(key: str, applicable: bool, passed: bool | None = None, **details: Any) -> None:
        checks.append({"key": key, "applicable": applicable, "status": "not_applicable" if not applicable else "passed" if passed else "failed", **details})

    check("row_count", True, frame.height > 0, actual=frame.height)
    check("valid_time", True, frame.height > 0, rows_before_activation=dropped_before_activation)
    check("amount_format", True, sum(numeric_errors.values()) == 0, errors=numeric_errors)
    check("store_scope", True, bool(store_ids), stores=sorted(store_ids))
    usable_dedupe_keys = [key for key in source.dedupe_keys if key in frame.columns]
    post_dedupe_unique = frame.height == frame.unique(subset=usable_dedupe_keys).height if usable_dedupe_keys else None
    check("duplicate_business_key", bool(usable_dedupe_keys), post_dedupe_unique, removed=summary["duplicate_rows_removed"], keys=usable_dedupe_keys)
    if source.expected_rows:
        tolerance = Decimal(str((source.recognition or {}).get("expected_row_tolerance_pct", "0.20")))
        minimum = int(Decimal(source.expected_rows) * (Decimal(1) - tolerance))
        maximum = int(Decimal(source.expected_rows) * (Decimal(1) + tolerance))
        check("expected_volume", True, minimum <= frame.height <= maximum, actual=frame.height, minimum=minimum, maximum=maximum)
    else:
        check("expected_volume", False)
    for validation in source.validations or []:
        kind, field = validation.get("type"), validation.get("field")
        if kind == "required_field":
            passed = field in frame.columns and frame[field].null_count() == 0
            check(f"required_field:{field}", True, passed)
        elif kind == "non_negative" and field in frame.columns:
            passed = bool((frame[field] >= 0).all())
            check(f"non_negative:{field}", True, passed)
        elif kind in {"row_count", "order_count"}:
            actual = frame.height if kind == "row_count" else summary["order_count"]
            passed = int(validation.get("min", 0)) <= actual <= int(validation.get("max", 2**63 - 1))
            check(kind, True, passed, actual=actual, minimum=validation.get("min"), maximum=validation.get("max"))
        elif kind == "control_total" and field in summary:
            expected = Decimal(str(validation.get("expected", "0")))
            tolerance = Decimal(str(validation.get("tolerance", "0.01")))
            actual = Decimal(str(summary[field]))
            check(f"control_total:{field}", True, abs(actual - expected) <= tolerance, actual=str(actual), expected=str(expected), tolerance=str(tolerance))
    if not any(item["key"].startswith("control_total:") for item in checks):
        check("unexplained_difference", False)
    if not any(validation.get("type") == "cross_source_match" for validation in source.validations or []):
        check("cross_source_reconciliation", False)
    applicable = [item for item in checks if item["applicable"]]
    passed_count = sum(item["status"] == "passed" for item in applicable)
    return {"passed": passed_count == len(applicable), "checks": checks, "applicable_count": len(applicable), "passed_count": passed_count, "pass_rate": passed_count / len(applicable) if applicable else None, "summary": summary}


def create_ingestion(
    db: Session,
    storage: ObjectStorage,
    ctx: RequestContext,
    source_id: str,
    filename: str,
    data: bytes,
    store_id: str | None,
    is_backfill: bool,
) -> IngestionRun:
    if not ctx.enterprise_id:
        raise HTTPException(status_code=400, detail="enterprise context required")
    source = db.scalar(select(SourceDefinition).where(SourceDefinition.id == source_id, SourceDefinition.enterprise_id == ctx.enterprise_id))
    if not source or source.archived_at:
        raise HTTPException(status_code=404, detail="source definition not found")
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(
        select(IngestionRun).where(
            IngestionRun.enterprise_id == ctx.enterprise_id,
            IngestionRun.source_sha256 == digest,
        )
    )
    if existing:
        return existing
    raw_key = f"{ctx.enterprise_id}/raw/{digest}/{Path(filename).name}"
    storage.put(raw_key, data)
    frame, duplicates, numeric_errors = _normalise(parse_tabular(filename, data), source, store_id)
    store_ids = _validate_stores(db, source, frame, store_id, ctx)
    enterprise = db.get(Enterprise, ctx.enterprise_id)
    stores = {item.id: item for item in db.scalars(select(Store).where(Store.id.in_(store_ids))).all()}
    enterprise_activation = _aware(enterprise.activation_at)
    source_activation = _aware(source.activation_at)
    before_filter = frame.height
    if not is_backfill:
        frame = frame.filter(
            pl.struct(["store_id", "occurred_at"]).map_elements(
                lambda row: _aware(row["occurred_at"]) >= max(enterprise_activation, source_activation, _aware(stores[row["store_id"]].activation_at)),
                return_dtype=pl.Boolean,
            )
        )
        if frame.is_empty():
            raise HTTPException(status_code=422, detail="file contains no data on or after activation_at")
    coverage_start = frame["occurred_at"].min()
    coverage_end = frame["occurred_at"].max()
    summary = _duckdb_summary(frame, duplicates)
    summary["store_ids"] = sorted(store_ids)
    quality = _quality(source, frame, summary, numeric_errors, before_filter - frame.height, store_ids)
    normalized_buffer = io.BytesIO()
    frame.write_parquet(normalized_buffer, compression="zstd")
    normalized_key = f"{ctx.enterprise_id}/normalized/{digest}.parquet"
    storage.put(normalized_key, normalized_buffer.getvalue(), "application/vnd.apache.parquet")
    now = utcnow()
    semantic_model = db.scalar(
        select(SemanticModelVersion)
        .where(
            SemanticModelVersion.enterprise_id == ctx.enterprise_id,
            SemanticModelVersion.status == "published",
            SemanticModelVersion.industry_template == "ecommerce_standard",
            or_(SemanticModelVersion.effective_from.is_(None), SemanticModelVersion.effective_from <= now),
            or_(SemanticModelVersion.effective_to.is_(None), SemanticModelVersion.effective_to > now),
        )
        .order_by(SemanticModelVersion.version.desc())
        .limit(1)
    )
    model_check = {"key": "semantic_model", "applicable": True, "status": "passed" if semantic_model else "failed", "model_id": semantic_model.id if semantic_model else None}
    quality["checks"].append(model_check)
    quality["applicable_count"] += 1
    quality["passed_count"] += int(bool(semantic_model))
    quality["pass_rate"] = quality["passed_count"] / quality["applicable_count"]
    quality["passed"] = quality["passed_count"] == quality["applicable_count"]
    run = IngestionRun(
        enterprise_id=ctx.enterprise_id,
        source_definition_id=source.id,
        store_id=store_id,
        source_sha256=digest,
        original_filename=Path(filename).name,
        raw_object_key=raw_key,
        normalized_object_key=normalized_key,
        status="awaiting_confirmation" if quality["passed"] else "quality_failed",
        effective_from=coverage_start,
        effective_to=coverage_end,
        is_backfill=is_backfill,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        source_version=source.version,
        source_config_id=source.id,
        rule_version=source.version,
        rule_config_id=source.id,
        model_version=semantic_model.version if semantic_model else None,
        semantic_model_id=semantic_model.id if semantic_model else None,
        quality_result=quality,
        summary=summary,
        created_by=ctx.user_id,
    )
    db.add(run)
    db.flush()
    record_audit(db, ctx, "ingest", "ingestions", run.id, {"sha256": digest, "backfill": is_backfill})
    db.commit()
    db.refresh(run)
    return run


def confirm_ingestion(db: Session, ctx: RequestContext, run: IngestionRun, accepted: bool, note: str | None) -> IngestionRun:
    if run.status not in {"awaiting_confirmation", "quality_failed"}:
        raise HTTPException(status_code=409, detail="run cannot be confirmed in its current state")
    if not accepted:
        run.status = "rejected"
    elif not run.quality_result.get("passed"):
        raise HTTPException(status_code=409, detail="quality gates have not passed")
    else:
        run.status = "quality_passed"
        run.confirmed_by = ctx.user_id
    record_audit(db, ctx, "confirm" if accepted else "reject", "ingestions", run.id, {"note": note})
    db.commit()
    db.refresh(run)
    return run


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value if value is not None else 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=422, detail="amount cannot be represented as Numeric(20,4)") from exc
    if abs(parsed) >= Decimal("10000000000000000"):
        raise HTTPException(status_code=422, detail="amount exceeds Numeric(20,4)")
    return parsed


def _bucket(value: datetime, grain: str) -> datetime:
    value = _aware(value)
    if grain in {"event", "hour"}:
        return value.replace(minute=0, second=0, microsecond=0)
    if grain == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def publish_ingestion(db: Session, storage: ObjectStorage, ctx: RequestContext, run: IngestionRun) -> IngestionRun:
    if run.status == "published" or run.status == "locked":
        return run
    if run.status != "quality_passed":
        raise HTTPException(status_code=409, detail="quality confirmation is required before publish")
    semantic_model = db.scalar(select(SemanticModelVersion).where(SemanticModelVersion.id == run.semantic_model_id, SemanticModelVersion.enterprise_id == run.enterprise_id, SemanticModelVersion.version == run.model_version, SemanticModelVersion.status == "published", SemanticModelVersion.industry_template == "ecommerce_standard"))
    if not semantic_model:
        raise HTTPException(status_code=409, detail="the bound e-commerce semantic model is not available")
    required_metrics = {"sales", "refund", "platform_fee", "advertising_fee", "shipping_fee", "product_cost", "profit"}
    metric_keys = set(db.scalars(select(MetricDefinition.key).where(MetricDefinition.enterprise_id == run.enterprise_id, MetricDefinition.semantic_model_id == semantic_model.id, MetricDefinition.status == "published")).all())
    missing_metrics = sorted(required_metrics - metric_keys)
    if missing_metrics:
        raise HTTPException(status_code=409, detail=f"semantic model is missing required metrics: {', '.join(missing_metrics)}")
    if run.is_backfill:
        candidates = db.scalars(
            select(IngestionRun).where(
                IngestionRun.enterprise_id == run.enterprise_id,
                IngestionRun.source_definition_id == run.source_definition_id,
                IngestionRun.locked_at.is_not(None),
                IngestionRun.coverage_start <= run.coverage_end,
                IngestionRun.coverage_end >= run.coverage_start,
            )
        ).all()
        run_stores = set(run.summary.get("store_ids", [])) | ({run.store_id} if run.store_id else set())
        overlap = any(run_stores.intersection(set(item.summary.get("store_ids", [])) | ({item.store_id} if item.store_id else set())) for item in candidates)
        if overlap:
            raise HTTPException(status_code=409, detail="backfill overlaps a locked accounting period")
    if not run.normalized_object_key:
        raise HTTPException(status_code=500, detail="normalized artifact is missing")
    frame = pl.read_parquet(io.BytesIO(storage.get(run.normalized_object_key)))
    source = db.get(SourceDefinition, run.source_definition_id)
    aggregates: dict[tuple[str | None, datetime], dict[str, Any]] = defaultdict(
        lambda: {"rows": 0, "orders": set(), "revenue": Decimal(0), "refund": Decimal(0), "platform_fee": Decimal(0), "advertising_fee": Decimal(0), "shipping_fee": Decimal(0), "cost": Decimal(0)}
    )
    for row in frame.iter_rows(named=True):
        occurred = _aware(row["occurred_at"])
        current_store = row.get("store_id") or run.store_id
        record = NormalizedRecord(
            enterprise_id=run.enterprise_id,
            ingestion_run_id=run.id,
            store_id=current_store,
            occurred_at=occurred,
            order_id=row.get("order_id"),
            event_type=str(row.get("event_type") or "sale"),
            revenue=_decimal(row.get("revenue")),
            refund=_decimal(row.get("refund")),
            platform_fee=_decimal(row.get("platform_fee")),
            advertising_fee=_decimal(row.get("advertising_fee")),
            shipping_fee=_decimal(row.get("shipping_fee")),
            product_cost=_decimal(row.get("product_cost")),
            raw_payload={key: str(value) if isinstance(value, (datetime, Decimal)) else value for key, value in row.items()},
        )
        db.add(record)
        key = (current_store, _bucket(occurred, source.data_granularity if source else "day"))
        agg = aggregates[key]
        agg["rows"] += 1
        if row.get("order_id"):
            agg["orders"].add(str(row["order_id"]))
        agg["revenue"] += record.revenue
        agg["refund"] += record.refund
        agg["platform_fee"] += record.platform_fee
        agg["advertising_fee"] += record.advertising_fee
        agg["shipping_fee"] += record.shipping_fee
        agg["cost"] += record.product_cost
    for (current_store, period), agg in aggregates.items():
        db.add(
            CertifiedAggregate(
                enterprise_id=run.enterprise_id,
                ingestion_run_id=run.id,
                store_id=current_store,
                period_start=period,
                grain=source.data_granularity if source else "day",
                row_count=agg["rows"],
                order_count=len(agg["orders"]),
                revenue=agg["revenue"],
                refund=agg["refund"],
                platform_fee=agg["platform_fee"],
                advertising_fee=agg["advertising_fee"],
                shipping_fee=agg["shipping_fee"],
                product_cost=agg["cost"],
                fees=agg["platform_fee"] + agg["advertising_fee"] + agg["shipping_fee"],
                profit=agg["revenue"] - agg["refund"] - agg["platform_fee"] - agg["advertising_fee"] - agg["shipping_fee"] - agg["cost"],
            )
        )
    run.status = "published"
    run.published_at = utcnow()
    run.approved_by = ctx.user_id
    record_audit(db, ctx, "publish", "ingestions", run.id, {"certified_periods": len(aggregates)})
    db.commit()
    db.refresh(run)
    return run


def lock_ingestion(db: Session, ctx: RequestContext, run: IngestionRun) -> IngestionRun:
    if run.status != "published":
        raise HTTPException(status_code=409, detail="only a published run can be locked")
    run.status = "locked"
    run.locked_at = utcnow()
    record_audit(db, ctx, "lock", "ingestions", run.id)
    db.commit()
    db.refresh(run)
    return run
