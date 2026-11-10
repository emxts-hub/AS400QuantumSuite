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
_WEEKLY_REPORT_EMAIL_SERVICE_NAME = "AS400 Quantum Suite_weekly_report_smtp"
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
    required_mappings = ("SERVER_CONFIGS", "EXPECTED_SUBSYSTEMS", "EXPECTED_PORTS", "EXPECTED_WEB_APPS", "EMAIL_ALERTS", "LOGIN_CREDENTIALS")
    missing_sections = [key for key in required_mappings if key not in payload or not isinstance(payload.get(key), dict)]
    if missing_sections:
        for key in missing_sections:
            if key == "EXPECTED_WEB_APPS":
                payload[key] = {}
            else:
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


def save_weekly_report_email_password(username, password):
    """Store the weekly report SMTP password separately from alert credentials."""
    if not username:
        return False
    if _KEYRING_AVAILABLE and keyring is not None:
        try:
            keyring.set_password(
                _WEEKLY_REPORT_EMAIL_SERVICE_NAME,
                username,
                password or "",
            )
            return True
        except Exception:
            return False
    return False


def get_weekly_report_email_password(username):
    """Retrieve the weekly report SMTP password from its own keyring entry."""
    if not username:
        return ""
    env_password = os.getenv("WEEKLY_REPORT_SMTP_PASSWORD")
    if env_password is not None:
        return str(env_password)
    if _KEYRING_AVAILABLE and keyring is not None:
        try:
            return (
                keyring.get_password(_WEEKLY_REPORT_EMAIL_SERVICE_NAME, username)
                or ""
            )
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


def load_weekly_report_email():
    """Load the independent weekly-report SMTP profile, migrating legacy settings."""
    config = _config_module()
    config_path = config.get_config_path()
    merged = config.DEFAULT_WEEKLY_REPORT_EMAIL.copy()
    weekly_profile_found = False
    legacy_profile = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            weekly_profile = data.get("WEEKLY_REPORT_EMAIL")
            weekly_profile_found = isinstance(weekly_profile, dict)
            if weekly_profile_found:
                merged.update(weekly_profile)
            legacy_profile = data.get("EMAIL_ALERTS", {})
        except (OSError, ValueError, TypeError, AttributeError):
            legacy_profile = {}

    if not weekly_profile_found and isinstance(legacy_profile, dict):
        for field in (
            "smtp_server",
            "port",
            "use_tls",
            "username",
            "from_address",
            "to_addresses",
        ):
            if field in legacy_profile:
                merged[field] = legacy_profile[field]
        merged["enabled"] = bool(
            legacy_profile.get("weekly_asp_cpu_report_enabled", False)
        )

    merged["enabled"] = bool(merged.get("enabled", False))
    merged["smtp_server"] = str(merged.get("smtp_server") or "").strip()
    merged["username"] = str(merged.get("username") or "").strip()
    merged["from_address"] = str(merged.get("from_address") or "").strip()
    merged["to_addresses"] = _coerce_to_list(merged.get("to_addresses", []))
    try:
        merged["port"] = int(merged.get("port", 587) or 587)
    except (TypeError, ValueError):
        merged["port"] = 587
    merged["use_tls"] = bool(merged.get("use_tls", True))

    password = ""
    if weekly_profile_found:
        password = get_weekly_report_email_password(merged["username"])
    elif isinstance(legacy_profile, dict):
        password = get_email_password(merged["username"])
    merged["password"] = password
    return merged


def save_all_configs(
    server_configs,
    expected_subsystems=None,
    expected_ports=None,
    expected_web_apps=None,
    email_alerts=None,
    login_credentials=None,
    log_root=None,
    asp_workbook_path=None,
    cpu_workbook_path=None,
    weekly_report_email=None,
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
    if expected_web_apps is None:
        expected_web_apps = config.load_expected_web_apps()

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
    merged_email.pop("weekly_asp_cpu_report_enabled", None)

    try:
        password = merged_email.pop("password", None)
        username = merged_email.get("username", "")
        if password is not None and username:
            save_email_password(username, password)
    except Exception:
        pass

    merged_weekly_email = load_weekly_report_email()
    if isinstance(weekly_report_email, dict):
        previous_username = str(merged_weekly_email.get("username", "")).strip()
        merged_weekly_email.update(weekly_report_email)
        if (
            previous_username
            and previous_username != str(merged_weekly_email.get("username", "")).strip()
            and "password" not in weekly_report_email
        ):
            merged_weekly_email["password"] = ""

    try:
        weekly_password = merged_weekly_email.pop("password", None)
        weekly_username = merged_weekly_email.get("username", "")
        if weekly_password is not None and weekly_username:
            save_weekly_report_email_password(weekly_username, weekly_password)
    except Exception:
        pass

    data = {
        "SERVER_CONFIGS": server_configs,
        "EXPECTED_SUBSYSTEMS": expected_subsystems,
        "EXPECTED_PORTS": expected_ports,
        "EXPECTED_WEB_APPS": expected_web_apps,
    }
    for key, value in existing.items():
        if key not in data:
            data[key] = value

    data["EMAIL_ALERTS"] = merged_email
    data["WEEKLY_REPORT_EMAIL"] = merged_weekly_email
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

    normalized_asp_workbook_path = None
    if asp_workbook_path is not None:
        raw_path = str(asp_workbook_path).strip()
        normalized_asp_workbook_path = (
            os.path.abspath(os.path.expandvars(os.path.expanduser(raw_path)))
            if raw_path
            else ""
        )
        data["ASP_WORKBOOK_PATH"] = normalized_asp_workbook_path

    normalized_cpu_workbook_path = None
    if cpu_workbook_path is not None:
        raw_path = str(cpu_workbook_path).strip()
        normalized_cpu_workbook_path = (
            os.path.abspath(os.path.expandvars(os.path.expanduser(raw_path)))
            if raw_path
            else ""
        )
        data["CPU_WORKBOOK_PATH"] = normalized_cpu_workbook_path

    saved = config.safe_json_save(config_path, data)
    if saved and normalized_log_root:
        config.set_logs_root(normalized_log_root)
    if saved and (
        normalized_asp_workbook_path is not None
        or normalized_cpu_workbook_path is not None
    ):
        config.set_excel_workbook_paths(
            normalized_asp_workbook_path
            if normalized_asp_workbook_path is not None
            else config.ASP_WORKBOOK_PATH,
            normalized_cpu_workbook_path
            if normalized_cpu_workbook_path is not None
            else config.CPU_WORKBOOK_PATH,
        )
    return saved
