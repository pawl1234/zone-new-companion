"""Search tab: find a movie, series or channel across every working account."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from zone_new_companion.services.catalog_store import RESULT_LIMIT, SearchHit
from zone_new_companion.ui.theme import set_role, theme

NAME_COL, TYPE_COL, CATEGORY_COL, ACCOUNT_COL = range(4)
_HEADERS = ["Name", "Type", "Category", "Account"]
_KIND_LABELS = {"live": "Live", "movies": "Movie", "series": "Series"}
# Typing pauses this long before a search runs.
_DEBOUNCE_MS = 250


class SearchResultsModel(QAbstractTableModel):
    """Search hits, one row per stream and account."""

    def __init__(self) -> None:
        super().__init__()
        self._hits: list[SearchHit] = []

    def set_hits(self, hits: list[SearchHit]) -> None:
        self.beginResetModel()
        self._hits = list(hits)
        self.endResetModel()

    def refresh(self) -> None:
        """Repaint after a theme change."""
        if self._hits:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._hits) - 1, len(_HEADERS) - 1))

    def hit_at(self, row: int) -> SearchHit | None:
        return self._hits[row] if 0 <= row < len(self._hits) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._hits)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        hit = self.hit_at(index.row())
        if hit is None:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (hit.name, _KIND_LABELS.get(hit.kind, hit.kind), hit.category, hit.account_label)[column]
        if role == Qt.ItemDataRole.ForegroundRole and column != NAME_COL:
            return theme.color("text_muted")
        if role == Qt.ItemDataRole.ToolTipRole and column == NAME_COL:
            return "Double-click to play" if hit.kind != "series" else "Double-click to open the seasons"
        return None


class SearchPanel(QWidget):
    """Search box, scope and type filters, and the hits."""

    # (text, scope "all"|"starred"|"active", kind "live"|"movies"|"series"|None)
    search_requested = pyqtSignal(str, str, object)
    # (SearchHit, open in VLC)
    hit_play_requested = pyqtSignal(object, bool)
    hit_open_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Search movies, series and channels in your working accounts…")
        self.query_edit.setClearButtonEnabled(True)
        self._search_action = self.query_edit.addAction(theme.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        controls.addWidget(self.query_edit, 1)
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("All working accounts", "all")
        self.scope_combo.addItem("Starred accounts", "starred")
        self.scope_combo.addItem("Account being browsed", "active")
        self.scope_combo.setToolTip("Where to search; accounts that are down (red) are always left out")
        controls.addWidget(self.scope_combo)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("All types", None)
        self.kind_combo.addItem("Movies", "movies")
        self.kind_combo.addItem("Series", "series")
        self.kind_combo.addItem("Live", "live")
        controls.addWidget(self.kind_combo)
        layout.addLayout(controls)

        self.status_label = set_role(QLabel(""), "muted")
        self.status_label.setToolTip(
            "Every working XTREAM account's catalog is downloaded in the background\n"
            "and refreshed daily. Accounts on the same server share one download.",
        )
        layout.addWidget(self.status_label)

        self.model = SearchResultsModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setIconSize(QSize(14, 14))
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(34)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(NAME_COL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(TYPE_COL, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(CATEGORY_COL, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(ACCOUNT_COL, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(CATEGORY_COL, 220)
        header.resizeSection(ACCOUNT_COL, 260)
        self.table.doubleClicked.connect(self._on_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.table, 1)

        self.empty_label = set_role(QLabel(""), "empty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label, 1)
        self._show_empty("Type at least two letters to search.")

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_search)
        self.query_edit.textChanged.connect(lambda _text: self._debounce.start())
        self.query_edit.returnPressed.connect(self._emit_search)
        self.scope_combo.currentIndexChanged.connect(lambda _index: self._emit_search())
        self.kind_combo.currentIndexChanged.connect(lambda _index: self._emit_search())
        theme.changed.connect(self._on_theme_changed)

    def focus_search(self) -> None:
        self.query_edit.setFocus()
        self.query_edit.selectAll()

    def refresh(self) -> None:
        """Run the current search again (e.g. after accounts changed)."""
        if len(self.query_edit.text().strip()) >= 2:
            self._emit_search()

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def show_hits(self, hits: list[SearchHit]) -> None:
        self.model.set_hits(hits)
        self.table.scrollToTop()
        if hits:
            self.table.show()
            self.empty_label.hide()
        else:
            self._show_empty("Nothing found in the searchable accounts.")
        if len(hits) >= RESULT_LIMIT:
            self.status_label.setText(f"Showing the best {RESULT_LIMIT} hits - type more to narrow it down")

    def _show_empty(self, text: str) -> None:
        self.empty_label.setText(text)
        self.empty_label.show()
        self.table.hide()

    def _emit_search(self) -> None:
        self._debounce.stop()
        text = self.query_edit.text().strip()
        if len(text) < 2:
            self.model.set_hits([])
            self._show_empty("Type at least two letters to search.")
            return
        self.search_requested.emit(text, self.scope_combo.currentData(), self.kind_combo.currentData())

    def _on_double_clicked(self, index: QModelIndex) -> None:
        hit = self.model.hit_at(index.row())
        if hit is None:
            return
        if hit.kind == "series":
            self.hit_open_requested.emit(hit)
        else:
            self.hit_play_requested.emit(hit, False)

    def _show_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        hit = self.model.hit_at(index.row()) if index.isValid() else None
        if hit is None:
            return
        menu = QMenu(self.table)
        if hit.kind == "series":
            menu.addAction("Open seasons", lambda: self.hit_open_requested.emit(hit))
        else:
            menu.addAction("Play", lambda: self.hit_play_requested.emit(hit, False))
            menu.addAction("Open in VLC", lambda: self.hit_play_requested.emit(hit, True))
            menu.addAction("Browse this account", lambda: self.hit_open_requested.emit(hit))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_theme_changed(self) -> None:
        self._search_action.setIcon(theme.icon("search"))
        self.model.refresh()
