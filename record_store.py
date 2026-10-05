# record_store.py
# -*- coding: utf-8 -*-
"""
記録（topic_stats / segments / ab_votes / ab_votes_enriched）を Postgres に保存する。

- 環境変数 DATABASE_URL があるときだけ使う（ないときは ogiri_duel.py が今までどおり logs/ に保存する）
- ドライバは pg8000（Python だけで書かれていて、eventlet の monkey_patch 後のソケットでそのまま動く）
- 書き込みは専用のワーカースレッド（eventlet 下ではグリーンスレッド）がキューから順に行う。
  データベースが遅い・落ちているときも、ゲームの処理は待たされない。失敗はログに出して捨てる
- 必要な表は、最初に接続したときに無ければ作る
- ログは print(..., flush=True) で出す。gunicorn の下では標準出力がパイプになり、
  flush しないと溜まったまま表示されない（PCで PYTHONUNBUFFERED=1 を付けて試したときとの違い）
"""
import json
import os
import queue
import ssl
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

QUEUE_MAX = 1000          # 書き込み待ちの上限（溢れたら古いものから捨てずに、新しいものを捨てて警告）
CONNECT_TIMEOUT_SEC = 15  # Neon はしばらく使わないと止まり、起動に数秒かかることがある
RETRY_AFTER_SEC = 30      # 書き込みに失敗したら、この秒数は接続を試さずに捨てる（障害中にキューが詰まらないように）

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS topic_stats (
        key        text PRIMARY KEY,
        prompt_id  text,
        text       text,
        genre      text,
        imp        integer NOT NULL DEFAULT 0,
        ewma_skip  double precision NOT NULL DEFAULT 0.3,
        last_at    timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS topic_stats_prompt_id_idx ON topic_stats (prompt_id)",
    """CREATE TABLE IF NOT EXISTS segments (
        segment_id  text PRIMARY KEY,
        game_id     text,
        prompt_id   text,
        prompt_text text,
        genre       text,
        skip_count  integer NOT NULL DEFAULT 0,
        started_at  timestamptz,
        ended_at    timestamptz,
        answers     jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at  timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS segments_prompt_id_idx ON segments (prompt_id)",
    "CREATE INDEX IF NOT EXISTS segments_started_at_idx ON segments (started_at)",
    """CREATE TABLE IF NOT EXISTS ab_votes (
        id         bigserial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        pair_id    text,
        game_id    text,
        prompt_id  text,
        choice     text,
        valid      boolean,
        data       jsonb NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ab_votes_prompt_id_idx ON ab_votes (prompt_id)",
    "CREATE INDEX IF NOT EXISTS ab_votes_created_at_idx ON ab_votes (created_at)",
    """CREATE TABLE IF NOT EXISTS ab_votes_enriched (
        id         bigserial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        ts         timestamptz,
        segment_id text,
        game_id    text,
        pair_id    text,
        prompt_id  text,
        choice     text,
        valid      boolean,
        data       jsonb NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ab_votes_enriched_segment_id_idx ON ab_votes_enriched (segment_id)",
    "CREATE INDEX IF NOT EXISTS ab_votes_enriched_prompt_id_idx ON ab_votes_enriched (prompt_id)",
    "CREATE INDEX IF NOT EXISTS ab_votes_enriched_ts_idx ON ab_votes_enriched (ts)",
    # 同じ人の回答どうしのペアか（2人のゲーム用）。既にある表にも足す。古い行は NULL のまま
    "ALTER TABLE ab_votes ADD COLUMN IF NOT EXISTS same_author boolean",
    "ALTER TABLE ab_votes_enriched ADD COLUMN IF NOT EXISTS same_author boolean",
    # ペアごとの区切りID（AB評価のペアを試合の全お題から作るようになったため）。古い行は NULL のまま
    "ALTER TABLE ab_votes ADD COLUMN IF NOT EXISTS segment_id text",
    "CREATE INDEX IF NOT EXISTS ab_votes_segment_id_idx ON ab_votes (segment_id)",
]


def parse_database_url(url: str) -> Dict[str, Any]:
    """postgresql://user:pass@host:port/db?sslmode=...&channel_binding=... を pg8000 の接続引数に読み替える。

    sslmode（libpq の設定）→ pg8000 の ssl_context:
      disable           → SSLなし
      allow / prefer    → SSLを試み、使えなければSSLなし（pg8000 の既定動作）
      require           → SSL必須。証明書もOSの信頼済み証明書で検証する（libpq の require より厳しい側に倒す）
      verify-ca         → SSL必須。証明書を検証（ホスト名は見ない）
      verify-full / 指定なしでホストがローカル以外 → SSL必須。証明書とホスト名を検証
    channel_binding は pg8000 に設定項目がない。pg8000 はSSL接続時、サーバーが対応していれば
    自動でチャネルバインディング（SCRAM-SHA-256-PLUS）を使うので、読み飛ばしてよい。
    """
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"unsupported scheme: {u.scheme}")
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    host = u.hostname or "localhost"
    sslmode = q.get("sslmode") or ("disable" if host in ("localhost", "127.0.0.1", "::1") else "verify-full")

    if sslmode == "disable":
        ssl_context: Any = False
    elif sslmode in ("allow", "prefer"):
        ssl_context = None
    elif sslmode in ("require", "verify-full"):
        ssl_context = ssl.create_default_context()
    elif sslmode == "verify-ca":
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
    else:
        raise ValueError(f"unsupported sslmode: {sslmode}")

    ignored = sorted(k for k in q if k not in ("sslmode", "channel_binding", "application_name"))
    return {
        "user": unquote(u.username or ""),
        "password": unquote(u.password) if u.password is not None else None,
        "host": host,
        "port": u.port or 5432,
        "database": unquote(u.path.lstrip("/")) or None,
        "ssl_context": ssl_context,
        "application_name": q.get("application_name", "ogiri-duel"),
        "_sslmode": sslmode,
        "_ignored": ignored,
    }


def _log(level: str, msg: str):
    # pid を付けるのは、キューに積んだプロセスと書き込むスレッドのプロセスが同じか確かめるため
    print(f"[{level}] record store (pid={os.getpid()}): {msg}", flush=True)


class RecordStore:
    def __init__(self, database_url: str):
        self._params = parse_database_url(database_url)
        self._lock = threading.Lock()
        self._q: "queue.Queue[Tuple[str, Callable, tuple]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None
        self._thread_pid: Optional[int] = None
        self._conn = None
        self._schema_ready = False
        self._retry_at = 0.0
        p = self._params
        _log("INFO", f"Postgres {p['host']}:{p['port']}/{p['database']} (sslmode={p['_sslmode']})")
        if p["_ignored"]:
            _log("WARN", f"ignored DATABASE_URL options: {', '.join(p['_ignored'])}")
        self._submit("schema", lambda conn: None)  # 起動直後に接続して表を作る（スレッドもここで起動する）

    # ---- 外から呼ぶ書き込み（すべてキューに積むだけで、すぐ戻る） ----
    def increment_topic_stat(self, key: str, prompt_id: Optional[str], text: str, genre: str, skipped: bool):
        s = 1.0 if skipped else 0.0
        def op(conn):
            conn.run(
                """INSERT INTO topic_stats (key, prompt_id, text, genre, imp, ewma_skip, last_at)
                   VALUES (:key, :pid, :text, :genre, 1, round((0.7 * 0.3 + 0.3 * CAST(:s AS double precision))::numeric, 4), now())
                   ON CONFLICT (key) DO UPDATE SET
                       imp       = topic_stats.imp + 1,
                       ewma_skip = round((0.7 * topic_stats.ewma_skip + 0.3 * CAST(:s AS double precision))::numeric, 4),
                       last_at   = now(),
                       text      = EXCLUDED.text,
                       genre     = EXCLUDED.genre,
                       prompt_id = COALESCE(EXCLUDED.prompt_id, topic_stats.prompt_id)""",
                key=key, pid=prompt_id, text=text, genre=genre, s=s)
        self._submit("topic_stats", op)

    def insert_segment(self, row: Dict[str, Any]):
        def op(conn):
            conn.run(
                """INSERT INTO segments (segment_id, game_id, prompt_id, prompt_text, genre, skip_count,
                                         started_at, ended_at, answers)
                   VALUES (:sid, :gid, :pid, :ptext, :genre, :skips,
                           CAST(:started AS timestamptz), CAST(:ended AS timestamptz), CAST(:answers AS jsonb))
                   ON CONFLICT (segment_id) DO NOTHING""",
                sid=row.get("segment_id"), gid=row.get("game_id"), pid=row.get("prompt_id"),
                ptext=row.get("prompt_text"), genre=row.get("genre", ""), skips=int(row.get("skip_count") or 0),
                started=row.get("started_at"), ended=row.get("ended_at"),
                answers=json.dumps(row.get("answers") or [], ensure_ascii=False))
        self._submit("segments", op)

    def insert_ab_vote(self, row: Dict[str, Any]):
        def op(conn):
            conn.run(
                """INSERT INTO ab_votes (pair_id, game_id, segment_id, prompt_id, choice, valid, same_author, data)
                   VALUES (:pair, :gid, :seg, :pid, :choice, :valid, :same, CAST(:data AS jsonb))""",
                pair=row.get("pair_id"), gid=row.get("game_id"), seg=row.get("segment_id"), pid=row.get("prompt_or_image_id"),
                choice=row.get("choice"), valid=row.get("valid"), same=row.get("same_author"),
                data=json.dumps(row, ensure_ascii=False))
        self._submit("ab_votes", op)

    def insert_ab_vote_enriched(self, row: Dict[str, Any]):
        def op(conn):
            conn.run(
                """INSERT INTO ab_votes_enriched (ts, segment_id, game_id, pair_id, prompt_id, choice, valid, same_author, data)
                   VALUES (CAST(:ts AS timestamptz), :seg, :gid, :pair, :pid, :choice, :valid, :same, CAST(:data AS jsonb))""",
                ts=row.get("ts"), seg=row.get("segment_id"), gid=row.get("game_id"), pair=row.get("pair_id"),
                pid=(row.get("prompt") or {}).get("id"), choice=row.get("choice"), valid=row.get("valid"),
                same=row.get("same_author"), data=json.dumps(row, ensure_ascii=False))
        self._submit("ab_votes_enriched", op)

    def flush(self, timeout: float = 30.0) -> bool:
        """キューが空になるまで待つ（テスト・終了処理用）。"""
        self._ensure_worker()
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.1)
        return False

    # ---- 内部 ----
    def _ensure_worker(self):
        """書き込み用スレッドが、今のプロセスで動いているようにする。
        起動後に fork されたプロセス（gunicorn の --preload など）では、親で起動したスレッドは存在しない。
        その場合はキューも作り直して、このプロセスでスレッドを起動する。止まっていた場合も起動し直す。"""
        pid = os.getpid()
        if self._thread is not None and self._thread_pid == pid and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread_pid == pid and self._thread.is_alive():
                return
            if self._thread_pid is not None and self._thread_pid != pid:
                _log("WARN", f"process changed since the writer thread was started (pid {self._thread_pid} -> {pid}); "
                             "starting a new writer thread in this process")
                self._q = queue.Queue(maxsize=QUEUE_MAX)
                self._conn = None
            elif self._thread is not None:
                _log("ERROR", "writer thread was not running; starting it again")
            self._thread = threading.Thread(target=self._worker, name="record-store", daemon=True)
            self._thread_pid = pid
            self._thread.start()

    def _submit(self, name: str, op: Callable):
        self._ensure_worker()
        try:
            self._q.put_nowait((name, op, ()))
        except queue.Full:
            _log("WARN", f"queue full, dropped a {name} write")

    def _connect(self):
        import pg8000.native
        p = self._params
        conn = pg8000.native.Connection(
            user=p["user"], password=p["password"], host=p["host"], port=p["port"], database=p["database"],
            ssl_context=p["ssl_context"], timeout=CONNECT_TIMEOUT_SEC, application_name=p["application_name"])
        if not self._schema_ready:
            for sql in SCHEMA_SQL:
                conn.run(sql)
            self._schema_ready = True
            _log("INFO", "tables ready")
        return conn

    def _close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _write_one(self, name: str, op: Callable):
        if self._conn is None and time.time() < self._retry_at:
            _log("ERROR", f"database unavailable, dropped a {name} write")
            return
        # 接続が切れていた（Neon のアイドル切断など）ときのため、失敗したら1回だけつなぎ直して再実行
        for attempt in (1, 2):
            try:
                if self._conn is None:
                    self._conn = self._connect()
                op(self._conn)
                return
            except Exception as e:
                self._close()
                if attempt == 1:
                    _log("WARN", f"{name} write failed, reconnecting and retrying once: {type(e).__name__}: {e}")
                else:
                    _log("ERROR", f"{name} write failed: {type(e).__name__}: {e}")
                    self._retry_at = time.time() + RETRY_AFTER_SEC

    def _worker(self):
        q = self._q  # このスレッドが受け持つキュー（プロセスが変わると作り直されるため、起動時のものを持つ）
        _log("INFO", f"writer thread started ({threading.current_thread().name})")
        try:
            while True:
                name, op, _ = q.get()
                try:
                    self._write_one(name, op)
                except BaseException as e:
                    # ここに来るのは想定外の例外（eventlet のタイムアウトなど Exception 以外も含む）。
                    # 全部表示して、終了の合図でなければ次の書き込みへ進む（スレッドを黙って止めない）
                    _log("ERROR", f"unexpected error in writer thread while writing {name}:\n{traceback.format_exc()}")
                    self._close()  # 途中で止まった接続は状態が分からないので捨て、次の書き込みでつなぎ直す
                    if isinstance(e, (SystemExit, KeyboardInterrupt)) or type(e).__name__ == "GreenletExit":
                        raise
                finally:
                    q.task_done()
        except BaseException:
            _log("ERROR", f"writer thread is stopping because of:\n{traceback.format_exc()}")
            raise
        finally:
            _log("INFO", "writer thread stopped")
