from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from commerce_harness.evidence_policy import (
    LEARNING_POLICY_VERSION,
    NORMALIZATION_RULE_VERSION,
    PERFORMANCE_ENGINE_VERSION,
)
from commerce_harness.snapshot.artifacts import NormalizedArtifactManifest
from commerce_harness.snapshot.store import SnapshotManifest

from .schema import REQUIRED_TABLES, SCHEMA_STATEMENTS, SCHEMA_VERSION

_log = logging.getLogger(__name__)

_INITIALIZED_DATABASES: set[str] = set()
_INITIALIZATION_LOCK = threading.Lock()
_CONNECTION_OPEN_LOCK = threading.Lock()
_CONNECTION_RETRY_COUNT = 20
_CONNECTION_RETRY_SECONDS = 0.025


class DuckDBMemory:
    """Transactional metadata catalog for the three-layer workbench."""

    def __init__(self, database: str | os.PathLike[str] = ":memory:") -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._run_log_supports_skipped: bool | None = None
        self.connection = self._connect()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        for attempt in range(_CONNECTION_RETRY_COUNT):
            try:
                with _CONNECTION_OPEN_LOCK:
                    connection = duckdb.connect(self.database)
                # Audit timestamps must be timezone-stable across hosts.
                connection.execute("SET TimeZone='UTC'")
                return connection
            except duckdb.BinderException as exc:
                if (
                    "Unique file handle conflict" not in str(exc)
                    or attempt == _CONNECTION_RETRY_COUNT - 1
                ):
                    raise
                time.sleep(_CONNECTION_RETRY_SECONDS * (attempt + 1))
        raise AssertionError("connection retry loop must return or raise")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> DuckDBMemory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        self.connection.execute("BEGIN TRANSACTION")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def initialize(self) -> None:
        if self.database == ":memory:":
            self._initialize_database()
            return
        cache_key = str(Path(self.database).resolve())
        with _INITIALIZATION_LOCK:
            if cache_key in _INITIALIZED_DATABASES:
                return
            self._initialize_database()
            _INITIALIZED_DATABASES.add(cache_key)

    def _initialize_database(self) -> None:
        self._migrate_checklist_result_v8()
        self._migrate_run_log_skipped_v16()
        with self.transaction() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                DELETE FROM performance_result_head
                WHERE result_id IN (
                    SELECT result_id
                    FROM performance_result
                    WHERE coalesce(evidence_policy_version, '') <> ?
                       OR coalesce(engine_version, '') <> ?
                )
                """,
                [NORMALIZATION_RULE_VERSION, PERFORMANCE_ENGINE_VERSION],
            )
            connection.execute(
                """
                UPDATE performance_result
                SET status = 'superseded'
                WHERE status <> 'superseded'
                  AND (
                      coalesce(evidence_policy_version, '') <> ?
                      OR coalesce(engine_version, '') <> ?
                  )
                """,
                [NORMALIZATION_RULE_VERSION, PERFORMANCE_ENGINE_VERSION],
            )
            connection.execute(
                """
                UPDATE residual_suggestion
                SET guard_status = 'obsolete_evidence_policy',
                    critic_status = 'not_current_policy'
                WHERE coalesce(evidence_policy_version, '') <> ?
                """,
                [NORMALIZATION_RULE_VERSION],
            )
            connection.execute(
                """
                UPDATE autonomy_evaluation
                SET eligible = false
                WHERE coalesce(policy_version, '') <> ?
                """,
                [LEARNING_POLICY_VERSION],
            )
            connection.execute(
                """
                INSERT INTO input_revision_state(
                    revision_id, status, reason, approved_by
                )
                SELECT revision_id, status, reason, approved_by
                FROM input_revision revision
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM input_revision_state state
                    WHERE state.revision_id = revision.revision_id
                )
                """
            )
            connection.execute(
                """
                INSERT INTO harness_schema_version(version)
                VALUES (?)
                ON CONFLICT (version) DO NOTHING
                """,
                [SCHEMA_VERSION],
            )
        self.seed_builtin_invariants()
        missing = REQUIRED_TABLES - self.table_names()
        if missing:
            raise RuntimeError(f"DuckDB schema is incomplete: {sorted(missing)}")
        dangling_revision_count = int(
            self.fetchone_required(
                """
                SELECT count(*)
                FROM checklist_result result
                LEFT JOIN input_revision revision
                  ON revision.revision_id = result.revision_id
                WHERE result.revision_id IS NOT NULL
                  AND revision.revision_id IS NULL
                """
            )[0]
        )
        if dangling_revision_count:
            raise RuntimeError(
                "checklist_result contains dangling input revision references"
            )
        dangling_supersedes_count = int(
            self.fetchone_required(
                """
                SELECT count(*)
                FROM input_revision child
                LEFT JOIN input_revision parent
                  ON parent.revision_id = child.supersedes_revision_id
                WHERE child.supersedes_revision_id IS NOT NULL
                  AND parent.revision_id IS NULL
                """
            )[0]
        )
        if dangling_supersedes_count:
            raise RuntimeError(
                "input_revision contains dangling supersedes references"
            )
        dangling_state_count = int(
            self.fetchone_required(
                """
                SELECT count(*)
                FROM input_revision_state state
                LEFT JOIN input_revision revision
                  ON revision.revision_id = state.revision_id
                WHERE revision.revision_id IS NULL
                """
            )[0]
        )
        if dangling_state_count:
            raise RuntimeError(
                "input_revision_state contains dangling revision references"
            )

    def _migrate_checklist_result_v8(self) -> None:
        tables = self.table_names()
        if "checklist_result" not in tables:
            return
        version_row = self.connection.execute(
            "SELECT max(version) FROM harness_schema_version"
        ).fetchone()
        current_version = int(version_row[0] or 0) if version_row else 0
        # v4 originally renamed checklist_result_v4 to checklist_result.
        # DuckDB retained the temporary name in the inbound FK metadata of
        # input_revision. DuckDB also blocks any status-only UPDATE of a row
        # referenced by a physical FK. v8 rebuilds the final table without that
        # physical FK and initialize() validates the soft reference instead.
        if current_version >= 8:
            return
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TEMP TABLE checklist_result_migration AS
                SELECT * FROM checklist_result
                """
            )
            connection.execute("DROP TABLE checklist_result")
            connection.execute(
                """
                CREATE TABLE checklist_result (
                    result_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
                    period_id VARCHAR NOT NULL REFERENCES accounting_period(period_id),
                    requirement_id VARCHAR NOT NULL
                        REFERENCES checklist_requirement(requirement_id),
                    status VARCHAR NOT NULL
                        CHECK (
                            status IN (
                                'pending', 'present', 'missing',
                                'failed', 'not_applicable'
                            )
                        ),
                    revision_id VARCHAR,
                    observed_json JSON,
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
                    UNIQUE (run_id, period_id, requirement_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO checklist_result
                SELECT * FROM checklist_result_migration
                """
            )
            connection.execute("DROP TABLE checklist_result_migration")

    def _migrate_run_log_skipped_v16(self) -> None:
        """Drop the stale ``__probe_skipped`` audit rows left by earlier builds.

        DuckDB cannot ALTER a CHECK constraint, and rebuilding ``run_log`` is
        impossible while child tables hold foreign keys into it. New databases
        get the v16 DDL that already accepts ``skipped``; older databases keep
        the legacy CHECK and record empty reconcile runs as ``cancelled`` with
        ``error_code='skipped_empty'`` instead.
        """

        if "run_log" not in self.table_names():
            return
        self.connection.execute(
            "DELETE FROM run_log WHERE run_id = '__probe_skipped'"
        )

    def run_log_supports_skipped(self) -> bool:
        """Whether ``run_log`` accepts ``status='skipped'``.

        Read from catalog metadata rather than probing with a write: probing
        inside an open transaction aborts it on legacy schemas, and a crash
        between probe and cleanup would leave a fabricated run in the audit
        ledger.
        """

        cached = self._run_log_supports_skipped
        if cached is not None:
            return cached
        supported = False
        try:
            rows = self.connection.execute(
                """
                SELECT constraint_text
                FROM duckdb_constraints()
                WHERE table_name = 'run_log' AND constraint_type = 'CHECK'
                """
            ).fetchall()
        except duckdb.Error:
            rows = []
        for (constraint_text,) in rows:
            text = str(constraint_text)
            if "status" in text and "'skipped'" in text:
                supported = True
                break
        self._run_log_supports_skipped = supported
        return supported

    def seed_builtin_invariants(self) -> int:
        invariants_path = (
            Path(__file__).resolve().parents[2]
            / "packs"
            / "builtin"
            / "ecommerce_settlement"
            / "invariants.json"
        )
        if not invariants_path.is_file():
            _log.debug("builtin invariants not found at %s", invariants_path)
            return 0
        from commerce_harness.spec.invariant import load_invariants_from_json_path

        definitions = load_invariants_from_json_path(invariants_path)
        seeded = 0
        for inv in definitions:
            canon_dict = inv.canonical_dict()
            canon_dict["title"] = inv.title
            canon_dict["domain"] = inv.domain
            canon_dict["origin"] = inv.origin
            definition_json = json.dumps(
                canon_dict, ensure_ascii=False, sort_keys=True
            )
            self.connection.execute(
                """
                INSERT INTO invariant_definition (
                    invariant_id, domain, family, title, definition_json, origin
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (invariant_id) DO NOTHING
                """,
                [
                    inv.invariant_id,
                    inv.domain,
                    inv.family,
                    inv.title,
                    definition_json,
                    inv.origin,
                ],
            )
            version_id = f"{inv.invariant_id}:1.0.0"
            self.connection.execute(
                """
                INSERT INTO invariant_version (
                    invariant_version_id, invariant_id, semver, status
                )
                VALUES (?, ?, '1.0.0', 'active')
                ON CONFLICT (invariant_version_id) DO NOTHING
                """,
                [version_id, inv.invariant_id],
            )
            seeded += 1
        return seeded

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            """
        ).fetchall()
        return {str(row[0]) for row in rows}

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | None = None,
    ) -> duckdb.DuckDBPyConnection:
        return self.connection.execute(sql, parameters or [])

    def fetchone_required(
        self,
        sql: str,
        parameters: Sequence[Any] | None = None,
    ) -> tuple[Any, ...]:
        row = self.execute(sql, parameters).fetchone()
        if row is None:
            raise LookupError("query unexpectedly returned no row")
        return row

    def register_snapshot(self, manifest: SnapshotManifest) -> None:
        self.connection.execute(
            """
            INSERT INTO source_snapshot (
                snapshot_id, content_sha256, byte_size, object_uri, source_uri,
                source_modified_ns, source_etag, original_name, media_type,
                captured_at, manifest_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                manifest.snapshot_id,
                manifest.content_sha256,
                manifest.byte_size,
                manifest.object_path,
                manifest.source.uri,
                manifest.source.modified_ns,
                manifest.source.etag,
                manifest.original_name,
                manifest.media_type,
                manifest.captured_at,
                json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True),
            ],
        )

    def register_artifact(
        self,
        manifest: NormalizedArtifactManifest,
        *,
        source_snapshot_id: str,
        normalization_run_id: str | None = None,
        input_revision_id: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO normalized_artifact (
                artifact_id, normalization_run_id, input_revision_id, content_sha256,
                source_snapshot_id, dataset_kind, schema_version, rule_version,
                row_count, byte_size, parquet_uri, partition_json, arrow_schema,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                manifest.artifact_id,
                normalization_run_id,
                input_revision_id,
                manifest.content_sha256,
                source_snapshot_id,
                manifest.spec.dataset_kind,
                manifest.spec.schema_version,
                manifest.spec.rule_version,
                manifest.row_count,
                manifest.byte_size,
                manifest.parquet_path,
                json.dumps(manifest.spec.partition, ensure_ascii=False, sort_keys=True)
                if manifest.spec.partition is not None
                else None,
                manifest.arrow_schema,
                manifest.created_at,
            ],
        )
