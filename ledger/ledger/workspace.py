"""工作区：交上来的表存哪、算过的账记在哪、这个月结没结。

没有工作区就谈不上「自动计算」。店长今天交淘宝、明天交聚水潭，引擎得能把两次
交的表凑在一起算；不留档就只能要求一次性传齐，那还是人在替系统记事。

三件事，各自解决一个具体问题：

留档
    文件按 sha256 存一份，同一份表重复上传只占一份空间。**同名视为同一份数据的
    新版本**：店长改了数据重新导出，文件名不会变（「聚水潭成本-淘宝喜必顺.xlsx」），
    如果两版都参与计算，成本就是双份。所以按 (店铺, 文件名) 认槽位，新版本顶掉旧的，
    旧版本留在历史里不删——事后要能回答「上周算出来的数是拿哪一版算的」。

快照
    每次算账把结果整份存下来。总览页要展示十几家店的状态，不可能每次打开都把所有
    店重算一遍；更要紧的是结账那一刻的数字必须冻住，之后模型改了、字典补了，
    已结的账不能跟着变。

账期
    open（进行中）/ closed（已结账）。结账要求自检层放行，结完冻住快照。
    结账后又有新数据交上来，不偷偷重算，而是挂个「有新数据待处理」的旗子，
    要人反结账才动——账已经报出去了，系统不能自己把数字改掉。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator

from .version import engine_version

#: 账期状态。只有两个——「算过了」不是一种状态，那是有没有快照的事。
OPEN = "open"
CLOSED = "closed"

_SCHEMA = """
create table if not exists file (
  sha        text primary key,
  name       text not null,
  size       integer not null,
  first_seen text not null
);

-- 槽位：(店铺, 文件名) → 当前生效的那一版。算账只读这张表。
create table if not exists slot (
  store_id   text not null,
  name       text not null,
  sha        text not null,
  updated_at text not null,
  by         text not null default '',
  primary key (store_id, name)
);

-- 历史版本，只增不删。留着回答「当时是拿哪一版算的」。
create table if not exists version (
  id       integer primary key autoincrement,
  store_id text not null,
  name     text not null,
  sha      text not null,
  at       text not null,
  by       text not null default ''
);

-- 算账快照。result 是给界面的整份结构，冻结用。
create table if not exists run (
  id        integer primary key autoincrement,
  store_id  text not null,
  period    text not null,
  at        text not null,
  can_close integer not null,
  evidence_ready integer not null default 0,
  evidence_error text not null default '',
  -- 哪一版代码算的。改坏了要回滚，得先说得清回到哪一版；带 -dirty 的那些
  -- 是拿没进版本库的代码算的，不可复现，不能当回滚目标。
  engine    text not null default '',
  shas      text not null,
  result    text not null
);
create index if not exists run_key on run (store_id, period, id desc);

create table if not exists period (
  store_id   text not null,
  period     text not null,
  state      text not null,
  changed_at text not null,
  by         text not null default '',
  note       text not null default '',
  run_id     integer,
  -- 结账那一刻这家店的留档到第几版。判断「结账后有没有新数据」靠它，不靠时间戳：
  -- 时间戳只到秒，同一秒内交的表就检测不出来，还得担心机器时钟被人调过。
  at_version integer not null default 0,
  primary key (store_id, period)
);
"""

#: 后加的列。老工作区打开时补上，不用导数据。
_COLUMNS = {
    "period": {"at_version": "integer not null default 0"},
    # Old runs have no trustworthy proof that their Parquet archive completed.
    # They intentionally migrate to not-ready and must be recomputed before close.
    "run": {
        "evidence_ready": "integer not null default 0",
        "evidence_error": "text not null default ''",
        # 老记录留空。空就是空——不知道是哪一版算的，别猜一个填进去。
        "engine": "text not null default ''",
    },
}


class WorkspaceError(Exception):
    """工作区操作被拒绝。消息是人话，可以直接显示。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """给已有的库补上后加的列。工作区是长期资产，不能因为加个字段就让人重建。"""
    for table, columns in _COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"pragma table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"alter table {table} add column {name} {decl}")


