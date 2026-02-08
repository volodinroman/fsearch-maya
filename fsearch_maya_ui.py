import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SEARCHER_PATH = _THIS_DIR / "fsearch.py"
_SEARCHER_SPEC = importlib.util.spec_from_file_location("codex_file_searcher_runtime", str(_SEARCHER_PATH))
if _SEARCHER_SPEC is None or _SEARCHER_SPEC.loader is None:
    raise RuntimeError(f"Could not load file_searcher from {_SEARCHER_PATH}")
_SEARCHER_MODULE = importlib.util.module_from_spec(_SEARCHER_SPEC)
_SEARCHER_SPEC.loader.exec_module(_SEARCHER_MODULE)
FileSearcher = _SEARCHER_MODULE.FileSearcher

try:
    import maya.cmds as cmds  # type: ignore
    import maya.OpenMayaUI as omui  # type: ignore

    MAYA_AVAILABLE = True
except Exception:
    MAYA_AVAILABLE = False

QT_API = None
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance

    QT_API = "PySide6"
except Exception:
    from PySide2 import QtCore, QtGui, QtWidgets
    from shiboken2 import wrapInstance

    QT_API = "PySide2"


ROLE_TYPE = QtCore.Qt.UserRole + 1
ROLE_PATH = QtCore.Qt.UserRole + 2
ITEM_FOLDER = "folder"
ITEM_FILE = "file"
MAYA_EXTENSIONS = {".ma", ".mb"}
TREE_STYLE = """
QTreeWidget {
    background-color: #2b2b2b;
    alternate-background-color: #353535;
}
QTreeWidget::item {
    background-color: #2b2b2b;
    padding-top: 3px;
    padding-bottom: 3px;
}
QTreeWidget::item:alternate {
    background-color: #353535;
    padding-top: 3px;
    padding-bottom: 3px;
}
QTreeWidget::item:selected {
    background-color: #4b6eaf;
    color: #ffffff;
}
"""


class RowHeightDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, row_height, parent=None):
        super().__init__(parent)
        self._row_height = int(row_height)

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        if hint.height() < self._row_height:
            hint.setHeight(self._row_height)
        return hint


def _menu_exec(menu, pos):
    if hasattr(menu, "exec"):
        return menu.exec(pos)
    return menu.exec_(pos)


def _app_exec(app):
    if hasattr(app, "exec"):
        return app.exec()
    return app.exec_()


def maya_main_window():
    if not MAYA_AVAILABLE:
        return None
    ptr = omui.MQtUtil.mainWindow()
    if ptr is None:
        return None
    return wrapInstance(int(ptr), QtWidgets.QWidget)


