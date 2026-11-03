import os
import tempfile
from urllib.parse import urlparse

import requests
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
        path = parsed.path.strip("/")
        return path.startswith("emxts-hub/") and path.endswith("/releases/latest")
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


class UpdateDownloadWorker(QThread):
    download_progress = pyqtSignal(int, int)
    download_finished = pyqtSignal(bool, str, str)

    def __init__(self, update_page_url, destination):
        super().__init__()
        self.update_page_url = update_page_url
        self.destination = destination

    def run(self):
        temp_path = ""
        try:
            if not _valid_update_url(self.update_page_url):
                raise ValueError("The update URL is invalid")

            headers = {"User-Agent": f"AS400QuantumSuite/{APP_VERSION}"}
            api_url = "https://api.github.com/repos/emxts-hub/AS400QuantumSuite/releases/latest"
            release_response = requests.get(api_url, headers=headers, timeout=15)
            release_response.raise_for_status()
            release = release_response.json()
            if not isinstance(release, dict):
                raise ValueError("GitHub returned invalid release metadata")

            assets = release.get("assets")
            if not isinstance(assets, list):
                raise ValueError("The latest release does not contain a valid asset list")
            installer = next(
                (
                    asset for asset in assets
                    if isinstance(asset, dict)
                    and asset.get("name") == "AS400QuantumSuite-Setup.exe"
                ),
                None,
            )
            if installer is None:
                raise ValueError("The latest release does not include the Windows installer")

            download_url = installer.get("browser_download_url")
            if not isinstance(download_url, str):
                raise ValueError("The latest release contains an invalid installer URL")
            parsed_download_url = urlparse(download_url)
            if (
                parsed_download_url.scheme != "https"
                or parsed_download_url.netloc.lower() != "github.com"
                or not parsed_download_url.path.startswith(
                    "/emxts-hub/AS400QuantumSuite/releases/download/"
                )
            ):
                raise ValueError("The latest release contains an invalid installer URL")

            destination_dir = os.path.dirname(os.path.abspath(self.destination))
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".as400-update-",
                suffix=".part",
                dir=destination_dir,
                delete=False,
            ) as output:
                temp_path = output.name
                with requests.get(
                    download_url,
                    headers=headers,
                    stream=True,
                    timeout=(15, 60),
                ) as download_response:
                    download_response.raise_for_status()
                    try:
                        total_bytes = int(download_response.headers.get("content-length", 0))
                    except (TypeError, ValueError):
                        total_bytes = 0

                    downloaded_bytes = 0
                    for chunk in download_response.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            output.write(chunk)
                            downloaded_bytes += len(chunk)
                            self.download_progress.emit(downloaded_bytes, total_bytes)

            if downloaded_bytes == 0:
                raise ValueError("The downloaded installer is empty")
            if total_bytes and downloaded_bytes != total_bytes:
                raise ValueError("The installer download was incomplete")

            os.replace(temp_path, self.destination)
            temp_path = ""
            self.download_finished.emit(True, self.destination, "")
        except (requests.RequestException, OSError, ValueError) as error:
            error_message = str(error)
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError as cleanup_error:
                    error_message = (
                        f"{error_message}; could not remove temporary file "
                        f"{temp_path}: {cleanup_error}"
                    )
            self.download_finished.emit(False, "", error_message)