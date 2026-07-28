from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .workbench import initialize, paths, require_initialized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fa-harness",
        description="电商财务确定性对账 Harness",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="初始化仓库外数据工作台")
    init.add_argument("--workspace", type=Path, required=True)

    status = subparsers.add_parser("status", help="显示工作台与外部模型状态")
    status.add_argument("--workspace", type=Path, required=True)

    schema = subparsers.add_parser("schema", help="初始化或检查 DuckDB schema")
    schema.add_argument("--workspace", type=Path, required=True)

    scan = subparsers.add_parser("scan", help="只读扫描 finance-win 来源元数据")
    scan.add_argument("--workspace", type=Path, required=True)

    freeze = subparsers.add_parser("freeze", help="冻结候选输入到不可变快照")
    freeze.add_argument("--workspace", type=Path, required=True)

    profile = subparsers.add_parser("profile", help="对冻结文件做有限模板识别")
    profile.add_argument("--workspace", type=Path, required=True)

    normalize = subparsers.add_parser(
        "normalize",
        help="按已识别模板生成带行级证据的不可变 Parquet",
    )
    normalize.add_argument("--workspace", type=Path, required=True)
    normalize.add_argument("--period", action="append", dest="periods")
    normalize.add_argument(
        "--store",
        help="多店铺环境中必须提供稳定店铺 ID，避免跨店混算",
    )

    adjudicate = subparsers.add_parser(
        "adjudicate",
        help="按可复核证据自动选择输入并登记保守业务口径",
    )
    adjudicate.add_argument("--workspace", type=Path, required=True)

    recon = subparsers.add_parser(
        "recon",
        help="运行确定性核对并持久化明细、关联、差额",
    )
    recon.add_argument("--workspace", type=Path, required=True)
    recon.add_argument("--period", required=True)
    recon.add_argument(
        "--store",
        help="多店铺环境中必须提供稳定店铺 ID，避免跨店混算",
    )

    diff = subparsers.add_parser(
        "diff",
        help="将当前重算与已冻结历史输出逐指标对表",
    )
    diff.add_argument("--workspace", type=Path, required=True)
    diff.add_argument("--period", required=True)
    diff.add_argument(
        "--store",
        help="多店铺环境中必须提供稳定店铺 ID，避免跨店混算",
    )

    baseline = subparsers.add_parser(
        "baseline",
        help="在裁决与守恒门禁通过后建立或冻结黄金基线",
    )
    baseline.add_argument("--workspace", type=Path, required=True)
    baseline.add_argument("--period", required=True)
    baseline.add_argument(
        "--store",
        help="多店铺环境中必须提供稳定店铺 ID，避免跨店混算",
    )
    baseline.add_argument("--freeze", action="store_true")
    baseline.add_argument("--actor")

    hygiene = subparsers.add_parser("hygiene", help="运行清洁任务（回填空运行、归档旧明细）")
    hygiene.add_argument("--workspace", type=Path, required=True)

    experiment = subparsers.add_parser("experiment", help="反事实实验管理")
    experiment.add_argument("--workspace", type=Path, required=True)
    experiment.add_argument(
        "action",
        choices=["propose", "run", "show", "list"],
        help="实验操作",
    )
    experiment.add_argument("--experiment-id", help="实验 ID（show/run 用）")
    experiment.add_argument("--hypothesis", help="假设 JSON（propose 用）")
    experiment.add_argument("--baseline-run-id", help="基线运行 ID（propose 用）")

    attack_seed = subparsers.add_parser("attack-seed", help="填充内置攻击案例")
    attack_seed.add_argument("--workspace", type=Path, required=True)

    invariants_seed = subparsers.add_parser("invariants-seed", help="填充内置不变量定义")
    invariants_seed.add_argument("--workspace", type=Path, required=True)

    serve = subparsers.add_parser("serve", help="启动本机对账工作台")
    serve.add_argument("--workspace", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    return parser


def _schema(workspace: Path) -> dict[str, Any]:
    config = load_config(workspace=workspace)
    workbench = require_initialized(config)
    try:
        from .memory.database import DuckDBMemory
    except ImportError as exc:
        raise RuntimeError("账本存储模块尚未安装完整") from exc
    with DuckDBMemory(workbench.database) as store:
        store.initialize()
        return {"database": str(workbench.database), "schema_ready": True}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            config = load_config(args.config, args.workspace)
            workbench = initialize(config)
            result = {
                "workspace": str(workbench.root),
                "database": str(workbench.database),
                "customer_data_in_git": False,
            }
        elif args.command == "status":
            config = load_config(args.config, args.workspace)
            workbench = paths(config)
            from .llm_runtime import RuntimeLlmStore

            llm_status = RuntimeLlmStore(workbench.root).public_status()
            result = {
                "workspace": str(workbench.root),
                "initialized": (workbench.root / ".fa-workbench.json").is_file(),
                "database_exists": workbench.database.is_file(),
                "llm_required": False,
                "llm_enabled": llm_status.enabled,
                "llm_configured": llm_status.configured,
                "llm_protocol": llm_status.protocol,
                "llm_model": llm_status.selected_model or None,
                "autonomy_level": config.llm.autonomy_level,
                "redaction_required": config.llm.redaction_required,
            }
        elif args.command == "schema":
            result = _schema(args.workspace)
        elif args.command == "scan":
            from dataclasses import asdict

            from .inventory import scan_inventory

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            result = asdict(scan_inventory(config, workbench))
            result["path"] = str(result["path"])
        elif args.command == "freeze":
            from dataclasses import asdict

            from .freeze import freeze_candidates

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            result = asdict(freeze_candidates(config, workbench))
        elif args.command == "profile":
            from dataclasses import asdict

            from .profiling import profile_snapshots

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            result = asdict(profile_snapshots(workbench))
        elif args.command == "normalize":
            from dataclasses import asdict

            from .phase_a import adjudicate_workspace, normalize_workspace

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            normalized = asdict(
                normalize_workspace(
                    workbench,
                    periods=tuple(
                        args.periods or config.source.scope.resolved_periods()
                    ),
                    store_id=args.store,
                )
            )
            result = {
                **normalized,
                "adjudication": adjudicate_workspace(
                    workbench,
                    mode=config.reconciliation.mode,
                ).to_dict(),
            }
        elif args.command == "adjudicate":
            from .phase_a import adjudicate_workspace

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            result = adjudicate_workspace(
                workbench,
                mode=config.reconciliation.mode,
            ).to_dict()
        elif args.command == "recon":
            from dataclasses import asdict

            from .phase_a import reconcile_period

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            result = asdict(
                reconcile_period(
                    workbench,
                    period_token=args.period,
                    store_id=args.store,
                    mode=config.reconciliation.mode,
                )
            )
        elif args.command == "diff":
            from dataclasses import asdict

            from .phase_a import compare_period

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            result = asdict(
                compare_period(
                    workbench,
                    period_token=args.period,
                    store_id=args.store,
                )
            )
        elif args.command == "baseline":
            from dataclasses import asdict

            from .phase_a import create_baseline

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            result = asdict(
                create_baseline(
                    workbench,
                    period_token=args.period,
                    store_id=args.store,
                    freeze=args.freeze,
                    actor=args.actor,
                )
            )
        elif args.command == "hygiene":
            from .hygiene import (
                archive_stale_reconcile_details,
                backfill_empty_reconcile_runs,
            )
            from .memory.database import DuckDBMemory

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            with DuckDBMemory(workbench.database) as store:
                store.initialize()
                backfilled = backfill_empty_reconcile_runs(store)
                archived = archive_stale_reconcile_details(store)
            result = {"backfilled_runs": backfilled, **archived}
        elif args.command == "experiment":
            from .experiment import ExperimentProposal, ExperimentRunner
            from .memory.database import DuckDBMemory

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            with DuckDBMemory(workbench.database) as store:
                store.initialize()
                runner = ExperimentRunner()
                if args.action == "list":
                    rows = store.execute(
                        "SELECT experiment_id, hypothesis_kind, verdict, created_at "
                        "FROM experiment ORDER BY created_at DESC"
                    ).fetchall()
                    result = {
                        "experiments": [
                            {
                                "experiment_id": r[0],
                                "hypothesis_kind": r[1],
                                "verdict": r[2],
                                "created_at": str(r[3]) if r[3] else None,
                            }
                            for r in rows
                        ]
                    }
                elif args.action == "show":
                    if not args.experiment_id:
                        raise ValueError("--experiment-id 必须提供")
                    row = store.fetchone_required(
                        "SELECT experiment_id, hypothesis_kind, hypothesis_json, "
                        "verdict, verdict_reasons, created_at, decided_at "
                        "FROM experiment WHERE experiment_id = ?",
                        [args.experiment_id],
                    )
                    result = {
                        "experiment_id": row[0],
                        "hypothesis_kind": row[1],
                        "hypothesis_json": json.loads(str(row[2])) if row[2] else {},
                        "verdict": row[3],
                        "verdict_reasons": json.loads(str(row[4])) if row[4] else [],
                        "created_at": str(row[5]) if row[5] else None,
                        "decided_at": str(row[6]) if row[6] else None,
                    }
                elif args.action == "propose":
                    if not args.hypothesis or not args.baseline_run_id:
                        raise ValueError(
                            "--hypothesis and --baseline-run-id required for propose"
                        )
                    hypothesis_json = json.loads(args.hypothesis)
                    proposal = ExperimentProposal(
                        hypothesis_kind=hypothesis_json.get("kind", "unknown"),
                        hypothesis_json=hypothesis_json,
                        proposed_by="cli",
                        baseline_run_id=args.baseline_run_id,
                        scope={},
                    )
                    record = runner.propose(proposal)
                    store.execute(
                        """
                        INSERT INTO experiment (
                            experiment_id, hypothesis_kind, hypothesis_json,
                            proposed_by, baseline_run_id, shadow_run_id, scope_json,
                            baseline_code_sha, shadow_code_sha,
                            baseline_input_sha256, shadow_input_sha256,
                            output_sha256, verdict, verdict_reasons
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            record.experiment_id,
                            record.hypothesis_kind,
                            json.dumps(record.hypothesis_json),
                            record.proposed_by,
                            record.baseline_run_id,
                            record.shadow_run_id,
                            json.dumps(record.scope_json),
                            record.baseline_code_sha,
                            record.shadow_code_sha,
                            record.baseline_input_sha256,
                            record.shadow_input_sha256,
                            record.output_sha256,
                            record.verdict,
                            json.dumps(record.verdict_reasons),
                        ],
                    )
                    result = {
                        "experiment_id": record.experiment_id,
                        "verdict": record.verdict,
                    }
                elif args.action == "run":
                    if not args.experiment_id:
                        raise ValueError("--experiment-id required for run")
                    row = store.fetchone_required(
                        "SELECT experiment_id, hypothesis_kind, hypothesis_json, "
                        "proposed_by, baseline_run_id, scope_json, "
                        "baseline_code_sha, baseline_input_sha256 "
                        "FROM experiment WHERE experiment_id = ?",
                        [args.experiment_id],
                    )
                    from .experiment import ExperimentRecord

                    record = ExperimentRecord(
                        experiment_id=row[0],
                        hypothesis_kind=row[1],
                        hypothesis_json=json.loads(str(row[2])) if row[2] else {},
                        proposed_by=str(row[3]),
                        baseline_run_id=str(row[4]),
                        shadow_run_id=None,
                        scope_json=json.loads(str(row[5])) if row[5] else {},
                        baseline_code_sha=str(row[6] or ""),
                        shadow_code_sha="",
                        baseline_input_sha256=str(row[7] or ""),
                        shadow_input_sha256=str(row[7] or ""),
                        output_sha256=None,
                        verdict="pending",
                        verdict_reasons=[],
                        created_at="",
                        decided_at=None,
                    )
                    from .code_identity import resolve_code_identity
                    from .experiment.wiring import (
                        build_shadow_run,
                        period_is_locked,
                        persist_experiment_results,
                    )

                    scope = record.scope_json or {}
                    period_token = str(
                        scope.get("period_token")
                        or record.hypothesis_json.get("period_token")
                        or ""
                    )
                    if not period_token:
                        raise ValueError(
                            "实验缺少 period_token，无法定位冻结输入；"
                            "请在 scope 或 hypothesis 中提供"
                        )
                    scope_store_id = str(
                        scope.get("store_id")
                        or record.hypothesis_json.get("store_id")
                        or ""
                    ) or None
                    shadow_run, period_id, floor = build_shadow_run(
                        store,
                        period_token=period_token,
                        store_id=scope_store_id,
                    )
                    record.shadow_code_sha = resolve_code_identity().value
                    record = ExperimentRunner(
                        shadow_run=shadow_run,
                        code_sha=record.shadow_code_sha,
                        materiality_floor=floor,
                    ).run(
                        record,
                        period_locked=period_is_locked(store, period_id=period_id),
                    )
                    persist_experiment_results(
                        store,
                        record,
                        period_id=period_id,
                        store_id=scope_store_id,
                    )
                    result = {
                        "experiment_id": record.experiment_id,
                        "verdict": record.verdict,
                        "verdict_reasons": record.verdict_reasons,
                        "metric_count": len(record.metrics or {}),
                        "delta_count": len(record.deltas or []),
                    }
                else:
                    raise ValueError(f"unknown experiment action: {args.action}")
        elif args.command == "attack-seed":
            from .attacks.seed import ensure_seed_attacks
            from .memory.database import DuckDBMemory

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            with DuckDBMemory(workbench.database) as store:
                store.initialize()
                seeded = ensure_seed_attacks(store)
            result = {"attack_cases_seeded": seeded}
        elif args.command == "invariants-seed":
            from .memory.database import DuckDBMemory

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            with DuckDBMemory(workbench.database) as store:
                store.initialize()
                seeded = store.seed_builtin_invariants()
            result = {"invariants_seeded": seeded}
        else:
            container_bind = os.getenv("FA_CONTAINER_BIND") == "1"
            if args.host not in {"127.0.0.1", "localhost", "::1"} and not container_bind:
                raise ValueError("阶段 B 工作台默认只允许绑定本机回环地址")
            import uvicorn

            from .api import create_app
            from .phase_a import adjudicate_workspace

            config = load_config(args.config, args.workspace)
            workbench = require_initialized(config)
            adjudicate_workspace(
                workbench,
                mode=config.reconciliation.mode,
            )
            web_dist = Path(
                os.getenv(
                    "FA_WEB_DIST",
                    str(Path(__file__).parents[1] / "web" / "dist"),
                )
            )
            uvicorn.run(
                create_app(config, web_dist),
                host=args.host,
                port=args.port,
                log_level="info",
            )
            return 0
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(f"fa-harness: {exc}", file=sys.stderr)
        return 1
