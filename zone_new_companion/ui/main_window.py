"""Main UI shell."""

from __future__ import annotations

import sys
from datetime import datetime
from urllib.parse import urlparse

from PyQt6.QtCore import QModelIndex, QSize, QSortFilterProxyModel, Qt, pyqtSignal
from PyQt6.QtGui import QActionGroup, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from zone_new_companion.services.bulk_checker import account_key
from zone_new_companion.services.country_filter import LANGUAGES, codes_for_language, filter_categories, filter_items
from zone_new_companion.state import AppState
from zone_new_companion.ui.widgets.add_account_dialog import ACCOUNT_TYPES
from zone_new_companion.ui.widgets.media_table import (
    NAME_COL,
    NOW_COL,
    NUM_COL,
    PLAY_COL,
    STATUS_COL,
    VERIFY_COL,
    MediaTableModel,
)
from zone_new_companion.ui.theme import set_role, set_variant, theme
from zone_new_companion.ui.widgets.search_panel import SearchPanel
from zone_new_companion.ui.widgets.toast import ToastLabel
from zone_new_companion import __version__


class MainWindow(QMainWindow):
    """Main application window."""

    # Carries the PortalType to add.
    add_account_requested = pyqtSignal(object)
    category_selected = pyqtSignal(str, object)
    item_activated = pyqtSignal(str, object)
    verify_item_requested = pyqtSignal(str, object)
    hls_probe_requested = pyqtSignal(object)
    hls_play_requested = pyqtSignal(object)
    # Carries the item and whether to play its HLS variant.
    vlc_play_requested = pyqtSignal(object, bool)
    verify_all_channels_requested = pyqtSignal(str)
    verify_tab_requested = pyqtSignal(str)
    verify_cancel_requested = pyqtSignal()
    # All logs signals removed for performance
    now_playing_requested = pyqtSignal(str, object)
    back_requested = pyqtSignal(str)
    bulk_check_requested = pyqtSignal()
    working_account_selected = pyqtSignal(object)
    working_accounts_clear_requested = pyqtSignal()
    working_account_remove_requested = pyqtSignal(object)
    working_accounts_recheck_requested = pyqtSignal()
    working_account_star_toggled = pyqtSignal(object)
    history_selected = pyqtSignal(int)
    history_clear_requested = pyqtSignal()
    language_changed = pyqtSignal(str)
    language_strict_changed = pyqtSignal(bool)
    debug_dump_requested = pyqtSignal()
    theme_mode_changed = pyqtSignal(str)
    # How many working accounts are indexed for search (0 = all).
    index_limit_changed = pyqtSignal(int)

    # Ordered by importance: type + provider + real URL lead, account details follow.
    # The leading star column pins an account to the top of the list.
    # "Streams" is the second stage: login works (Status) vs. video actually flows.
    # "Search" tells whether the account's catalog is indexed for the Search tab.
    _ACCOUNT_COLUMNS = [
        "★", "Type", "Streams", "Search", "Provider", "Real URL", "Account", "Status", "Expiry", "Connections",
        "Timezone",
    ]
    _STAR_COL = 0
    _TYPE_COL = 1
    _STREAM_COL = 2
    _SEARCH_COL = 3
    _STRETCH_COLS = (4, 5)
    _INDEX_LIMITS = (3, 5, 10, 20, 0)
    # Badge text and color token per PortalType value.
    _TYPE_BADGES = {
        "xtream": ("XTREAM", "xtream"),
        "m3u": ("M3U", "m3u"),
        "stalker": ("STALKER", "stalker"),
    }
    _TAB_ICONS = {"Live": "tv", "Movies": "film", "Series": "layers", "Search": "search"}

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"zone-new-companion v{__version__}")
        self.setMinimumSize(1000, 650)
        self._help_menu = QMenu("Help", self)
        self._history_menu = QMenu("History", self)
        self._verify_menu = QMenu("Verify", self)
        self._view_menu = QMenu("View", self)
        self.menuBar().addMenu(self._help_menu)
        self.menuBar().addMenu(self._verify_menu)
        self.menuBar().addMenu(self._history_menu)
        self.menuBar().addMenu(self._view_menu)

        theme_menu = self._view_menu.addMenu("Theme")
        self._theme_actions = QActionGroup(self)
        for mode, label in (("system", "Match system"), ("light", "Light"), ("dark", "Dark")):
            action = theme_menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            self._theme_actions.addAction(action)
        self._theme_actions.triggered.connect(lambda action: self.theme_mode_changed.emit(action.data()))

        index_menu = self._view_menu.addMenu("Search index")
        index_menu.setToolTipsVisible(True)
        self._index_limit_actions = QActionGroup(self)
        for limit in self._INDEX_LIMITS:
            action = index_menu.addAction(f"Index up to {limit} accounts" if limit else "Index all accounts")
            action.setCheckable(True)
            action.setData(limit)
            action.setToolTip("Starred accounts come first, then the ones that stream")
            self._index_limit_actions.addAction(action)
        self._index_limit_actions.triggered.connect(lambda action: self.index_limit_changed.emit(action.data()))

        self._help_info_action = self._help_menu.addAction("Info")
        theme.bind_icon(self._help_info_action, "info")
        self._help_exit_action = self._help_menu.addAction("Exit")
        theme.bind_icon(self._help_exit_action, "log-out")
        self._help_exit_action.triggered.connect(self.close)

        # Verify menu actions
        self._verify_cancel_action = self._verify_menu.addAction("Cancel Verification")
        theme.bind_icon(self._verify_cancel_action, "circle-stop")
        self._verify_cancel_action.triggered.connect(self._emit_cancel_verification)
        
        self._verify_menu.addSeparator()
        
        self._verify_live_action = self._verify_menu.addAction("Verify Live")
        theme.bind_icon(self._verify_live_action, "tv")
        self._verify_live_action.triggered.connect(lambda: self._emit_verify_tab("Live"))
        
        self._verify_movies_action = self._verify_menu.addAction("Verify Movies")
        theme.bind_icon(self._verify_movies_action, "film")
        self._verify_movies_action.triggered.connect(lambda: self._emit_verify_tab("Movies"))
        
        self._verify_series_action = self._verify_menu.addAction("Verify Series")
        theme.bind_icon(self._verify_series_action, "layers")
        self._verify_series_action.triggered.connect(lambda: self._emit_verify_tab("Series"))

        # Logs functionality completely removed

        # Shown only in debug mode (enable_debug_menu).
        self._debug_menu = QMenu("Debug", self)
        self._debug_dump_action = self._debug_menu.addAction("Dump full catalog")
        self._debug_dump_action.setToolTip("Fetch every category's streams and write them to the debug dump")
        self._debug_dump_action.triggered.connect(self.debug_dump_requested.emit)

        self._toast = ToastLabel(self)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 8)

        # Working accounts of every type, with their connection details inline;
        # they are the main view. Accounts come in through the Add menu.
        # Clicking a row loads that account's streams into the tabs below; the
        # splitter trades room between the two.
        # A plain panel with its own heading: a QGroupBox title is drawn onto
        # the frame border and collides with it under the dark theme.
        self.accounts_group = set_role(QFrame(), "card")
        accounts_layout = QVBoxLayout(self.accounts_group)
        accounts_layout.setContentsMargins(16, 14, 16, 8)
        accounts_layout.setSpacing(10)

        accounts_buttons = QHBoxLayout()
        accounts_buttons.setSpacing(8)
        self.accounts_title = set_role(QLabel("Working Accounts"), "title")
        accounts_buttons.addWidget(self.accounts_title)
        self.accounts_counts = set_role(QLabel(""), "pill")
        self.accounts_counts.hide()
        accounts_buttons.addWidget(self.accounts_counts)
        accounts_buttons.addSpacing(16)
        self.add_account_button = set_variant(QPushButton("Add"), "primary")
        self.add_account_button.setProperty("hasMenu", True)
        theme.bind_icon(self.add_account_button, "plus", "on_accent")
        self.add_account_button.setToolTip("Add an XTREAM, M3U or STALKER account, or import many XTREAM URLs")
        add_menu = QMenu(self.add_account_button)
        for portal, (menu_text, _title) in ACCOUNT_TYPES.items():
            add_menu.addAction(menu_text, lambda portal=portal: self.add_account_requested.emit(portal))
        add_menu.addSeparator()
        bulk_action = add_menu.addAction("Bulk import XTREAM URLs…", self.bulk_check_requested.emit)
        theme.bind_icon(bulk_action, "list-plus")
        self.add_account_button.setMenu(add_menu)
        accounts_buttons.addWidget(self.add_account_button)
        self.recheck_accounts_button = QPushButton("Recheck")
        theme.bind_icon(self.recheck_accounts_button, "refresh-cw")
        self.recheck_accounts_button.setToolTip(
            "Check which accounts (XTREAM, M3U, STALKER) still log in and can actually stream,\n"
            "and offer to remove the ones that no longer log in",
        )
        self.recheck_accounts_button.clicked.connect(self.working_accounts_recheck_requested.emit)
        accounts_buttons.addWidget(self.recheck_accounts_button)
        accounts_buttons.addStretch(1)
        self.remove_account_button = set_variant(QPushButton("Remove"), "ghost")
        theme.bind_icon(self.remove_account_button, "x")
        self.remove_account_button.setToolTip("Remove the selected account from the list")
        self.remove_account_button.clicked.connect(self._emit_remove_account)
        accounts_buttons.addWidget(self.remove_account_button)
        self.clear_accounts_button = set_variant(QPushButton("Clear"), "danger")
        theme.bind_icon(self.clear_accounts_button, "trash-2", "danger")
        self.clear_accounts_button.setToolTip("Remove all saved accounts")
        self.clear_accounts_button.clicked.connect(self._confirm_clear_accounts)
        accounts_buttons.addWidget(self.clear_accounts_button)
        accounts_layout.addLayout(accounts_buttons)

        self.accounts_empty_label = QLabel(
            "No accounts yet — use Add to add an XTREAM, M3U or STALKER account.",
        )
        self.accounts_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_role(self.accounts_empty_label, "empty")
        accounts_layout.addWidget(self.accounts_empty_label, 1)
        self.accounts_table = QTableWidget()
        self.accounts_table.setToolTip(
            "Your accounts - click one to browse its streams,\n"
            "click the ☆ to star it and keep it at the top",
        )
        self.accounts_table.setColumnCount(len(self._ACCOUNT_COLUMNS))
        self.accounts_table.setHorizontalHeaderLabels(self._ACCOUNT_COLUMNS)
        self.accounts_table.verticalHeader().setVisible(False)
        self.accounts_table.verticalHeader().setDefaultSectionSize(36)
        self.accounts_table.setShowGrid(False)
        self.accounts_table.setIconSize(QSize(14, 14))
        self.accounts_table.setAlternatingRowColors(True)
        self.accounts_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.accounts_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.accounts_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        # Provider and Real URL are the focus, so they take the slack.
        for col in range(len(self._ACCOUNT_COLUMNS)):
            mode = (
                QHeaderView.ResizeMode.Stretch if col in self._STRETCH_COLS
                else QHeaderView.ResizeMode.ResizeToContents
            )
            self.accounts_table.horizontalHeader().setSectionResizeMode(col, mode)
        self.accounts_table.cellClicked.connect(self._emit_working_account)
        accounts_layout.addWidget(self.accounts_table, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 6, 0, 0)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        filter_row.addWidget(set_role(QLabel("Language"), "muted"))
        self.language_combo = QComboBox()
        self.language_combo.addItem("All languages", "")
        for key, (label, _codes) in LANGUAGES.items():
            self.language_combo.addItem(label, key)
        self.language_combo.setToolTip(
            "Portals have no language field, so it is guessed from tags like\n"
            "'DE: ARD', '|DE| Sport', 'GERMANY' or an EPG id ending in '.de'.\n"
            "Entries tagged with another country are hidden. MULTI-AUDIO /\n"
            "MULTI-SUB-AUDIO count as every language.",
        )
        self.language_combo.currentIndexChanged.connect(
            lambda _index: self.language_changed.emit(self.language_combo.currentData()),
        )
        filter_row.addWidget(self.language_combo)
        self.language_strict_check = QCheckBox("Hide untagged")
        self.language_strict_check.setChecked(True)
        self.language_strict_check.setToolTip(
            "Also hide categories without any language tag (e.g. 'VIP | GOLDEN EVENTS').\n"
            "Untick to keep them; channels inside a matching category always stay.",
        )
        self.language_strict_check.toggled.connect(self.language_strict_changed.emit)
        filter_row.addWidget(self.language_strict_check)
        filter_row.addStretch(1)
        right_layout.addLayout(filter_row)

        # Create tab widget (verify button moved to titlebar menu)
        self.tab_widget = QTabWidget()
        
        self.category_lists: dict[str, QListWidget] = {}
        self.item_tables: dict[str, QTableView] = {}
        self._item_models: dict[str, MediaTableModel] = {}
        self._item_proxies: dict[str, QSortFilterProxyModel] = {}
        # What each widget last rendered, so a state change only redraws the
        # parts that actually changed.
        self._rendered_categories: dict[str, tuple] = {}
        self._rendered_items: dict[str, tuple] = {}
        self._rendered_annotations: dict[str, tuple] = {}
        for tab_name in ("Live", "Movies", "Series"):
            tab = QWidget()
            tab_layout = QHBoxLayout(tab)
            tab_layout.setContentsMargins(0, 0, 0, 0)
            tab_layout.setSpacing(12)
            categories = QListWidget()
            categories.setToolTip("Categories - Click to load channels/movies/series")
            categories.setAlternatingRowColors(True)
            model = MediaTableModel()
            proxy = QSortFilterProxyModel()
            proxy.setSourceModel(model)
            table = QTableView()
            table.setModel(proxy)
            table.setToolTip("Playlist items - Double-click to play, right-click for options")
            table.setAlternatingRowColors(True)
            # "#" is the provider's position and the default sort.
            table.setSortingEnabled(True)
            table.sortByColumn(NUM_COL, Qt.SortOrder.AscendingOrder)
            header = table.horizontalHeader()
            # Measuring every row of a huge list to fit columns is slow; a sample does.
            header.setResizeContentsPrecision(200)
            for col in (NUM_COL, STATUS_COL, VERIFY_COL, PLAY_COL):
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(NAME_COL, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(NOW_COL, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            table.verticalHeader().setDefaultSectionSize(34)
            table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            table.setShowGrid(False)
            table.setIconSize(QSize(14, 14))
            table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
            table.clicked.connect(lambda index, name=tab_name: self._on_item_clicked(name, index))
            table.doubleClicked.connect(lambda index, name=tab_name: self._on_item_double_clicked(name, index))
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(
                lambda pos, name=tab_name, view=table: self._show_item_menu(name, view, pos),
            )
            tab_layout.addWidget(categories, 1)
            tab_layout.addWidget(table, 3)
            self.tab_widget.addTab(tab, tab_name)
            self.category_lists[tab_name] = categories
            self.item_tables[tab_name] = table
            self._item_models[tab_name] = model
            self._item_proxies[tab_name] = proxy

            categories.itemClicked.connect(
                lambda item, name=tab_name: self.category_selected.emit(name, item.data(Qt.ItemDataRole.UserRole)),
            )
        # Cross-account search over the downloaded catalogs; Ctrl+F jumps here.
        self.search_panel = SearchPanel()
        self.tab_widget.addTab(self.search_panel, "Search")
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.show_search)
        right_layout.addWidget(self.tab_widget, 1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        splitter.addWidget(self.accounts_group)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 400])
        layout.addWidget(splitter, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(320)
        self.progress.hide()
        self.status_label = QLabel("Ready")
        status = QStatusBar()
        status.addPermanentWidget(self.status_label, 1)
        status.addPermanentWidget(self.progress)
        self.setStatusBar(status)

        self._account_results: list = []
        # account_key -> (text, tooltip, color token) for the Search column.
        self._index_states: dict[tuple, tuple[str, str, str]] = {}
        self._apply_theme_icons()
        theme.changed.connect(self._on_theme_changed)

    def show_search(self) -> None:
        self.tab_widget.setCurrentWidget(self.search_panel)
        self.search_panel.focus_search()

    def show_tab(self, tab_name: str) -> None:
        """Bring a tab ("Live", "Movies", "Series", "Search") to the front."""
        for index in range(self.tab_widget.count()):
            if self.tab_widget.tabText(index) == tab_name:
                self.tab_widget.setCurrentIndex(index)
                return

    def set_index_limit(self, limit: int) -> None:
        """Tick the View → Search index entry without emitting index_limit_changed."""
        for action in self._index_limit_actions.actions():
            action.setChecked(action.data() == limit)

    def set_index_states(self, states: dict[tuple, tuple[str, str, str]]) -> None:
        """Update the Search column in place (no full redraw, selection stays)."""
        self._index_states = dict(states)
        for row in range(self.accounts_table.rowCount()):
            star = self.accounts_table.item(row, self._STAR_COL)
            cell = self.accounts_table.item(row, self._SEARCH_COL)
            if star is not None and cell is not None and star.data(Qt.ItemDataRole.UserRole) is not None:
                self._fill_search_cell(cell, star.data(Qt.ItemDataRole.UserRole))

    def _fill_search_cell(self, cell: QTableWidgetItem, result) -> None:
        text, tip, token = self._index_states.get(account_key(result), ("", "", "text_muted"))
        cell.setText(text)
        cell.setToolTip(tip)
        cell.setForeground(theme.color(token))

    def set_theme_mode(self, mode: str) -> None:
        """Tick the View → Theme entry without emitting theme_mode_changed."""
        for action in self._theme_actions.actions():
            action.setChecked(action.data() == mode)

    def _apply_theme_icons(self) -> None:
        for index in range(self.tab_widget.count()):
            self.tab_widget.setTabIcon(index, theme.icon(self._TAB_ICONS[self.tab_widget.tabText(index)]))

    def _on_theme_changed(self) -> None:
        """Repaint what carries theme colors outside the stylesheet."""
        self._apply_theme_icons()
        for model in self._item_models.values():
            model.refresh()
        selected = self.accounts_table.currentRow()
        self.show_working_accounts(self._account_results, announce=False)
        if selected >= 0:
            self.accounts_table.selectRow(selected)

    def _confirm_clear_accounts(self) -> None:
        if self.accounts_table.rowCount() == 0:
            return
        answer = QMessageBox.question(
            self,
            "Clear accounts",
            f"Remove all {self.accounts_table.rowCount()} accounts from the list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.working_accounts_clear_requested.emit()

    def _emit_remove_account(self) -> None:
        row = self.accounts_table.currentRow()
        cell = self.accounts_table.item(row, self._STAR_COL) if row >= 0 else None
        if cell is not None and cell.data(Qt.ItemDataRole.UserRole) is not None:
            self.working_account_remove_requested.emit(cell.data(Qt.ItemDataRole.UserRole))

    def apply_state(self, state: AppState) -> None:
        """Render state changes; only the parts that changed are redrawn."""
        self.status_label.setText(state.status_text)

        # Background loads (categories, series, stream start) never block the
        # window; the bar just shows that something is still running.
        if state.busy or state.activity:
            self.progress.show()
            self.progress.setRange(0, 0)  # Indeterminate progress
            if state.activity:
                label = state.activity[-1] if len(state.activity) == 1 else f"{len(state.activity)} tasks running…"
                self.progress.setToolTip("\n".join(state.activity))
            elif "Priority verifying" in state.status_text:
                label = "Priority Verification..."
            elif "Verifying" in state.status_text:
                label = "Verifying..."
            else:
                label = "Loading..."
            self.progress.setFormat(label)
        else:
            self.progress.hide()
            self.progress.setFormat("")
            self.progress.setToolTip("")
        wanted = codes_for_language(state.language)
        for tab_name, categories in state.categories.items():
            key = (categories, state.language, state.language_strict)
            if self._same(self._rendered_categories.get(tab_name), key):
                continue
            self._rendered_categories[tab_name] = key
            list_widget = self.category_lists[tab_name]
            list_widget.setUpdatesEnabled(False)
            list_widget.clear()
            for category in filter_categories(categories, wanted, state.language_strict):
                item = QListWidgetItem(category.name)
                item.setData(Qt.ItemDataRole.UserRole, category)
                list_widget.addItem(item)
            list_widget.setUpdatesEnabled(True)
        for tab_name, items in state.current_items.items():
            model = self._item_models[tab_name]
            tab_categories = state.categories.get(tab_name, [])
            key = (items, state.language, state.language_strict, tab_categories)
            if not self._same(self._rendered_items.get(tab_name), key):
                self._rendered_items[tab_name] = key
                model.set_items(filter_items(items, wanted, tab_categories, state.language_strict))
                self.item_tables[tab_name].scrollToTop()
            annotations = (
                state.now_playing.get(tab_name, {}),
                state.persistent_verification_results,
                state.verification_results.get(tab_name, {}),
            )
            if not self._same(self._rendered_annotations.get(tab_name), annotations):
                self._rendered_annotations[tab_name] = annotations
                model.set_annotations(*annotations)

    @staticmethod
    def _same(previous: tuple | None, current: tuple) -> bool:
        """Whether a render key holds the very same objects (state is replaced, not mutated)."""
        return previous is not None and len(previous) == len(current) and all(
            a is b if not isinstance(a, str) else a == b for a, b in zip(previous, current)
        )

    def show_working_accounts(self, results: list, announce: bool = True, select=None) -> None:
        """Render the accounts (with details); ``select`` picks the row to highlight."""
        self._account_results = list(results)
        self.accounts_table.setRowCount(0)
        selected_row = 0
        for result in results:
            row = self.accounts_table.rowCount()
            self.accounts_table.insertRow(row)
            if select is not None and result is select:
                selected_row = row
            provider = urlparse(result.base_url).netloc or result.base_url
            connections = (
                f"{result.active_connections}/{result.max_connections}"
                if result.active_connections or result.max_connections else ""
            )
            values = [
                "",
                "",
                "",
                "",
                provider,
                result.real_url or result.base_url,
                result.username or result.mac_address or "—",
                result.status,
                result.expiry,
                connections,
                result.timezone,
            ]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if col == self._STAR_COL:
                    cell.setData(Qt.ItemDataRole.UserRole, result)
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    cell.setToolTip("Unstar" if result.starred else "Star - keep at the top")
                    cell.setIcon(
                        theme.icon("star", "star", filled=True) if result.starred
                        else theme.icon("star", "text_disabled"),
                    )
                elif col == self._TYPE_COL:
                    self._fill_type_cell(cell, result.portal_type)
                elif col == self._STREAM_COL:
                    self._fill_stream_cell(cell, result)
                elif col == self._SEARCH_COL:
                    self._fill_search_cell(cell, result)
                self.accounts_table.setItem(row, col, cell)
        counts: dict[str, int] = {}
        for result in results:
            counts[result.portal_type] = counts.get(result.portal_type, 0) + 1
        breakdown = " · ".join(
            f"{counts[key]} {label}" for key, (label, _token) in self._TYPE_BADGES.items() if counts.get(key)
        )
        self.accounts_counts.setText(breakdown)
        self.accounts_counts.setVisible(bool(breakdown))
        self.accounts_table.setVisible(bool(results))
        self.accounts_empty_label.setVisible(not results)
        self.recheck_accounts_button.setEnabled(bool(results))
        self.remove_account_button.setEnabled(bool(results))
        self.clear_accounts_button.setEnabled(bool(results))
        if results:
            self.accounts_table.selectRow(selected_row)
            self.accounts_table.scrollToItem(self.accounts_table.item(selected_row, self._STAR_COL))
            if announce:
                self.notify(f"{len(results)} working account(s) ready — click one to browse")

    @classmethod
    def _fill_type_cell(cls, cell: QTableWidgetItem, portal_type: str) -> None:
        """Colored badge text for the account's kind."""
        label, token = cls._TYPE_BADGES.get(portal_type, (portal_type.upper(), "text_muted"))
        cell.setText(label)
        cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        cell.setForeground(theme.color(token))
        font = cell.font()
        font.setWeight(QFont.Weight.DemiBold)
        cell.setFont(font)

    @staticmethod
    def _light_icon(color: QColor) -> QIcon:
        """A round status light; as an icon it keeps its color on selected rows."""
        pixmap = QPixmap(14, 14)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        halo = QColor(color)
        halo.setAlphaF(0.25)
        painter.setBrush(halo)
        painter.drawEllipse(0, 0, 14, 14)
        painter.setBrush(color)
        painter.drawEllipse(3, 3, 8, 8)
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _fill_stream_cell(cell: QTableWidgetItem, result) -> None:
        """Green/red light for whether the account's streams currently play."""
        if result.stream_ok is None:
            text, color = "Not checked", theme.color("text_muted")
            tip = "Streaming not tested yet - press Recheck"
        elif result.stream_ok:
            text, color = "Streaming", theme.color("success")
            tip = f"Streams working (checked {result.stream_checked_at})"
        else:
            since = result.stream_down_since or result.stream_checked_at
            today = datetime.now().strftime("%Y-%m-%d")
            text = f"Down since {since[len(today) + 1:] if since.startswith(today) else since}"
            color = theme.color("danger")
            tip = (
                f"Streams not working: {result.stream_status or 'unknown'}\n"
                f"Down since {since}, last checked {result.stream_checked_at}"
            )
        cell.setText(text)
        cell.setIcon(MainWindow._light_icon(color))
        cell.setForeground(color)
        cell.setToolTip(tip)
        font = cell.font()
        font.setWeight(QFont.Weight.DemiBold)
        cell.setFont(font)

    def _emit_working_account(self, row: int, col: int = 0) -> None:
        cell = self.accounts_table.item(row, self._STAR_COL)
        if cell is None:
            return
        result = cell.data(Qt.ItemDataRole.UserRole)
        if result is None:
            return
        if col == self._STAR_COL:
            self.working_account_star_toggled.emit(result)
        else:
            self.working_account_selected.emit(result)

    def set_recheck_running(self, running: bool, text: str = "Rechecking…") -> None:
        """Lock the Recheck button (showing progress) while a recheck runs."""
        self.recheck_accounts_button.setEnabled(not running and self.accounts_table.rowCount() > 0)
        self.recheck_accounts_button.setText(text if running else "Recheck")

    def notify(self, text: str) -> None:
        """Show toast notification."""
        self._toast.show_message(text)

    def set_history_actions(self, labels: list[str]) -> None:
        """Populate top menu history actions."""
        self._history_menu.clear()
        if not labels:
            action = self._history_menu.addAction("No successful connections yet")
            action.setEnabled(False)
            return
        for index, label in enumerate(labels):
            action = self._history_menu.addAction(label)
            action.triggered.connect(lambda checked=False, idx=index: self.history_selected.emit(idx))

    def set_history_grouped(self, groups: dict[str, list[tuple[int, str]]]) -> None:
        """Populate history menu grouped by day."""
        self._history_menu.clear()
        clear_action = self._history_menu.addAction("Clear History")
        clear_action.triggered.connect(self.history_clear_requested.emit)
        saved_menu = self._history_menu.addMenu("Saved Credentials")
        if not groups:
            action = saved_menu.addAction("No successful connections yet")
            action.setEnabled(False)
            return
        for day, entries in groups.items():
            sub = saved_menu.addMenu(day)
            for index, label in entries:
                action = sub.addAction(label)
                action.triggered.connect(lambda checked=False, idx=index: self.history_selected.emit(idx))

    def set_language(self, language: str) -> None:
        """Select a language without emitting language_changed."""
        index = self.language_combo.findData(language)
        self.language_combo.blockSignals(True)
        self.language_combo.setCurrentIndex(max(index, 0))
        self.language_combo.blockSignals(False)

    def enable_debug_menu(self, dump_dir: str) -> None:
        """Show the Debug menu; it names where the dumps are written."""
        folder_action = self._debug_menu.addAction(f"Dumps: {dump_dir}")
        folder_action.setEnabled(False)
        self.menuBar().addMenu(self._debug_menu)

    def set_language_strict(self, strict: bool) -> None:
        """Set the "Hide untagged" box without emitting language_strict_changed."""
        self.language_strict_check.blockSignals(True)
        self.language_strict_check.setChecked(strict)
        self.language_strict_check.blockSignals(False)

    def set_help_info(self, callback) -> None:
        self._help_info_action.triggered.connect(callback)

    def _media_at(self, tab_name: str, index: QModelIndex):
        source = self._item_proxies[tab_name].mapToSource(index)
        return self._item_models[tab_name].media_at(source.row())

    def _on_item_clicked(self, tab_name: str, index: QModelIndex) -> None:
        media = self._media_at(tab_name, index)
        if media is None:
            return
        if index.column() == PLAY_COL:
            self.item_activated.emit(tab_name, media)
        elif index.column() == VERIFY_COL:
            self.verify_item_requested.emit(tab_name, media)
        else:
            self.now_playing_requested.emit(tab_name, media)

    def _on_item_double_clicked(self, tab_name: str, index: QModelIndex) -> None:
        # The single click already handled the button columns.
        if index.column() in (PLAY_COL, VERIFY_COL):
            return
        media = self._media_at(tab_name, index)
        if media is not None:
            self.item_activated.emit(tab_name, media)

    def _show_item_menu(self, tab_name: str, table: QTableView, pos) -> None:
        index = table.indexAt(pos)
        media = self._media_at(tab_name, index) if index.isValid() else None
        # Right-click has always meant "back" (series → seasons → episodes);
        # only a playable stream row gets the menu, which keeps Back as an entry.
        if media is None or media.item_type not in {"channel", "vod", "episode"}:
            self.back_requested.emit(tab_name)
            return
        menu = QMenu(table)
        menu.addAction("Play", lambda: self.item_activated.emit(tab_name, media))
        menu.addAction("Play HLS", lambda: self.hls_play_requested.emit(media))
        vlc_menu = menu.addMenu("Open in VLC")
        vlc_menu.addAction("Play", lambda: self.vlc_play_requested.emit(media, False))
        vlc_menu.addAction("Play HLS", lambda: self.vlc_play_requested.emit(media, True))
        menu.addAction("Verify", lambda: self.verify_item_requested.emit(tab_name, media))
        menu.addAction("Check for HLS (m3u8)", lambda: self.hls_probe_requested.emit(media))
        if media.item_type == "episode":
            menu.addSeparator()
            menu.addAction("Back to seasons", lambda: self.back_requested.emit(tab_name))
        menu.exec(table.viewport().mapToGlobal(pos))

    def show_hls_result(self, name: str, hls_url: str | None) -> None:
        """Report an HLS probe; a found URL goes to the clipboard for Apple TV players."""
        if hls_url is None:
            self.notify(f"{name}: no HLS stream, only TS")
            return
        QGuiApplication.clipboard().setText(hls_url)
        self.notify(f"{name}: HLS available, URL copied")

    def _emit_verify_all_channels(self) -> None:
        """Emit verify all channels for current tab."""
        current_index = self.tab_widget.currentIndex()
        current_tab = self.tab_widget.tabText(current_index)
        if current_tab in ("Live", "Movies", "Series"):
            self.verify_all_channels_requested.emit(current_tab)

    def _emit_verify_tab(self, tab_name: str) -> None:
        """Emit verify request for specific tab."""
        if tab_name in ("Live", "Movies", "Series"):
            self.verify_tab_requested.emit(tab_name)

    def _emit_cancel_verification(self) -> None:
        """Emit cancel verification request."""
        self.verify_cancel_requested.emit()

    # All logs emit methods removed for performance
