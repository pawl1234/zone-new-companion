"""Simple non-intrusive toast widget."""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QParallelAnimationGroup, QPoint, QPropertyAnimation, QTimer, Qt
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

# How far the toast slides up while it fades in, in pixels.
_SLIDE = 12
_FADE_IN_MS = 220
_FADE_OUT_MS = 260


class ToastLabel(QLabel):
    """Centered toast above the status bar that fades and slides in, then fades out."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._show_anim = QParallelAnimationGroup(self)
        self._show_anim.addAnimation(self._fade)
        self._show_anim.addAnimation(self._slide)

        self._hide_anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._hide_anim.setDuration(_FADE_OUT_MS)
        self._hide_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._hide_anim.setEndValue(0.0)
        self._hide_anim.finished.connect(self.hide)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._hide_anim.start)
        self.hide()

    def show_message(self, text: str, timeout_ms: int = 2600) -> None:
        """Show toast and auto-hide; a new message replaces the current one."""
        self._timer.stop()
        self._hide_anim.stop()
        self._show_anim.stop()
        self.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        target = QPoint(0, 0)
        if parent is not None:
            # Centered just above the status bar, clear of the toolbar buttons.
            status_bar = getattr(parent, "statusBar", None)
            bottom = parent.height() - (status_bar().height() if callable(status_bar) else 0)
            target = QPoint((parent.width() - self.width()) // 2, bottom - self.height() - 20)
        # Already visible: swap the text in place instead of sliding again.
        start = target if self.isVisible() else target + QPoint(0, _SLIDE)
        self._fade.setDuration(_FADE_IN_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(1.0)
        self._slide.setDuration(_FADE_IN_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.setStartValue(start)
        self._slide.setEndValue(target)
        self.move(start)
        self.show()
        self.raise_()
        self._show_anim.start()
        self._timer.start(timeout_ms)
