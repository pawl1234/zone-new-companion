"""Dialog to add one XTREAM, M3U or STALKER account to Working Accounts."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from zone_new_companion.models import Credentials, PortalType
from zone_new_companion.ui.theme import set_role, set_variant

# Menu text and dialog title per type, in menu order.
ACCOUNT_TYPES = {
    PortalType.XTREAM: ("XTREAM account…", "Add XTREAM account"),
    PortalType.M3U: ("M3U playlist…", "Add M3U playlist"),
    PortalType.STALKER: ("STALKER portal (MAC)…", "Add STALKER portal"),
}


class AddAccountDialog(QDialog):
    """Fields for one account type; the account is checked before it is added."""

    submitted = pyqtSignal(object)

    def __init__(self, portal: PortalType, prefill: Credentials | None = None, parent=None) -> None:
        super().__init__(parent)
        self._portal = portal
        self.setWindowTitle(ACCOUNT_TYPES[portal][1])
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        root.addLayout(form)

        self.base_url = QLineEdit()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.mac_address = QLineEdit()
        if portal == PortalType.XTREAM:
            # A pasted get.php / player_api.php link fills user + password.
            self.base_url.setPlaceholderText("http://example.com:8080 or a full get.php link")
            self.base_url.editingFinished.connect(self._split_xtream_link)
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            self.password_toggle = QPushButton("Show")
            self.password_toggle.setCheckable(True)
            self.password_toggle.setToolTip("Show or hide the password")
            self.password_toggle.toggled.connect(self._toggle_password_visibility)
            password_row = QHBoxLayout()
            password_row.setContentsMargins(0, 0, 0, 0)
            password_row.addWidget(self.password)
            password_row.addWidget(self.password_toggle)
            form.addRow("Server URL", self.base_url)
            form.addRow("Username", self.username)
            form.addRow("Password", password_row)
        elif portal == PortalType.M3U:
            self.base_url.setPlaceholderText("http://example.com/playlist.m3u8 or get.php?…")
            form.addRow("Playlist URL", self.base_url)
        else:
            self.base_url.setPlaceholderText("http://example.com/c/")
            self.mac_address.setPlaceholderText("00:1A:79:00:00:00")
            form.addRow("Portal URL", self.base_url)
            form.addRow("MAC Address", self.mac_address)

        if prefill is not None and prefill.portal_type == portal:
            self.base_url.setText(prefill.base_url)
            self.username.setText(prefill.username)
            self.password.setText(prefill.password)
            self.mac_address.setText(prefill.mac_address)

        hint = set_role(QLabel("The account is checked (login + streaming) before it is added."), "muted")
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = set_variant(QPushButton("Cancel"), "ghost")
        self.cancel_button.clicked.connect(self.reject)
        self.add_button = set_variant(QPushButton("Add"), "primary")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self._emit_credentials)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.add_button)
        root.addLayout(buttons)

    def _emit_credentials(self) -> None:
        if not self.add_button.isEnabled():
            return
        self.submitted.emit(Credentials(
            name="Last Used",
            base_url=self.base_url.text().strip(),
            portal_type=self._portal,
            username=self.username.text().strip(),
            password=self.password.text().strip(),
            mac_address=self.mac_address.text().strip(),
        ))

    def _split_xtream_link(self) -> None:
        """Move username/password out of a pasted get.php link into their fields."""
        parsed = urlparse(self.base_url.text().strip())
        params = parse_qs(parsed.query)
        username = (params.get("username") or [""])[0]
        password = (params.get("password") or [""])[0]
        if not (parsed.scheme and parsed.hostname and username and password):
            return
        port = f":{parsed.port}" if parsed.port else ""
        self.base_url.setText(f"{parsed.scheme}://{parsed.hostname}{port}")
        self.username.setText(username)
        self.password.setText(password)

    def _toggle_password_visibility(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.password.setEchoMode(mode)
        self.password_toggle.setText("Hide" if visible else "Show")

    def set_checking(self, checking: bool) -> None:
        """Lock the fields while the account is being checked."""
        for widget in (self.base_url, self.username, self.password, self.mac_address, self.add_button):
            widget.setEnabled(not checking)
        self.add_button.setText("Checking…" if checking else "Add")
