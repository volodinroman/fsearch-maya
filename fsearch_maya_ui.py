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
        self.setWindowTitle("File Searcher")
        self.setMinimumSize(900, 620)

        self.searcher = FileSearcher()
        self._config_path = Path(self.searcher._config_path)

        self._build_ui()
        self._connect_signals()
        self._load_settings()
        self._refresh_stats()

    def _build_ui(self):
        # self.setStyleSheet(
        #     """
        #     QDialog { background: #f3f4f6; color: #1f2937; }
        #     QTabWidget::pane { border: 1px solid #d1d5db; background: #ffffff; }
        #     QLineEdit, QListWidget, QTreeWidget, QCheckBox, QSpinBox {
        #         font-size: 12px;
        #     }
        #     QLineEdit, QListWidget, QTreeWidget, QSpinBox {
        #         border: 1px solid #d1d5db;
        #         border-radius: 6px;
        #         padding: 6px;
        #         background: #ffffff;
        #     }
        #     QPushButton {
        #         background: #e5e7eb;
        #         border: 1px solid #d1d5db;
        #         border-radius: 6px;
        #         padding: 6px 10px;
        #     }
        #     QPushButton:hover { background: #dbe1e8; }
        #     QLabel#Caption { color: #4b5563; font-size: 11px; }
        #     """
        # )

        root = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs)

        self.search_tab = QtWidgets.QWidget()
        self.settings_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.search_tab, "Search")
        self.tabs.addTab(self.settings_tab, "Settings")

        self._build_search_tab()
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
        self.results_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        layout.addWidget(self.results_tree, 1)

        self.search_status = QtWidgets.QLabel("Type to search.")
        self.search_status.setObjectName("Caption")
        layout.addWidget(self.search_status)

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
        self.index_on_import_check = QtWidgets.QCheckBox("Index on import")
        self.max_results_spin = QtWidgets.QSpinBox()
        self.max_results_spin.setRange(1, 5000)
        self.db_path_edit = QtWidgets.QLineEdit()
        self.db_path_edit.setPlaceholderText("maya_project_index.db")
        form.addRow("Extensions", self.extensions_edit)
        form.addRow("Max results", self.max_results_spin)
        form.addRow("DB path", self.db_path_edit)
        form.addRow("", self.include_folders_check)
        form.addRow("", self.index_on_import_check)
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

        self.add_root_btn.clicked.connect(self._add_root)
        self.remove_root_btn.clicked.connect(self._remove_selected_roots)
        self.save_settings_btn.clicked.connect(self._save_settings)
        self.rebuild_btn.clicked.connect(self._rebuild_index)

    def _load_settings(self):
        self.searcher.refresh_config()
        cfg = self.searcher.config

        self.roots_list.clear()
        for root in cfg.get("roots", []):
            self.roots_list.addItem(str(root))

        self.extensions_edit.setText(", ".join(cfg.get("file_extensions", [])))
        self.include_folders_check.setChecked(bool(cfg.get("include_folders", False)))
        self.index_on_import_check.setChecked(bool(cfg.get("index_on_import", False)))
        self.max_results_spin.setValue(int(cfg.get("max_results", 200)))
        self.db_path_edit.setText(str(cfg.get("db_path", "maya_project_index.db")))

    def _refresh_stats(self):
        stats = self.searcher.get_stats()
        total = stats.get("total_items", 0)
        self.search_status.setText(f"Indexed items: {total}")

    def _run_search(self):
        query = self.search_edit.text().strip()
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
        self.search_status.setText(f"Results: {len(results)}")

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
            chosen = _menu_exec(menu, self.results_tree.viewport().mapToGlobal(pos))
            if chosen == copy_action:
                QtWidgets.QApplication.clipboard().setText(item_path)
            elif chosen == reveal_action:
                self._reveal_in_explorer(item_path, is_file=False)
        elif item_type == ITEM_FILE:
            open_action = menu.addAction("Open File")
            copy_action = menu.addAction("Copy Path")
            reveal_action = menu.addAction("Reveal in Explorer")
            open_folder_action = menu.addAction("Open Containing Folder")
            chosen = _menu_exec(menu, self.results_tree.viewport().mapToGlobal(pos))
            if chosen == open_action:
                self._open_file(item_path)
            elif chosen == copy_action:
                QtWidgets.QApplication.clipboard().setText(item_path)
            elif chosen == reveal_action:
                self._reveal_in_explorer(item_path, is_file=True)
            elif chosen == open_folder_action:
                self._open_folder(str(Path(item_path).parent))

    def _on_item_double_click(self, item, _column):
        if item.data(0, ROLE_TYPE) == ITEM_FILE:
            self._open_file(item.data(0, ROLE_PATH))

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
            "index_on_import": self.index_on_import_check.isChecked(),
            "db_path": self.db_path_edit.text().strip() or "maya_project_index.db",
        }
        return cfg

    def _save_settings(self):
        cfg = self._collect_settings()
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.searcher.refresh_config()
        self.settings_status.setText(f"Saved: {self._config_path}")

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

    def _open_file(self, file_path):
        file_path = os.path.normpath(file_path)
        if MAYA_AVAILABLE:
            try:
                cmds.file(file_path, open=True, force=True)
                return
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Open File", f"Failed to open in Maya:\n{exc}")
                return

        try:
            os.startfile(file_path)  # type: ignore[attr-defined]
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Open File", f"Failed to open file:\n{exc}")

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
