import requests
from urllib.parse import urlparse
from PyQt6.QtCore import QThread, pyqtSignal
from config import APP_VERSION, VERSION_CHECK_URL, parse_version

_DEFAULT_UPDATE_URL = "https://github.com/emxts-hub/AS400QuantumSuite/releases/latest"
_ALLOWED_UPDATE_HOSTS = {"github.com", "emxts-hub.github.io"}


def _valid_update_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https":
        return False
    host = parsed.netloc.lower()
    if host == "github.com":
        return parsed.path.startswith("/emxts-hub/AS400QuantumSuite/")
    return host == "emxts-hub.github.io"


class VersionCheckWorker(QThread):
    # Signal emits: (success, min_required_version, latest_version, update_url, error_msg)
    version_checked = pyqtSignal(bool, str, str, str, str)

    def run(self):
        try:
            headers = {"User-Agent": f"LPARManager/{APP_VERSION}"}
            response = requests.get(VERSION_CHECK_URL, headers=headers, timeout=5)

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Version metadata must be a JSON object")
                latest_ver = data.get("latest_version", APP_VERSION)
                min_ver = data.get("min_required_version", APP_VERSION)
                update_url = data.get("update_url", _DEFAULT_UPDATE_URL)
                parse_version(latest_ver)
                parse_version(min_ver)
                if not _valid_update_url(update_url):
                    raise ValueError("Version metadata contains an invalid update URL")

                self.version_checked.emit(True, min_ver, latest_ver, update_url, "")
            else:
                self.version_checked.emit(
                    False,
                    "",
                    "",
                    "",
                    f"HTTP Server returned status code {response.status_code}",
                )
        except Exception as e:
            # Allow fallback on network connection failure
            self.version_checked.emit(False, "", "", "", str(e))