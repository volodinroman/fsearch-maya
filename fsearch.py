import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    from maya import OpenMaya  # type: ignore

    MAYA_AVAILABLE = True
except Exception:
    MAYA_AVAILABLE = False


class FileSearcher:
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: Optional[str] = None):
        if getattr(self, "_initialized", False):
            return

        self._project_dir = Path(__file__).resolve().parent
        print(self._project_dir)
        self._data_dir = self._project_dir / ".data"
        self._config_path = Path(config_path) if config_path else self._data_dir / "config.json"
        self._db_lock = threading.Lock()
        self._conn = None
        self.config = {}

        self.refresh_config()
        self._init_db()
        self._initialized = True

    def _log(self, message: str, level: str = "info") -> None:
        if MAYA_AVAILABLE:
            if level == "error":
                OpenMaya.MGlobal.displayError(f"[FileSearcher] {message}")
            elif level == "warning":
                OpenMaya.MGlobal.displayWarning(f"[FileSearcher] {message}")
            else:
                OpenMaya.MGlobal.displayInfo(f"[FileSearcher] {message}")
            return
        print(f"[{level.upper()}] [FileSearcher] {message}")

    def _default_config(self) -> Dict:
        return {
            "roots": [],
            "file_extensions": [".ma", ".mb"],
            "auto_rebuild_on_launch": False,
            "use_custom_font": True,
            "font_size": 10,
            "remember_last_search": True,
            "last_search_query": "",
            "window_size": {"width": 1400, "height": 800},
            "use_search_debounce": True,
            "search_debounce_ms": 200,
            "include_folders": False,
            "max_results": 200,
            "db_path": "fsearch.db",
        }

    def _load_config(self, path: Path) -> Dict:
        if not path.exists():
            cfg = self._default_config()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            return cfg

        try:
            with path.open("r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            self._log(f"Failed to read config, using defaults: {exc}", level="error")
            return self._default_config()

        defaults = self._default_config()
        defaults.update(cfg if isinstance(cfg, dict) else {})
        if "auto_rebuild_on_launch" not in defaults:
            defaults["auto_rebuild_on_launch"] = False
        if isinstance(cfg, dict) and "index_on_import" in cfg and "auto_rebuild_on_launch" not in cfg:
            defaults["auto_rebuild_on_launch"] = bool(cfg.get("index_on_import", False))
        return defaults

    def refresh_config(self) -> None:
        self.config = self._load_config(self._config_path)
        db_name = self.config.get("db_path", "maya_project_index.db")
        db_path = Path(db_name)
        if db_path.is_absolute():
            self._db_path = db_path
        else:
            self._db_path = self._data_dir / db_path.name

    def _init_db(self) -> None:
        with self._db_lock:
            if self._conn is not None:
                return

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.create_function("REGEXP", 2, self._regexp)

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

    def _normalized_extensions(self) -> List[str]:
        exts = self.config.get("file_extensions", [])
        normalized = []
        for ext in exts:
            ext = str(ext).strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = f".{ext}"
            normalized.append(ext)
        return normalized

    def _iter_files(self, roots: Iterable[str], extensions: List[str], include_folders: bool):
        extension_set = set(extensions)
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.exists() or not root_path.is_dir():
                self._log(f"Skipping invalid root: {root_path}", level="warning")
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

    def rebuild_index(self, show_progress: bool = True, callback=None) -> None:
        roots = self.config.get("roots", [])
        extensions = self._normalized_extensions()
        include_folders = bool(self.config.get("include_folders", False))
        start = time.time()

        if show_progress:
            self._log("Starting index rebuild...")

        with self._db_lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM files;")

            count = 0
            batch = []
            batch_size = 1000

            for row in self._iter_files(roots, extensions, include_folders):
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
        if show_progress:
            self._log(f"Rebuilt index with {count} items in {duration:.2f}s.")
        if callback:
            callback(f"Done. {count} items in {duration:.2f}s.")

    def _tokens_from_text(self, text: str) -> List[str]:
        tokens = []
        for token in text.split():
            token = token.strip().lower()
            if token:
                tokens.append(token)
        return tokens

    def _build_fts_match_query(self, tokens: List[str]) -> str:
        # Quote each token to avoid FTS syntax issues on special characters.
        safe_tokens = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
        return " AND ".join(safe_tokens)

    def search(self, query: str, limit: Optional[int] = None) -> List[Dict]:
        tokens = self._tokens_from_text(query)
        if not tokens:
            return []

        max_results = int(limit or self.config.get("max_results", 200))
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
                # Fallback to LIKE-only behavior if MATCH query fails.
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

    def regex_search(self, pattern: str, limit: Optional[int] = None) -> List[Dict]:
        max_results = int(limit or self.config.get("max_results", 200))
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
