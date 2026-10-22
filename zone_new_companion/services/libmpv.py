"""Locate libmpv and import python-mpv for the built-in player.

The built-in player needs the libmpv shared library. A packaged app ships it
next to the executable; in development it comes from the system (Homebrew,
the distro's libmpv package, or a libmpv-2.dll on PATH). Without it the app
falls back to external VLC.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import ModuleType

LOGGER = logging.getLogger(__name__)

# Same user agent the VLC launch sends; some portals only serve known players.
USER_AGENT = "Lavf/57.83.100"

_module: ModuleType | None = None
_error: str | None = None


def _library_dirs() -> list[str]:
    """Folders that may hold libmpv, the app's own bundle first."""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(getattr(sys, "_MEIPASS", ""))
        dirs.append(os.path.dirname(sys.executable))
    # A libmpv dropped into the project root works in development too.
    dirs.append(str(Path(__file__).resolve().parents[2]))
    if sys.platform == "darwin":
        dirs += ["/opt/homebrew/lib", "/usr/local/lib"]
    return [d for d in dirs if d and os.path.isdir(d)]


def _expose(dirs: list[str]) -> None:
    """Make the folders visible to ctypes.util.find_library, which python-mpv uses."""
    if sys.platform == "win32":
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        for folder in dirs:
            os.add_dll_directory(folder)
    elif sys.platform == "darwin":
        current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(dirs + ([current] if current else []))


def load() -> ModuleType | None:
    """The python-mpv module, or None when libmpv cannot be loaded (see ``error``)."""
    global _module, _error
    if _module is not None or _error is not None:
        return _module
    _expose(_library_dirs())
    try:
        import mpv  # noqa: PLC0415 - must run after _expose
    except (ImportError, OSError) as exc:
        _error = str(exc)
        LOGGER.warning("Built-in player unavailable, falling back to VLC: %s", exc)
        return None
    _module = mpv
    return _module


def error() -> str | None:
    """Why libmpv could not be loaded, after a failed ``load()``."""
    return _error
