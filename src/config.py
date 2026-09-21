import os
import sys
import json
import uuid
import time
from datetime import datetime, timezone

APP_NAME = "AS400 Quantum Suite"
APP_VERSION = "5.0.0"
USER_PROFILE = os.environ.get("USERPROFILE") or os.path.expanduser("~")
DEFAULT_ONEDRIVE_SHAREPOINT_PATH = os.path.join(
    USER_PROFILE,
    "OneDrive - Questronix Corporation",
    "MCSU Engineering and Hybrid Infra - Documents",
    "Projects",
    "BY",
    "RUNBOOK",
    "ASPCPU logs",
)
ONEDRIVE_SHAREPOINT_PATH = DEFAULT_ONEDRIVE_SHAREPOINT_PATH

HARD_EXPIRATION_DATE = datetime(2026, 12, 31, tzinfo=timezone.utc)
_expiration_env = os.getenv("APP_HARD_EXPIRATION_DATE")
if _expiration_env:
    try:
        parsed_expiration = datetime.fromisoformat(_expiration_env.replace("Z", "+00:00"))
        HARD_EXPIRATION_DATE = (
            parsed_expiration.replace(tzinfo=timezone.utc)
            if parsed_expiration.tzinfo is None
            else parsed_expiration.astimezone(timezone.utc)
        )
    except ValueError:
        pass

# GitHub Pages URL serving your version metadata
VERSION_CHECK_URL = "https://emxts-hub.github.io/monitoringtool/version.json"

def is_build_expired() -> bool:
    if HARD_EXPIRATION_DATE is None:
        return False
    now = datetime.now(timezone.utc)
    return now > HARD_EXPIRATION_DATE


def parse_version(ver_str: str) -> tuple:
    """Convert semver string ('1.0.0') to integer tuple (1, 0, 0) for comparison."""
    try:
        clean_str = ver_str.split("-")[0].strip()
        parts = tuple(map(int, clean_str.split(".")))
        if len(parts) != 3 or any(part < 0 for part in parts):
            raise ValueError("version must contain three non-negative components")
        return parts
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid version: {ver_str!r}") from None


def get_app_data_dir():
    """Returns the writable application-data directory for the current platform."""
    if sys.platform == "win32":
        base_dir = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.expanduser("~\\AppData\\Local")
        )
    elif sys.platform == "darwin":
        base_dir = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base_dir = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")

    app_data_dir = os.path.join(base_dir, APP_NAME)
    os.makedirs(app_data_dir, exist_ok=True)
    return app_data_dir

def get_config_path():
    """Returns the writable path to the application configuration file."""
    return os.path.join(get_app_data_dir(), "config.json")


def _load_saved_logs_root():
    try:
        with open(get_config_path(), "r", encoding="utf-8") as file:
            value = (json.load(file) or {}).get("LOGS_ROOT")
        if isinstance(value, str) and value.strip():
            return os.path.abspath(os.path.expandvars(os.path.expanduser(value.strip())))
    except (OSError, TypeError, ValueError):
        pass
    return DEFAULT_ONEDRIVE_SHAREPOINT_PATH


def set_logs_root(path):
    """Set the active root directory used for monthly logs."""
    global ONEDRIVE_SHAREPOINT_PATH
    raw_path = str(path).strip()
    if not raw_path:
        raise ValueError("Log root cannot be empty")
    normalized = os.path.abspath(os.path.expandvars(os.path.expanduser(raw_path)))
    ONEDRIVE_SHAREPOINT_PATH = normalized
    return normalized


ONEDRIVE_SHAREPOINT_PATH = _load_saved_logs_root()

LEGACY_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def _fallback_json_path(file_path: str) -> str:
    """Return the local fallback path used when the preferred Onedrive file is locked."""
    file_path = os.path.normpath(file_path)
    legacy_dir = os.path.normpath(LEGACY_LOGS_DIR)
    if os.path.dirname(file_path) == legacy_dir:
        return file_path
    return os.path.join(legacy_dir, os.path.basename(file_path))


def _year_log_root():
    """Return the top-level year folder under the SharePoint ASPCPU log archive."""
    current_year = datetime.now().strftime("%Y")
    root = os.path.join(ONEDRIVE_SHAREPOINT_PATH, current_year)
    os.makedirs(root, exist_ok=True)
    return root


