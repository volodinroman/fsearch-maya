"""Shared UI constants and delegates for the Maya file search interface."""

from typing import Callable, Iterable, List, Optional

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except Exception:
    from PySide2 import QtCore, QtGui, QtWidgets


ROLE_TYPE = QtCore.Qt.UserRole + 1
ROLE_PATH = QtCore.Qt.UserRole + 2
ITEM_FOLDER = "folder"
ITEM_FILE = "file"
MAYA_EXTENSIONS = {".ma", ".mb"}
WINDOW_OBJECT_NAME = "fsearchMayaUI"

TREE_STYLE = """
QTreeWidget {
    background-color: #2b2b2b;
    alternate-background-color: #353535;
    outline: none;
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
    background-color: #5285a6;
    color: #ffffff;
}
QTreeWidget::item:focus,
QTreeView::item:focus {
    outline: none;
    border: none;
}
"""


class RowHeightDelegate(QtWidgets.QStyledItemDelegate):
    """Tree item delegate with fixed row height and token highlight rendering."""

    def __init__(
        self,
        row_height: int,
        parent=None,
        tokens_getter: Optional[Callable[[], Iterable[str]]] = None,
        highlight_color: str = "#F6673B",
    ):
        super().__init__(parent)
        self._row_height = int(row_height)
        self._tokens_getter = tokens_getter
        self._highlight_color = QtGui.QColor(highlight_color)

    def sizeHint(self, option, index):
        """Ensure a minimum row height for denser tree readability."""
        hint = super().sizeHint(option, index)
        if hint.height() < self._row_height:
            hint.setHeight(self._row_height)
        return hint

    @staticmethod
    def _highlight_ranges(text: str, tokens: List[str]):
        """Return merged match ranges for all tokens in text."""
        text_lower = str(text).lower()
        ranges = []
        for token in sorted({t.lower() for t in tokens if t}, key=len, reverse=True):
            start = 0
            while True:
                pos = text_lower.find(token, start)
                if pos < 0:
                    break
                ranges.append((pos, pos + len(token)))
                start = pos + len(token)
        if not ranges:
            return []
        ranges.sort(key=lambda r: (r[0], r[1]))
        merged = [list(ranges[0])]
        for start, end in ranges[1:]:
            if start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(s, e) for s, e in merged]

    def paint(self, painter, option, index):
        """Draw item text with highlighted token substrings."""
        if self._tokens_getter is None or index.column() != 0:
            return super().paint(painter, option, index)

        tokens = list(self._tokens_getter() or [])
        if not tokens:
            return super().paint(painter, option, index)

        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        ranges = self._highlight_ranges(text, tokens)
        if not ranges:
            return super().paint(painter, option, index)

        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        text_rect = style.subElementRect(QtWidgets.QStyle.SE_ItemViewItemText, opt, opt.widget)

        # Let Qt paint the item background/selection, then draw styled text manually.
        draw_opt = QtWidgets.QStyleOptionViewItem(opt)
        draw_opt.text = ""
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, draw_opt, painter, draw_opt.widget)

        normal_font = QtGui.QFont(opt.font)
        bold_font = QtGui.QFont(opt.font)
        bold_font.setBold(True)
        normal_fm = QtGui.QFontMetrics(normal_font)
        bold_fm = QtGui.QFontMetrics(bold_font)

        if opt.state & QtWidgets.QStyle.State_Selected:
            normal_color = opt.palette.color(QtGui.QPalette.HighlightedText)
        else:
            normal_color = opt.palette.color(QtGui.QPalette.Text)

        segments = []
        cursor = 0
        for start, end in ranges:
            if cursor < start:
                segments.append((text[cursor:start], False))
            segments.append((text[start:end], True))
            cursor = end
        if cursor < len(text):
            segments.append((text[cursor:], False))

        painter.save()
        painter.setClipRect(text_rect)
        x = text_rect.left()
        y = text_rect.top() + (text_rect.height() + normal_fm.ascent() - normal_fm.descent()) // 2
        max_x = text_rect.right()

        for segment_text, is_highlight in segments:
            if not segment_text:
                continue
            if is_highlight:
                painter.setFont(bold_font)
                painter.setPen(self._highlight_color)
                seg_w = bold_fm.horizontalAdvance(segment_text)
            else:
                painter.setFont(normal_font)
                painter.setPen(normal_color)
                seg_w = normal_fm.horizontalAdvance(segment_text)
            if x > max_x:
                break
            painter.drawText(x, y, segment_text)
            x += seg_w

        painter.restore()
