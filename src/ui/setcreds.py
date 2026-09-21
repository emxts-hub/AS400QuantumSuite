"""Settings and credential persistence for AS400 Quantum Suite."""

import json
import os


def _config_module():
    import config

    return config


try:
    import keyring
    _KEYRING_AVAILABLE = True
except Exception:
    keyring = None
    _KEYRING_AVAILABLE = False

_EMAIL_SERVICE_NAME = "AS400 Quantum Suite_smtp"
_IBMI_SERVICE_NAME = "AS400 Quantum Suite_ibmi"
_SETTINGS_BACKUP_FORMAT = "AS400 Quantum Suite settings backup"
_SETTINGS_BACKUP_VERSION = 1


def save_settings_backup(file_path, data):
    """Write a portable settings backup, including credentials supplied by the caller."""
    if not isinstance(data, dict):
        raise ValueError("Settings backup data must be an object")
    payload = dict(data)
    payload["format"] = _SETTINGS_BACKUP_FORMAT
    payload["version"] = _SETTINGS_BACKUP_VERSION
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")
    return True


def load_settings_backup(file_path):
    """Read and validate a portable settings backup file."""
    with open(file_path, "r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("Settings backup must contain an object")
    if payload.get("format") != _SETTINGS_BACKUP_FORMAT:
        raise ValueError("This file is not an AS400 Quantum Suite settings backup")
    if payload.get("version") != _SETTINGS_BACKUP_VERSION:
        raise ValueError("Unsupported settings backup version")
    required_mappings = ("SERVER_CONFIGS", "EXPECTED_SUBSYSTEMS", "EXPECTED_PORTS", "EMAIL_ALERTS", "LOGIN_CREDENTIALS")
    if any(not isinstance(payload.get(key), dict) for key in required_mappings):
        raise ValueError("Settings backup is missing required sections")
    return payload


def save_email_password(username, password):
    """Store SMTP password securely in the OS keyring when available."""
    if not username:
        return False
    if _KEYRING_AVAILABLE and keyring is not None:
        try:
            keyring.set_password(_EMAIL_SERVICE_NAME, username, password or "")
            return True
        except Exception:
            return False
    return False


def get_email_password(username):
    """Retrieve SMTP password from keyring or supported environment variables."""
    if not username:
        return ""

    env_password = os.getenv("SMTP_PASSWORD") or os.getenv("APP_SMTP_PASSWORD")
    if env_password is not None:
        return str(env_password)

    if _KEYRING_AVAILABLE and keyring is not None:
        try:
            return keyring.get_password(_EMAIL_SERVICE_NAME, username) or ""
        except Exception:
            pass
    return ""


def save_ibmi_password(username, password):
    """Store the IBM i login password securely in the OS keyring."""
    if not username or not (_KEYRING_AVAILABLE and keyring is not None):
        return False
    try:
        keyring.set_password(_IBMI_SERVICE_NAME, username, password or "")
        return True
    except Exception:
        return False


def get_ibmi_password(username):
    """Retrieve a remembered IBM i login password from the OS keyring."""
    if not username or not (_KEYRING_AVAILABLE and keyring is not None):
        return ""
    try:
        return keyring.get_password(_IBMI_SERVICE_NAME, username) or ""
    except Exception:
        return ""


def load_login_credentials():
    """Load the remembered IBM i username and preference from config.json."""
    config_path = _config_module().get_config_path()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as file:
                credentials = (json.load(file) or {}).get("LOGIN_CREDENTIALS", {})
            if isinstance(credentials, dict):
                return {
                    "remember": bool(credentials.get("remember", False)),
                    "username": str(credentials.get("username", "")),
                }
        except Exception:
            pass
    return {"remember": False, "username": ""}


def _coerce_to_list(value):
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def load_email_alerts():
    """Load email alert settings and resolve credentials at runtime."""
    config = _config_module()
    config_path = config.get_config_path()
    merged = config.DEFAULT_EMAIL_ALERTS.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
                user_email = data.get("EMAIL_ALERTS")
                if isinstance(user_email, dict):
                    merged.update(user_email)
        except Exception:
            pass

    env_server = os.getenv("SMTP_SERVER")
    if env_server:
        merged["smtp_server"] = env_server
    env_username = os.getenv("SMTP_USERNAME")
    if env_username:
        merged["username"] = env_username
    env_from = os.getenv("SMTP_FROM_ADDRESS")
    if env_from:
        merged["from_address"] = env_from
    env_to = os.getenv("SMTP_TO_ADDRESSES")
    if env_to:
        merged["to_addresses"] = _coerce_to_list(env_to)
    env_port = os.getenv("SMTP_PORT")
    if env_port:
        try:
            merged["port"] = int(env_port)
        except ValueError:
            pass
    env_tls = os.getenv("SMTP_USE_TLS")
    if env_tls is not None:
        merged["use_tls"] = _env_bool("SMTP_USE_TLS", merged.get("use_tls", True))
    env_enabled = os.getenv("SMTP_ENABLED")
    if env_enabled is not None:
        merged["enabled"] = _env_bool("SMTP_ENABLED", merged.get("enabled", False))

    merged["to_addresses"] = _coerce_to_list(merged.get("to_addresses", []))
    try:
        merged["port"] = int(merged.get("port", 587) or 587)
    except Exception:
        merged["port"] = 587
    merged["enabled"] = bool(merged.get("enabled", False))
    merged["use_tls"] = bool(merged.get("use_tls", True))
    try:
        merged["threshold_percent"] = float(merged.get("threshold_percent", 40.0) or 40.0)
    except Exception:
        merged["threshold_percent"] = 40.0
    try:
        merged["cooldown_minutes"] = int(merged.get("cooldown_minutes", 10) or 10)
    except Exception:
        merged["cooldown_minutes"] = 10
    try:
        merged["log_dedupe_seconds"] = int(merged.get("log_dedupe_seconds", 60) or 60)
    except Exception:
        merged["log_dedupe_seconds"] = 60

    try:
        merged["password"] = get_email_password(merged.get("username", ""))
    except Exception:
        merged["password"] = ""
    return merged


def save_all_configs(
    server_configs,
    expected_subsystems=None,
    expected_ports=None,
    email_alerts=None,
    login_credentials=None,
    log_root=None,
):
    """Save server, alert, and login preferences into config.json."""
    config = _config_module()
    config_path = config.get_config_path()
    config_dir = os.path.dirname(config_path)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)

    if expected_subsystems is None:
        expected_subsystems = config.load_expected_subsystems()
    if expected_ports is None:
        expected_ports = config.load_expected_ports()

    existing = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as file:
                existing = json.load(file) or {}
        except Exception:
            existing = {}

    merged_email = load_email_alerts()
    if isinstance(email_alerts, dict):
        merged_email.update(email_alerts)

    try:
        password = merged_email.pop("password", None)
        username = merged_email.get("username", "")
        if password is not None and username:
            save_email_password(username, password)
    except Exception:
        pass

    data = {
        "SERVER_CONFIGS": server_configs,
        "EXPECTED_SUBSYSTEMS": expected_subsystems,
        "EXPECTED_PORTS": expected_ports,
    }
    for key, value in existing.items():
        if key not in data:
            data[key] = value

    data["EMAIL_ALERTS"] = merged_email
    if isinstance(login_credentials, dict):
        data["LOGIN_CREDENTIALS"] = {
            "remember": bool(login_credentials.get("remember", False)),
            "username": str(login_credentials.get("username", "")),
        }

    normalized_log_root = None
    if log_root is not None and str(log_root).strip():
        normalized_log_root = os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(log_root).strip()))
        )
        data["LOGS_ROOT"] = normalized_log_root

    saved = config.safe_json_save(config_path, data)
    if saved and normalized_log_root:
        config.set_logs_root(normalized_log_root)
    return saved
