"""Application bootstrap."""

from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

# Set Windows AppUserModelID BEFORE any UI initialization (must be before QApplication)
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('kidpoleon.zone-new-companion.1.2.3')
    except Exception:
        pass

# Determine base directory for resources (PyInstaller or normal)
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import Qt
import qdarkstyle

from zone_new_companion.config import ConfigStore
from zone_new_companion.controllers.app_controller import AppController
from zone_new_companion.services.bulk_checker import BulkResult, friendly_error, is_server_unavailable
from zone_new_companion.logging_config import configure_logging
from zone_new_companion.services.filter_debug import DEBUG_DIR, debug_enabled
from zone_new_companion.models import Credentials, MediaItem, PlaylistCategory, PortalType
from zone_new_companion.state import StateStore
from zone_new_companion.ui.main_window import MainWindow
from zone_new_companion.ui.widgets.add_account_dialog import AddAccountDialog
from zone_new_companion.ui.widgets.bulk_check_dialog import BulkCheckDialog
from zone_new_companion import __version__


def run() -> None:
    """Start the GUI application."""
    app_data = Path.home() / ".zone-new-companion"
    configure_logging(app_data / "logs", debug=debug_enabled())

    config_store = ConfigStore(app_data / "config.json")
    state_store = StateStore()
    controller = AppController(state_store, config_store)

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("zone-new-companion")
    qt_app.setApplicationVersion(__version__)
    
    # Set application icon (shows in taskbar on Windows)
    icon_path = os.path.join(BASE_DIR, "zone_new_companion", "icon", "icon.ico")
    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        qt_app.setWindowIcon(app_icon)
    
    if controller.config.ui.dark_theme:
        qt_app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api="pyqt6"))

    window = MainWindow()
    
    # Set window icon explicitly (inherited from app, but setting again ensures it)
    if os.path.exists(icon_path):
        window.setWindowIcon(QIcon(icon_path))
    
    window.resize(controller.config.ui.width, controller.config.ui.height)
    _refresh_history_menu(window, controller)
    window.set_help_info(lambda: _show_info(window))

    def on_error(message: str) -> None:
        QMessageBox.critical(window, "Error", message)

    state_store.state_changed.connect(window.apply_state)
    window.add_account_requested.connect(
        lambda portal: _open_add_account(window, controller, portal, controller.config.last_input, on_error),
    )
    window.category_selected.connect(
        lambda tab_name, category: _load_category(controller, tab_name, category, on_error, window.notify),
    )
    window.item_activated.connect(
        lambda tab_name, item: _activate_item(controller, tab_name, item, on_error, window.notify),
    )
    window.back_requested.connect(lambda tab_name: _go_back(controller, tab_name, window.notify, on_error))
    window.history_selected.connect(lambda index: _pick_history(window, controller, index, on_error))
    window.history_clear_requested.connect(lambda: _clear_history(window, controller))
    window.verify_item_requested.connect(
        lambda tab_name, item: controller.verify_single_item(tab_name, item, on_success=window.notify, on_error=on_error),
    )
    window.hls_play_requested.connect(
        lambda item: controller.play(item, on_success=window.notify, on_error=on_error, prefer_hls=True),
    )
    window.hls_probe_requested.connect(
        lambda item: controller.probe_hls(
            item, on_result=lambda hls_url: window.show_hls_result(item.name, hls_url), on_error=on_error,
        ),
    )
    window.verify_all_channels_requested.connect(
        lambda tab_name: controller.verify_all_channels(tab_name, on_success=window.notify, on_error=on_error),
    )
    window.verify_tab_requested.connect(
        lambda tab_name: controller.verify_tab(tab_name, on_success=window.notify, on_error=on_error),
    )
    window.verify_cancel_requested.connect(
        lambda: controller.cancel_verification(on_success=window.notify, on_error=on_error),
    )
    window.now_playing_requested.connect(lambda tab_name, item: controller.request_now_playing(tab_name, item))
    window.bulk_check_requested.connect(lambda: _open_bulk_check(window, controller, on_error))
    window.working_account_selected.connect(
        lambda result: controller.connect_from_bulk(
            result,
            on_success=lambda message: _on_connect_success(window, controller, message),
            on_error=on_error,
        ),
    )

    window.working_accounts_clear_requested.connect(
        lambda: _show_working_accounts(window, controller, []),
    )
    window.working_account_remove_requested.connect(
        lambda result: window.show_working_accounts(controller.remove_working_accounts([result]), announce=False),
    )
    window.working_account_star_toggled.connect(
        lambda result: window.show_working_accounts(controller.toggle_working_account_star(result), announce=False),
    )
    window.working_accounts_recheck_requested.connect(lambda: _recheck_working_accounts(window, controller, on_error))
    window.show_working_accounts(controller.working_accounts(), announce=False)

    window.set_language(controller.config.ui.language)
    window.language_changed.connect(controller.set_language)
    window.set_language_strict(controller.config.ui.language_strict)
    window.language_strict_changed.connect(controller.set_language_strict)
    if controller.debug_enabled:
        window.enable_debug_menu(str(DEBUG_DIR))
        window.debug_dump_requested.connect(lambda: controller.dump_full_catalog(window.notify, on_error))

    qt_app.aboutToQuit.connect(lambda: controller.save_ui_state(window.width(), window.height()))
    
    # Show as normal window with controls (not fullscreen) - ensures minimize/maximize/close buttons
    window.show()
    
    sys.exit(qt_app.exec())


