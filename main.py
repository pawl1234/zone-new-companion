"""Entry point for zone-new-companion."""

import sys
import os
import traceback
from pathlib import Path


def self_test(report_path: str) -> int:
    """Import the whole app and load libmpv without opening a window.

    The build scripts run the packaged executable with ``--self-test <file>``:
    a windowed build has no console, so the result goes to the report file.
    Exit code 0: all fine; 1: an import failed; 2: libmpv could not be loaded.
    """
    try:
        import zone_new_companion.app  # noqa: F401 - pulls in every module the app needs
        from zone_new_companion.services import libmpv
        code = 0 if libmpv.load() is not None else 2
        report = "ok" if code == 0 else f"libmpv unavailable: {libmpv.error()}"
    except Exception:
        code, report = 1, traceback.format_exc()
    Path(report_path).write_text(report + "\n", encoding="utf-8")
    return code


def report_fatal_error(message: str) -> None:
    """Make a startup crash visible: a windowed build has no console for print()."""
    log_dir = Path.home() / ".zone-new-companion" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "startup-error.log").write_text(message, encoding="utf-8")
    except OSError:
        pass
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    elif sys.platform == "win32":
        # Qt may be what failed to load, so use the plain Win32 message box.
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"zone-new-companion could not start.\n\n{message}\n\nSaved to {log_dir / 'startup-error.log'}",
            "zone-new-companion",
            0x10,  # MB_ICONERROR
        )


def main():
    """Main entry point with error handling and debug mode."""
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))
    try:
        # Check for debug mode
        debug_mode = os.getenv("DEBUG", "0") == "1"
        if debug_mode:
            print("Debug mode enabled: dumps go to ~/.zone-new-companion/debug/")
        # Qt's own logging is very noisy; only on request.
        if os.getenv("DEBUG_QT", "0") == "1":
            os.environ["QT_LOGGING_RULES"] = "*=true"

        # Import and run the application
        # Note: Windows AppUserModelID is set in app.py BEFORE QApplication creation
        from zone_new_companion.app import run
        run()

    except KeyboardInterrupt:
        print("\nApplication interrupted by user")
        sys.exit(0)
    except Exception as e:
        report_fatal_error(f"Fatal error: {e}\n\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
