# maya-file-search

Fast file indexing/search for Autodesk Maya using SQLite + FTS5.

## Features
- Hybrid search pipeline: `FTS5` first, `LIKE` fallback for partial-path coverage.
- Search metrics in UI (FTS contribution and query time).
- Bookmarks tab with multi-select and keyboard delete.
- Persistent settings in `.data/config.json`.
- Rebuild index with progress feedback and cancel support.

## Run in Maya
```python
import fsearch_maya_ui
fsearch_maya_ui.show_file_searcher_ui()
```

