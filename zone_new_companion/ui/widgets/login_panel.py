"""Account import bar: add one XTREAM, M3U or STALKER account to the list."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QWidget,
)

from zone_new_companion.models import Credentials, PortalType

# Combo order; the stacked field pages follow it.
_TYPES = [(PortalType.XTREAM, "XTREAM"), (PortalType.M3U, "M3U"), (PortalType.STALKER, "STALKER")]


def _row(*widgets: tuple[QWidget, int]) -> QWidget:
    """A margin-less row of (widget, stretch) pairs."""
    page = QWidget()
    layout = QHBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget, stretch in widgets:
        layout.addWidget(widget, stretch)
    return page


class LoginPanel(QWidget):
    """One compact row: pick a type, fill its fields, add it to Working Accounts."""

    add_clicked = pyqtSignal(object)
    bulk_check_clicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)

        root.addWidget(QLabel("Add account:"))
        self.type_combo = QComboBox()
        for portal, label in _TYPES:
            self.type_combo.addItem(label, portal)
        self.type_combo.setToolTip("Kind of account to add")
        root.addWidget(self.type_combo)

        # XTREAM: a pasted get.php / player_api.php link fills user + password.
        self.xtream_base_url = QLineEdit()
        self.xtream_base_url.setPlaceholderText("Server URL or full get.php link")
        self.xtream_base_url.setToolTip(
            "XTREAM server URL, e.g. http://example.com:8080\n"
            "A full get.php?username=…&password=… link fills the other fields",
        )
        self.xtream_base_url.editingFinished.connect(self._split_xtream_link)
        self.xtream_username = QLineEdit()
        self.xtream_username.setPlaceholderText("Username")
        self.xtream_password = QLineEdit()
        self.xtream_password.setPlaceholderText("Password")
        self.xtream_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.xtream_password_toggle = QPushButton("Show")
        self.xtream_password_toggle.setCheckable(True)
        self.xtream_password_toggle.setToolTip("Show or hide the password")
        self.xtream_password_toggle.toggled.connect(self._toggle_password_visibility)

        self.m3u_url = QLineEdit()
        self.m3u_url.setPlaceholderText("Playlist URL (http://example.com/playlist.m3u8 or get.php?…)")
        self.m3u_url.setToolTip("M3U playlist URL or XTREAM get.php URL")

        self.stalker_base_url = QLineEdit()
        self.stalker_base_url.setPlaceholderText("Portal URL, e.g. http://example.com/c/")
        self.stalker_base_url.setToolTip("STALKER portal URL")
        self.stalker_mac = QLineEdit()
        self.stalker_mac.setPlaceholderText("MAC 00:1A:79:00:00:00")
        self.stalker_mac.setToolTip("MAC address for STALKER authentication")

        self.fields = QStackedWidget()
        self.fields.addWidget(_row(
            (self.xtream_base_url, 4), (self.xtream_username, 2), (self.xtream_password, 2),
            (self.xtream_password_toggle, 0),
        ))
        self.fields.addWidget(_row((self.m3u_url, 1)))
        self.fields.addWidget(_row((self.stalker_base_url, 3), (self.stalker_mac, 2)))
        self.type_combo.currentIndexChanged.connect(self.fields.setCurrentIndex)
        root.addWidget(self.fields, 1)

        self.add_button = QPushButton("Add")
        self.add_button.setDefault(True)
        self.add_button.setToolTip("Check the account (login + streaming) and add it to Working Accounts")
        self.add_button.clicked.connect(self._emit_credentials)
        root.addWidget(self.add_button)
        for field in (
            self.xtream_base_url, self.xtream_username, self.xtream_password,
            self.m3u_url, self.stalker_base_url, self.stalker_mac,
        ):
            field.returnPressed.connect(self._emit_credentials)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(separator)

        self.bulk_check_button = QPushButton("Bulk Import…")
        self.bulk_check_button.setToolTip("Check many Xtream URLs at once and add the working ones")
        self.bulk_check_button.clicked.connect(self.bulk_check_clicked.emit)
        root.addWidget(self.bulk_check_button)

    def _emit_credentials(self) -> None:
        """Emit credentials for the selected account type."""
        if not self.add_button.isEnabled():
            return
        portal = self.type_combo.currentData()
        username = password = mac_address = ""
        if portal == PortalType.XTREAM:
            base_url = self.xtream_base_url.text().strip()
            username = self.xtream_username.text().strip()
            password = self.xtream_password.text().strip()
        elif portal == PortalType.M3U:
            base_url = self.m3u_url.text().strip()
        else:
            base_url = self.stalker_base_url.text().strip()
            mac_address = self.stalker_mac.text().strip()

        self.add_clicked.emit(Credentials(
            name="Last Used",
            base_url=base_url,
            portal_type=portal,
            username=username,
            password=password,
            mac_address=mac_address,
        ))

    def _split_xtream_link(self) -> None:
        """Move username/password out of a pasted get.php link into their fields."""
        parsed = urlparse(self.xtream_base_url.text().strip())
        params = parse_qs(parsed.query)
        username = (params.get("username") or [""])[0]
        password = (params.get("password") or [""])[0]
        if not (parsed.scheme and parsed.hostname and username and password):
            return
        port = f":{parsed.port}" if parsed.port else ""
        self.xtream_base_url.setText(f"{parsed.scheme}://{parsed.hostname}{port}")
        self.xtream_username.setText(username)
        self.xtream_password.setText(password)

    def _toggle_password_visibility(self, visible: bool) -> None:
        """Switch the XTREAM password field between masked and plain text."""
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.xtream_password.setEchoMode(mode)
        self.xtream_password_toggle.setText("Hide" if visible else "Show")

    def set_checking(self, checking: bool) -> None:
        """Lock the Add button while the new account is being checked."""
        self.add_button.setEnabled(not checking)
        self.add_button.setText("Checking…" if checking else "Add")

    def clear(self) -> None:
        """Clear all editable fields."""
        self.xtream_base_url.clear()
        self.xtream_username.clear()
        self.xtream_password.clear()
        self.xtream_password_toggle.setChecked(False)
        self.m3u_url.clear()
        self.stalker_base_url.clear()
        self.stalker_mac.clear()

    def populate_form(self, credentials: Credentials) -> None:
        """Load credentials into the fields of their type."""
        index = next((i for i, (portal, _label) in enumerate(_TYPES) if portal == credentials.portal_type), 0)
        self.type_combo.setCurrentIndex(index)
        if credentials.portal_type == PortalType.XTREAM:
            self.xtream_base_url.setText(credentials.base_url)
            self.xtream_username.setText(credentials.username)
            self.xtream_password.setText(credentials.password)
        elif credentials.portal_type == PortalType.M3U:
            self.m3u_url.setText(credentials.base_url)
        elif credentials.portal_type == PortalType.STALKER:
            self.stalker_base_url.setText(credentials.base_url)
            self.stalker_mac.setText(credentials.mac_address)
