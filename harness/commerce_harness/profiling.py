from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bootstrap import CONTRACT_ID
from .code_identity import resolve_code_identity
from .freeze import _writer_lock
from .memory.database import DuckDBMemory
from .parse.models import ArchiveRoute, FileRoute
from .parse.router import (
    AmbiguousTemplateError,
    NoTemplateMatchError,
    TemplateRouter,
)
from .workbench import WorkbenchPaths

PARSER_VERSION = "finite_templates_v4"
PROFILE_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProfileResult:
    run_id: str
    parser_version: str
    total: int
    matched: int
    unmatched: int
    ambiguous: int
    unsupported: int
    failed: int


def _route_payload(
    route: FileRoute | ArchiveRoute,
) -> tuple[str, str, str, dict[str, Any]]:
    if isinstance(route, FileRoute):
        return (
            route.source_kind.value,
            route.template_id,
            route.fingerprint.digest,
            {
                "kind": "file",
                "source_kind": route.source_kind.value,
                "template_id": route.template_id,
                "fields": route.fields,
                "location": {
                    "sheet": route.location.sheet_name,
                    "member": route.location.member_name,
                    "header_row": route.location.header_row,
                },
                "fingerprint": route.fingerprint.digest,
            },
        )
    kinds = sorted({item.source_kind.value for item in route.entries})
    templates = sorted({item.template_id for item in route.entries})
    return (
        ",".join(kinds),
        ",".join(templates),
        route.fingerprint.digest,
        {
            "kind": "archive",
            "entries": [
                {
                    "source_kind": item.source_kind.value,
                    "template_id": item.template_id,
                    "fields": item.fields,
                    "member": item.location.member_name,
                    "header_row": item.location.header_row,
                    "fingerprint": item.fingerprint.digest,
                }
                for item in route.entries
            ],
            "unmatched_members": list(route.unmatched_members),
            "fingerprint": route.fingerprint.digest,
        },
    )


def profile_snapshots(workbench: WorkbenchPaths) -> ProfileResult:
    run_id = f"parse_{uuid.uuid4().hex}"
    code_sha = resolve_code_identity().value
    counts: Counter[str] = Counter()
    router = TemplateRouter()
    with _writer_lock(workbench.locks / "duckdb-writer.lock"):  # noqa: SIM117
        with DuckDBMemory(workbench.database) as database:
            database.initialize()
            snapshots = database.execute(
                """
                SELECT snapshot_id, byte_size, object_uri, original_name,
                       content_sha256
                FROM source_snapshot
                ORDER BY source_uri, captured_at
                """
            ).fetchall()
            manifest_sha = hashlib.sha256(
                "\n".join(str(row[4]) for row in snapshots).encode("ascii")
            ).hexdigest()
            contract_id = (
                CONTRACT_ID
                if database.execute(
                    """
                    SELECT 1 FROM reconciliation_contract WHERE contract_id = ?
                    """,
                    [CONTRACT_ID],
                ).fetchone()
                else None
            )
            database.execute(
                """
                INSERT INTO run_log (
                    run_id, contract_id, run_kind, status,
                    input_manifest_sha256, code_sha
                )
                VALUES (?, ?, 'parse', 'running', ?, ?)
                """,
                [run_id, contract_id, manifest_sha, code_sha],
            )
            try:
                for snapshot_id, byte_size, object_uri, original_name, _sha in snapshots:
                    profile_id = hashlib.sha256(
                        f"{snapshot_id}|{PARSER_VERSION}".encode()
                    ).hexdigest()
                    if database.execute(
                        "SELECT 1 FROM source_profile WHERE profile_id = ?",
                        [profile_id],
                    ).fetchone():
                        existing_status = database.fetchone_required(
                            "SELECT status FROM source_profile WHERE profile_id = ?",
                            [profile_id],
                        )[0]
                        counts[str(existing_status)] += 1
                        continue
                    suffix = Path(str(original_name or "")).suffix.casefold()
                    status = "failed"
                    source_kind = None
                    template_id = None
                    fingerprint = None
                    route_json = None
                    error_detail = None
                    if suffix not in {".csv", ".xlsx", ".zip"}:
                        status = "unsupported"
                        error_detail = f"阶段 A 不处理 {suffix or '未知'} 文件"
                    elif int(byte_size) > PROFILE_MAX_BYTES:
                        status = "unsupported"
                        error_detail = "文件超过 64MiB 结构画像上限"
                    else:
                        try:
                            content = Path(str(object_uri)).read_bytes()
                            route = router.route_bytes(str(original_name), content)
                            (
                                source_kind,
                                template_id,
                                fingerprint,
                                route_payload,
                            ) = _route_payload(route)
                            route_json = json.dumps(
                                route_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            status = "matched"
                        except NoTemplateMatchError as exc:
                            status = "unmatched"
                            error_detail = str(exc)
                        except AmbiguousTemplateError as exc:
                            status = "ambiguous"
                            error_detail = str(exc)
                        except Exception as exc:
                            status = "failed"
                            error_detail = str(exc)
                    database.execute(
                        """
                        INSERT INTO source_profile (
                            profile_id, run_id, snapshot_id, parser_version,
                            status, source_kind, template_id,
                            fingerprint_sha256, route_json, error_detail
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            profile_id,
                            run_id,
                            snapshot_id,
                            PARSER_VERSION,
                            status,
                            source_kind,
                            template_id,
                            fingerprint,
                            route_json,
                            error_detail,
                        ],
                    )
                    counts[status] += 1
            except Exception as exc:
                database.execute(
                    """
                    UPDATE run_log
                    SET status = 'failed', finished_at = current_timestamp,
                        error_code = 'profile_failed', error_detail = ?
                    WHERE run_id = ?
                    """,
                    [str(exc), run_id],
                )
                raise
            else:
                database.execute(
                    """
                    UPDATE run_log
                    SET status = 'succeeded', finished_at = current_timestamp,
                        metrics_json = ?
                    WHERE run_id = ?
                    """,
                    [
                        json.dumps(dict(counts), sort_keys=True),
                        run_id,
                    ],
                )
    return ProfileResult(
        run_id=run_id,
        parser_version=PARSER_VERSION,
        total=sum(counts.values()),
        matched=counts["matched"],
        unmatched=counts["unmatched"],
        ambiguous=counts["ambiguous"],
        unsupported=counts["unsupported"],
        failed=counts["failed"],
    )
