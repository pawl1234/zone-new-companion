"""Search tab: find a movie, series or channel across every working account."""

from __future__ import annotations

from PyQt6.QtCore import QModelIndex, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from zone_new_companion.services.catalog_store import SearchFilters, SearchGroup, SearchHit, SearchResult
from zone_new_companion.ui.theme import set_role, set_variant, theme

TITLE_COL, YEAR_COL, TYPE_COL, QUALITY_COL, LANGUAGE_COL, WHERE_COL = range(6)
_HEADERS = ["Title", "Year", "Type", "Quality", "Language", "Where"]
_KIND_LABELS = {"live": "Live", "movies": "Movie", "series": "Series"}
# Typing pauses this long before a search runs.
_DEBOUNCE_MS = 250
# Spin box value meaning "any year".
_ANY_YEAR = 1899
# Each item's UserRole holds its SearchGroup (top level) or SearchHit (child).
_PAYLOAD = Qt.ItemDataRole.UserRole
# Row height matching the browse tables.
_ROW_HEIGHT = 32


class SearchPanel(QWidget):
    """Search box, scope/type/sort, a filter bar and the hits grouped by title."""

    # (text, scope "all"|"starred"|"active", SearchFilters)
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
        self.sort_combo = QComboBox()
        for label, key in (
            ("Best match", "relevance"),
            ("Newest year", "year"),
            ("Recently added", "added"),
            ("Most accounts", "accounts"),
            ("Title A–Z", "title"),
        ):
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip("Order of the results")
        controls.addWidget(self.sort_combo)
        self.filters_button = set_variant(QPushButton("Filters"), "ghost")
        self.filters_button.setCheckable(True)
        self.filters_button.setToolTip("Quality, year, date added, category and account filters")
        controls.addWidget(self.filters_button)
        layout.addLayout(controls)

        self.filter_bar = self._build_filter_bar()
        self.filter_bar.hide()
        self.filters_button.toggled.connect(self.filter_bar.setVisible)
        layout.addWidget(self.filter_bar)

        self.status_label = set_role(QLabel(""), "muted")
        self.status_label.setToolTip(
            "Every working XTREAM account's catalog is downloaded in the background\n"
            "and refreshed daily. Accounts on the same server share one download.\n"
            "The Language setting above the tabs applies to the search as well.",
        )
        layout.addWidget(self.status_label)

        self.model = QStandardItemModel(0, len(_HEADERS))
        self.model.setHorizontalHeaderLabels(_HEADERS)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(14, 14))
        self.tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        # Double-click plays; the arrow (or a click on it) opens the accounts.
        self.tree.setExpandsOnDoubleClick(False)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(TITLE_COL, QHeaderView.ResizeMode.Stretch)
        for col in (YEAR_COL, TYPE_COL, QUALITY_COL, LANGUAGE_COL):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(WHERE_COL, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(WHERE_COL, 300)
        self.tree.doubleClicked.connect(self._on_double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.tree, 1)

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
        for combo in (self.scope_combo, self.kind_combo, self.sort_combo):
            combo.currentIndexChanged.connect(lambda _index: self._emit_search())
        self._last_result = SearchResult([])
        # Set while a search started by a background change (an account got
        # indexed, went down, was removed) is pending: its result is merged
        # in without collapsing what the user opened or scrolled to.
        self._background_pending = False
        self._catalog_status = ""
        theme.changed.connect(self._on_theme_changed)

    def _build_filter_bar(self) -> QFrame:
        bar = set_role(QFrame(), "card")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)

        self.quality_combo = QComboBox()
        for label, key in (("Any quality", ""), ("SD or better", "SD"), ("HD or better", "HD"),
                           ("FHD or better", "FHD"), ("4K only", "4K")):
            self.quality_combo.addItem(label, key)
        self.quality_combo.setToolTip("Taken from tags like 4K, UHD, FHD, 1080p, HD in the name or category")
        row.addWidget(self.quality_combo)

        row.addWidget(set_role(QLabel("Year"), "muted"))
        self.year_from_spin = self._year_spin("from")
        row.addWidget(self.year_from_spin)
        row.addWidget(set_role(QLabel("–"), "muted"))
        self.year_to_spin = self._year_spin("to")
        row.addWidget(self.year_to_spin)

        self.added_combo = QComboBox()
        for label, days in (("Added any time", 0), ("Added last 7 days", 7), ("Added last 30 days", 30),
                            ("Added last 90 days", 90)):
            self.added_combo.addItem(label, days)
        row.addWidget(self.added_combo)

        self.category_edit = QLineEdit()
        self.category_edit.setPlaceholderText("Category contains…")
        self.category_edit.setClearButtonEnabled(True)
        self.category_edit.setMaximumWidth(180)
        row.addWidget(self.category_edit)

        self.tested_check = QCheckBox("Streaming")
        self.tested_check.setToolTip("Only accounts whose last Recheck could actually play a stream (green)")
        row.addWidget(self.tested_check)
        self.free_check = QCheckBox("Free connection")
        self.free_check.setToolTip("Only accounts with fewer active connections than allowed")
        row.addWidget(self.free_check)
        self.expiry_combo = QComboBox()
        for label, days in (("Any expiry", 0), ("Valid 7+ days", 7), ("Valid 30+ days", 30)):
            self.expiry_combo.addItem(label, days)
        self.expiry_combo.setToolTip("Leave out accounts that expire sooner")
        row.addWidget(self.expiry_combo)
        row.addStretch(1)
        reset = set_variant(QPushButton("Reset"), "ghost")
        reset.clicked.connect(self._reset_filters)
        row.addWidget(reset)

        for combo in (self.quality_combo, self.added_combo, self.expiry_combo):
            combo.currentIndexChanged.connect(lambda _index: self._filters_changed())
        for spin in (self.year_from_spin, self.year_to_spin):
            spin.valueChanged.connect(lambda _value: self._filters_changed(debounce=True))
        self.category_edit.textChanged.connect(lambda _text: self._filters_changed(debounce=True))
        for check in (self.tested_check, self.free_check):
            check.toggled.connect(lambda _checked: self._filters_changed())
        return bar

    @staticmethod
    def _year_spin(label: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(_ANY_YEAR, 2100)
        spin.setSpecialValueText("any")
        spin.setValue(_ANY_YEAR)
        spin.setToolTip(f"Release year {label} (titles without a year are left out once set)")
        return spin

    def focus_search(self) -> None:
        self.query_edit.setFocus()
        self.query_edit.selectAll()

    def refresh(self, background: bool = False) -> None:
        """Run the current search again (e.g. after accounts or the language changed).

        ``background``: the user did not ask for it, so their view is kept.
        """
        if len(self.query_edit.text().strip()) >= 2:
            self._emit_search(background=background)

    def set_status(self, text: str) -> None:
        self._catalog_status = text
        self._update_status()

    def filters(self) -> SearchFilters:
        """The filter bar's settings (scope and language are added by the controller)."""
        year_from, year_to = self.year_from_spin.value(), self.year_to_spin.value()
        return SearchFilters(
            kind=self.kind_combo.currentData(),
            min_quality=self.quality_combo.currentData(),
            year_from=0 if year_from == _ANY_YEAR else year_from,
            year_to=0 if year_to == _ANY_YEAR else year_to,
            added_within_days=self.added_combo.currentData(),
            category_text=self.category_edit.text(),
            stream_tested_only=self.tested_check.isChecked(),
            valid_for_days=self.expiry_combo.currentData(),
            free_slots_only=self.free_check.isChecked(),
            sort=self.sort_combo.currentData(),
        )

    def show_result(self, result: SearchResult) -> None:
        background, self._background_pending = self._background_pending, False
        if background and _signature(result) == _signature(self._last_result):
            self._last_result = result
            return
        view = self._view_state() if background else None
        self._last_result = result
        self.model.removeRows(0, self.model.rowCount())
        for group in result.groups:
            self.model.appendRow(self._group_row(group))
        self._update_status()
        if result.groups:
            self.tree.show()
            self.empty_label.hide()
            if view is None:
                self.tree.scrollToTop()
            else:
                self._restore_view(view)
        else:
            self._show_empty("Nothing found in the searchable accounts" + (
                " with these filters." if self._active_filter_count() else "."
            ))

    def _view_state(self) -> tuple[set[tuple], tuple | None, int]:
        """(expanded groups, selected row, scroll position) to restore after a rebuild."""
        expanded: set[tuple] = set()
        for row in range(self.model.rowCount()):
            index = self.model.index(row, TITLE_COL)
            if self.tree.isExpanded(index):
                expanded.add(_row_key(index.data(_PAYLOAD)))
        current = self.tree.currentIndex()
        selected = None
        if current.isValid():
            selected = (
                _row_key(current.parent().siblingAtColumn(TITLE_COL).data(_PAYLOAD)) if current.parent().isValid() else None,
                _row_key(current.siblingAtColumn(TITLE_COL).data(_PAYLOAD)),
            )
        return expanded, selected, self.tree.verticalScrollBar().value()

    def _restore_view(self, view: tuple[set[tuple], tuple | None, int]) -> None:
        expanded, selected, scroll = view
        for row in range(self.model.rowCount()):
            index = self.model.index(row, TITLE_COL)
            key = _row_key(index.data(_PAYLOAD))
            if key in expanded:
                self.tree.setExpanded(index, True)
            if selected is None:
                continue
            parent_key, own_key = selected
            if parent_key is None and key == own_key:
                self.tree.setCurrentIndex(index)
            elif parent_key == key:
                for child in range(self.model.rowCount(index)):
                    child_index = self.model.index(child, TITLE_COL, index)
                    if _row_key(child_index.data(_PAYLOAD)) == own_key:
                        self.tree.setCurrentIndex(child_index)
        self.tree.verticalScrollBar().setValue(scroll)

    def _group_row(self, group: SearchGroup) -> list[QStandardItem]:
        count = group.account_count
        where = group.hits[0].account_label if count == 1 else f"{count} accounts"
        row = self._row(
            group.title,
            str(group.year) if group.year else "",
            _KIND_LABELS.get(group.kind, group.kind),
            " · ".join(group.qualities),
            " · ".join(group.languages),
            where,
        )
        row[0].setData(group, _PAYLOAD)
        best = group.best
        row[0].setToolTip(
            ("Double-click to play" if best.playable else "Double-click to open the seasons")
            + f" on {best.account_label}" + ("" if count == 1 else " (the best account; expand to pick another)"),
        )
        # One copy on one account needs no list to pick from.
        if len(group.hits) > 1:
            for hit in sorted(group.hits, key=lambda hit: hit.play_rank):
                row[0].appendRow(self._hit_row(hit))
        return row

    def _hit_row(self, hit: SearchHit) -> list[QStandardItem]:
        where = ("★ " if hit.starred else "") + hit.account_label + (f" — {hit.category}" if hit.category else "")
        row = self._row(hit.name, str(hit.year) if hit.year else "", "", hit.quality, hit.languages, where)
        row[0].setData(hit, _PAYLOAD)
        stream = {True: "streams OK", False: "not streaming", None: "streams not tested"}[hit.stream_ok]
        slots = {True: "free connection", False: "all connections in use", None: "connections unknown"}[hit.free_slots]
        tip = f"{hit.account_label}\n{stream} · {slots}"
        for item in row:
            item.setForeground(theme.color("text_muted"))
            item.setToolTip(tip)
        return row

    @staticmethod
    def _row(*texts: str) -> list[QStandardItem]:
        items = [QStandardItem(text) for text in texts]
        for item in items:
            item.setEditable(False)
        items[0].setSizeHint(QSize(0, _ROW_HEIGHT))
        return items

    def _update_status(self) -> None:
        text = self._catalog_status
        if self._last_result.truncated:
            text += f" · showing the best {len(self._last_result.groups)} titles - type more to narrow it down"
        self.status_label.setText(text)
        active = self._active_filter_count()
        self.filters_button.setText(f"Filters ({active})" if active else "Filters")

    def _active_filter_count(self) -> int:
        filters = self.filters()
        return sum(
            bool(value) for value in (
                filters.min_quality, filters.year_from or filters.year_to, filters.added_within_days,
                filters.category_text.strip(), filters.stream_tested_only, filters.valid_for_days,
                filters.free_slots_only,
            )
        )

    def _reset_filters(self) -> None:
        widgets = (self.quality_combo, self.added_combo, self.expiry_combo, self.year_from_spin,
                   self.year_to_spin, self.category_edit, self.tested_check, self.free_check)
        for widget in widgets:
            widget.blockSignals(True)
        self.quality_combo.setCurrentIndex(0)
        self.added_combo.setCurrentIndex(0)
        self.expiry_combo.setCurrentIndex(0)
        self.year_from_spin.setValue(_ANY_YEAR)
        self.year_to_spin.setValue(_ANY_YEAR)
        self.category_edit.clear()
        self.tested_check.setChecked(False)
        self.free_check.setChecked(False)
        for widget in widgets:
            widget.blockSignals(False)
        self._filters_changed()

    def _filters_changed(self, debounce: bool = False) -> None:
        self._update_status()
        if debounce:
            self._debounce.start()
        else:
            self._emit_search()

    def _show_empty(self, text: str) -> None:
        self.empty_label.setText(text)
        self.empty_label.show()
        self.tree.hide()

    def _emit_search(self, background: bool = False) -> None:
        self._debounce.stop()
        # The newest request decides: a search the user starts resets the view.
        self._background_pending = background
        text = self.query_edit.text().strip()
        if len(text) < 2:
            self.model.removeRows(0, self.model.rowCount())
            self._last_result = SearchResult([])
            self._update_status()
            self._show_empty("Type at least two letters to search.")
            return
        self.search_requested.emit(text, self.scope_combo.currentData(), self.filters())

    def _hit_at(self, index: QModelIndex) -> SearchHit | None:
        """The hit a row stands for: a group plays its best account."""
        payload = index.siblingAtColumn(TITLE_COL).data(_PAYLOAD) if index.isValid() else None
        return payload.best if isinstance(payload, SearchGroup) else payload

    def _on_double_clicked(self, index: QModelIndex) -> None:
        hit = self._hit_at(index)
        if hit is None:
            return
        if hit.playable:
            self.hit_play_requested.emit(hit, False)
        else:
            self.hit_open_requested.emit(hit)

    def _show_menu(self, pos) -> None:
        index = self.tree.indexAt(pos)
        hit = self._hit_at(index)
        if hit is None:
            return
        menu = QMenu(self.tree)
        if not hit.playable:
            menu.addAction(f"Open seasons on {hit.account_label}", lambda: self.hit_open_requested.emit(hit))
        else:
            menu.addAction(f"Play on {hit.account_label}", lambda: self.hit_play_requested.emit(hit, False))
            menu.addAction("Open in VLC", lambda: self.hit_play_requested.emit(hit, True))
            menu.addAction("Browse this account", lambda: self.hit_open_requested.emit(hit))
        title_index = index.siblingAtColumn(TITLE_COL)
        if self.model.hasChildren(title_index):
            menu.addSeparator()
            expanded = self.tree.isExpanded(title_index)
            menu.addAction(
                "Hide accounts" if expanded else "Show all accounts",
                lambda: self.tree.setExpanded(title_index, not expanded),
            )
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _on_theme_changed(self) -> None:
        self._search_action.setIcon(theme.icon("search"))
        self.show_result(self._last_result)


def _row_key(payload: SearchGroup | SearchHit | None) -> tuple:
    """What identifies a row across searches."""
    if isinstance(payload, SearchGroup):
        return ("group", payload.kind, payload.title, payload.year)
    if isinstance(payload, SearchHit):
        return ("hit", payload.account_key, payload.kind, payload.provider_id)
    return ()


def _signature(result: SearchResult) -> tuple:
    """Equal signatures render identical rows, so a background result can be skipped."""
    return tuple(
        (
            _row_key(group),
            tuple(sorted((*_row_key(hit), hit.starred, hit.stream_ok, hit.free_slots) for hit in group.hits)),
        )
        for group in result.groups
    )
