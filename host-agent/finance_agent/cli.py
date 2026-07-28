from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import AgentConfig, load_config
from .connectors import LocalFixtureConnector, WindowsSshConnector
from .connectors.base import ReadOnlyConnector
from .control_plane import ControlPlaneClient
from .engine import RecomputeSpec, deterministic_recompute
from .profiling import profile_file
from .state import AgentState
from .worker import AgentWorker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finance-agent",
        description="finance-win 外置只读采集与确定性计算代理",
    )
    parser.add_argument("--config", type=Path, help="TOML 配置文件")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("register", help="用环境变量中的注册令牌登记代理")
    scan = subparsers.add_parser(
        "scan", help="本地直接执行一次只读扫描，不联系控制面"
    )
    scan.add_argument(
        "--hash-pbix",
        action="store_true",
        help="稳定文件按需读取完整 PBIX 并计算 SHA-256",
    )
    profile = subparsers.add_parser("profile", help="对本地文件生成确定性结构画像")
    profile.add_argument("path", type=Path)
    profile.add_argument("--purpose")
    recompute = subparsers.add_parser(
        "recompute", help="在 Docker 外直接执行一次确定性重计算"
    )
    recompute.add_argument("path", type=Path)
    recompute.add_argument("--business-key", required=True)
    recompute.add_argument(
        "--amount-column", action="append", required=True, dest="amount_columns"
    )
    recompute.add_argument("--date-column")
    recompute.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("once", help="心跳、领取并执行至多一个任务")
    subparsers.add_parser("daemon", help="持续心跳、领取任务并上报持久化进度")
    return parser


def _connector(config: AgentConfig) -> ReadOnlyConnector:
    if config.connector == "local_fixture":
        return LocalFixtureConnector(config)
    if config.connector == "ssh_windows":
        return WindowsSshConnector(config)
    raise ValueError(f"未知 connector: {config.connector}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    if args.command == "profile":
        profile_result = profile_file(args.path, args.purpose)
        print(json.dumps(profile_result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "recompute":
        recompute_result = deterministic_recompute(
            args.path,
            RecomputeSpec(
                business_key=args.business_key,
                amount_columns=tuple(args.amount_columns),
                date_column=args.date_column,
            ),
            args.output,
        )
        print(json.dumps(recompute_result, ensure_ascii=False, indent=2))
        return 0

    connector = _connector(config)
    if args.command == "scan":
        records = connector.scan()
        if args.hash_pbix:
            from dataclasses import replace

            records = [
                replace(item, sha256=connector.stable_sha256(item))
                if item.extension == ".pbix" and item.purpose == "pbix_asset"
                else item
                for item in records
            ]
        print(
            json.dumps(
                {"count": len(records), "files": [item.to_dict() for item in records]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    state = AgentState(config.state_dir)
    control = ControlPlaneClient(config, state)
    try:
        if args.command == "register":
            registration = control.register()
            safe = {
                key: value
                for key, value in registration.items()
                if key != "access_token"
            }
            safe["access_token_stored"] = bool(registration.get("access_token"))
            print(json.dumps(safe, ensure_ascii=False, indent=2))
            return 0
        worker = AgentWorker(config, connector, state, control)
        if args.command == "once":
            job_result = worker.run_once()
            print(
                json.dumps(
                    job_result or {"status": "idle"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        worker.run_daemon()
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports any fatal job error
        print(f"finance-agent: {exc}", file=sys.stderr)
        return 1
    finally:
        control.close()
        state.close()
