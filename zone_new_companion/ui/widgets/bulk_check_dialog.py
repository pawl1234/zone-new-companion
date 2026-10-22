"""Bulk Xtream URL availability checker dialog.

Paste many Xtream ``get.php`` / ``player_api.php`` URLs (one per line, or
``Host/User/Pass/M3U`` account blocks), check
them all in parallel, then review the working accounts, the ones whose server
is only unreachable right now (kept for a later Recheck) and, collapsed, the
rejected ones, and jump back into the main window to browse + stream them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from zone_new_companion.services.bulk_checker import BulkResult, friendly_error, is_server_unavailable
from zone_new_companion.ui.theme import set_role, set_variant, theme

if TYPE_CHECKING:
    from zone_new_companion.controllers.app_controller import AppController

_WORKING_COLUMNS = ["#", "Server", "Expiry", "Max Conn", "Active", "Timezone", "Account"]
_UNREACHABLE_COLUMNS = ["#", "Server", "Account", "Reason"]
_FAILED_COLUMNS = ["#", "URL", "Reason"]

_PLACEHOLDER = (
    "Paste one Xtream URL per line, e.g.\n"
    "http://example.com:8080/get.php?username=USER&password=PASS&type=m3u_plus\n"
    "http://other.tv:25461/player_api.php?username=USER2&password=PASS2\n"
    "or account blocks with Host: / User: / Pass: / M3U: lines, separated by -----"
)


class BulkCheckDialog(QDialog):
    """Run a bulk check, then hand the working accounts back to the main window."""

    # Emitted with the full list[BulkResult] of working accounts when the user
    # chooses to browse. The main window renders these as a pickable list.
    accounts_chosen = pyqtSignal(object)  # list[BulkResult]

    def __init__(self, controller: "AppController", parent=None, entries: list[str] | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._signals = None
        self._running = False
        # line_number -> (table, row) so progress and final ticks update in place.
        self._rows: dict[int, tuple[QTableWidget, int]] = {}

        self.setWindowTitle("Bulk URL Check")
        self.setMinimumSize(900, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        root.addWidget(set_role(QLabel("Paste Xtream URLs (one per line) or account blocks to check"), "section"))
        self.input_box = QPlainTextEdit()
        self.input_box.setPlaceholderText(_PLACEHOLDER)
        self.input_box.setMaximumHeight(130)
        root.addWidget(self.input_box)

        controls = QHBoxLayout()
        self.check_button = set_variant(QPushButton("Check All"), "primary")
        theme.bind_icon(self.check_button, "circle-check", "on_accent")
        self.check_button.clicked.connect(self._start)
        self.stop_button = QPushButton("Stop")
        theme.bind_icon(self.stop_button, "circle-stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        controls.addWidget(self.check_button)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)
        self.summary_label = set_role(QLabel(""), "muted")
        controls.addWidget(self.summary_label)
        root.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.hide()
        root.addWidget(self.progress)

        # --- working accounts ---------------------------------------------
        self.working_header = set_role(QLabel("Working accounts"), "section")
        root.addWidget(self.working_header)
        self.working_table = self._make_table(_WORKING_COLUMNS, stretch_cols=(1, 6))
        self.working_table.cellDoubleClicked.connect(lambda *_: self._browse())
        root.addWidget(self.working_table, 3)

        # --- server unreachable: kept, since the server may come back -------
        self.unreachable_header = set_role(QLabel(), "warning")
        self.unreachable_header.setToolTip(
            "The server did not answer or was overloaded - the account itself was not refused.\n"
            "These are added to Working Accounts with a red light; Recheck turns them green once the server is back.",
        )
        root.addWidget(self.unreachable_header)
        self.unreachable_table = self._make_table(_UNREACHABLE_COLUMNS, stretch_cols=(1, 3))
        root.addWidget(self.unreachable_table, 1)

        # --- non-working accounts (collapsed) -----------------------------
        self.failed_toggle = set_variant(QPushButton("Non-working accounts (0)"), "disclosure")
        self.failed_toggle.setCheckable(True)
        self.failed_toggle.setChecked(False)
        self.failed_toggle.toggled.connect(self._toggle_failed)
        root.addWidget(self.failed_toggle)
        self.failed_table = self._make_table(_FAILED_COLUMNS, stretch_cols=(1, 2))
        self.failed_table.hide()
        root.addWidget(self.failed_table, 2)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.browse_button = set_variant(QPushButton("Browse streams"), "primary")
        self.browse_button.setToolTip(
            "Return to the main window with all working accounts\n(plus the ones whose server is unreachable, marked red)",
        )
        self.browse_button.setEnabled(False)
        self.browse_button.clicked.connect(self._browse)
        self.close_button = set_variant(QPushButton("Close"), "ghost")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        footer.addWidget(self.browse_button)
        root.addLayout(footer)
        self._update_unreachable_section()

        # Accounts imported from elsewhere are checked right away.
        if entries:
            self.input_box.setPlainText("\n".join(entries))
            self._start()

    @staticmethod
    def _make_table(columns: list[str], *, stretch_cols: tuple[int, ...]) -> QTableWidget:
        table = QTableWidget()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(32)
        table.setShowGrid(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        for col in stretch_cols:
            table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        return table

    # --- run lifecycle -------------------------------------------------

    def _start(self) -> None:
        if self._running:
            return
        raw = self.input_box.toPlainText()
        if not raw.strip():
            self.summary_label.setText("Paste at least one URL.")
            return

        self.working_table.setRowCount(0)
        self.unreachable_table.setRowCount(0)
        self.failed_table.setRowCount(0)
        self._rows.clear()
        self._running = True
        self.check_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.browse_button.setEnabled(False)
        self.input_box.setReadOnly(True)
        self.progress.setRange(0, 0)  # indeterminate until first progress tick
        self.progress.show()
        self.summary_label.setText("Checking...")
        self._update_failed_toggle()
        self._update_unreachable_section()

        self._signals = self._controller.start_bulk_check(raw)
        self._signals.progress.connect(self._on_progress)
        self._signals.succeeded.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._signals.finished.connect(self._on_finished)

    def _stop(self) -> None:
        self._controller.cancel_bulk_check()
        self.stop_button.setEnabled(False)
        self.summary_label.setText("Stopping...")

    def _on_progress(self, result: BulkResult, done: int, total: int) -> None:
        if self.progress.maximum() != total:
            self.progress.setRange(0, total)
        self.progress.setValue(done)
        self._upsert_row(result)
        self.summary_label.setText(f"{done}/{total} checked · {self.working_table.rowCount()} available")

    def _on_done(self, results: list[BulkResult]) -> None:
        for result in results:
            self._upsert_row(result)
        ok = self.working_table.rowCount()
        total = len(results)
        unreachable = self.unreachable_table.rowCount()
        kept = f" · {unreachable} server unreachable (kept)" if unreachable else ""
        self.summary_label.setText(f"Done · {ok}/{total} available{kept}")
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(total)

    def _on_failed(self, message: str) -> None:
        self.summary_label.setText(f"Error: {message}")

    def _on_finished(self) -> None:
        self._running = False
        self.check_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.input_box.setReadOnly(False)
        self.progress.hide()
        self._signals = None
        self.browse_button.setEnabled(bool(self._working_results()))

    # --- table helpers -------------------------------------------------

    def _upsert_row(self, result: BulkResult) -> None:
        if result.ok:
            table = self.working_table
        elif is_server_unavailable(result):
            table = self.unreachable_table
        else:
            table = self.failed_table
        existing = self._rows.get(result.line_number)
        if existing is not None and existing[0] is table:
            row = existing[1]
        else:
            row = table.rowCount()
            table.insertRow(row)
            self._rows[result.line_number] = (table, row)

        if result.ok:
            values = [
                str(result.line_number),
                result.real_url or result.base_url,
                result.expiry,
                result.max_connections,
                result.active_connections,
                result.timezone,
                result.username,
            ]
        elif table is self.unreachable_table:
            values = [
                str(result.line_number),
                result.base_url,
                result.username,
                friendly_error(result.error),
            ]
        else:
            values = [
                str(result.line_number),
                result.raw or result.base_url,
                result.error or "Failed",
            ]

        for col, text in enumerate(values):
            cell = QTableWidgetItem(text)
            if col == 0:
                cell.setData(Qt.ItemDataRole.UserRole, result)
            if col == len(values) - 1 and table is not self.working_table:
                cell.setToolTip(result.error)
            self.table_set(table, row, col, cell, result)

        self._update_failed_toggle()
        self._update_unreachable_section()

    @staticmethod
    def table_set(table: QTableWidget, row: int, col: int, cell: QTableWidgetItem, result: BulkResult) -> None:
        if result.ok:
            cell.setBackground(theme.color("success", 0.12))
        elif is_server_unavailable(result):
            cell.setBackground(theme.color("warning", 0.12))
        else:
            cell.setBackground(theme.color("danger", 0.10))
        table.setItem(row, col, cell)

    def _toggle_failed(self, checked: bool) -> None:
        self.failed_table.setVisible(checked)
        self._update_failed_toggle()

    def _update_failed_toggle(self) -> None:
        chevron = "chevron-down" if self.failed_toggle.isChecked() else "chevron-right"
        self.failed_toggle.setIcon(theme.icon(chevron))
        self.failed_toggle.setText(f"Non-working accounts ({self.failed_table.rowCount()})")

    def _update_unreachable_section(self) -> None:
        count = self.unreachable_table.rowCount()
        self.unreachable_header.setText(
            f"Server unreachable ({count}) - kept in Working Accounts (red) for a later Recheck",
        )
        self.unreachable_header.setVisible(count > 0)
        self.unreachable_table.setVisible(count > 0)

    def _working_results(self) -> list[BulkResult]:
        """Accounts to hand over: working ones, then unreachable-server ones."""
        results: list[BulkResult] = []
        for table in (self.working_table, self.unreachable_table):
            for row in range(table.rowCount()):
                cell = table.item(row, 0)
                if cell is None:
                    continue
                data = cell.data(Qt.ItemDataRole.UserRole)
                if isinstance(data, BulkResult) and (data.ok or is_server_unavailable(data)):
                    results.append(data)
        return results

    def _browse(self) -> None:
        results = self._working_results()
        if not results:
            return
        self.accounts_chosen.emit(results)
        self.accept()
