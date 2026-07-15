"""사용자 상태(학습기록·즐겨찾기·북마크·최근·마지막페이지·답지분할선) 동기화 저장소.

서버 본체는 읽기 전용 파일 서버지만, 기기 간 동기화는 서버에 '작은' 공유 상태가
있어야 가능하다. 항목 단위 Last-Write-Wins 맵(LWW-map)으로 저장한다: 각
(book_id, kind, item_key)마다 값 + 타임스탬프(ms) + tombstone(삭제표시)을 두고,
더 최신 타임스탬프가 이긴다. 두 기기가 서로 다른 항목을 바꾸면 둘 다 살아남고,
같은 항목을 바꾸면 나중 것이 이긴다. 인증 없는 tailnet 단일 사용자 전제 —
책마다 하나의 공유 상태다.

DB는 data/sync.db에 둔다. 편집기 rsync는 --exclude=* 화이트리스트라 이 파일을
안 건드리고, 코드 배포 rsync는 --exclude data라 data/ 전체를 건너뛰므로, 전송/
배포를 반복해도 동기화 상태가 보존된다.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

# item_key는 어떤 문자열이든 되지만 book_id는 DB 조회 키로만 쓰므로(파일 경로가
# 아니라 경로 순회 위험은 없다) 그래도 형식을 좁게 검증해 잡음 데이터를 막는다.
_BOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# 클라이언트 원장과 정확히 같은 종류들. 모르는 kind는 병합에서 조용히 무시한다.
SYNC_KINDS = {"marks", "favorites", "bookmarks", "recent", "answerSplits", "lastPage"}

_init_lock = threading.Lock()
_initialized: set[str] = set()


def validate_book_id(book_id: str) -> str:
    if not _BOOK_ID_RE.match(book_id or ""):
        raise ValueError("invalid book_id")
    return book_id


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(db_path: Path) -> None:
    key = str(db_path)
    if key in _initialized:
        return
    with _init_lock:
        if key in _initialized:
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(db_path)
        try:
            # 단일 uvicorn 프로세스지만 sync 핸들러가 스레드풀에서 돌아 여러
            # 스레드가 DB에 붙을 수 있다 — WAL로 읽기/쓰기 동시성을 확보한다.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_kv (
                    book_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    value TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (book_id, kind, item_key)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        _initialized.add(key)


def load_state(db_path: Path, book_id: str) -> dict:
    """book_id의 전체 원장을 {kind: {item_key: {v, t, d}}}로 돌려준다.

    tombstone(d=1)도 포함한다 — 아직 삭제를 못 본 다른 기기가 자기 로컬 사본을
    지우려면 삭제 표시가 전파돼야 하기 때문이다.
    """
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, item_key, value, deleted, updated_at FROM sync_kv WHERE book_id=?",
            (book_id,),
        ).fetchall()
    finally:
        conn.close()
    out: dict = {}
    for r in rows:
        deleted = int(r["deleted"])
        value = None if deleted or r["value"] is None else json.loads(r["value"])
        out.setdefault(r["kind"], {})[r["item_key"]] = {"v": value, "t": r["updated_at"], "d": deleted}
    return out


def merge_state(db_path: Path, book_id: str, incoming: dict) -> dict:
    """incoming {kind: {item_key: {v,t,d}}}를 LWW로 병합하고 병합 후 전체 원장을 돌려준다.

    각 항목은 들어온 t가 저장된 t보다 클 때만 덮어쓴다(같거나 작으면 무시). 이
    규칙은 양방향(클라이언트 push / 서버 pull) 모두 동일해서, 어느 쪽이 먼저 와도
    최종 결과가 같다(멱등).
    """
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        for kind, items in (incoming or {}).items():
            if kind not in SYNC_KINDS or not isinstance(items, dict):
                continue
            for item_key, entry in items.items():
                if not isinstance(entry, dict):
                    continue
                t = entry.get("t")
                if not isinstance(t, (int, float)):
                    continue
                deleted = 1 if entry.get("d") else 0
                value = None if deleted else json.dumps(entry.get("v"), ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO sync_kv (book_id, kind, item_key, value, deleted, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(book_id, kind, item_key) DO UPDATE SET
                        value=excluded.value,
                        deleted=excluded.deleted,
                        updated_at=excluded.updated_at
                    WHERE excluded.updated_at > sync_kv.updated_at
                    """,
                    (book_id, kind, str(item_key), value, deleted, float(t)),
                )
        conn.commit()
    finally:
        conn.close()
    return load_state(db_path, book_id)