def _load_category(
    controller: AppController,
    tab_name: str,
    category: PlaylistCategory,
    on_error: Callable[[str], None],
    on_success: Callable[[str], None],
) -> None:
    controller.load_items(
        tab_name=tab_name,
        category=category,
        on_success=on_success,
        on_error=on_error,
    )


def _activate_item(
    controller: AppController,
    tab_name: str,
    item: MediaItem,
    on_error: Callable[[str], None],
    on_success: Callable[[str], None],
) -> None:
    controller.activate_item(tab_name, item, on_success=on_success, on_error=on_error)


def _go_back(
    controller: AppController,
    tab_name: str,
    on_success: Callable[[str], None],
    on_error: Callable[[str], None],
) -> None:
    if controller.go_back(tab_name):
        on_success("Returned to previous list")
    else:
        on_error("No previous list for this tab.")


def _pick_history(
    window: MainWindow,
    controller: AppController,
    index: int,
    on_error: Callable[[str], None],
) -> None:
    history = controller.history_entries()
    if 0 <= index < len(history):
        _open_add_account(window, controller, history[index].portal_type, history[index], on_error)


def _open_bulk_check(
    window: MainWindow,
    controller: AppController,
    on_error: Callable[[str], None],
) -> None:
    dialog = BulkCheckDialog(controller, parent=window)
    # The dialog hands back every working account; the main window renders them
    # as a pickable list and each click loads that account's streams.
    dialog.accounts_chosen.connect(
        lambda results: window.show_working_accounts(controller.merge_working_accounts(results)),
    )
    # Make sure a running check is cancelled if the dialog is dismissed.
    dialog.finished.connect(lambda _code: controller.cancel_bulk_check())
    dialog.exec()


def _open_add_account(
    window: MainWindow,
    controller: AppController,
    portal: PortalType,
    prefill: Credentials | None,
    on_error: Callable[[str], None],
) -> None:
    """Ask for one account of the chosen type; it stays open while it is checked."""
    dialog = AddAccountDialog(portal, prefill, parent=window)
    dialog.submitted.connect(lambda credentials: _add_account(window, controller, dialog, credentials, on_error))
    dialog.exec()


def _add_account(
    window: MainWindow,
    controller: AppController,
    dialog: AddAccountDialog,
    credentials: Credentials,
    on_error: Callable[[str], None],
) -> None:
    """Check one typed-in account, add it to the list and browse it."""
    signals = controller.check_new_account(credentials)
    if isinstance(signals, str):
        QMessageBox.warning(dialog, "Check the input", signals)
        return
    dialog.set_checking(True)
    running = [True]

    def on_finished() -> None:
        running[0] = False
        dialog.set_checking(False)

    signals.succeeded.connect(lambda results: _finish_add(window, controller, dialog, results, on_error))
    signals.failed.connect(on_error)
    signals.finished.connect(on_finished)
    # Closing the dialog abandons a check that is still running.
    dialog.rejected.connect(lambda: running[0] and controller.cancel_bulk_check())
    # Keep the signals object alive until the check reports back.
    dialog._signals = signals


