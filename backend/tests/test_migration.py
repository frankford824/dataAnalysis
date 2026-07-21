from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_head(tmp_path):
    database = tmp_path / "migration.db"
    env = {**os.environ, "COMMERCE_DATABASE_URL": f"sqlite:///{database}"}
    completed = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=os.path.dirname(os.path.dirname(__file__)), env=env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    tables = set(inspect(create_engine(f"sqlite:///{database}")).get_table_names())
    assert {"enterprises", "stores", "source_definitions", "ingestion_runs", "certified_aggregates", "user_accounts"}.issubset(tables)