@dataclass(frozen=True, slots=True)
class Kept:
    """一次留档的结果。"""

    sha: str
    name: str
    store_id: str
    #: 之前这个槽位放的是哪一版。空表示第一次交这份表。
    replaced: str = ""
    #: 内容和当前生效版本一模一样。界面上说「和上次一样，没变化」比说「已上传」有用。
    unchanged: bool = False


@dataclass(frozen=True, slots=True)
class PeriodState:
    """一家店一个账期的状态。总览页每个格子就是这个。"""

    store_id: str
    period: str
    state: str = OPEN
    changed_at: str = ""
    by: str = ""
    note: str = ""
    #: 当前展示的是哪一次算账。开着的时候是最近一次，结账后是被冻住的那一次。
    #:
    #: 事实明细按 run_id 落档，下钻要靠它取回来。所以这里必须给出「正在看的那一次」，
    #: 而不是只在结账时才有值——否则进行中的账期点开任何一行都是空的。
    run_id: int | None = None
    result: dict[str, Any] | None = None
    at: str = ""
    #: 算这份账的那一版引擎。老记录是空的——空就是「不知道」，不猜。
    #:
    #: 不进 `result`：`result` 是回放逐字段比对的那份东西，塞进去的话每提交一次
    #: 基线就全变，比对当天就废了。版本是「这份结果的出身」，不是结果本身。
    engine: str = ""
    #: 结账之后又有文件交上来。要人反结账才会重算。
    stale: bool = False

    @property
    def closed(self) -> bool:
        return self.state == CLOSED


