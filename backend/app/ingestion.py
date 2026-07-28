from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

try:
    import duckdb
    import polars as pl
except ModuleNotFoundError:  # The default control-plane image keeps heavy engines outside Docker.
    duckdb = None  # type: ignore[assignment]
    pl = None  # type: ignore[assignment]
from fastapi import HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit import record_audit
from .models import (
    CertifiedAggregate,
    CrossSourceReconciliation,
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
from .standard_model import calculate_amounts, is_order_event, quantize, validate_published_model


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
        if (source.amount_directions or {}).get(column) == "negative":
            frame = frame.with_columns((-pl.col(column)).alias(column))
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
        frame = frame.with_columns(
            pl.struct(dedupe_keys).map_elements(
                lambda values: hashlib.sha256(
                    json.dumps({key: str(values.get(key)) for key in dedupe_keys}, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest(),
                return_dtype=pl.String,
            ).alias("__business_key")
        )
    else:
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("__business_key"))
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


def _duckdb_summary(frame: pl.DataFrame, duplicate_rows: int, source_kind: str) -> dict[str, Any]:
    connection = duckdb.connect(":memory:")
    try:
        connection.register("input_rows", frame.to_arrow())
        row = connection.execute(
            """SELECT count(*) AS rows,
            count(DISTINCT CASE WHEN lower(coalesce(event_type, '')) IN ('sale', 'order') AND ? IN ('orders', 'mixed') THEN order_id END) AS orders,
            coalesce(sum(revenue), 0) AS revenue, coalesce(sum(refund), 0) AS refund,
            coalesce(sum(platform_fee), 0), coalesce(sum(advertising_fee), 0),
            coalesce(sum(shipping_fee), 0), coalesce(sum(product_cost), 0),
            coalesce(sum(revenue-refund-platform_fee-advertising_fee-shipping_fee-product_cost), 0)
            FROM input_rows"""
        , [source_kind]).fetchone()
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


def _month_start(value: datetime) -> datetime:
    return _aware(value).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(value: datetime) -> datetime:
    return (_month_start(value).replace(day=28) + timedelta(days=4)).replace(day=1)


def _quality_run_status(quality: dict[str, Any]) -> str:
    statuses = {item.get("status") for item in quality.get("checks", []) if item.get("applicable")}
    if "failed" in statuses:
        return "quality_failed"
    if "pending" in statuses:
        return "quality_pending"
    return "awaiting_confirmation"


def _scope_summaries(db: Session, frame: pl.DataFrame, store_ids: set[str], source_kind: str) -> list[dict[str, Any]]:
    logical_by_store = {
        store.id: store.logical_id
        for store in db.scalars(select(Store).where(Store.id.in_(store_ids))).all()
    }
    grouped: dict[tuple[str, datetime], list[dict[str, Any]]] = defaultdict(list)
    for row in frame.iter_rows(named=True):
        grouped[(logical_by_store[str(row["store_id"])], _month_start(row["occurred_at"]))].append(row)
    result: list[dict[str, Any]] = []
    for (logical_id, period), rows in grouped.items():
        amounts = {key: sum((quantize(row.get(key)) for row in rows), start=Decimal(0)) for key in CANONICAL_NUMERIC}
        calculated = calculate_amounts(amounts)
        orders = {str(row.get("order_id")) for row in rows if row.get("order_id") and is_order_event(str(row.get("event_type") or ""), source_kind)}
        result.append({
            "store_logical_id": logical_id,
            "period_start": period.isoformat(),
            "row_count": len(rows),
            "order_count": len(orders),
            **{key: str(value) for key, value in calculated.items()},
        })
    return result


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
        elif kind == "cross_source_match":
            checks.append({
                "key": f"cross_source:{validation.get('mode', 'required_source')}:{validation.get('dependency_source_logical_id') or validation.get('dependency_source_id') or 'unconfigured'}",
                "applicable": True,
                "status": "pending",
                "message": f"等待本月{validation.get('label', '依赖文件')}",
            })
    if not any(item["key"].startswith("control_total:") for item in checks):
        check("unexplained_difference", False)
    if not any(validation.get("type") == "cross_source_match" for validation in source.validations or []):
        check("cross_source_reconciliation", False)
    applicable = [item for item in checks if item["applicable"]]
    passed_count = sum(item["status"] == "passed" for item in applicable)
    return {"passed": passed_count == len(applicable), "checks": checks, "applicable_count": len(applicable), "passed_count": passed_count, "pass_rate": passed_count / len(applicable) if applicable else None, "summary": summary}


def _scope_for_run(run: IngestionRun, logical_id: str, period: datetime) -> dict[str, Any] | None:
    period_key = _month_start(period).isoformat()
    return next(
        (
            item for item in (run.summary or {}).get("scopes", [])
            if item.get("store_logical_id") == logical_id and item.get("period_start") == period_key
        ),
        None,
    )


def _dependency_run(
    db: Session,
    run: IngestionRun,
    dependency_logical_id: str,
    store_logical_id: str,
    period: datetime,
) -> IngestionRun | None:
    source_ids = set(db.scalars(select(SourceDefinition.id).where(
        SourceDefinition.enterprise_id == run.enterprise_id,
        SourceDefinition.logical_id == dependency_logical_id,
    )).all())
    if not source_ids:
        return None
    candidates = db.scalars(
        select(IngestionRun).where(
            IngestionRun.enterprise_id == run.enterprise_id,
            IngestionRun.source_definition_id.in_(source_ids),
            IngestionRun.id != run.id,
            IngestionRun.status.not_in(["rejected", "superseded"]),
        ).order_by(IngestionRun.created_at.desc())
    ).all()
    return next((candidate for candidate in candidates if _scope_for_run(candidate, store_logical_id, period)), None)


def _business_keys_for_scope(storage: ObjectStorage, run: IngestionRun, store_ids: set[str], period: datetime, field: str) -> set[str]:
    if not run.normalized_object_key:
        return set()
    frame = pl.read_parquet(io.BytesIO(storage.get(run.normalized_object_key)))
    if field not in frame.columns:
        return set()
    next_month = (period.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1)
    frame = frame.filter(
        pl.col("store_id").cast(pl.String).is_in(sorted(store_ids))
        & (pl.col("occurred_at") >= period)
        & (pl.col("occurred_at") < next_month)
    )
    return {str(value) for value in frame[field].drop_nulls().to_list() if str(value)}


def refresh_cross_source_quality(db: Session, storage: ObjectStorage, run: IngestionRun) -> None:
    source = db.get(SourceDefinition, run.source_definition_id)
    validations = [item for item in (source.validations or []) if item.get("type") == "cross_source_match"] if source else []
    if not validations:
        return
    db.query(CrossSourceReconciliation).filter(CrossSourceReconciliation.ingestion_run_id == run.id).delete(synchronize_session=False)
    checks = [item for item in (run.quality_result or {}).get("checks", []) if not str(item.get("key", "")).startswith("cross_source:")]
    all_store_versions = list(db.scalars(select(Store).where(Store.enterprise_id == run.enterprise_id)).all())
    physical_by_logical: dict[str, set[str]] = defaultdict(set)
    for store in all_store_versions:
        physical_by_logical[store.logical_id].add(store.id)
    for index, validation in enumerate(validations):
        dependency_logical_id = validation.get("dependency_source_logical_id")
        if not dependency_logical_id and validation.get("dependency_source_id"):
            dependency = db.get(SourceDefinition, validation["dependency_source_id"])
            dependency_logical_id = dependency.logical_id if dependency else None
        mode = validation.get("mode", "required_source")
        details: list[dict[str, Any]] = []
        if validation.get("applicable") is False:
            checks.append({
                "key": f"cross_source:{index}:{mode}",
                "applicable": False,
                "status": "not_applicable",
                "message": validation.get("reason") or "该来源不参与本项核对",
                "details": details,
            })
            continue
        for scope in (run.summary or {}).get("scopes", []):
            logical_id = str(scope["store_logical_id"])
            period = datetime.fromisoformat(scope["period_start"])
            dependency_run = _dependency_run(db, run, str(dependency_logical_id or ""), logical_id, period) if dependency_logical_id else None
            status_value = "pending"
            actual_value = expected_value = difference_value = None
            detail: dict[str, Any] = {"dependency_run_id": dependency_run.id if dependency_run else None}
            if dependency_run:
                dependency_scope = _scope_for_run(dependency_run, logical_id, period) or {}
                if mode == "required_source":
                    status_value = "passed"
                elif mode == "control_total":
                    field = str(validation.get("field", "revenue"))
                    dependency_field = str(validation.get("dependency_field", field))
                    actual = quantize(scope.get(field))
                    expected = quantize(dependency_scope.get(dependency_field))
                    tolerance = quantize(validation.get("tolerance", "0.0100"))
                    difference = quantize(actual - expected)
                    status_value = "passed" if abs(difference) <= tolerance else "failed"
                    actual_value, expected_value, difference_value = str(actual), str(expected), str(difference)
                    detail.update(field=field, dependency_field=dependency_field, tolerance=str(tolerance))
                elif mode == "business_key":
                    field = str(validation.get("field", "order_id"))
                    dependency_field = str(validation.get("dependency_field", field))
                    current_keys = _business_keys_for_scope(storage, run, physical_by_logical[logical_id], period, field)
                    dependency_keys = _business_keys_for_scope(storage, dependency_run, physical_by_logical[logical_id], period, dependency_field)
                    missing = current_keys - dependency_keys
                    allowed = int(validation.get("allowed_unmatched", 0))
                    status_value = "passed" if len(missing) <= allowed else "failed"
                    actual_value, expected_value, difference_value = str(len(current_keys & dependency_keys)), str(len(current_keys)), str(len(missing))
                    detail.update(field=field, dependency_field=dependency_field, missing_count=len(missing), allowed_unmatched=allowed)
                else:
                    status_value = "failed"
                    detail["reason"] = "unsupported cross-source mode"
            reconciliation = CrossSourceReconciliation(
                enterprise_id=run.enterprise_id,
                ingestion_run_id=run.id,
                dependency_run_id=dependency_run.id if dependency_run else None,
                validation_key=f"cross_source:{index}:{mode}",
                status=status_value,
                store_logical_id=logical_id,
                period_start=period,
                rule_version=run.rule_version,
                actual_value=actual_value,
                expected_value=expected_value,
                difference=difference_value,
                details=detail,
            )
            db.add(reconciliation)
            details.append({"store_logical_id": logical_id, "period_start": scope["period_start"], "status": status_value, "actual": actual_value, "expected": expected_value, "difference": difference_value, **detail})
        statuses = {item["status"] for item in details}
        overall = "not_applicable" if not details else "failed" if "failed" in statuses else "pending" if "pending" in statuses else "passed"
        checks.append({
            "key": f"cross_source:{index}:{mode}",
            "applicable": bool(details),
            "status": overall,
            "message": f"等待本月{validation.get('label', '依赖文件')}" if overall == "pending" else None,
            "details": details,
        })
    quality = dict(run.quality_result or {})
    quality["checks"] = checks
    applicable = [item for item in checks if item.get("applicable")]
    quality["applicable_count"] = len(applicable)
    quality["passed_count"] = sum(item.get("status") == "passed" for item in applicable)
    quality["pass_rate"] = quality["passed_count"] / len(applicable) if applicable else None
    quality["passed"] = quality["passed_count"] == len(applicable)
    run.quality_result = quality
    statuses = {item.get("status") for item in applicable}
    if run.status in {"quality_pending", "quality_failed", "awaiting_confirmation"}:
        run.status = "quality_failed" if "failed" in statuses else "quality_pending" if "pending" in statuses else "awaiting_confirmation"
    if "failed" in statuses:
        existing_problem = db.scalar(select(Problem).where(
            Problem.enterprise_id == run.enterprise_id,
            Problem.ingestion_run_id == run.id,
            Problem.kind == "cross_source_reconciliation",
            Problem.status == "open",
        ))
        if not existing_problem:
            problem = Problem(
                enterprise_id=run.enterprise_id,
                ingestion_run_id=run.id,
                kind="cross_source_reconciliation",
                user_message="跨来源核对未通过，当前数据不能发布",
                technical_detail={"checks": [item for item in checks if str(item.get("key", "")).startswith("cross_source:")]},
                created_by=run.created_by,
            )
            db.add(problem)
            db.flush()
            db.add(ReviewQueueItem(enterprise_id=run.enterprise_id, problem_id=problem.id, priority=90))


def refresh_related_cross_source_quality(db: Session, storage: ObjectStorage, run: IngestionRun) -> None:
    candidates = db.scalars(select(IngestionRun).where(
        IngestionRun.enterprise_id == run.enterprise_id,
        IngestionRun.status.in_(["quality_pending", "quality_failed", "awaiting_confirmation"]),
    )).all()
    for candidate in candidates:
        refresh_cross_source_quality(db, storage, candidate)


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
    summary = _duckdb_summary(frame, duplicates, source.source_kind)
    summary["store_ids"] = sorted(store_ids)
    summary["scopes"] = _scope_summaries(db, frame, store_ids, source.source_kind)
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
        status=_quality_run_status(quality),
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
    refresh_cross_source_quality(db, storage, run)
    refresh_related_cross_source_quality(db, storage, run)
    record_audit(db, ctx, "ingest", "ingestions", run.id, {"sha256": digest, "backfill": is_backfill})
    db.commit()
    db.refresh(run)
    return run


def confirm_ingestion(db: Session, ctx: RequestContext, run: IngestionRun, accepted: bool, note: str | None) -> IngestionRun:
    if run.status != "awaiting_confirmation" and accepted:
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


def authorize_locked_correction(
    db: Session,
    ctx: RequestContext,
    run: IngestionRun,
    reason: str,
    locked_run_id: str | None = None,
) -> IngestionRun:
    if run.status not in {"awaiting_confirmation", "quality_passed"}:
        raise HTTPException(status_code=409, detail="correction can only be authorized after quality checks")
    source = db.get(SourceDefinition, run.source_definition_id)
    if not source:
        raise HTTPException(status_code=409, detail="source definition is unavailable")
    scope_keys = {
        (str(item["store_logical_id"]), str(item["period_start"])[:7])
        for item in (run.summary or {}).get("scopes", [])
    }
    source_ids = set(db.scalars(select(SourceDefinition.id).where(
        SourceDefinition.enterprise_id == run.enterprise_id,
        SourceDefinition.logical_id == source.logical_id,
    )).all())
    candidates = db.scalars(select(IngestionRun).where(
        IngestionRun.enterprise_id == run.enterprise_id,
        IngestionRun.source_definition_id.in_(source_ids),
        IngestionRun.locked_at.is_not(None),
    )).all()
    overlapping = [
        candidate for candidate in candidates
        if any(
            (str(item["store_logical_id"]), str(item["period_start"])[:7]) in scope_keys
            for item in (candidate.summary or {}).get("scopes", [])
        )
    ]
    if locked_run_id:
        overlapping = [candidate for candidate in overlapping if candidate.id == locked_run_id]
    if not overlapping:
        raise HTTPException(status_code=409, detail="no overlapping locked accounting period was found")
    if len(overlapping) != 1:
        raise HTTPException(status_code=409, detail="correction must identify exactly one locked run")
    run.correction_of_run_id = overlapping[0].id
    run.correction_reason = reason
    run.correction_approved_by = ctx.user_id
    record_audit(db, ctx, "authorize_locked_correction", "ingestions", run.id, {"locked_run_id": overlapping[0].id, "reason": reason})
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
    refresh_cross_source_quality(db, storage, run)
    if not (run.quality_result or {}).get("passed"):
        db.rollback()
        raise HTTPException(status_code=409, detail="all applicable quality and cross-source checks must pass before publish")
    semantic_model = db.scalar(select(SemanticModelVersion).where(SemanticModelVersion.id == run.semantic_model_id, SemanticModelVersion.enterprise_id == run.enterprise_id, SemanticModelVersion.version == run.model_version, SemanticModelVersion.status == "published", SemanticModelVersion.industry_template == "ecommerce_standard"))
    if not semantic_model:
        raise HTTPException(status_code=409, detail="the bound e-commerce semantic model is not available")
    model_checksum = validate_published_model(db, semantic_model)
    if not run.normalized_object_key:
        raise HTTPException(status_code=500, detail="normalized artifact is missing")
    frame = pl.read_parquet(io.BytesIO(storage.get(run.normalized_object_key)))
    source = db.get(SourceDefinition, run.source_definition_id)
    if not source:
        raise HTTPException(status_code=409, detail="source definition is unavailable")
    stores = {
        item.id: item for item in db.scalars(select(Store).where(Store.enterprise_id == run.enterprise_id)).all()
    }
    scope_months = {
        (str(item["store_logical_id"]), datetime.fromisoformat(item["period_start"]))
        for item in (run.summary or {}).get("scopes", [])
    }
    old_run_ids: set[str] = set()
    if source.import_mode == "monthly_snapshot":
        for store_logical_id, period in scope_months:
            next_period = _next_month(period)
            current_records = db.scalars(select(NormalizedRecord).where(
                NormalizedRecord.enterprise_id == run.enterprise_id,
                NormalizedRecord.source_logical_id == source.logical_id,
                NormalizedRecord.store_logical_id == store_logical_id,
                NormalizedRecord.occurred_at >= period,
                NormalizedRecord.occurred_at < next_period,
                NormalizedRecord.is_current.is_(True),
            )).all()
            current_aggregates = db.scalars(select(CertifiedAggregate).where(
                CertifiedAggregate.enterprise_id == run.enterprise_id,
                CertifiedAggregate.source_logical_id == source.logical_id,
                CertifiedAggregate.store_logical_id == store_logical_id,
                CertifiedAggregate.period_start >= period,
                CertifiedAggregate.period_start < next_period,
                CertifiedAggregate.is_current.is_(True),
            )).all()
            locked_ids = {
                aggregate.ingestion_run_id
                for aggregate in current_aggregates
                if (db.get(IngestionRun, aggregate.ingestion_run_id) and db.get(IngestionRun, aggregate.ingestion_run_id).locked_at)
            }
            if locked_ids and (
                locked_ids != {run.correction_of_run_id}
                or not run.correction_reason
                or not run.correction_approved_by
            ):
                raise HTTPException(status_code=409, detail={
                    "code": "locked_correction_required",
                    "message": "该月份已经锁定，需要企业管理员填写更正原因后才能替换",
                    "locked_run_ids": sorted(locked_ids),
                })
            superseded_at = utcnow()
            for record in current_records:
                record.is_current = False
                record.superseded_at = superseded_at
                record.superseded_by_run_id = run.id
                old_run_ids.add(record.ingestion_run_id)
            for aggregate in current_aggregates:
                aggregate.is_current = False
                aggregate.superseded_at = superseded_at
                aggregate.superseded_by_run_id = run.id
                old_run_ids.add(aggregate.ingestion_run_id)
    existing_incremental: set[tuple[str, str]] = set()
    if source.import_mode == "incremental":
        existing_incremental = set(db.execute(
            select(NormalizedRecord.store_logical_id, NormalizedRecord.business_key).where(
                NormalizedRecord.enterprise_id == run.enterprise_id,
                NormalizedRecord.source_logical_id == source.logical_id,
                NormalizedRecord.is_current.is_(True),
                NormalizedRecord.business_key.is_not(None),
            )
        ).all())
    aggregates: dict[tuple[str | None, str, datetime], dict[str, Any]] = defaultdict(
        lambda: {"rows": 0, "orders": set(), "revenue": Decimal(0), "refund": Decimal(0), "platform_fee": Decimal(0), "advertising_fee": Decimal(0), "shipping_fee": Decimal(0), "cost": Decimal(0)}
    )
    cross_run_duplicates = 0
    for row in frame.iter_rows(named=True):
        occurred = _aware(row["occurred_at"])
        current_store = row.get("store_id") or run.store_id
        store = stores.get(str(current_store))
        if not store:
            raise HTTPException(status_code=422, detail="normalized row references an unavailable store")
        business_key = str(row.get("__business_key")) if row.get("__business_key") else None
        if source.import_mode == "incremental" and business_key and (store.logical_id, business_key) in existing_incremental:
            cross_run_duplicates += 1
            continue
        stored_business_key = business_key
        if source.import_mode == "monthly_snapshot" and business_key:
            # Snapshot keys are unique only inside their accounting month. The
            # month prefix prevents a legitimate recurring external key from
            # colliding with another current month while preserving same-month
            # replacement safety in the database constraint.
            stored_business_key = hashlib.sha256(
                f"{occurred:%Y-%m}:{business_key}".encode("utf-8")
            ).hexdigest()
        record = NormalizedRecord(
            enterprise_id=run.enterprise_id,
            ingestion_run_id=run.id,
            store_id=current_store,
            store_logical_id=store.logical_id,
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
            source_logical_id=source.logical_id,
            business_key=stored_business_key,
        )
        db.add(record)
        if business_key:
            existing_incremental.add((store.logical_id, business_key))
        key = (current_store, store.logical_id, _bucket(occurred, source.data_granularity))
        agg = aggregates[key]
        agg["rows"] += 1
        if row.get("order_id") and is_order_event(record.event_type, source.source_kind):
            agg["orders"].add(str(row["order_id"]))
        agg["revenue"] += record.revenue
        agg["refund"] += record.refund
        agg["platform_fee"] += record.platform_fee
        agg["advertising_fee"] += record.advertising_fee
        agg["shipping_fee"] += record.shipping_fee
        agg["cost"] += record.product_cost
    for (current_store, store_logical_id, period), agg in aggregates.items():
        calculated = calculate_amounts({
            "revenue": agg["revenue"],
            "refund": agg["refund"],
            "platform_fee": agg["platform_fee"],
            "advertising_fee": agg["advertising_fee"],
            "shipping_fee": agg["shipping_fee"],
            "product_cost": agg["cost"],
        })
        db.add(
            CertifiedAggregate(
                enterprise_id=run.enterprise_id,
                ingestion_run_id=run.id,
                store_id=current_store,
                store_logical_id=store_logical_id,
                period_start=period,
                grain=source.data_granularity,
                row_count=agg["rows"],
                order_count=len(agg["orders"]),
                revenue=calculated["revenue"],
                refund=calculated["refund"],
                platform_fee=calculated["platform_fee"],
                advertising_fee=calculated["advertising_fee"],
                shipping_fee=calculated["shipping_fee"],
                product_cost=calculated["product_cost"],
                fees=calculated["fees"],
                profit=calculated["profit"],
                source_definition_id=source.id,
                source_logical_id=source.logical_id,
                source_version=run.source_version,
                model_version=run.model_version or semantic_model.version,
                model_checksum=model_checksum,
            )
        )
    for old_run_id in old_run_ids:
        old_run = db.get(IngestionRun, old_run_id)
        if old_run and old_run.id != run.id:
            old_run.status = "superseded"
    updated_summary = dict(run.summary or {})
    updated_summary["cross_run_duplicate_rows_removed"] = cross_run_duplicates
    updated_summary["published_row_count"] = sum(item["rows"] for item in aggregates.values())
    run.summary = updated_summary
    run.supersedes_run_ids = sorted(old_run_ids)
    run.status = "published"
    run.published_at = utcnow()
    run.approved_by = ctx.user_id
    record_audit(db, ctx, "publish", "ingestions", run.id, {"certified_periods": len(aggregates), "supersedes": sorted(old_run_ids), "cross_run_duplicates": cross_run_duplicates})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="a current business key already exists for this source and store") from exc
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
