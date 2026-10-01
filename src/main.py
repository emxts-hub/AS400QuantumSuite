# main.py

import sys
import os
import ctypes
import traceback
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QTimer

from config import get_resource_path, APP_VERSION, is_build_expired, parse_version
from dialogs import AppExpirationDialog
from ui.styles import LIGHT_STYLESHEET
import ui.main_window
from version_worker import VersionCheckWorker


# Safe logger setup to capture crashes instead of freezing silently
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_error.log")

class DummyStream:
    """Safe fall-through stream to prevent write blocks on Windows frozen builds."""
    def write(self, data):
        pass
    def flush(self):
        pass

if sys.platform == "win32" and getattr(sys, 'frozen', False):
    try:
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass
    sys.stdout = DummyStream()
    sys.stderr = DummyStream()


def log_exception(exc_type, exc_value, exc_traceback):
    """Logs unhandled exceptions to app_error.log instead of freezing."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n--- UNHANDLED EXCEPTION ---\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)

sys.excepthook = log_exception


def resource_path(relative_path):
    return get_resource_path(relative_path)

def main():
    try:
        # 1. Initialize QApplication FIRST
        app = QApplication(sys.argv)
        app.setProperty("is_dark_theme", False)
        app.setStyleSheet(LIGHT_STYLESHEET)

        app_icon = QIcon(resource_path("logo.png"))
        app.setWindowIcon(app_icon)

        # 2. Check Expiration right after QApplication setup
        if is_build_expired():
            dialog = AppExpirationDialog(
                title="Application Expired",
                message="This build of the application has expired.",
                download_url="https://github.com/emxts-hub/AS400QuantumSuite/releases/latest"
            )
            dialog.exec()
            sys.exit(0)

        # 3. Load & Render Main UI First
        window = ui.main_window.IBMiDashboard()
        window.setWindowIcon(app_icon)
        window.showMaximized()

        # 4. Defer Version Worker check until GUI loop is active
        def run_version_check():
            def handle_version_check(success, min_ver, latest_ver, download_url, err):
                if success:
                    current_ver_tuple = parse_version(APP_VERSION)
                    min_ver_tuple = parse_version(min_ver)

                    if current_ver_tuple < min_ver_tuple:
                        window.hide()
                        dialog = AppExpirationDialog(
                            title="Update Required",
                            message=f"Your version ({APP_VERSION}) is no longer supported. Minimum required version is {min_ver}.",
                            download_url=download_url,
                        )
                        dialog.exec()
                        sys.exit(0)

            worker = VersionCheckWorker()
            worker.version_checked.connect(handle_version_check)
            worker.start()
            window._version_worker = worker

        # Defer execution by 200ms so window opens instantly
        
        QTimer.singleShot(200, run_version_check)

        sys.exit(app.exec())

    except Exception as e:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\nFatal startup crash: {str(e)}\n")
            traceback.print_exc(file=f)
        sys.exit(1)


if __name__ == "__main__":
    main()