class FileSearcherUI(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or maya_main_window())
        self.setWindowTitle("FSearch")
        self.setMinimumSize(1400, 800)
        self._default_font = QtGui.QFont(self.font())
        icon_path = _THIS_DIR / "assets" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QtGui.QIcon(str(icon_path)))

        self.searcher = FileSearcher()
        self._config_path = Path(self.searcher._config_path)
        self._bookmarks = []
        self._is_loading_settings = False
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
        layout = QtWidgets.QVBoxLayout(self.search_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search: car front or car, front")
        layout.addWidget(self.search_edit)

        search_opts = QtWidgets.QHBoxLayout()
        self.regex_check = QtWidgets.QCheckBox("Regex")
        self.case_sensitive_check = QtWidgets.QCheckBox("Case Sensitive (Regex)")
        search_opts.addWidget(self.regex_check)
        search_opts.addWidget(self.case_sensitive_check)
        search_opts.addStretch(1)
        layout.addLayout(search_opts)

        self.results_tree = QtWidgets.QTreeWidget()
        self.results_tree.setHeaderLabel("Folder / Full Path")
        self.results_tree.setRootIsDecorated(True)
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setStyleSheet(TREE_STYLE)
        self.results_tree.setItemDelegate(RowHeightDelegate(24, self.results_tree))
        self.results_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        layout.addWidget(self.results_tree, 1)

        self.search_status = QtWidgets.QLabel("Type to search.")
        self.search_status.setObjectName("Caption")
        layout.addWidget(self.search_status)

    def _build_bookmarks_tab(self):
        layout = QtWidgets.QVBoxLayout(self.bookmarks_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.bookmarks_tree = QtWidgets.QTreeWidget()
        self.bookmarks_tree.setHeaderLabel("Path")
        self.bookmarks_tree.setAlternatingRowColors(True)
        self.bookmarks_tree.setRootIsDecorated(False)
        self.bookmarks_tree.setStyleSheet(TREE_STYLE)
        self.bookmarks_tree.setItemDelegate(RowHeightDelegate(24, self.bookmarks_tree))
        self.bookmarks_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        layout.addWidget(self.bookmarks_tree, 1)

        self.bookmarks_status = QtWidgets.QLabel("Bookmarks: 0")
        self.bookmarks_status.setObjectName("Caption")
        layout.addWidget(self.bookmarks_status)

    def _build_settings_tab(self):
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
        self.extensions_edit.setPlaceholderText(".ma, .mb, .abc")
        self.include_folders_check = QtWidgets.QCheckBox("Include folders in index")
        self.auto_rebuild_on_launch_check = QtWidgets.QCheckBox("Auto-rebuilding on launch")
        self.remember_last_search_check = QtWidgets.QCheckBox("Remember last search")
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
        form.addRow("DB path", self.db_path_edit)
        form.addRow("", self.include_folders_check)
        form.addRow("", self.auto_rebuild_on_launch_check)
        form.addRow("", self.remember_last_search_check)
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
        self.search_edit.textChanged.connect(self._run_search)
        self.results_tree.customContextMenuRequested.connect(self._open_context_menu)
        self.results_tree.itemDoubleClicked.connect(self._on_item_double_click)
        self.bookmarks_tree.customContextMenuRequested.connect(self._open_bookmarks_context_menu)
        self.bookmarks_tree.itemDoubleClicked.connect(self._on_bookmark_item_double_click)

        self.add_root_btn.clicked.connect(self._add_root)
        self.remove_root_btn.clicked.connect(self._remove_selected_roots)
        self.save_settings_btn.clicked.connect(self._save_settings)
        self.rebuild_btn.clicked.connect(self._rebuild_index)
        self.remember_last_search_check.toggled.connect(self._on_remember_last_search_changed)
        self.use_custom_font_check.toggled.connect(self._on_font_settings_changed)
        self.font_size_spin.valueChanged.connect(self._on_font_settings_changed)

    def _load_settings(self):
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
            self.remember_last_search_check.setChecked(bool(cfg.get("remember_last_search", True)))
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
        self.search_status.setText("Type to search.")

    def _run_search(self):
        query = self.search_edit.text().strip()
        self._persist_last_search_query(query)
        if not query:
            self.results_tree.clear()
            self.search_status.setText("Type to search.")
            return

        try:
            if self.regex_check.isChecked():
                results = self.searcher.regex_search(query)
                if self.case_sensitive_check.isChecked():
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
        self.search_status.setText(f"Found: files {files_count}, folders {folders_count}")

    def _populate_tree(self, results):
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

    def _normalize_bookmarks(self, raw_bookmarks):
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

    def _open_context_menu(self, pos):
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
            reveal_action = menu.addAction("Reveal in Explorer")
            open_folder_action = menu.addAction("Open Containing Folder")
            bookmark_action = menu.addAction("Create Bookmark")
            chosen = _menu_exec(menu, self.results_tree.viewport().mapToGlobal(pos))
            if open_action is not None and chosen == open_action:
                self._open_in_maya(item_path)
            elif chosen == copy_action:
                QtWidgets.QApplication.clipboard().setText(item_path)
            elif chosen == reveal_action:
                self._reveal_in_explorer(item_path, is_file=True)
            elif chosen == open_folder_action:
                self._open_folder(str(Path(item_path).parent))
            elif chosen == bookmark_action:
                self._add_bookmark(item_path, ITEM_FILE)

    def _open_bookmarks_context_menu(self, pos):
        item = self.bookmarks_tree.itemAt(pos)
        if item is None:
            return

        item_type = item.data(0, ROLE_TYPE)
        item_path = item.data(0, ROLE_PATH)
        if not item_path or item_type not in (ITEM_FILE, ITEM_FOLDER):
            return

        menu = QtWidgets.QMenu(self)
        reveal_action = menu.addAction("Reveal in Explorer")
        open_maya_action = None
        if item_type == ITEM_FILE and self._is_maya_file(item_path):
            open_maya_action = menu.addAction("Open in Maya")
        remove_action = menu.addAction("Remove Bookmark")

        chosen = _menu_exec(menu, self.bookmarks_tree.viewport().mapToGlobal(pos))
        if chosen == reveal_action:
            self._reveal_in_explorer(item_path, is_file=(item_type == ITEM_FILE))
        elif open_maya_action is not None and chosen == open_maya_action:
            self._open_in_maya(item_path)
        elif chosen == remove_action:
            self._remove_bookmark(item_path, item_type)

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
        roots = [self.roots_list.item(i).text().strip() for i in range(self.roots_list.count())]
        roots = [r for r in roots if r]
        exts = [e.strip() for e in self.extensions_edit.text().split(",") if e.strip()]
        cfg = {
            "roots": roots,
            "file_extensions": exts,
            "include_folders": self.include_folders_check.isChecked(),
            "max_results": int(self.max_results_spin.value()),
            "auto_rebuild_on_launch": self.auto_rebuild_on_launch_check.isChecked(),
            "remember_last_search": self.remember_last_search_check.isChecked(),
            "use_custom_font": self.use_custom_font_check.isChecked(),
            "font_size": int(self.font_size_spin.value()),
            "db_path": self.db_path_edit.text().strip() or "maya_project_index.db",
            "bookmarks": self._bookmarks,
            "last_search_query": self.search_edit.text().strip() if self.remember_last_search_check.isChecked() else "",
            "window_size": {"width": int(self.width()), "height": int(self.height())},
        }
        return cfg

    def _on_remember_last_search_changed(self, *_args):
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

    def _on_font_settings_changed(self, *_args):
        self.font_size_spin.setEnabled(self.use_custom_font_check.isChecked())
        self._apply_font_settings(self.use_custom_font_check.isChecked(), self.font_size_spin.value())
        if not self._is_loading_settings:
            self._save_settings(silent=True)

    def _run_auto_rebuild_on_launch_if_enabled(self):
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
        cfg = self._collect_settings()
        self._update_config_fields(cfg)
        if not silent:
            self.settings_status.setText(f"Saved: {self._config_path}")

    def _persist_bookmarks(self):
        cfg = {"bookmarks": self._bookmarks}
        self._update_config_fields(cfg)

    def _load_config_json(self):
        if self._config_path.exists():
            try:
                raw = json.loads(self._config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _update_config_fields(self, updates):
        cfg = dict(self.searcher.config) if isinstance(self.searcher.config, dict) else {}
        file_cfg = self._load_config_json()
        cfg.update(file_cfg)
        cfg.update(updates)
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.searcher.refresh_config()

    def _persist_last_search_query(self, query):
        if self._is_loading_settings:
            return
        if not self.remember_last_search_check.isChecked():
            return
        current = str(self.searcher.config.get("last_search_query", ""))
        if query == current:
            return
        self._update_config_fields({"last_search_query": query})

    def _persist_window_size(self):
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
        super().resizeEvent(event)
        if not self._is_loading_settings:
            self._window_state_timer.start()

    def _rebuild_index(self):
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
        file_path = os.path.normpath(file_path)
        if not self._is_maya_file(file_path):
            return
        if MAYA_AVAILABLE:
            try:
                cmds.file(file_path, open=True, force=True)
                return
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Open File", f"Failed to open in Maya:\n{exc}")
                return
        QtWidgets.QMessageBox.warning(self, "Open in Maya", "Maya API is not available in this session.")

    def _open_folder(self, folder_path):
        folder_path = os.path.normpath(folder_path)
        try:
            os.startfile(folder_path)  # type: ignore[attr-defined]
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Open Folder", f"Failed to open folder:\n{exc}")

    def _reveal_in_explorer(self, path, is_file):
        path = os.path.normpath(path)
        try:
            if is_file:
                subprocess.Popen(["explorer", f"/select,{path}"])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Reveal", f"Failed to reveal path:\n{exc}")


_WINDOW_INSTANCE = None


def show_file_searcher_ui():
    global _WINDOW_INSTANCE
    if _WINDOW_INSTANCE is None:
        _WINDOW_INSTANCE = FileSearcherUI()
    _WINDOW_INSTANCE.show()
    _WINDOW_INSTANCE.raise_()
    _WINDOW_INSTANCE.activateWindow()
    return _WINDOW_INSTANCE


if __name__ == "__main__":
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = show_file_searcher_ui()
    w.show()
    sys.exit(_app_exec(app))
