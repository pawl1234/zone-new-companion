"""Item table model: a model/view table so big categories render instantly.

A QTableWidget builds one object per cell (and the old version two buttons per
row), all on the UI thread; with thousands of channels that froze the window
for seconds on every state change. The view only paints visible rows.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QColor

from zone_new_companion.models import MediaItem

NUM_COL, NAME_COL, NOW_COL, STATUS_COL, VERIFY_COL, PLAY_COL = range(6)
HEADERS = ["#", "Name", "Now Playing", "Status", "Verify", "Play"]

_WHITE = QColor("#ffffff")
_BLACK = QColor("#000000")
# (status cell background, status text, tint for the rest of the row)
_OK = (QColor("#2d5a2d"), _WHITE, QColor(46, 125, 50, 80))
_OFF = (QColor("#8b0000"), _WHITE, QColor(198, 40, 40, 80))
_UNSTABLE = (QColor("#ff8c00"), _BLACK, None)
_UNKNOWN = (QColor("#4a4a4a"), _WHITE, None)


def provider_num(metadata: dict, position: int) -> int:
    """The provider's channel number ("num"/"number"), else list position."""
    for key in ("num", "number"):
        try:
            return int(float(metadata.get(key)))
        except (TypeError, ValueError):
            continue
    return position


def _status_colors(status: str) -> tuple[QColor, QColor, QColor | None]:
    if status.startswith(("OK", "✓")):
        return _OK
    if status.startswith(("OFF", "✗")):
        return _OFF
    if status.startswith(("UNSTABLE", "⚠")):
        return _UNSTABLE
    return _UNKNOWN


class MediaTableModel(QAbstractTableModel):
    """Rows of MediaItem plus their now-playing and verification text."""

    def __init__(self) -> None:
        super().__init__()
        self._items: list[MediaItem] = []
        self._nums: list[int] = []
        self._now_playing: dict[str, str] = {}
        self._persistent: dict[str, str] = {}
        self._verification: dict[str, str] = {}

    def set_items(self, items: list[MediaItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._nums = [provider_num(media.metadata, position) for position, media in enumerate(items, start=1)]
        self.endResetModel()

    def set_annotations(
        self,
        now_playing: dict[str, str],
        persistent: dict[str, str],
        verification: dict[str, str],
    ) -> None:
        """Swap now-playing/status text; repaints cells, keeps rows and selection."""
        self._now_playing = now_playing
        self._persistent = persistent
        self._verification = verification
        if self._items:
            self.dataChanged.emit(self.index(0, NUM_COL), self.index(len(self._items) - 1, STATUS_COL))

    def media_at(self, row: int) -> MediaItem | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def _status(self, media: MediaItem) -> str:
        # Persistent results survive category changes; fall back to this tab's.
        return self._persistent.get(media.id) or self._verification.get(media.id, "")

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        media = self._items[row]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == NUM_COL:
                return self._nums[row]
            if col == NAME_COL:
                return media.name
            if col == NOW_COL:
                return self._now_playing.get(media.id, "")
            if col == STATUS_COL:
                return self._status(media)
            if col == VERIFY_COL:
                return "🔍"
            return "▶"
        if role == Qt.ItemDataRole.UserRole:
            return media
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (VERIFY_COL, PLAY_COL):
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ToolTipRole and col in (VERIFY_COL, PLAY_COL):
            return "Verify stream" if col == VERIFY_COL else "Play stream"
        if role == Qt.ItemDataRole.BackgroundRole and col <= STATUS_COL:
            background, _text, tint = _status_colors(self._status(media))
            return background if col == STATUS_COL else tint
        if role == Qt.ItemDataRole.ForegroundRole and col == STATUS_COL:
            return _status_colors(self._status(media))[1]
        return None
