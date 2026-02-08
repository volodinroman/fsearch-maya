import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


class FileIndexRepository:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def set_db_path(self, db_path: Path) -> None:
        target = Path(db_path)
        if target == self._db_path and self._conn is not None:
            return
        self.close()
        self._db_path = target
        self._connect()

    def _connect(self) -> None:
        with self._db_lock:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.create_function("REGEXP", 2, self._regexp)
            self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                path_lower TEXT NOT NULL,
                filename TEXT NOT NULL,
                modified REAL NOT NULL,
                size INTEGER NOT NULL,
                is_dir INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute("PRAGMA table_info(files);")
        cols = {row[1] for row in cur.fetchall()}
        if "path_lower" not in cols:
            cur.execute("ALTER TABLE files ADD COLUMN path_lower TEXT NOT NULL DEFAULT '';")
            cur.execute("UPDATE files SET path_lower = lower(path) WHERE path_lower = '';")
        if "is_dir" not in cols:
            cur.execute("ALTER TABLE files ADD COLUMN is_dir INTEGER NOT NULL DEFAULT 0;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_files_path_lower ON files(path_lower);")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                path,
                filename,
                content='files',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 1'
            );
            """
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                INSERT INTO files_fts(rowid, path, filename)
                VALUES (new.id, new.path, new.filename);
            END;
            """
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                INSERT INTO files_fts(files_fts, rowid, path, filename)
                VALUES ('delete', old.id, old.path, old.filename);
            END;
            """
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
                INSERT INTO files_fts(files_fts, rowid, path, filename)
                VALUES ('delete', old.id, old.path, old.filename);
                INSERT INTO files_fts(rowid, path, filename)
                VALUES (new.id, new.path, new.filename);
            END;
            """
        )
        self._conn.commit()

    @staticmethod
    def _regexp(expr: str, item: str) -> int:
        if item is None:
            return 0
        try:
            return 1 if re.search(expr, item, re.IGNORECASE) else 0
        except re.error:
            return 0

    @property
    def is_indexed(self) -> bool:
        if self._conn is None:
            return False
        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM files;")
            row = cur.fetchone()
            return bool(row and row["c"] > 0)

    def rebuild_index(
        self,
        roots: Iterable[str],
        extensions: List[str],
        include_folders: bool,
        show_progress: bool = True,
        callback=None,
        logger=None,
    ) -> None:
        start = time.time()
        if show_progress and logger:
            logger("Starting index rebuild...")

        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM files;")

            count = 0
            batch = []
            batch_size = 1000

            for row in self._iter_files(roots, extensions, include_folders, logger):
                batch.append(row)
                count += 1
                if len(batch) >= batch_size:
                    cur.executemany(
                        """
                        INSERT INTO files (path, path_lower, filename, modified, size, is_dir)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        batch,
                    )
                    batch.clear()
                    if callback:
                        callback(f"Indexed {count} items...")

            if batch:
                cur.executemany(
                    """
                    INSERT INTO files (path, path_lower, filename, modified, size, is_dir)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    batch,
                )

            cur.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("last_index_time", str(time.time())),
            )
            self._conn.commit()

        duration = time.time() - start
        if show_progress and logger:
            logger(f"Rebuilt index with {count} items in {duration:.2f}s.")
        if callback:
            callback(f"Done. {count} items in {duration:.2f}s.")

    def _iter_files(self, roots: Iterable[str], extensions: List[str], include_folders: bool, logger):
        extension_set = set(extensions)
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.exists() or not root_path.is_dir():
                if logger:
                    logger(f"Skipping invalid root: {root_path}", level="warning")
                continue

            for base, dirnames, filenames in os.walk(root_path):
                if include_folders:
                    for dirname in dirnames:
                        folder_path = Path(base) / dirname
                        folder_str = str(folder_path.as_posix())
                        try:
                            stat = folder_path.stat()
                        except OSError:
                            continue
                        yield (folder_str, folder_str.lower(), dirname, float(stat.st_mtime), 0, 1)

                for filename in filenames:
                    suffix = Path(filename).suffix.lower()
                    if extension_set and suffix not in extension_set:
                        continue
                    full_path = Path(base) / filename
                    try:
                        stat = full_path.stat()
                    except OSError:
                        continue
                    full_path_str = str(full_path.as_posix())
                    yield (
                        full_path_str,
                        full_path_str.lower(),
                        filename,
                        float(stat.st_mtime),
                        int(stat.st_size),
                        0,
                    )

    @staticmethod
    def _tokens_from_text(text: str) -> List[str]:
        return [token.strip().lower() for token in text.split() if token.strip()]

    @staticmethod
    def _build_fts_match_query(tokens: List[str]) -> str:
        safe_tokens = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
        return " AND ".join(safe_tokens)

    def search(self, query: str, max_results: int) -> List[Dict]:
        tokens = self._tokens_from_text(query)
        if not tokens:
            return []

        collected: List[Dict] = []
        seen_paths = set()

        def _append_rows(rows, source: str):
            for row in rows:
                row_dict = dict(row)
                row_path = str(row_dict.get("path", "")).lower()
                if not row_path or row_path in seen_paths:
                    continue
                row_dict["search_source"] = source
                seen_paths.add(row_path)
                collected.append(row_dict)
                if len(collected) >= max_results:
                    break

        where_parts = []
        params = []
        for token in tokens:
            where_parts.append("(path_lower LIKE ? OR lower(filename) LIKE ?)")
            like_value = f"%{token}%"
            params.extend([like_value, like_value])

        where_clause = " AND ".join(where_parts)
        with self._db_lock:
            cur = self._conn.cursor()
            fts_match_query = self._build_fts_match_query(tokens)
            try:
                cur.execute(
                    """
                    SELECT f.path, f.filename, f.modified, f.size, f.is_dir, bm25(files_fts) AS rank
                    FROM files_fts
                    JOIN files AS f ON f.id = files_fts.rowid
                    WHERE files_fts MATCH ?
                    ORDER BY rank ASC, f.is_dir ASC, f.path ASC
                    LIMIT ?;
                    """,
                    (fts_match_query, max_results),
                )
                _append_rows(cur.fetchall(), "fts")
            except sqlite3.Error:
                pass

            if len(collected) < max_results:
                cur.execute(
                    f"""
                    SELECT path, filename, modified, size, is_dir, 0.0 AS rank
                    FROM files
                    WHERE {where_clause}
                    ORDER BY is_dir ASC, path ASC
                    LIMIT ?;
                    """,
                    (*params, max_results),
                )
                _append_rows(cur.fetchall(), "like")

        return collected[:max_results]

    def regex_search(self, pattern: str, max_results: int) -> List[Dict]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT path, filename, modified, size, is_dir
                FROM files
                WHERE path_lower REGEXP ? OR filename REGEXP ?
                ORDER BY path
                LIMIT ?;
                """,
                (pattern, pattern, max_results),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> Dict:
        if self._conn is None:
            return {}
        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM files;")
            total = cur.fetchone()["c"]
            cur.execute("SELECT value FROM meta WHERE key = ?", ("last_index_time",))
            row = cur.fetchone()
            last_index = float(row["value"]) if row else None

        db_size_mb = 0.0
        if self._db_path.exists():
            db_size_mb = self._db_path.stat().st_size / (1024 * 1024)

        return {
            "total_items": total,
            "last_index": last_index,
            "db_size_mb": round(db_size_mb, 2),
            "db_path": str(self._db_path),
        }

    def close(self) -> None:
        if self._conn is not None:
            with self._db_lock:
                self._conn.close()
            self._conn = None
