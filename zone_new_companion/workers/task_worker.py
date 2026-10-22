"""Generic QRunnable wrapper for background tasks."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot


class TaskSignals(QObject):
    """Signals for async task lifecycle."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class BulkCheckSignals(QObject):
    """Signals for a bulk-check run.

    ``progress`` carries (BulkResult, done, total) as each line resolves;
    ``succeeded`` carries the full list[BulkResult] when the run completes.
    """

    progress = pyqtSignal(object, int, int)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class TaskWorker(QRunnable):
    """Run a callable in QThreadPool and emit status signals."""

    def __init__(self, task: Callable[[], Any]) -> None:
        super().__init__()
        self._task = task
        self.signals = TaskSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self._task()
        except Exception as exc:  # noqa: BLE001
            safe_emit(self.signals.failed, str(exc))
        else:
            safe_emit(self.signals.succeeded, result)
        safe_emit(self.signals.finished)


def safe_emit(signal: Any, *args: Any) -> None:
    """Emit unless the app is closing and the signals object is already gone.

    An exception escaping QRunnable.run makes PyQt abort the whole process.
    """
    try:
        signal.emit(*args)
    except RuntimeError:
        pass
