from pathlib import Path
from typing import Dict, List, Optional

from fsearch_config import SearchConfigStore
from fsearch_index import FileIndexRepository

try:
    from maya import OpenMaya  # type: ignore
except Exception:
    OpenMaya = None


class FileSearcher:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: Optional[str] = None):
        if getattr(self, "_initialized", False):
            return

        self._project_dir = Path(__file__).resolve().parent
        self._config_store = SearchConfigStore(self._project_dir, config_path=config_path)
        self._config_path = self._config_store.config_path

        self.config: Dict = self._config_store.load()
        db_path = self._config_store.resolve_db_path(self.config)
        self._index = FileIndexRepository(db_path)
        self._initialized = True

    def _log(self, message: str, level: str = "info") -> None:
        if OpenMaya is not None:
            if level == "error":
                OpenMaya.MGlobal.displayError(f"[FileSearcher] {message}")
            elif level == "warning":
                OpenMaya.MGlobal.displayWarning(f"[FileSearcher] {message}")
            else:
                OpenMaya.MGlobal.displayInfo(f"[FileSearcher] {message}")
            return
        print(f"[{level.upper()}] [FileSearcher] {message}")

    def refresh_config(self) -> None:
        self.config = self._config_store.load()
        db_path = self._config_store.resolve_db_path(self.config)
        self._index.set_db_path(db_path)

    @property
    def is_indexed(self) -> bool:
        return self._index.is_indexed

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

    def rebuild_index(self, show_progress: bool = True, callback=None) -> None:
        roots = self.config.get("roots", [])
        extensions = self._normalized_extensions()
        include_folders = bool(self.config.get("include_folders", False))
        self._index.rebuild_index(
            roots=roots,
            extensions=extensions,
            include_folders=include_folders,
            show_progress=show_progress,
            callback=callback,
            logger=self._log,
        )

    def search(self, query: str, limit: Optional[int] = None) -> List[Dict]:
        max_results = int(limit or self.config.get("max_results", 200))
        return self._index.search(query=query, max_results=max_results)

    def regex_search(self, pattern: str, limit: Optional[int] = None) -> List[Dict]:
        max_results = int(limit or self.config.get("max_results", 200))
        return self._index.regex_search(pattern=pattern, max_results=max_results)

    def get_stats(self) -> Dict:
        return self._index.get_stats()

    def close(self) -> None:
        self._index.close()