@dataclass
class Workspace:
    """一个目录 + 一个 sqlite。没有服务、没有连接池，就是文件夹。

    sqlite 只存索引和快照，文件本体按 sha 摊在 files/ 下。想搬机器直接拷目录，
    想看原始文件直接进目录翻——运维在这台机器上能自己搞定的事，不该只能通过界面做。
    """

    root: Path
    _local: threading.local = field(default_factory=threading.local, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        (self.root / "files").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #

    @property
    def conn(self) -> sqlite3.Connection:
        """每个线程一条连接。

        一条连接给多个线程共用是不行的：sqlite3 的连接对象内部有游标状态，两个线程
        同时执行就会撞出 `InterfaceError: bad parameter or other API misuse`——不是
        「数据库忙」那种能重试的错，是直接 500。

        这个坑之前藏着，因为老界面是一个请求接一个请求发的。新界面一进页面就并发
        拉启动信息、总览、店铺详情三份数据，一刷新就撞。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.root / "workspace.db", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # 界面读、上传写，同时发生很正常。WAL 让读不被写挡住。
            conn.execute("pragma journal_mode=wal")
            # WAL 只解决读写并发，写与写还是要排队。默认排不到就立刻抛
            # "database is locked"：两个人同时交表，后一个直接失败，而他交的表
            # 已经落盘了——文件在、账没记，这种半截状态最难查。等一会儿再说没写上，
            # 比立刻报错诚实得多。写事务本身只是插几行运行记录，等不到 30 秒。
            conn.execute("pragma busy_timeout=30000")
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.commit()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """关掉本线程那条。别的线程各自持有自己的，由进程退出收尾。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ #
    # 留档
    # ------------------------------------------------------------------ #

    def path_of(self, sha: str) -> Path:
        """内容在磁盘上的位置。两级目录，免得几万个文件挤在一层。"""
        return self.root / "files" / sha[:2] / sha

    def materialize(self, sha: str) -> Path | None:
        """把留档的内容按原始文件名还原出来一份，返回路径。

        内容文件是按哈希命名的，没有后缀，直接递给解析器认不出是 xlsx 还是 csv——
        `parse` 就是按后缀分派的。接表向导要单独拿一份文件出来看，所以得走这里，
        不能像算账那样整店导出。

        找不到内容返回 None：文件可能被人从磁盘上清掉了，这时该提示重新上传，
        而不是抛个 FileNotFound 让界面显示 500。
        """
        src = self.path_of(sha)
        if not src.exists():
            return None
        row = self.conn.execute("select name from file where sha=?", (sha,)).fetchone()
        name = (row["name"] if row else "") or sha
        out = self.root / "peek" / sha[:12] / Path(name).name
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            return out
        try:
            out.hardlink_to(src)
        except OSError:
            shutil.copy2(src, out)
        return out

    def keep(self, name: str, src: IO[bytes] | Path, store_id: str, by: str = "") -> Kept:
        """收一份表。

        `name` 必须是原始文件名：店铺归属和数据源识别全靠它，换成随机名这两件事立刻全瞎。
        """
        name = Path(name).name
        if not name:
            raise WorkspaceError("文件名是空的，收不下")

        # 落盘时还不知道 sha，先用随机名，算完再改名到最终位置。
        # 两个人同时上传不能撞在同一个临时文件上。
        blob = self.root / "files" / f".incoming-{uuid.uuid4().hex}"
        blob.parent.mkdir(parents=True, exist_ok=True)
        sha = _spool(src, blob)
        target = self.path_of(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            blob.unlink(missing_ok=True)
        else:
            blob.replace(target)

        at = _now()
        with self.conn as conn:
            conn.execute(
                "insert or ignore into file (sha, name, size, first_seen) values (?,?,?,?)",
                (sha, name, target.stat().st_size, at),
            )
            row = conn.execute(
                "select sha from slot where store_id=? and name=?", (store_id, name)
            ).fetchone()
            previous = row["sha"] if row else ""
            if previous == sha:
                return Kept(sha=sha, name=name, store_id=store_id, unchanged=True)
            conn.execute(
                "insert into slot (store_id, name, sha, updated_at, by) values (?,?,?,?,?) "
                "on conflict(store_id, name) do update set sha=excluded.sha, "
                "updated_at=excluded.updated_at, by=excluded.by",
                (store_id, name, sha, at, by),
            )
            conn.execute(
                "insert into version (store_id, name, sha, at, by) values (?,?,?,?,?)",
                (store_id, name, sha, at, by),
            )
        return Kept(sha=sha, name=name, store_id=store_id, replaced=previous)

    def active_files(self, store_id: str) -> list[Path]:
        """这家店当前生效的全部文件。算账就吃这个。

        文件按原始名字导出到一个工作目录里：引擎靠文件名认店铺和数据源，
        不能直接把 sha 命名的内容文件递进去。
        """
        rows = self.conn.execute(
            "select name, sha from slot where store_id=? order by name", (store_id,)
        ).fetchall()
        out: list[Path] = []
        work = self.root / "work" / store_id
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        for r in rows:
            src = self.path_of(r["sha"])
            if not src.exists():
                continue
            link = work / r["name"]
            try:
                link.hardlink_to(src)
            except OSError:
                shutil.copy2(src, link)
            out.append(link)
        return out

    def submissions(self, store_id: str | None = None) -> list[dict[str, Any]]:
        """交表清单。数据交付看板就是这张表。"""
        sql = (
            "select s.store_id, s.name, s.sha, s.updated_at, s.by, f.size, "
            "(select count(*) from version v where v.store_id=s.store_id and v.name=s.name) as versions "
            "from slot s join file f on f.sha=s.sha"
        )
        args: tuple[Any, ...] = ()
        if store_id:
            sql += " where s.store_id=?"
            args = (store_id,)
        sql += " order by s.store_id, s.name"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def forget(self, store_id: str, name: str) -> None:
        """把一份表撤下来，不再参与计算。内容和历史都留着。"""
        with self.conn as conn:
            conn.execute("delete from slot where store_id=? and name=?", (store_id, name))

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #

    def record(
        self,
        store_id: str,
        period: str,
        result: dict[str, Any],
        shas: list[str],
        *,
        evidence_ready: bool = True,
    ) -> int:
        """存一次算账结果。已结账的账期不覆盖快照，只标记有新数据。"""
        with self.conn as conn:
            state = conn.execute(
                "select state from period where store_id=? and period=?", (store_id, period)
            ).fetchone()
            cur = conn.execute(
                "insert into run "
                "(store_id, period, at, can_close, evidence_ready, engine, shas, result) "
                "values (?,?,?,?,?,?,?,?)",
                (
                    store_id, period, _now(), int(bool(result.get("can_close"))),
                    int(evidence_ready), engine_version(),
                    json.dumps(sorted(shas)), json.dumps(result, ensure_ascii=False),
                ),
            )
            if state is None:
                conn.execute(
                    "insert into period (store_id, period, state, changed_at) values (?,?,?,?)",
                    (store_id, period, OPEN, _now()),
                )
            return int(cur.lastrowid or 0)

    def mark_evidence(self, run_id: int, *, ready: bool, error: str = "") -> None:
        """Finalize a run only after its row-level evidence archive is durable."""
        with self.conn as conn:
            row = conn.execute("select result from run where id=?", (run_id,)).fetchone()
            if row is None:
                raise WorkspaceError(f"没有第 {run_id} 次算账记录")
            result = json.loads(row["result"])
            if not ready:
                message = "事实证据留档失败，不能结账"
                if error:
                    message += f"：{error}"
                result["can_close"] = False
                findings = result.setdefault("findings", [])
                if not any(f.get("id") == "evidence_archive" for f in findings):
                    findings.append({
                        "id": "evidence_archive",
                        "name": "事实证据留档",
                        "blocking": True,
                        "passed": False,
                        "message": message,
                    })
            conn.execute(
                "update run set evidence_ready=?, evidence_error=?, can_close=?, result=? where id=?",
                (
                    int(ready), error[:1000], int(bool(result.get("can_close"))),
                    json.dumps(result, ensure_ascii=False), run_id,
                ),
            )

    def facts_path(self, run_id: int) -> Path:
        """这次算账的事实行存哪。

        快照只有汇总数字，下钻要的是「这 3,000 行分别来自哪个文件第几行」。事实行
        动辄几十万行，塞进 JSON 快照会把数据库撑爆，所以单独落一份列式文件。
        写读都由上层用 polars 做，这里只管给位置——不想让存档层也依赖计算库。
        """
        d = self.root / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{run_id}.parquet"

    def latest_run(self, store_id: str, period: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "select * from run where store_id=? and period=? order by id desc limit 1",
            (store_id, period),
        ).fetchone()
        return dict(row) if row else None

    def state(self, store_id: str, period: str) -> PeriodState | None:
        """一个账期的完整状态：状态 + 该展示的那份快照 + 有没有过期。"""
        row = self.conn.execute(
            "select * from period where store_id=? and period=?", (store_id, period)
        ).fetchone()
        if row is None:
            return None
        return self._state(row)

    def overview(self) -> list[PeriodState]:
        """所有店 × 所有账期。总览矩阵的数据源。"""
        rows = self.conn.execute(
            "select * from period order by period desc, store_id"
        ).fetchall()
        return [self._state(r) for r in rows]

    def _state(self, row: sqlite3.Row) -> PeriodState:
        store_id, period = row["store_id"], row["period"]
        if row["state"] == CLOSED and row["run_id"]:
            shown = self.conn.execute("select * from run where id=?", (row["run_id"],)).fetchone()
        else:
            shown = self.conn.execute(
                "select * from run where store_id=? and period=? order by id desc limit 1",
                (store_id, period),
            ).fetchone()
        stale = False
        if row["state"] == CLOSED:
            # 结账之后有没有交过新表：比留档版本号。
            newer = self.conn.execute(
                "select count(*) as n from version where store_id=? and id > ?",
                (store_id, row["at_version"] or 0),
            ).fetchone()
            stale = bool(newer and newer["n"])
        return PeriodState(
            store_id=store_id,
            period=period,
            state=row["state"],
            changed_at=row["changed_at"] or "",
            by=row["by"] or "",
            note=row["note"] or "",
            run_id=int(shown["id"]) if shown else None,
            result=json.loads(shown["result"]) if shown else None,
            at=shown["at"] if shown else "",
            engine=(shown["engine"] or "") if shown else "",
            stale=stale,
        )

    # ------------------------------------------------------------------ #
    # 账期
    # ------------------------------------------------------------------ #

    def close_period(self, store_id: str, period: str, by: str = "", note: str = "") -> PeriodState:
        """结账。自检层不放行就不许结——这是整套东西存在的意义。"""
        run = self.latest_run(store_id, period)
        if run is None:
            raise WorkspaceError(f"{period} 还没算过账，不能结")
        if not run["evidence_ready"]:
            why = run["evidence_error"] or "事实证据尚未完成留档"
            raise WorkspaceError(f"{period} 结不了账：{why}")
        if not run["can_close"]:
            result = json.loads(run["result"])
            blockers = [
                f["message"] for f in result.get("findings", [])
                if f.get("blocking") and not f.get("passed")
            ]
            missing = result.get("missing_sources") or []
            if missing:
                blockers.append("还缺：" + "、".join(missing))
            why = blockers[0] if blockers else "自检没通过"
            raise WorkspaceError(f"{period} 结不了账：{why}")
        with self.conn as conn:
            version = conn.execute(
                "select coalesce(max(id), 0) as v from version where store_id=?", (store_id,)
            ).fetchone()["v"]
            conn.execute(
                "insert into period (store_id, period, state, changed_at, by, note, run_id, at_version) "
                "values (?,?,?,?,?,?,?,?) on conflict(store_id, period) do update set "
                "state=excluded.state, changed_at=excluded.changed_at, by=excluded.by, "
                "note=excluded.note, run_id=excluded.run_id, at_version=excluded.at_version",
                (store_id, period, CLOSED, _now(), by, note, run["id"], version),
            )
        state = self.state(store_id, period)
        assert state is not None
        return state

    def reopen_period(self, store_id: str, period: str, by: str = "", note: str = "") -> PeriodState:
        """反结账。谁反的、为什么反，必须留痕。"""
        if not note.strip():
            raise WorkspaceError("反结账要写原因")
        with self.conn as conn:
            changed = conn.execute(
                "update period set state=?, changed_at=?, by=?, note=?, run_id=null "
                "where store_id=? and period=? and state=?",
                (OPEN, _now(), by, note, store_id, period, CLOSED),
            ).rowcount
        if not changed:
            raise WorkspaceError(f"{period} 本来就没结账")
        state = self.state(store_id, period)
        assert state is not None
        return state

    def history(self, store_id: str, period: str) -> list[dict[str, Any]]:
        """这个账期算过几次、每次结论如何。翻旧账用。"""
        rows = self.conn.execute(
            "select id, at, can_close, evidence_ready, evidence_error "
            "from run where store_id=? and period=? order by id desc",
            (store_id, period),
        ).fetchall()
        return [dict(r) for r in rows]


def _spool(src: IO[bytes] | Path, target: Path) -> str:
    """边落盘边算 sha。大文件不整份读进内存。"""
    h = hashlib.sha256()
    with target.open("wb") as out:
        for chunk in _chunks(src):
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def _chunks(src: IO[bytes] | Path, size: int = 1 << 20) -> Iterator[bytes]:
    if isinstance(src, Path):
        with src.open("rb") as fh:
            yield from iter(lambda: fh.read(size), b"")
        return
    try:
        src.seek(0)
    except (OSError, AttributeError):  # pragma: no cover - 不可回绕的流
        pass
    yield from iter(lambda: src.read(size), b"")


def default_root() -> Path:
    """工作区默认放哪。`LEDGER_HOME` 可以改。

    默认放家目录而不是仓库里：这里存的是公司的账，跟着代码走会被 git 收进去，
    也会在部署时被覆盖掉。
    """
    env = os.environ.get("LEDGER_HOME")
    return Path(env).expanduser() if env else Path.home() / ".ledger"


def open_workspace(root: str | Path | None = None) -> Workspace:
    """打开（必要时创建）一个工作区。"""
    return Workspace(Path(root) if root else default_root())


__all__ = [
    "CLOSED",
    "OPEN",
    "default_root",
    "Kept",
    "PeriodState",
    "Workspace",
    "WorkspaceError",
    "open_workspace",
]
