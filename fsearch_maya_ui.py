"""Maya Qt dialog for searching indexed files and managing bookmarks."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import maya.OpenMayaUI as omui  # type: ignore
import maya.cmds as cmds  # type: ignore
from fsearch import FileSearcher
from fsearch_ui_common import (
    ITEM_FILE,
    ITEM_FOLDER,
    MAYA_EXTENSIONS,
    ROLE_PATH,
    ROLE_TYPE,
    TREE_STYLE,
    WINDOW_OBJECT_NAME,
    RowHeightDelegate,
)

_THIS_DIR = Path(__file__).resolve().parent

QT_API = None
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance

    QT_API = "PySide6"
except Exception:
    from PySide2 import QtCore, QtGui, QtWidgets
    from shiboken2 import wrapInstance

    QT_API = "PySide2"

def _menu_exec(menu, pos):
    """Qt5/Qt6-compatible context menu execution."""
    if hasattr(menu, "exec"):
        return menu.exec(pos)
    return menu.exec_(pos)


def _app_exec(app):
    """Qt5/Qt6-compatible application loop execution."""
    if hasattr(app, "exec"):
        return app.exec()
    return app.exec_()


def maya_main_window():
    """Return Maya main window wrapped as QWidget parent."""
    ptr = omui.MQtUtil.mainWindow()
    if ptr is None:
        return None
    return wrapInstance(int(ptr), QtWidgets.QWidget)


class FileSearcherUI(QtWidgets.QDialog):
    """Main dialog: search results, bookmarks, and settings tabs."""

    def __init__(self, parent=None):
        super().__init__(parent or maya_main_window())
        self.setObjectName(WINDOW_OBJECT_NAME)
        self.setWindowTitle("FSearch")
        self.setMinimumSize(600, 600)
        self._default_font = QtGui.QFont(self.font())
        icon_path = _THIS_DIR / "assets" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QtGui.QIcon(str(icon_path)))

        self.searcher = FileSearcher()
        self._config_path = Path(self.searcher._config_path)
        self._bookmarks = []
        self._is_loading_settings = False
        self._current_search_tokens = []
        self._search_debounce_timer = QtCore.QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(100)
        self._search_debounce_timer.timeout.connect(self._run_search)
        self._window_state_timer = QtCore.QTimer(self)
        self._window_state_timer.setSingleShot(True)
        self._window_state_timer.setInterval(300)
        self._window_state_timer.timeout.connect(self._persist_window_size)

        self._build_ui()
        self._connect_signals()
        self._load_settings()
        self._run_auto_rebuild_on_launch_if_enabled()
        self._refresh_stats()

    def _load_custom_font(self, font_size):
        """Load bundled custom font at requested size."""
        font_path = _THIS_DIR / "assets" / "JetBrainsMono-Regular.ttf"
        if not font_path.exists():
            return None
        font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
        if font_id < 0:
            return None
        families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
        if not families:
            return None
        return QtGui.QFont(families[0], int(font_size))

    def _apply_font_settings(self, use_custom_font, font_size):
        """Apply either bundled font or default UI font."""
        if use_custom_font:
            custom_font = self._load_custom_font(font_size)
            if custom_font is not None:
                self._ui_font = custom_font
            else:
                self._ui_font = QtGui.QFont(self._default_font)
        else:
            self._ui_font = QtGui.QFont(self._default_font)

        self.setFont(self._ui_font)
        if hasattr(self, "results_tree"):
            self.results_tree.setFont(self._ui_font)
            self.results_tree.header().setFont(self._ui_font)
        if hasattr(self, "bookmarks_tree"):
            self.bookmarks_tree.setFont(self._ui_font)
            self.bookmarks_tree.header().setFont(self._ui_font)

    def _build_ui(self):
        """Build the tab container and tab contents."""
        root = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs)

        self.search_tab = QtWidgets.QWidget()
        self.bookmarks_tab = QtWidgets.QWidget()
        self.settings_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.search_tab, "Search")
        self.tabs.addTab(self.bookmarks_tab, "Bookmarks")
        self.tabs.addTab(self.settings_tab, "Settings")

        self._build_search_tab()
        self._build_bookmarks_tab()
        self._build_settings_tab()

    def _build_search_tab(self):
        """Create search input, options, results tree, and status line."""
        layout = QtWidgets.QVBoxLayout(self.search_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search: car front")
        layout.addWidget(self.search_edit)

        search_opts = QtWidgets.QHBoxLayout()
        self.regex_check = QtWidgets.QCheckBox("Regex")
        search_opts.addWidget(self.regex_check)
        search_opts.addStretch(1)
        layout.addLayout(search_opts)

        self.results_tree = QtWidgets.QTreeWidget()
        self.results_tree.setHeaderLabel("Folder / Full Path")
        self.results_tree.setRootIsDecorated(True)
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setStyleSheet(TREE_STYLE)
        self.results_tree.setItemDelegate(
            RowHeightDelegate(24, self.results_tree, tokens_getter=lambda: self._current_search_tokens)
        )
        self.results_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        layout.addWidget(self.results_tree, 1)

        self.search_status = QtWidgets.QLabel("Type to search.")
        self.search_status.setObjectName("Caption")
        layout.addWidget(self.search_status)

    def _build_bookmarks_tab(self):
        """Create bookmarks list, actions, and status line."""
        layout = QtWidgets.QVBoxLayout(self.bookmarks_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.bookmarks_tree = QtWidgets.QTreeWidget()
        self.bookmarks_tree.setHeaderLabel("Path")
        self.bookmarks_tree.setAlternatingRowColors(True)
        self.bookmarks_tree.setRootIsDecorated(False)
        self.bookmarks_tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.bookmarks_tree.setStyleSheet(TREE_STYLE)
        self.bookmarks_tree.setItemDelegate(RowHeightDelegate(24, self.bookmarks_tree))
        self.bookmarks_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        layout.addWidget(self.bookmarks_tree, 1)

        bookmarks_btn_row = QtWidgets.QHBoxLayout()
        self.delete_all_bookmarks_btn = QtWidgets.QPushButton("Delete All Bookmarks")
        bookmarks_btn_row.addWidget(self.delete_all_bookmarks_btn)
        bookmarks_btn_row.addStretch(1)
        layout.addLayout(bookmarks_btn_row)

        self.bookmarks_status = QtWidgets.QLabel("Bookmarks: 0")
        self.bookmarks_status.setObjectName("Caption")
        layout.addWidget(self.bookmarks_status)

    def _build_settings_tab(self):
        """Create settings controls for indexing, search, and UI preferences."""
        layout = QtWidgets.QVBoxLayout(self.settings_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        roots_caption = QtWidgets.QLabel("Roots")
        roots_caption.setObjectName("Caption")
        layout.addWidget(roots_caption)

        self.roots_list = QtWidgets.QListWidget()
        layout.addWidget(self.roots_list, 1)

        roots_btn_row = QtWidgets.QHBoxLayout()
        self.add_root_btn = QtWidgets.QPushButton("Add Root")
        self.remove_root_btn = QtWidgets.QPushButton("Remove Selected")
        roots_btn_row.addWidget(self.add_root_btn)
        roots_btn_row.addWidget(self.remove_root_btn)
        roots_btn_row.addStretch(1)
        layout.addLayout(roots_btn_row)

        form = QtWidgets.QFormLayout()
        self.extensions_edit = QtWidgets.QLineEdit()
        self.extensions_edit.setPlaceholderText(".ma, .mb")
        self.include_folders_check = QtWidgets.QCheckBox("Include folders in index")
        self.auto_rebuild_on_launch_check = QtWidgets.QCheckBox("Auto-rebuilding on launch")
        self.regex_case_sensitive_check = QtWidgets.QCheckBox("Case Sensitive (Regex)")
        self.remember_last_search_check = QtWidgets.QCheckBox("Remember last search")
        self.use_search_debounce_check = QtWidgets.QCheckBox("Use Search Debounce")
        self.search_debounce_ms_spin = QtWidgets.QSpinBox()
        self.search_debounce_ms_spin.setRange(0, 2000)
        self.search_debounce_ms_spin.setSingleStep(25)
        self.use_custom_font_check = QtWidgets.QCheckBox("Use Custom Font")
        self.font_size_spin = QtWidgets.QSpinBox()
        self.font_size_spin.setRange(6, 36)
        self.max_results_spin = QtWidgets.QSpinBox()
        self.max_results_spin.setRange(1, 5000)
        self.db_path_edit = QtWidgets.QLineEdit()
        self.db_path_edit.setPlaceholderText("maya_project_index.db")
        form.addRow("Extensions", self.extensions_edit)
        form.addRow("Max results", self.max_results_spin)
        form.addRow("Font Size", self.font_size_spin)
        form.addRow("Debounce (ms)", self.search_debounce_ms_spin)
        form.addRow("DB path", self.db_path_edit)
        form.addRow("", self.include_folders_check)
        form.addRow("", self.auto_rebuild_on_launch_check)
        form.addRow("", self.regex_case_sensitive_check)
        form.addRow("", self.remember_last_search_check)
        form.addRow("", self.use_search_debounce_check)
        form.addRow("", self.use_custom_font_check)
        layout.addLayout(form)

        btn_row = QtWidgets.QHBoxLayout()
        self.save_settings_btn = QtWidgets.QPushButton("Save Settings")
        self.rebuild_btn = QtWidgets.QPushButton("Rebuild Index")
        btn_row.addWidget(self.save_settings_btn)
        btn_row.addWidget(self.rebuild_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.settings_status = QtWidgets.QLabel("")
        self.settings_status.setObjectName("Caption")
        layout.addWidget(self.settings_status)

    def _connect_signals(self):
        """Wire Qt signals to handlers."""
        self.search_edit.textChanged.connect(self._schedule_search)
        self.results_tree.customContextMenuRequested.connect(self._open_context_menu)
        self.results_tree.itemDoubleClicked.connect(self._on_item_double_click)
        self.bookmarks_tree.customContextMenuRequested.connect(self._open_bookmarks_context_menu)
        self.bookmarks_tree.itemDoubleClicked.connect(self._on_bookmark_item_double_click)

        self.add_root_btn.clicked.connect(self._add_root)
        self.remove_root_btn.clicked.connect(self._remove_selected_roots)
        self.save_settings_btn.clicked.connect(self._save_settings)
        self.rebuild_btn.clicked.connect(self._rebuild_index)
        self.delete_all_bookmarks_btn.clicked.connect(self._delete_all_bookmarks)
        self.remember_last_search_check.toggled.connect(self._on_remember_last_search_changed)
        self.use_search_debounce_check.toggled.connect(self._on_debounce_settings_changed)
        self.search_debounce_ms_spin.valueChanged.connect(self._on_debounce_settings_changed)
        self.use_custom_font_check.toggled.connect(self._on_font_settings_changed)
        self.font_size_spin.valueChanged.connect(self._on_font_settings_changed)

        self.delete_bookmarks_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence.Delete, self.bookmarks_tree)
        self.delete_bookmarks_shortcut.activated.connect(self._remove_selected_bookmarks)

    def _load_settings(self):
        """Load settings from config and apply them to widgets."""
        self._is_loading_settings = True
        try:
            self.searcher.refresh_config()
            cfg = self.searcher.config

            self.roots_list.clear()
            for root in cfg.get("roots", []):
                self.roots_list.addItem(str(root))

            self.extensions_edit.setText(", ".join(cfg.get("file_extensions", [])))
            self.include_folders_check.setChecked(bool(cfg.get("include_folders", False)))
            self.auto_rebuild_on_launch_check.setChecked(
                bool(cfg.get("auto_rebuild_on_launch", cfg.get("index_on_import", False)))
            )
            self.regex_case_sensitive_check.setChecked(bool(cfg.get("regex_case_sensitive", False)))
            self.remember_last_search_check.setChecked(bool(cfg.get("remember_last_search", True)))
            self.use_search_debounce_check.setChecked(bool(cfg.get("use_search_debounce", True)))
            self.search_debounce_ms_spin.setValue(int(cfg.get("search_debounce_ms", 200)))
            self.search_debounce_ms_spin.setEnabled(self.use_search_debounce_check.isChecked())
            self._search_debounce_timer.setInterval(max(0, int(self.search_debounce_ms_spin.value())))
            self.use_custom_font_check.setChecked(bool(cfg.get("use_custom_font", True)))
            self.font_size_spin.setValue(int(cfg.get("font_size", 10)))
            self.font_size_spin.setEnabled(self.use_custom_font_check.isChecked())
            self.max_results_spin.setValue(int(cfg.get("max_results", 200)))
            self.db_path_edit.setText(str(cfg.get("db_path", "maya_project_index.db")))
            window_size = cfg.get("window_size", {})
            if isinstance(window_size, dict):
                width = int(window_size.get("width", self.width()))
                height = int(window_size.get("height", self.height()))
                self.resize(max(self.minimumWidth(), width), max(self.minimumHeight(), height))
            self._bookmarks = self._normalize_bookmarks(cfg.get("bookmarks", []))
            self._populate_bookmarks()
            self._apply_font_settings(self.use_custom_font_check.isChecked(), self.font_size_spin.value())
            last_query = str(cfg.get("last_search_query", "")).strip()
            if self.remember_last_search_check.isChecked() and last_query:
                self.search_edit.setText(last_query)
        finally:
            self._is_loading_settings = False

    def _refresh_stats(self):
        """Set default status text for search panel."""
        self.search_status.setText("Type to search.")

    def _schedule_search(self):
        """Run search immediately or through debounce timer."""
        if not self.use_search_debounce_check.isChecked():
            self._run_search()
            return
        self._search_debounce_timer.setInterval(max(0, int(self.search_debounce_ms_spin.value())))
        self._search_debounce_timer.start()

    def _run_search(self):
        """Execute search and update results tree and metrics."""
        query = self.search_edit.text().strip()
        self._persist_last_search_query(query)
        self._current_search_tokens = self._tokens_from_query(query)
        if not query:
            self.results_tree.clear()
            self.search_status.setText("Type to search.")
            return

        started_at = time.perf_counter()
        try:
            if self.regex_check.isChecked():
                results = self.searcher.regex_search(query)
                if self.regex_case_sensitive_check.isChecked():
                    # case-sensitive filtering when regex mode requests it
                    import re

                    rx = re.compile(query)
                    results = [r for r in results if rx.search(r["path"]) or rx.search(r["filename"])]
            else:
                results = self.searcher.search(query)
        except Exception as exc:
            self.search_status.setText(f"Search failed: {exc}")
            return

        self._populate_tree(results)
        folders_count = sum(1 for row in results if bool(row.get("is_dir", 0)))
        files_count = len(results) - folders_count
        fts_rows = sum(1 for row in results if str(row.get("search_source", "")) == "fts")
        fts_percent = (fts_rows / len(results) * 100.0) if results else 0.0
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        self.search_status.setText(
            f"Found: files {files_count}, folders {folders_count} | FTS5: {fts_percent:.1f}% ({fts_rows}/{len(results)}) | {elapsed_ms:.1f} ms"
        )

    def _populate_tree(self, results):
        """Render grouped search results into a folder/file tree."""
        self.results_tree.clear()
        grouped = {}
        folder_only = set()

        for row in results:
            path = str(row.get("path", ""))
            if not path:
                continue
            normalized = path.replace("\\", "/")
            is_dir = bool(row.get("is_dir", 0))
            if is_dir:
                folder_only.add(normalized)
                continue
            parent = str(Path(normalized).parent).replace("\\", "/")
            grouped.setdefault(parent, []).append(normalized)

        top_folders = sorted(set(grouped.keys()) | folder_only)
        for folder in top_folders:
            folder_item = QtWidgets.QTreeWidgetItem([folder])
            folder_item.setData(0, ROLE_TYPE, ITEM_FOLDER)
            folder_item.setData(0, ROLE_PATH, folder)
            self.results_tree.addTopLevelItem(folder_item)

            children = sorted(set(grouped.get(folder, [])))
            for full_path in children:
                child = QtWidgets.QTreeWidgetItem([full_path])
                child.setData(0, ROLE_TYPE, ITEM_FILE)
                child.setData(0, ROLE_PATH, full_path)
                folder_item.addChild(child)
            if children:
                folder_item.setExpanded(True)

    def _tokens_from_query(self, text):
        """Tokenize user search query by spaces."""
        tokens = []
        for token in str(text).split():
            token = token.strip().lower()
            if token:
                tokens.append(token)
        return tokens

    def _normalize_bookmarks(self, raw_bookmarks):
        """Normalize bookmarks and deduplicate by type/path."""
        normalized_bookmarks = []
        seen = set()
        for raw in raw_bookmarks if isinstance(raw_bookmarks, list) else []:
            if isinstance(raw, dict):
                path = str(raw.get("path", "")).strip()
                item_type = str(raw.get("type", "")).strip().lower()
            else:
                path = str(raw).strip()
                item_type = ITEM_FOLDER if Path(path).suffix == "" else ITEM_FILE

            if not path:
                continue
            if item_type not in (ITEM_FILE, ITEM_FOLDER):
                item_type = ITEM_FOLDER if Path(path).suffix == "" else ITEM_FILE

            normalized_path = path.replace("\\", "/")
            key = (item_type, normalized_path.lower())
            if key in seen:
                continue
            seen.add(key)
            normalized_bookmarks.append({"path": normalized_path, "type": item_type})
        return normalized_bookmarks

    def _populate_bookmarks(self):
        """Render bookmarks list from in-memory collection."""
        self.bookmarks_tree.clear()
        for bookmark in self._bookmarks:
            path = bookmark.get("path")
            item_type = bookmark.get("type")
            if not path or item_type not in (ITEM_FILE, ITEM_FOLDER):
                continue
            item = QtWidgets.QTreeWidgetItem([path])
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_TYPE, item_type)
            self.bookmarks_tree.addTopLevelItem(item)
        self.bookmarks_status.setText(f"Bookmarks: {len(self._bookmarks)}")

    def _is_maya_file(self, path):
        return Path(str(path)).suffix.lower() in MAYA_EXTENSIONS

    def _add_bookmark(self, path, item_type):
        """Add new bookmark entry unless it already exists."""
        normalized_path = str(path).replace("\\", "/")
        key = (item_type, normalized_path.lower())
        existing = {(b.get("type"), str(b.get("path", "")).lower()) for b in self._bookmarks}
        if key in existing:
            self.bookmarks_status.setText("Bookmark already exists.")
            return
        self._bookmarks.append({"path": normalized_path, "type": item_type})
        self._populate_bookmarks()
        self._persist_bookmarks()

    def _remove_bookmark(self, path, item_type):
        normalized_path = str(path).replace("\\", "/")
        target = (item_type, normalized_path.lower())
        self._bookmarks = [
            b
            for b in self._bookmarks
            if (b.get("type"), str(b.get("path", "")).lower()) != target
        ]
        self._populate_bookmarks()
        self._persist_bookmarks()

    def _remove_selected_bookmarks(self):
        """Remove all currently selected bookmark rows."""
        selected = self.bookmarks_tree.selectedItems()
        if not selected:
            return
        targets = {
            (
                item.data(0, ROLE_TYPE),
                str(item.data(0, ROLE_PATH) or "").replace("\\", "/").lower(),
            )
            for item in selected
            if item.data(0, ROLE_TYPE) in (ITEM_FILE, ITEM_FOLDER) and item.data(0, ROLE_PATH)
        }
        if not targets:
            return
        self._bookmarks = [
            b
            for b in self._bookmarks
            if (b.get("type"), str(b.get("path", "")).replace("\\", "/").lower()) not in targets
        ]
        self._populate_bookmarks()
        self._persist_bookmarks()

    def _delete_all_bookmarks(self):
        """Remove all bookmarks."""
        if not self._bookmarks:
            return
        self._bookmarks = []
        self._populate_bookmarks()
        self._persist_bookmarks()

    def _open_context_menu(self, pos):
        """Open context menu for search results tree items."""
        item = self.results_tree.itemAt(pos)
        if item is None:
            return

        item_type = item.data(0, ROLE_TYPE)
        item_path = item.data(0, ROLE_PATH)
        if not item_path:
            return

        menu = QtWidgets.QMenu(self)
        if item_type == ITEM_FOLDER:
            copy_action = menu.addAction("Copy Path")
            reveal_action = menu.addAction("Reveal in Explorer")
            bookmark_action = menu.addAction("Create Bookmark")
            chosen = _menu_exec(menu, self.results_tree.viewport().mapToGlobal(pos))
            if chosen == copy_action:
                QtWidgets.QApplication.clipboard().setText(item_path)
            elif chosen == reveal_action:
                self._reveal_in_explorer(item_path, is_file=False)
            elif chosen == bookmark_action:
                self._add_bookmark(item_path, ITEM_FOLDER)
        elif item_type == ITEM_FILE:
            open_action = None
            if self._is_maya_file(item_path):
                open_action = menu.addAction("Open File")
            copy_action = menu.addAction("Copy Path")
            open_folder_action = menu.addAction("Open Containing Folder")
            bookmark_action = menu.addAction("Create Bookmark")
            chosen = _menu_exec(menu, self.results_tree.viewport().mapToGlobal(pos))
            if open_action is not None and chosen == open_action:
                self._open_in_maya(item_path)
            elif chosen == copy_action:
                QtWidgets.QApplication.clipboard().setText(item_path)
            elif chosen == open_folder_action:
                self._open_folder(str(Path(item_path).parent))
            elif chosen == bookmark_action:
                self._add_bookmark(item_path, ITEM_FILE)

    def _open_bookmarks_context_menu(self, pos):
        """Open context menu for single or multi-selected bookmarks."""
        item = self.bookmarks_tree.itemAt(pos)
        if item is None:
            return

        selected_items = self.bookmarks_tree.selectedItems()
        if item not in selected_items:
            # Match common explorer behavior: right-click selects the clicked row.
            self.bookmarks_tree.clearSelection()
            item.setSelected(True)
            self.bookmarks_tree.setCurrentItem(item)
            selected_items = [item]
        valid_selected = [
            it
            for it in selected_items
            if it.data(0, ROLE_TYPE) in (ITEM_FILE, ITEM_FOLDER) and it.data(0, ROLE_PATH)
        ]
        if not valid_selected:
            return
        single_item = valid_selected[0] if len(valid_selected) == 1 else None

        menu = QtWidgets.QMenu(self)
        open_folder_action = menu.addAction("Open Containing Folder")
        open_maya_action = None
        if single_item is not None:
            item_type = single_item.data(0, ROLE_TYPE)
            item_path = single_item.data(0, ROLE_PATH)
        else:
            item_type = None
            item_path = None
        if single_item is not None and item_type == ITEM_FILE and self._is_maya_file(item_path):
            open_maya_action = menu.addAction("Open in Maya")
        remove_action = menu.addAction("Remove Bookmark")

        chosen = _menu_exec(menu, self.bookmarks_tree.viewport().mapToGlobal(pos))
        if chosen == open_folder_action:
            for selected_item in valid_selected:
                selected_type = selected_item.data(0, ROLE_TYPE)
                selected_path = selected_item.data(0, ROLE_PATH)
                if selected_type == ITEM_FILE:
                    self._open_folder(str(Path(selected_path).parent))
                else:
                    self._open_folder(selected_path)
        elif open_maya_action is not None and chosen == open_maya_action:
            self._open_in_maya(item_path)
        elif chosen == remove_action:
            self._remove_selected_bookmarks()

    def _on_item_double_click(self, item, _column):
        if item.data(0, ROLE_TYPE) != ITEM_FILE:
            return
        item_path = item.data(0, ROLE_PATH)
        if self._is_maya_file(item_path):
            self._open_in_maya(item_path)

    def _on_bookmark_item_double_click(self, item, _column):
        if item.data(0, ROLE_TYPE) != ITEM_FILE:
            return
        item_path = item.data(0, ROLE_PATH)
        if self._is_maya_file(item_path):
            self._open_in_maya(item_path)

    def _add_root(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose Root Folder")
        if not folder:
            return
        normalized = folder.replace("\\", "/")
        existing = {self.roots_list.item(i).text() for i in range(self.roots_list.count())}
        if normalized not in existing:
            self.roots_list.addItem(normalized)

    def _remove_selected_roots(self):
        for item in self.roots_list.selectedItems():
            self.roots_list.takeItem(self.roots_list.row(item))

    def _collect_settings(self):
        """Collect current settings into a config payload."""
        roots = [self.roots_list.item(i).text().strip() for i in range(self.roots_list.count())]
        roots = [r for r in roots if r]
        exts = [e.strip() for e in self.extensions_edit.text().split(",") if e.strip()]
        cfg = {
            "roots": roots,
            "file_extensions": exts,
            "include_folders": self.include_folders_check.isChecked(),
            "max_results": int(self.max_results_spin.value()),
            "auto_rebuild_on_launch": self.auto_rebuild_on_launch_check.isChecked(),
            "regex_case_sensitive": self.regex_case_sensitive_check.isChecked(),
            "remember_last_search": self.remember_last_search_check.isChecked(),
            "use_search_debounce": self.use_search_debounce_check.isChecked(),
            "search_debounce_ms": int(self.search_debounce_ms_spin.value()),
            "use_custom_font": self.use_custom_font_check.isChecked(),
            "font_size": int(self.font_size_spin.value()),
            "db_path": self.db_path_edit.text().strip() or "maya_project_index.db",
            "bookmarks": self._bookmarks,
            "last_search_query": self.search_edit.text().strip() if self.remember_last_search_check.isChecked() else "",
            "window_size": {"width": int(self.width()), "height": int(self.height())},
        }
        return cfg

    def _on_remember_last_search_changed(self, *_args):
        """Persist remember-last-search toggle and value changes."""
        if self._is_loading_settings:
            return
        if self.remember_last_search_check.isChecked():
            self._update_config_fields(
                {
                    "remember_last_search": True,
                    "last_search_query": self.search_edit.text().strip(),
                }
            )
        else:
            self._update_config_fields({"remember_last_search": False, "last_search_query": ""})

    def _on_debounce_settings_changed(self, *_args):
        """Apply debounce controls and persist them."""
        self.search_debounce_ms_spin.setEnabled(self.use_search_debounce_check.isChecked())
        self._search_debounce_timer.setInterval(max(0, int(self.search_debounce_ms_spin.value())))
        if not self._is_loading_settings:
            self._save_settings(silent=True)

    def _on_font_settings_changed(self, *_args):
        """Apply font controls and persist them."""
        self.font_size_spin.setEnabled(self.use_custom_font_check.isChecked())
        self._apply_font_settings(self.use_custom_font_check.isChecked(), self.font_size_spin.value())
        if not self._is_loading_settings:
            self._save_settings(silent=True)

    def _run_auto_rebuild_on_launch_if_enabled(self):
        """Rebuild index at launch when enabled in settings."""
        enabled = bool(self.searcher.config.get("auto_rebuild_on_launch", self.searcher.config.get("index_on_import", False)))
        if not enabled:
            return
        self.settings_status.setText("Auto-rebuilding index on launch...")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.searcher.rebuild_index(show_progress=True)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.settings_status.setText("Auto-rebuild completed.")

    def _save_settings(self, silent=False):
        """Save full settings payload to config."""
        cfg = self._collect_settings()
        self._update_config_fields(cfg)
        if not silent:
            self.settings_status.setText(f"Saved: {self._config_path}")

    def _persist_bookmarks(self):
        """Persist bookmarks only, preserving other config fields."""
        cfg = {"bookmarks": self._bookmarks}
        self._update_config_fields(cfg)

    def _load_config_json(self):
        """Load raw config file contents for merge-safe updates."""
        if self._config_path.exists():
            try:
                raw = json.loads(self._config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _update_config_fields(self, updates):
        """Merge field updates with current + raw config and save."""
        cfg = dict(self.searcher.config) if isinstance(self.searcher.config, dict) else {}
        file_cfg = self._load_config_json()
        # Merge disk file too, so externally added keys are preserved.
        cfg.update(file_cfg)
        cfg.update(updates)
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.searcher.refresh_config()

    def _persist_last_search_query(self, query):
        """Persist latest search text when feature is enabled."""
        if self._is_loading_settings:
            return
        if not self.remember_last_search_check.isChecked():
            return
        current = str(self.searcher.config.get("last_search_query", ""))
        if query == current:
            return
        self._update_config_fields({"last_search_query": query})

    def _persist_window_size(self):
        """Persist window size if it has changed."""
        if self._is_loading_settings:
            return
        size_payload = {"window_size": {"width": int(self.width()), "height": int(self.height())}}
        current = self.searcher.config.get("window_size", {})
        if (
            isinstance(current, dict)
            and int(current.get("width", -1)) == size_payload["window_size"]["width"]
            and int(current.get("height", -1)) == size_payload["window_size"]["height"]
        ):
            return
        self._update_config_fields(size_payload)

    def resizeEvent(self, event):
        """Throttle config writes while user is resizing the window."""
        super().resizeEvent(event)
        if not self._is_loading_settings:
            self._window_state_timer.start()

    def _rebuild_index(self):
        """Save settings, rebuild index, and refresh current search."""
        self._save_settings()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.searcher.rebuild_index(show_progress=True)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._refresh_stats()
        self.settings_status.setText("Index rebuild completed.")
        self._run_search()

    def _open_in_maya(self, file_path):
        """Open Maya scene file (.ma/.mb) using Maya file command."""
        file_path = os.path.normpath(file_path)
        if not self._is_maya_file(file_path):
            return
        try:
            cmds.file(file_path, open=True, force=True)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Open File", f"Failed to open in Maya:\n{exc}")

    def _open_folder(self, folder_path):
        """Open folder in OS file explorer."""
        folder_path = os.path.normpath(folder_path)
        try:
            os.startfile(folder_path)  # type: ignore[attr-defined]
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Open Folder", f"Failed to open folder:\n{exc}")

    def _reveal_in_explorer(self, path, is_file):
        """Reveal file or open folder in explorer."""
        path = os.path.normpath(path)
        try:
            if is_file:
                subprocess.Popen(["explorer", f"/select,{path}"])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Reveal", f"Failed to reveal path:\n{exc}")


def show_file_searcher_ui():
    """Create a fresh UI instance, replacing existing one by object name."""
    try:
        if cmds.window(WINDOW_OBJECT_NAME, exists=True):
            cmds.deleteUI(WINDOW_OBJECT_NAME, window=True)
        elif cmds.control(WINDOW_OBJECT_NAME, exists=True):
            cmds.deleteUI(WINDOW_OBJECT_NAME, control=True)
        elif cmds.workspaceControl(WINDOW_OBJECT_NAME, exists=True):
            cmds.deleteUI(WINDOW_OBJECT_NAME, control=True)
    except Exception:
        pass

    window = FileSearcherUI()
    window.show()
    window.raise_()
    window.activateWindow()
    return window


if __name__ == "__main__":
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = show_file_searcher_ui()
    w.show()
    sys.exit(_app_exec(app))
