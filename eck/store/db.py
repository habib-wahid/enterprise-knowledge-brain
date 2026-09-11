"""Knowledge store access.

Two rules hold everywhere in this module:
  * Rows are deterministic. Ids are content-derived and no row carries a
    wall-clock time; time lives on refresh_run (BR-11).
  * Writes go to a NEW database file which is renamed over the live one only
    after it validates. A reader never sees a half-built graph (BR-66).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .vocab import edge_kind, node_kind

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def node_id(asset_id: str, kind: str, fqn: str) -> str:
    """Deterministic node identity — same source, same id, every run."""
    return sha256(f"{asset_id}|{kind}|{fqn}".encode("utf-8"))[:16]


def edge_id(src: str, dst: str, kind: str, line: int) -> str:
    return sha256(f"{src}|{dst}|{kind}|{line}".encode("utf-8"))[:16]


def canonical_attrs(attrs: dict[str, Any] | None) -> str:
    """JSON with sorted keys — otherwise dict ordering breaks determinism."""
    return json.dumps(attrs or {}, sort_keys=True, separators=(",", ":"))


class KnowledgeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # ---------------------------------------------------------------- build

    @classmethod
    def create(cls, path: Path) -> "KnowledgeStore":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        store = cls(path)
        store.conn.executescript(SCHEMA_PATH.read_text())
        store.conn.commit()
        return store

    def start_run(self, estate_id: str, register_sha: str, stage: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO refresh_run (id, started_at, stage, status, estate_id,"
            " register_sha) VALUES (?,?,?,?,?,?)",
            (run_id, utc_now(), stage, "running", estate_id, register_sha),
        )
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE refresh_run SET finished_at=?, status=?, notes=? WHERE id=?",
            (utc_now(), status, notes, run_id),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- writes

    def add_asset(self, **kw: Any) -> None:
        self.conn.execute(
            "INSERT INTO asset (id,name,role,owner,tech,source_kind,abs_path,"
            "rel_path,source_commit,run_id) VALUES (:id,:name,:role,:owner,"
            ":tech,:source_kind,:abs_path,:rel_path,:source_commit,:run_id)",
            kw,
        )

    def add_exclusions(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO exclusion (asset_id,path_glob,reason,run_id)"
            " VALUES (:asset_id,:path_glob,:reason,:run_id)",
            list(rows),
        )

    def add_nodes(self, rows: Iterable[dict[str, Any]]) -> None:
        prepared = []
        for r in rows:
            node_kind(r["kind"])  # BR-12: raises UnknownKind if out of vocabulary
            r = dict(r)
            r["attrs"] = canonical_attrs(r.get("attrs"))
            prepared.append(r)
        self.conn.executemany(
            "INSERT OR IGNORE INTO node (id,asset_id,kind,name,fqn,signature,path,"
            "start_line,end_line,span_sha,origin,confidence,attrs,run_id)"
            " VALUES (:id,:asset_id,:kind,:name,:fqn,:signature,:path,"
            ":start_line,:end_line,:span_sha,:origin,:confidence,:attrs,:run_id)",
            prepared,
        )

    def add_edges(self, rows: Iterable[dict[str, Any]]) -> None:
        prepared = []
        for r in rows:
            edge_kind(r["kind"])  # BR-12
            r = dict(r)
            r["attrs"] = canonical_attrs(r.get("attrs"))
            prepared.append(r)
        self.conn.executemany(
            "INSERT OR IGNORE INTO edge (id,src_id,dst_id,kind,path,start_line,"
            "origin,confidence,attrs,run_id) VALUES (:id,:src_id,:dst_id,:kind,"
            ":path,:start_line,:origin,:confidence,:attrs,:run_id)",
            prepared,
        )

    def add_parse_failures(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO parse_failure (asset_id,path,reason,detail,run_id)"
            " VALUES (:asset_id,:path,:reason,:detail,:run_id)",
            list(rows),
        )

    def add_unresolved(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO unresolved_ref (asset_id,src_id,kind,raw,path,"
            "start_line,reason,run_id) VALUES (:asset_id,:src_id,:kind,:raw,"
            ":path,:start_line,:reason,:run_id)",
            list(rows),
        )

    # ---------------------------------------------------------------- misc

    def commit(self) -> None:
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def has_table(self, name: str) -> bool:
        """Does this database carry that table?

        A knowledge.db built before a capability landed simply lacks its
        tables. That is a knowledge gap to report (BR-70), not a crash: a
        reader must be able to open an older database and be told what it
        cannot answer, rather than receive a stack trace.
        """
        return bool(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,)).fetchone())

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()

    def publish(self, live_path: Path) -> None:
        """Atomic swap — BR-66. A reader sees the old graph or the new one."""
        self.commit()
        self.close()
        os.replace(self.path, live_path)

    # ------------------------------------------------- CAP-3 / CAP-5 writes

    def add_docs(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO doc (id,asset_id,title,path,last_commit,last_author,"
            "last_commit_date,origin,run_id) VALUES (:id,:asset_id,:title,"
            ":path,:last_commit,:last_author,:last_commit_date,:origin,:run_id)",
            list(rows))

    def add_chunks(self, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        self.conn.executemany(
            "INSERT OR IGNORE INTO chunk (id,asset_id,source,doc_id,node_id,"
            "heading,text,path,start_line,end_line,span_sha,origin,run_id)"
            " VALUES (:id,:asset_id,:source,:doc_id,:node_id,:heading,:text,"
            ":path,:start_line,:end_line,:span_sha,:origin,:run_id)", rows)
        self.conn.executemany(
            "INSERT INTO chunk_fts (chunk_id, text) VALUES (?,?)",
            [(r["id"], r["text"]) for r in rows])

    def add_vectors(self, rows: Iterable[tuple[str, int, bytes]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO chunk_vec (chunk_id,dim,vec) VALUES (?,?,?)",
            list(rows))

    def record_embedding_model(self, run_id: str, name: str, dim: int,
                               local: bool) -> None:
        self.conn.execute(
            "INSERT INTO embedding_model (run_id,name,dim,local) VALUES (?,?,?,?)",
            (run_id, name, dim, 1 if local else 0))

    def add_anchors(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO anchor (id,chunk_id,statement,statement_sha,"
            "doc_path,doc_start_line,doc_end_line,target_asset,target_kind,"
            "target_fqn,target_node_id,target_path,target_start_line,"
            "target_end_line,span_sha_at_approval,span_sha_now,state,confidence,"
            "justification,proposer,reviewed_by,reviewed_at,origin,run_id)"
            " VALUES (:id,:chunk_id,:statement,:statement_sha,:doc_path,"
            ":doc_start_line,:doc_end_line,:target_asset,:target_kind,"
            ":target_fqn,:target_node_id,:target_path,:target_start_line,"
            ":target_end_line,:span_sha_at_approval,:span_sha_now,:state,"
            ":confidence,:justification,:proposer,:reviewed_by,:reviewed_at,"
            ":origin,:run_id)", list(rows))

    # ------------------------------------------------------------- CAP-4/6

    def add_process(self, process: Iterable[dict[str, Any]],
                    stages: Iterable[dict[str, Any]],
                    anchors: Iterable[dict[str, Any]],
                    failures: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO process (id,name,description,origin,authored_by,"
            "authored_at,run_id) VALUES (:id,:name,:description,:origin,"
            ":authored_by,:authored_at,:run_id)", list(process))
        self.conn.executemany(
            "INSERT INTO process_stage (id,process_id,ordinal,stage_key,name,"
            "description,is_entry,entry_trigger,run_id) VALUES (:id,"
            ":process_id,:ordinal,:stage_key,:name,:description,:is_entry,"
            ":entry_trigger,:run_id)", list(stages))
        self.conn.executemany(
            "INSERT INTO process_stage_anchor (id,stage_id,target_asset,"
            "target_fqn,target_node_id,target_kind,target_path,"
            "target_start_line,target_end_line,state,run_id) VALUES (:id,"
            ":stage_id,:target_asset,:target_fqn,:target_node_id,"
            ":target_kind,:target_path,:target_start_line,:target_end_line,"
            ":state,:run_id)", list(anchors))
        self.conn.executemany(
            "INSERT INTO process_failure (id,stage_id,description,target_fqn,"
            "target_node_id,target_path,target_start_line,state,run_id)"
            " VALUES (:id,:stage_id,:description,:target_fqn,:target_node_id,"
            ":target_path,:target_start_line,:state,:run_id)", list(failures))

    def add_refdata_source(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO refdata_source (id,kind,asset_id,reason,run_id)"
            " VALUES (:id,:kind,:asset_id,:reason,:run_id)", list(rows))

    def add_refdata_values(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO refdata_value (id,source_id,asset_id,label,value,"
            "path,start_line,snapshot_at,origin,run_id) VALUES (:id,"
            ":source_id,:asset_id,:label,:value,:path,:start_line,"
            ":snapshot_at,:origin,:run_id)", list(rows))

    def add_refdata_excluded(self, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT INTO refdata_excluded (key_or_table,reason,run_id)"
            " VALUES (:key_or_table,:reason,:run_id)", list(rows))