def _monthly_log_root():
    """Return the SharePoint month folder that stores the daily JSON log files."""
    current_year = datetime.now().strftime("%Y")
    month_name = datetime.now().strftime("%B %Y")
    root = os.path.join(ONEDRIVE_SHAREPOINT_PATH, current_year, month_name)
    os.makedirs(root, exist_ok=True)
    return root


def get_logs_dir():
    """Use the current monthly SharePoint offline log directory; fall back to the legacy app folder only if needed."""
    try:
        preferred = _monthly_log_root()
        if os.path.isdir(preferred):
            return preferred
    except OSError:
        preferred = None

    try:
        os.makedirs(LEGACY_LOGS_DIR, exist_ok=True)
        return LEGACY_LOGS_DIR
    except OSError:
        raise


def get_monthly_logs_dir_for(month_key=None):
    """Return the SharePoint log folder for a specific month key, creating it if needed."""
    if month_key is None:
        month_key = datetime.now().strftime("%Y-%m")

    try:
        year, month_num = map(int, month_key.split("-"))
        month_label = datetime(year, month_num, 1).strftime("%B %Y")
    except (TypeError, ValueError):
        return get_logs_dir()

    preferred = os.path.join(ONEDRIVE_SHAREPOINT_PATH, str(year), month_label)
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except OSError:
        return get_logs_dir()


def get_all_logs_dirs():
    """Return every valid log root in the SharePoint archive, including the active month and historic month folders."""
    dirs = []
    candidates = [ONEDRIVE_SHAREPOINT_PATH, LEGACY_LOGS_DIR]
    for root_builder in (_monthly_log_root, _year_log_root):
        try:
            candidates.append(root_builder())
        except OSError:
            pass

    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            dirs.append(candidate)

    try:
        year_root = _year_log_root()
    except OSError:
        year_root = None
    if year_root is not None and os.path.isdir(year_root):
        for child in sorted(os.listdir(year_root)):
            child_path = os.path.join(year_root, child)
            if os.path.isdir(child_path):
                dirs.append(child_path)

    unique_dirs = []
    seen = set()
    for candidate in dirs:
        normalized = os.path.normpath(candidate)
        if normalized not in seen:
            seen.add(normalized)
            unique_dirs.append(candidate)
    return unique_dirs

def get_resource_path(relative_path):
    """Returns the best available path to a bundled resource, including dev, frozen, and user-data locations."""
    candidates = []

    def add_candidate(*parts):
        candidate = os.path.normpath(os.path.join(*parts))
        if candidate not in candidates:
            candidates.append(candidate)

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        add_candidate(meipass, relative_path)
        add_candidate(meipass, "src", relative_path)
        add_candidate(meipass, "Image & Sound", relative_path)
    elif getattr(sys, "frozen", False):
        add_candidate(os.path.dirname(sys.executable), relative_path)
        add_candidate(os.path.dirname(sys.executable), "src", relative_path)
        add_candidate(os.path.dirname(sys.executable), "Image & Sound", relative_path)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    add_candidate(script_dir, relative_path)
    add_candidate(script_dir, "src", relative_path)
    add_candidate(script_dir, "..", "Image & Sound", relative_path)
    add_candidate(os.getcwd(), relative_path)
    add_candidate(os.getcwd(), "src", relative_path)
    add_candidate(os.getcwd(), "Image & Sound", relative_path)
    add_candidate(get_app_data_dir(), relative_path)
    add_candidate(get_app_data_dir(), "src", relative_path)
    add_candidate(get_app_data_dir(), "Image & Sound", relative_path)

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return candidates[0] if candidates else relative_path

DEFAULT_SERVER_CONFIGS = {}
DEFAULT_EXPECTED_SUBSYSTEMS = {}
DEFAULT_EXPECTED_PORTS = {}
DEFAULT_EMAIL_ALERTS = {
    "enabled": False,
    "smtp_server": "",
    "port": 587,
    "use_tls": True,
    "username": "",
    "password": "",
    "from_address": "",
    "to_addresses": [],
    "threshold_percent": 40,
    "cooldown_minutes": 10,
    "refresh_interval_ms": 0,
    "log_dedupe_seconds": 60,
}


def _load_config_mapping(key, default):
    """Load a mapping from config.json, falling back when the shape is invalid."""
    config_path = get_config_path()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                value = data.get(key)
                if isinstance(value, dict):
                    return value
        except Exception:
            pass
    return default.copy()


def load_server_configs():
    """Loads server configurations from config.json or returns default."""
    return _load_config_mapping("SERVER_CONFIGS", DEFAULT_SERVER_CONFIGS)

