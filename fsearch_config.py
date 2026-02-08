"""Configuration storage utilities for the Maya file search tool."""

import json
from pathlib import Path
from typing import Dict, Optional


DEFAULT_CONFIG: Dict = {
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
    "regex_case_sensitive": False,
    "include_folders": False,
    "max_results": 200,
    "db_path": "fsearch.db",
    "bookmarks": [],
}


class SearchConfigStore:
    """Loads, saves, and updates the JSON configuration file."""

    def __init__(self, project_dir: Path, config_path: Optional[str] = None):
        self.project_dir = Path(project_dir)
        self.data_dir = self.project_dir / ".data"
        self.config_path = Path(config_path) if config_path else self.data_dir / "config.json"

    def load(self) -> Dict:
        """Load normalized config merged with defaults."""
        if not self.config_path.exists():
            cfg = dict(DEFAULT_CONFIG)
            self.save(cfg)
            return cfg

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return dict(DEFAULT_CONFIG)

        cfg = dict(DEFAULT_CONFIG)
        if isinstance(raw, dict):
            cfg.update(raw)
            # Backward compatibility with legacy key.
            if "index_on_import" in raw and "auto_rebuild_on_launch" not in raw:
                cfg["auto_rebuild_on_launch"] = bool(raw.get("index_on_import", False))
        return cfg

    def load_raw(self) -> Dict:
        """Load raw config as-is from disk without merging defaults."""
        if not self.config_path.exists():
            return {}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def save(self, config: Dict) -> None:
        """Persist config to disk with stable formatting."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def update_fields(self, updates: Dict) -> Dict:
        """Merge updates into current config and save."""
        cfg = self.load()
        raw = self.load_raw()
        cfg.update(raw)
        cfg.update(updates)
        self.save(cfg)
        return cfg

    def resolve_db_path(self, config: Dict) -> Path:
        """Resolve DB path from config to an absolute path under .data by default."""
        db_name = str(config.get("db_path", "maya_project_index.db"))
        db_path = Path(db_name)
        if db_path.is_absolute():
            return db_path
        return self.data_dir / db_path.name