def _finish_add(
    window: MainWindow,
    controller: AppController,
    dialog: AddAccountDialog,
    results: list,
    on_error: Callable[[str], None],
) -> None:
    if not dialog.isVisible():
        return
    result: BulkResult = results[0]
    kind = result.portal_type.upper()
    if not result.ok:
        reason = friendly_error(result.error)
        if is_server_unavailable(result):
            text = f"The {kind} server is unreachable right now ({reason})."
        else:
            text = f"The {kind} account did not log in: {reason}."
        answer = QMessageBox.question(
            dialog,
            "Account not working",
            f"{text}\n\nAdd it to the list anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
    dialog.accept()
    accounts = controller.add_working_account(result)
    window.show_working_accounts(accounts, announce=False, select=result)
    if not result.ok:
        window.notify(f"{kind} account added (not working right now)")
        return
    streaming = "and streaming" if result.stream_ok else "but not streaming"
    window.notify(f"{kind} account added — logs in {streaming}")
    controller.connect_from_bulk(
        result,
        on_success=lambda message: _on_connect_success(window, controller, message),
        on_error=on_error,
    )


def _show_working_accounts(window: MainWindow, controller: AppController, results: list) -> None:
    controller.save_working_accounts(results)
    window.show_working_accounts(results)


def _recheck_working_accounts(
    window: MainWindow,
    controller: AppController,
    on_error: Callable[[str], None],
) -> None:
    if not controller.working_accounts():
        return
    window.set_recheck_running(True)
    signals = controller.recheck_working_accounts()
    signals.progress.connect(
        lambda _result, done, total: window.set_recheck_running(True, f"Rechecking {done}/{total}…"),
    )
    signals.succeeded.connect(lambda results: _finish_recheck(window, controller, results))
    signals.failed.connect(on_error)
    signals.finished.connect(lambda: window.set_recheck_running(False))
    # Keep the signals object alive until the run reports back.
    window._recheck_signals = signals


def _finish_recheck(window: MainWindow, controller: AppController, results: list) -> None:
    dead = controller.apply_recheck(results)
    accounts = controller.working_accounts()
    window.show_working_accounts(accounts, announce=False)
    not_streaming = sum(1 for account in accounts if account.stream_ok is False)
    if not dead:
        if not_streaming:
            window.notify(f"{not_streaming} account(s) can't stream right now - kept in the list (red)")
        else:
            window.notify("All working accounts log in and stream")
        return
    # Group by provider: dead accounts almost always fail together per server.
    by_provider: dict[str, list] = {}
    for account in dead:
        provider = urlparse(account.base_url).hostname or account.base_url
        by_provider.setdefault(provider, []).append(account)
    summary = []
    for provider, group in sorted(by_provider.items(), key=lambda item: -len(item[1])):
        reasons = sorted({friendly_error(account.error) for account in group})
        count = f"{len(group)} account{'s' if len(group) != 1 else ''}"
        summary.append(f"•  {provider}  ({count}) — {', '.join(reasons)}")
    details = [
        f"{'★ ' if account.starred else ''}{account.portal_type.upper()} "
        f"{account.username or account.mac_address or 'playlist'} @ {urlparse(account.base_url).hostname}: "
        f"{account.error or 'not working'}"
        for account in dead
    ]
    starred = sum(1 for account in dead if account.starred)
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Remove non-working accounts?")
    box.setText(f"The server rejected {len(dead)} account{'s' if len(dead) != 1 else ''}.")
    box.setInformativeText(
        "\n".join(summary)
        + (f"\n\n{starred} of them {'is' if starred == 1 else 'are'} starred." if starred else "")
        + (
            "\n\nAccounts whose server is only unreachable stay in the list (red)."
            if not_streaming > len(dead)
            else ""
        )
        + "\n\nRemove them from the list?",
    )
    box.setDetailedText("\n".join(details))
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    answer = box.exec()
    if answer == QMessageBox.StandardButton.Yes:
        window.show_working_accounts(controller.remove_working_accounts(dead), announce=False)
        window.notify(f"Removed {len(dead)} non-working account(s)")


def _on_connect_success(window: MainWindow, controller: AppController, message: str) -> None:
    _refresh_history_menu(window, controller)
    window.notify(message)


def _refresh_history_menu(window: MainWindow, controller: AppController) -> None:
    groups: dict[str, list[tuple[int, str]]] = {}
    history = controller.history_entries()
    for idx, row in enumerate(history):
        day = (row.saved_at or "Unknown Date").split("T", 1)[0]
        label = f"{row.portal_type.value.upper()} | {row.base_url} | {row.username or row.mac_address or 'anonymous'}"
        groups.setdefault(day, []).append((idx, label))
    window.set_history_grouped(groups)


def _clear_history(window: MainWindow, controller: AppController) -> None:
    controller.clear_history()
    _refresh_history_menu(window, controller)
    window.notify("History cleared")


def _show_info(window: MainWindow) -> None:
    QMessageBox.information(
        window,
        "About zone-new-companion",
        "zone-new-companion\n"
        f"Version: {__version__}\n\n"
        "GitHub: https://github.com/kidpoleon/zone-new-companion\n"
        "Support: Open an issue on GitHub\n",
    )