def load_expected_subsystems():
    """Loads expected subsystems from config.json or returns default."""
    return _load_config_mapping("EXPECTED_SUBSYSTEMS", DEFAULT_EXPECTED_SUBSYSTEMS)

def load_expected_ports():
    """Loads expected ports from config.json or returns default."""
    return _load_config_mapping("EXPECTED_PORTS", DEFAULT_EXPECTED_PORTS)


def _atomic_write_json(target_path: str, payload) -> bool:
    """Write JSON to a destination path with an atomic replace and clean up temps on any error."""
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    temp_path = f"{target_path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
        return True
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def safe_json_save(file_path: str, data) -> bool:
    """Safely saves data to a JSON file using atomic file replacement to prevent OneDrive sync locks."""
    target_path = file_path
    try:
        return _atomic_write_json(target_path, data)
    except Exception:
        fallback_path = _fallback_json_path(file_path)
        if os.path.normpath(fallback_path) == os.path.normpath(file_path):
            return False
        try:
            return _atomic_write_json(fallback_path, data)
        except Exception:
            return False


def safe_json_append_and_save(file_path: str, new_entry: dict, max_retries: int = 20) -> bool:
    """Re-reads the latest file on disk right before writing to ensure no concurrent records are lost."""
    for attempt in range(max_retries):
        try:
            existing_data = []
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    try:
                        existing_data = json.load(f) or []
                        if not isinstance(existing_data, list):
                            existing_data = [existing_data]
                    except json.JSONDecodeError:
                        return False

            existing_data.append(new_entry)
            if _atomic_write_json(file_path, existing_data):
                return True
            return False

        except Exception:
            # OneDrive may briefly hold the daily file during synchronization.
            time.sleep(min(1.0, 0.15 * (attempt + 1)))

    fallback_path = _fallback_json_path(file_path)
    if os.path.normpath(fallback_path) != os.path.normpath(file_path):
        try:
            os.makedirs(os.path.dirname(fallback_path), exist_ok=True)
            current_data = []
            if os.path.exists(fallback_path):
                with open(fallback_path, "r", encoding="utf-8") as f:
                    try:
                        current_data = json.load(f) or []
                        if not isinstance(current_data, list):
                            current_data = [current_data]
                    except json.JSONDecodeError:
                        current_data = []
            current_data.append(new_entry)
            return _atomic_write_json(fallback_path, current_data)
        except Exception as fallback_err:
            print(f"[safe_json_append_and_save] Fallback save failed for {fallback_path}: {fallback_err}")
            return False

    return False


SERVER_CONFIGS = load_server_configs()
EXPECTED_SUBSYSTEMS = load_expected_subsystems()
EXPECTED_PORTS = load_expected_ports()

from ui.setcreds import (
    get_email_password,
    get_ibmi_password,
    load_email_alerts,
    load_login_credentials,
    save_all_configs,
    save_email_password,
    save_ibmi_password,
)
EMAIL_ALERTS = load_email_alerts()

MONITORED_PORTS = {}

SERVICE_COMMANDS = {
    21: "STRTCPSVR SERVER(*FTP)",
    22: "STRTCPSVR SERVER(*SSHD)",
    23: "STRTCPSVR SERVER(*TELNET)",
    25: "STRTCPSVR SERVER(*SMTP)",
    445: "STRTCPSVR SERVER(*NETS)",
    992: "STRTCPSVR SERVER(*ALL)",
    2001: "STRTCPSVR SERVER(*HTTP)",
    2002: "STRTCPSVR SERVER(*HTTP)",
    31111: "STRNETMAN",
    31114: "STARTTWS",
}

SUBSYSTEM_COMMANDS = {
    "QBATCH": "STRSBS SBSD(QBATCH)",
    "QINTER": "STRSBS SBSD(QINTER)",
    "QCMN": "STRSBS SBSD(QCMN)",
    "QCTL": "STRSBS SBSD(QCTL)",
    "QHTTPSVR": "STRTCPSVR SERVER(*HTTP)",
    "QSERVER": "STRSBS SBSD(QSERVER)",
    "QSNADS": "STRSBS SBSD(QSNADS)",
    "QSPL": "STRSBS SBSD(QSPL)",
    "QSYSWRK": "STRSBS SBSD(QSYSWRK)",
    "QUSRWRK": "STRSBS SBSD(QUSRWRK)",
    "Q1ABRMNET": "STRSBS SBSD(Q1ABRMNET)",
}