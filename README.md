# maya-file-search

Fast file indexing/search for Autodesk Maya using SQLite + FTS5.

## Features
- Hybrid search pipeline: `FTS5` first, `LIKE` fallback for partial-path coverage.
- Search metrics in UI (FTS contribution and query time).
- Bookmarks tab with multi-select and keyboard delete.
- Persistent settings in `.data/config.json`.
- Rebuild index with progress feedback and cancel support.

## Requirements
- Autodesk Maya (PySide2/PySide6 depending on Maya version)
- Python runtime bundled with Maya

## Run in Maya
```python
import fsearch_maya_ui
fsearch_maya_ui.show_file_searcher_ui()
```

## Development
Project metadata and lint/format tool config are in `pyproject.toml`.

Recommended commands:
```bash
python -m py_compile fsearch.py fsearch_config.py fsearch_index.py fsearch_ui_common.py fsearch_ui_views.py fsearch_maya_ui.py
ruff check .
```

## Architecture
See `ARCHITECTURE.md` for module-level design and data flow.
