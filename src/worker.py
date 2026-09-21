import os
import sys
import json
import re
import struct
import threading
import time
import uuid
import subprocess
import smtplib
import queue
from datetime import datetime, timedelta
from email.message import EmailMessage
import pyodbc
from PyQt6.QtCore import QRunnable, QObject, QThread, pyqtSignal
from config import (
    SERVER_CONFIGS, 
    MONITORED_PORTS, 
    EXPECTED_PORTS, 
    get_logs_dir,
    get_all_logs_dirs,
    get_resource_path, 
    EXPECTED_SUBSYSTEMS,
    safe_json_append_and_save,
    safe_json_save,
)
from ui.setcreds import load_email_alerts

_LOG_WRITE_LOCK = threading.Lock()
_LOG_MUTEX_NAME = "Local\\AS400QuantumSuite_DailyLogWrite"
_LOG_MUTEX_WAIT_MS = 30000
_LOG_QUEUE = queue.Queue()
_LOGGER_THREAD = None
_ALERT_STATE_LOCK = threading.Lock()
_LAST_ASP_ALERT_STATE = {}
_LAST_ASP_SOUND_STATE = {"armed": False, "sent_at": 0.0}
_LAST_ASP_EMAIL_STATE = {}
_LAST_SERVER_STATUS = {}
_SERVER_ALERTING_SERVERS = set()
_ALERT_SOUND_STOP_EVENT = threading.Event()
_ALERT_SOUND_PROCESSES = set()
_ALERT_SOUND_PROCESS_LOCK = threading.Lock()
_ALERT_SOUND_LOCK = threading.Lock()
_ALERT_SOUND_THREAD = None
_SERVER_ALERT_SOUND_STOP_EVENT = threading.Event()
_SERVER_ALERT_SOUND_PROCESSES = set()
_SERVER_ALERT_SOUND_PROCESS_LOCK = threading.Lock()
_SERVER_ALERT_SOUND_LOCK = threading.Lock()
_SERVER_ALERT_SOUND_THREAD = None


class DailyBackupFetchThread(QThread):
    host_name_ready = pyqtSignal(str, str)
    data_ready = pyqtSignal(str, list)
    error = pyqtSignal(str, str)

    HOST_NAME_QUERY = """
        SELECT 
            HOST_NAME
        FROM TABLE(QSYS2.SYSTEM_STATUS())   
    """

    QUERY = """
    SELECT JOB_NAME,
           JOB_ACTIVE_TIME AS START_TIME,
           JOB_END_TIME AS END_TIME,
           JOB_END_SEVERITY,
           CASE
               WHEN JOB_END_TIME IS NULL THEN 'RUNNING'
               WHEN JOB_END_SEVERITY > 30 THEN 'FAILED / ABNORMAL'
               WHEN JOB_END_SEVERITY BETWEEN 10 AND 30 THEN 'COMPLETED WITH WARNINGS'
               ELSE 'COMPLETED'
           END AS BACKUP_STATUS
    FROM TABLE(
            QSYS2.JOB_INFO(
                JOB_USER_FILTER => '*ALL',
                JOB_STATUS_FILTER => '*ALL',
                JOB_NAME_FILTER => ?
            )
        ) AS J
        ORDER BY JOB_ENTERED_SYSTEM_TIME DESC
        FETCH FIRST 1 ROW ONLY
"""

    def __init__(self, server_name, host, db, username, password, job_name_short):
        super().__init__()
        self.server_name = server_name
        self.host = host
        self.db = db
        self.username = username
        self.password = password
        self.job_name_short = job_name_short

    def run(self):
        conn = None
        try:
            conn = _new_connection(self.host, self.db, self.username, self.password)
            cursor = conn.cursor()
            try:
                cursor.execute(self.HOST_NAME_QUERY)
                host_row = cursor.fetchone()
                if host_row and host_row[0] is not None:
                    host_name = str(host_row[0]).strip()
                    if host_name:
                        self.host_name_ready.emit(self.server_name, host_name)
            except Exception:
                pass
            
            # Pass a tuple containing only one parameter:
            cursor.execute(self.QUERY, (self.job_name_short,))
            
            columns = [column[0].lower() for column in cursor.description]
            records = [dict(zip(columns, row)) for row in cursor.fetchall()]
            self.data_ready.emit(self.server_name, records)
        except Exception as exc:
            self.error.emit(self.server_name, str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _logger_worker():
    """Serializes log writes in a dedicated background thread so the UI remains responsive."""
    while True:
        item = _LOG_QUEUE.get()
        if item is None:
            _LOG_QUEUE.task_done()
            break

        sys_info, server_configs = item
        try:
            result = None
            for attempt in range(3):
                try:
                    result = save_single_lpar_log(sys_info, server_configs)
                    if result in {"saved", "already_recorded"}:
                        break
                    if result not in {"file_busy", "failed"}:
                        break
                except Exception as exc:
                    result = f"exception: {exc}"
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))

            if result not in {"saved", "already_recorded"}:
                print(f"[log_worker] Log persistence failed after retries: {result!r}")
        finally:
            _LOG_QUEUE.task_done()


def _start_logger_worker():
    global _LOGGER_THREAD
    if _LOGGER_THREAD is not None and _LOGGER_THREAD.is_alive():
        return
    _LOGGER_THREAD = threading.Thread(target=_logger_worker, name="AS400QuantumSuite-LogWriter", daemon=True)
    _LOGGER_THREAD.start()


def queue_log_persistence(sys_info, server_configs=None):
    """Queue a log write so refreshes stay responsive on the UI thread."""
    if sys_info is None:
        return
    _start_logger_worker()
    try:
        _LOG_QUEUE.put_nowait((sys_info, server_configs or SERVER_CONFIGS))
    except Exception:
        try:
            save_single_lpar_log(sys_info, server_configs)
        except Exception:
            pass


def _new_connection(host, db, username, password):
    # Performance tuning parameters for IBM i ODBC driver
    extra_params = (
        "NAM=1;"              # SQL System Naming (reduces library resolve time)
        "LAZYCLOSE=1;"        # Keep cursors active for fast statement execution
        "TRANSLATE=0;"        # Disable automatically converting CCSID values
    )
    return pyodbc.connect(
        f"DRIVER={{IBM i Access ODBC Driver}};"
        f"SYSTEM={host};"
        f"UID={username};"
        f"PWD={password};"
        f"DATABASE={db};"
        f"PREFETCH=1;"
        f"BLOCKFETCH=1;"
        f"CONN_TIMEOUT=3;"
        f"QUERY_TIMEOUT=3;"
        f"{extra_params}",
        timeout=3,
        autocommit=True,
    )


def _normalize_recipients(value):
    if isinstance(value, str):
        recipients = [item.strip() for item in value.split(',') if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        recipients = [str(item).strip() for item in value if str(item).strip()]
    else:
        recipients = []
    return recipients


def _repair_wav_file_if_needed(wav_path):
    """Fixes common WAV header corruption so Windows does not fall back to the system default chime."""
    try:
        with open(wav_path, "rb") as f:
            data = f.read()
    except Exception:
        return False

    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False

    riff_size = len(data) - 8
    data_idx = data.find(b"data")
    if data_idx == -1:
        return False

    data_size = len(data) - data_idx - 8
    current_riff = struct.unpack("<I", data[4:8])[0]
    current_data = struct.unpack("<I", data[data_idx + 4:data_idx + 8])[0]

    if current_riff == riff_size and current_data == data_size:
        return True

    try:
        fixed = bytearray(data)
        fixed[4:8] = struct.pack("<I", riff_size)
        fixed[data_idx + 4:data_idx + 8] = struct.pack("<I", data_size)
        with open(wav_path, "wb") as f:
            f.write(fixed)
        return True
    except Exception:
        return False


def _find_sound_path(name):
    candidate_paths = [
        get_resource_path(f"src/{name}"),
        get_resource_path(name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
        os.path.join(os.getcwd(), "src", name),
    ]

    for path in candidate_paths:
        if path and os.path.exists(path):
            return path

    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for root, _, files in os.walk(base_dir):
        if name.lower() in [filename.lower() for filename in files]:
            return os.path.join(root, name)
    return None


def play_alert_sound(name):
    """Play one alert sound without changing the looping ASP alert state."""
    sound_path = _find_sound_path(name)
    if not sound_path:
        print(f"Alert sound not found: {name}")
        return False

    try:
        _repair_wav_file_if_needed(sound_path)
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(str(sound_path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        elif sys.platform == "darwin":
            subprocess.run(["afplay", str(sound_path)], check=False, timeout=30)
        else:
            subprocess.run(["ffplay", "-nodisp", "-autoexit", str(sound_path)], check=False, timeout=30)
        return True
    except Exception:
        return False


def play_server_down_alert_sound():
    """Loop alert.wav until the monitored server recovers or VPN is lost."""
    global _SERVER_ALERT_SOUND_THREAD

    with _SERVER_ALERT_SOUND_LOCK:
        if _SERVER_ALERT_SOUND_THREAD is not None and _SERVER_ALERT_SOUND_THREAD.is_alive():
            return True

    sound_path = _find_sound_path("alert.wav")
    if not sound_path:
        print("Alert sound not found: alert.wav")
        return False

    try:
        _repair_wav_file_if_needed(sound_path)
    except Exception:
        pass

    _SERVER_ALERT_SOUND_STOP_EVENT.clear()

    def play_once():
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(
                str(sound_path),
                winsound.SND_FILENAME | winsound.SND_NODEFAULT | winsound.SND_ASYNC,
            )
            _SERVER_ALERT_SOUND_STOP_EVENT.wait(2.0)
            return

        command = ["afplay", str(sound_path)] if sys.platform == "darwin" else [
            "ffplay", "-nodisp", "-autoexit", str(sound_path)
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with _SERVER_ALERT_SOUND_PROCESS_LOCK:
            _SERVER_ALERT_SOUND_PROCESSES.add(process)
        try:
            while process.poll() is None and not _SERVER_ALERT_SOUND_STOP_EVENT.wait(0.1):
                pass
            if _SERVER_ALERT_SOUND_STOP_EVENT.is_set() and process.poll() is None:
                process.terminate()
        finally:
            with _SERVER_ALERT_SOUND_PROCESS_LOCK:
                _SERVER_ALERT_SOUND_PROCESSES.discard(process)

    def loop():
        global _SERVER_ALERT_SOUND_THREAD
        try:
            while not _SERVER_ALERT_SOUND_STOP_EVENT.is_set():
                try:
                    play_once()
                except Exception:
                    return
                if not _SERVER_ALERT_SOUND_STOP_EVENT.wait(1.0):
                    continue
        finally:
            with _SERVER_ALERT_SOUND_LOCK:
                if _SERVER_ALERT_SOUND_THREAD is threading.current_thread():
                    _SERVER_ALERT_SOUND_THREAD = None

    thread = threading.Thread(
        target=loop,
        daemon=True,
        name="AS400QuantumSuite-ServerAlertSound",
    )
    with _SERVER_ALERT_SOUND_LOCK:
        if _SERVER_ALERT_SOUND_THREAD is not None and _SERVER_ALERT_SOUND_THREAD.is_alive():
            return True
        _SERVER_ALERT_SOUND_THREAD = thread
    thread.start()
    return True


def stop_server_down_alert_sound():
    """Stop the looping server-disconnected sound without affecting ASP alerts."""
    global _SERVER_ALERT_SOUND_THREAD
    _SERVER_ALERT_SOUND_STOP_EVENT.set()

    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
            winsound.PlaySound(None, 0)
        except Exception:
            pass

    with _SERVER_ALERT_SOUND_PROCESS_LOCK:
        processes = list(_SERVER_ALERT_SOUND_PROCESSES)
        _SERVER_ALERT_SOUND_PROCESSES.clear()

    for process in processes:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass

    with _SERVER_ALERT_SOUND_LOCK:
        _SERVER_ALERT_SOUND_THREAD = None


def stop_all_server_status_alerts():
    """Stop server-down playback and forget all active server outage states."""
    with _ALERT_STATE_LOCK:
        _LAST_SERVER_STATUS.clear()
        _SERVER_ALERTING_SERVERS.clear()
    stop_server_down_alert_sound()


def play_asp_alert_sound():
    """Play the warning wav in a single looped thread without overlap: 1 second sound, 1 second silence."""
    global _ALERT_SOUND_THREAD

    with _ALERT_SOUND_LOCK:
        if _ALERT_SOUND_THREAD is not None and _ALERT_SOUND_THREAD.is_alive():
            return True

    _ALERT_SOUND_STOP_EVENT.clear()
    target_names = ["warning.wav"]
    wav_paths = []

    for name in target_names:
        candidate_paths = [
            get_resource_path(f"src/{name}"),
            get_resource_path(name),
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "src", name
            ),
            os.path.join(os.getcwd(), "src", name),
        ]

        found_path = None
        for path in candidate_paths:
            if path and os.path.exists(path):
                found_path = path
                break

        if not found_path:
            base_dir = getattr(
                sys,
                "_MEIPASS",
                os.path.dirname(os.path.abspath(__file__)),
            )
            for root, _, files in os.walk(base_dir):
                if name.lower() in [f.lower() for f in files]:
                    found_path = os.path.join(root, name)
                    break

        if found_path and os.path.exists(found_path):
            wav_paths.append(found_path)

    if not wav_paths:
        print(
            f"Alert sound error: Expected at least 1 sound file, found {len(wav_paths)}."
        )
        return False

    try:
        for path in wav_paths:
            try:
                _repair_wav_file_if_needed(path)
            except Exception:
                pass

        def _play_once(path):
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(
                    str(path),
                    winsound.SND_FILENAME | winsound.SND_NODEFAULT | winsound.SND_ASYNC,
                )
                return

            if sys.platform == "darwin":
                p_proc = subprocess.Popen(
                    ["afplay", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with _ALERT_SOUND_PROCESS_LOCK:
                    _ALERT_SOUND_PROCESSES.add(p_proc)
                try:
                    p_proc.wait()
                finally:
                    with _ALERT_SOUND_PROCESS_LOCK:
                        _ALERT_SOUND_PROCESSES.discard(p_proc)
                return

            try:
                p_proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with _ALERT_SOUND_PROCESS_LOCK:
                    _ALERT_SOUND_PROCESSES.add(p_proc)
                try:
                    p_proc.wait()
                finally:
                    with _ALERT_SOUND_PROCESS_LOCK:
                        _ALERT_SOUND_PROCESSES.discard(p_proc)
            except Exception:
                p_proc = subprocess.Popen(
                    ["aplay", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with _ALERT_SOUND_PROCESS_LOCK:
                    _ALERT_SOUND_PROCESSES.add(p_proc)
                try:
                    p_proc.wait()
                finally:
                    with _ALERT_SOUND_PROCESS_LOCK:
                        _ALERT_SOUND_PROCESSES.discard(p_proc)

        def _play_sequential():
            global _ALERT_SOUND_THREAD
            try:
                while not _ALERT_SOUND_STOP_EVENT.is_set():
                    for p in wav_paths:
                        if _ALERT_SOUND_STOP_EVENT.is_set():
                            return
                        _play_once(p)
                        if _ALERT_SOUND_STOP_EVENT.is_set():
                            return
                        time.sleep(1.0)
                        if _ALERT_SOUND_STOP_EVENT.is_set():
                            return
                        time.sleep(1.0)
            finally:
                with _ALERT_SOUND_LOCK:
                    if _ALERT_SOUND_THREAD is threading.current_thread():
                        _ALERT_SOUND_THREAD = None

        thread = threading.Thread(target=_play_sequential, daemon=True, name="AS400QuantumSuite-ASPAlertSound")
        with _ALERT_SOUND_LOCK:
            if _ALERT_SOUND_THREAD is not None and _ALERT_SOUND_THREAD.is_alive():
                return True
            _ALERT_SOUND_THREAD = thread
        thread.start()
        return True

    except Exception as e:
        print(f"Failed to play alert sounds: {e}")
        return False


def reset_asp_alert_sound():
    """Allow alert playback for a new monitoring session."""
    _ALERT_SOUND_STOP_EVENT.clear()


def stop_asp_alert_sound():
    """Stop any alert playback started by the monitoring worker."""
    global _ALERT_SOUND_THREAD
    _ALERT_SOUND_STOP_EVENT.set()

    def _stop_playback():
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
                winsound.PlaySound(None, 0)
            except Exception:
                pass

        with _ALERT_SOUND_PROCESS_LOCK:
            processes = list(_ALERT_SOUND_PROCESSES)
            _ALERT_SOUND_PROCESSES.clear()

        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass

        with _ALERT_SOUND_LOCK:
            _ALERT_SOUND_THREAD = None

    threading.Thread(target=_stop_playback, daemon=True).start()


def send_asp_alert(server_name, asp_value, threshold_percent):
    """Sends an SMTP notification when ASP usage crosses the configured threshold."""
    alert_cfg = load_email_alerts()
    if not alert_cfg.get("enabled"):
        return False

    smtp_server = str(alert_cfg.get("smtp_server", "")).strip()
    recipients = _normalize_recipients(alert_cfg.get("to_addresses", []))
    if not smtp_server or not recipients:
        return False

    username = str(alert_cfg.get("username", "")).strip()
    password = str(alert_cfg.get("password", "")).strip()
    from_address = str(alert_cfg.get("from_address", "")).strip() or username or "alerts@localhost"
    port = int(alert_cfg.get("port", 587) or 587)
    use_tls = bool(alert_cfg.get("use_tls", True))

    try:
        msg = EmailMessage()
        msg["Subject"] = f"ASP Threshold Alert - {server_name}"
        msg["From"] = from_address
        msg["To"] = ", ".join(recipients)
        msg.set_content(
            f"ASP usage on {server_name} reached {asp_value:.2f}% and exceeded the configured threshold of {threshold_percent:.2f}%.\n\n"
            f"This notification was generated automatically by the IBM i dashboard."
        )

        with smtplib.SMTP(smtp_server, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def send_server_status_alert(server_name, status, error=""):
    """Send an SMTP alert when a monitored server becomes unreachable."""
    alert_cfg = load_email_alerts()
    if not alert_cfg.get("enabled"):
        return False

    smtp_server = str(alert_cfg.get("smtp_server", "")).strip()
    recipients = _normalize_recipients(alert_cfg.get("to_addresses", []))
    if not smtp_server or not recipients:
        return False

    username = str(alert_cfg.get("username", "")).strip()
    password = str(alert_cfg.get("password", "")).strip()
    from_address = str(alert_cfg.get("from_address", "")).strip() or username or "alerts@localhost"
    port = int(alert_cfg.get("port", 587) or 587)
    use_tls = bool(alert_cfg.get("use_tls", True))

    try:
        msg = EmailMessage()
        msg["Subject"] = f"Server Down Alert - {server_name}"
        msg["From"] = from_address
        msg["To"] = ", ".join(recipients)
        details = f"\nConnection error: {error}" if error else ""
        msg.set_content(
            f"The monitored server {server_name} is down or not reachable.\n"
            f"Status: {status}{details}\n\n"
            f"This notification was generated automatically by the IBM i dashboard."
        )

        with smtplib.SMTP(smtp_server, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def maybe_send_server_status_alert(result):
    """Alert on a reachable-VPN outage and play up.wav on recovery.

    An offline result without the monitoring VPN is treated as a local network
    condition, not as proof that the IBM i server is down.
    """
    server_name = str(result.get("server") or result.get("config_key") or "Unknown server")
    status = str(result.get("status", "OFFLINE")).upper()
    is_down = status == "OFFLINE"
    is_up = status in {"ONLINE", "DEGRADED"}

    if is_down and not has_vpn_ip():
        should_stop_sound = False
        with _ALERT_STATE_LOCK:
            _LAST_SERVER_STATUS.pop(server_name, None)
            _SERVER_ALERTING_SERVERS.discard(server_name)
            should_stop_sound = not _SERVER_ALERTING_SERVERS
        if should_stop_sound:
            stop_server_down_alert_sound()
        return False

    with _ALERT_STATE_LOCK:
        previous_status = _LAST_SERVER_STATUS.get(server_name)
        _LAST_SERVER_STATUS[server_name] = status
        if is_down:
            _SERVER_ALERTING_SERVERS.add(server_name)
        elif is_up:
            _SERVER_ALERTING_SERVERS.discard(server_name)
        should_stop_sound = not _SERVER_ALERTING_SERVERS

    if is_down and previous_status != "OFFLINE":
        # Recheck immediately before alerting so a VPN disconnect cannot turn
        # a server connection failure into a false server-down notification.
        if not has_vpn_ip():
            with _ALERT_STATE_LOCK:
                _LAST_SERVER_STATUS.pop(server_name, None)
                _SERVER_ALERTING_SERVERS.discard(server_name)
                should_stop_sound = not _SERVER_ALERTING_SERVERS
            if should_stop_sound:
                stop_server_down_alert_sound()
            return False

        play_server_down_alert_sound()
        if not has_vpn_ip():
            with _ALERT_STATE_LOCK:
                _LAST_SERVER_STATUS.pop(server_name, None)
                _SERVER_ALERTING_SERVERS.discard(server_name)
                should_stop_sound = not _SERVER_ALERTING_SERVERS
            if should_stop_sound:
                stop_server_down_alert_sound()
            return False
        return send_server_status_alert(server_name, status, str(result.get("error", "")))

    if is_up and previous_status == "OFFLINE":
        if should_stop_sound:
            stop_server_down_alert_sound()
        return play_alert_sound("up.wav")

    return False


def maybe_send_asp_alert(server_name, asp_value):
    """Plays the ASP sound on each refresh while any server remains above threshold."""
    alert_cfg = load_email_alerts()
    email_enabled = bool(alert_cfg.get("enabled"))

    try:
        asp_value = float(asp_value if asp_value is not None else 0.0)
    except (TypeError, ValueError):
        asp_value = 0.0

    threshold_percent = float(alert_cfg.get("threshold_percent", 40.0) or 40.0)
    cooldown_seconds = max(0, int(float(alert_cfg.get("cooldown_minutes", 10) or 10) * 60))

    email_should_send = False

    with _ALERT_STATE_LOCK:
        now = time.monotonic()

        if asp_value < threshold_percent:
            # Clear state for THIS server
            _LAST_ASP_ALERT_STATE[server_name] = {"armed": False, "sent_at": 0.0}
            
            # ONLY stop sound if NO other servers are currently armed
            any_armed = any(st.get("armed", False) for st in _LAST_ASP_ALERT_STATE.values())
            if not any_armed:
                stop_asp_alert_sound()
            return False

        # Mark server as armed
        _LAST_ASP_ALERT_STATE[server_name] = {"armed": True, "sent_at": now}

        # Check email cooldown
        if email_enabled:
            email_state = _LAST_ASP_EMAIL_STATE.get(server_name, {"sent_at": None})
            last_sent = email_state.get("sent_at")

            if last_sent is None or (now - last_sent) >= cooldown_seconds:
                _LAST_ASP_EMAIL_STATE[server_name] = {"sent_at": now}
                email_should_send = True

    # Play sound as long as this server is active
    sound_played = play_asp_alert_sound()

    if email_should_send:
        email_sent = send_asp_alert(server_name, asp_value, threshold_percent)
        return email_sent or sound_played

    return sound_played


def has_vpn_ip(prefix="10.212."):
    """Returns whether Windows has an IPv4 address assigned in the VPN range."""
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "(Get-NetIPAddress -AddressFamily IPv4).IPAddress",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and any(
            address.strip().startswith(prefix)
            for address in result.stdout.splitlines()
        )
    except (OSError, subprocess.SubprocessError):
        return False


def cleanup_old_logs(days_to_keep=30):
    """Delete canonical daily log files older than the retention period in all archives."""
    cutoff_date = datetime.now() - timedelta(days=days_to_keep)
    log_pattern = re.compile(r"^lpar_history_(\d{4}-\d{2}-\d{2})\.json$")

    for logs_dir in get_all_logs_dirs():
        if not os.path.exists(logs_dir):
            continue
        try:
            filenames = os.listdir(logs_dir)
        except OSError:
            continue
        for filename in filenames:
            match = log_pattern.match(filename)
            if not match:
                continue
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
                if file_date < cutoff_date:
                    os.remove(os.path.join(logs_dir, filename))
            except (OSError, ValueError):
                pass


def _has_server_issues(sys_info, server_configs=None):
    """Check if server has issues worth logging (status not online, DOWN subsystems/services/ports, or errors)."""
    status = str(sys_info.get("status", "OFFLINE")).upper()
    if status not in ("ONLINE",):
        return True
    
    subsystems = sys_info.get("subsystems", [])
    if isinstance(subsystems, list):
        for sub in subsystems:
            if not isinstance(sub, dict):
                continue
            status = str(sub.get("status", "ACTIVE")).upper()
            name = str(sub.get("name", "")).strip()
            if name and status != "ACTIVE":
                return True

    configs = server_configs or SERVER_CONFIGS
    config_key = sys_info.get("config_key") or sys_info.get("server") or sys_info.get("host_name")
    cfg = configs.get(config_key, {})
    expected_key = cfg.get("expected_subsystems_key", config_key)
    expected_subs = EXPECTED_SUBSYSTEMS.get(expected_key, {})
    if isinstance(subsystems, list) and expected_subs:
        active_names = {
            str(sub.get("name", "")).strip().upper()
            for sub in subsystems
            if isinstance(sub, dict) and sub.get("name")
        }
        expected_names = {str(name).strip().upper() for name in expected_subs}
        if any(name not in active_names for name in expected_names):
            return True
    
    ports = sys_info.get("ports", [])
    if isinstance(ports, list):
        for port in ports:
            if isinstance(port, dict) and port.get("is_up") is False:
                return True
    
    if sys_info.get("error"):
        return True
    
    return False


def save_single_lpar_log(sys_info, server_configs=None):
    with _LOG_WRITE_LOCK:
        mutex_handle = _acquire_log_mutex()
        if mutex_handle is None:
            return "file_busy"
        try:
            return _save_single_lpar_log(sys_info, server_configs)
        finally:
            _release_log_mutex(mutex_handle)


def _acquire_log_mutex():
    """Serialize daily log replacement across multiple app processes on Windows."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, _LOG_MUTEX_NAME)
        if not handle:
            return None
        result = kernel32.WaitForSingleObject(handle, _LOG_MUTEX_WAIT_MS)
        if result in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED
            return (kernel32, handle)
        kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError):
        return None
    return None


def _release_log_mutex(mutex_handle):
    if mutex_handle is True or mutex_handle is None or sys.platform != "win32":
        return
    kernel32, handle = mutex_handle
    try:
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)
    except OSError:
        pass


def _read_log_entries(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle) or []
        if not isinstance(data, list):
            data = [data]
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _record_has_issue(record):
    if not isinstance(record, dict):
        return False

    status = str(record.get("status", "OFFLINE")).upper()
    if status not in {"ONLINE", "UP", "OK"}:
        return True

    if record.get("subsystems_summary") not in (None, "", "None") and str(record.get("subsystems_summary")).strip() != "None":
        return True

    services_down = record.get("services_down")
    if services_down not in (None, "", "None", []) and str(services_down).strip() != "None":
        return True

    detail = record.get("subsystems_detail", [])
    if isinstance(detail, list) and detail:
        return True

    return False


def _last_matching_record_for_server(filepath, server_name):
    try:
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as handle:
            existing_data = json.load(handle) or []
        if not isinstance(existing_data, list):
            existing_data = [existing_data]
    except (OSError, json.JSONDecodeError):
        return None

    for entry in reversed(existing_data):
        if not isinstance(entry, dict):
            continue
        records = entry.get("records", [])
        if not isinstance(records, list):
            records = [records] if isinstance(records, dict) else []
        for rec in reversed(records):
            if not isinstance(rec, dict):
                continue
            rec_server = str(rec.get("server") or rec.get("lpar") or rec.get("config_key") or "").strip()
            if rec_server == server_name:
                return rec
    return None


def _is_same_dedupe_window_record(existing_record, server_name, dedupe_seconds, candidate_record, now):
    if not isinstance(existing_record, dict):
        return False

    rec_server = str(existing_record.get("server") or existing_record.get("lpar") or existing_record.get("config_key") or "").strip()
    rec_ts = str(existing_record.get("timestamp") or "").strip()
    if rec_server != server_name:
        return False

    try:
        existing_dt = datetime.strptime(rec_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False

    if dedupe_seconds > 0 and (now - existing_dt).total_seconds() > dedupe_seconds:
        return False

    existing_status = str(existing_record.get("status", "OFFLINE")).upper()
    candidate_status = str(candidate_record.get("status", "OFFLINE")).upper()
    if existing_status != candidate_status:
        return False

    if existing_record.get("services_down") != candidate_record.get("services_down"):
        return False

    if existing_record.get("subsystems_summary") != candidate_record.get("subsystems_summary"):
        return False

    existing_detail = json.dumps(existing_record.get("subsystems_detail", []), sort_keys=True, default=str)
    candidate_detail = json.dumps(candidate_record.get("subsystems_detail", []), sort_keys=True, default=str)
    return existing_detail == candidate_detail


def _merge_and_remove_conflict_logs(canonical_path, date_str):
    """Merge OneDrive conflict snapshots into the daily file, then remove them."""
    logs_dir = os.path.dirname(canonical_path)
    conflict_pattern = re.compile(
        rf"^lpar_history_{re.escape(date_str)}-.+\.json$",
        re.IGNORECASE,
    )
    conflict_paths = [
        os.path.join(logs_dir, name)
        for name in os.listdir(logs_dir)
        if conflict_pattern.match(name)
    ]
    if not conflict_paths:
        return

    canonical_entries = _read_log_entries(canonical_path)
    if canonical_entries is None:
        return

    merged_entries = list(canonical_entries)
    signatures = {
        json.dumps(entry, sort_keys=True, default=str)
        for entry in merged_entries
    }
    processed_paths = []
    for conflict_path in conflict_paths:
        conflict_entries = _read_log_entries(conflict_path)
        if conflict_entries is None:
            continue
        for entry in conflict_entries:
            signature = json.dumps(entry, sort_keys=True, default=str)
            if signature not in signatures:
                merged_entries.append(entry)
                signatures.add(signature)
        processed_paths.append(conflict_path)

    if not safe_json_save(canonical_path, merged_entries):
        return
    for conflict_path in processed_paths:
        try:
            os.remove(conflict_path)
        except OSError:
            pass


def _save_single_lpar_log(sys_info, server_configs=None):
    """Appends a single LPAR result to the local offline log using safe re-read and atomic replacement."""
    logs_dir = get_logs_dir()
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    filepath = os.path.join(logs_dir, f"lpar_history_{date_str}.json")

    configs = server_configs or SERVER_CONFIGS
    config_key = sys_info.get("config_key") or sys_info.get("server") or sys_info.get("host_name") or "unknown"
    resolved_name = sys_info.get("host_name") or sys_info.get("server") or config_key
    server_name = resolved_name if str(resolved_name).strip() and not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", str(resolved_name).strip()) else config_key
    server_name = str(server_name).strip() or config_key

    issue_present = _has_server_issues(sys_info, configs)
    dedupe_seconds = int(load_email_alerts().get("log_dedupe_seconds", 60) or 60)

    try:
        last_record = _last_matching_record_for_server(filepath, server_name)
        previous_issue = bool(last_record and _record_has_issue(last_record))
        recovery_transition = bool(previous_issue and not issue_present)

        if not issue_present and last_record is not None:
            try:
                last_dt = datetime.strptime(str(last_record.get("timestamp") or ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                last_dt = None
            if last_dt is not None and last_dt.strftime("%Y-%m-%d %H:00:00") == now.strftime("%Y-%m-%d %H:00:00"):
                _merge_and_remove_conflict_logs(filepath, date_str)
                return "already_recorded"

        if not issue_present and not recovery_transition:
            pass

        if issue_present and last_record is not None:
            if _is_same_dedupe_window_record(last_record, server_name, dedupe_seconds, {
                "status": sys_info.get("status", "OFFLINE"),
                "subsystems_summary": "None" if not last_record.get("subsystems_summary", "None") else last_record.get("subsystems_summary"),
                "subsystems_detail": last_record.get("subsystems_detail", []),
                "services_down": last_record.get("services_down", "None"),
            }, now):
                _merge_and_remove_conflict_logs(filepath, date_str)
                return "already_recorded"
    except json.JSONDecodeError:
        return "failed"
    except OSError:
        return "file_busy"
    except Exception:
        return "failed"

    down_services = []
    ports = sys_info.get("ports")
    if isinstance(ports, list):
        for port in ports:
            if not isinstance(port, dict):
                continue
            if port.get("is_up") is False:
                name = port.get("name") or port.get("service")
                if name is None and port.get("port") is not None:
                    name = f"Port {port.get('port')}"
                if name:
                    down_services.append(name)

    services_down_val = down_services if down_services else "None"

    cfg = configs.get(config_key, configs.get(str(sys_info.get("server") or sys_info.get("host_name") or config_key), {}))
    ip_addr = cfg.get("host", "N/A") if isinstance(cfg, dict) else str(cfg)

    subsystems_list = sys_info.get("subsystems", [])
    down_subsystems = []
    active_names = set()

    if isinstance(subsystems_list, list):
        for sub in subsystems_list:
            if not isinstance(sub, dict):
                continue
            name = str(sub.get("name", "")).strip()
            status = str(sub.get("status", "ACTIVE")).upper()
            if name:
                active_names.add(name.upper())
            if status != "ACTIVE" and name:
                down_subsystems.append({
                    "name": name,
                    "status": status
                })

    expected_key = cfg.get("expected_subsystems_key", config_key) if isinstance(cfg, dict) else config_key
    expected_subs = EXPECTED_SUBSYSTEMS.get(expected_key, {})
    if not down_subsystems and expected_subs:
        expected_names = {str(name).strip().upper(): name for name in expected_subs}
        for expected_upper, expected_name in expected_names.items():
            if expected_upper not in active_names:
                down_subsystems.append({
                    "name": expected_name,
                    "status": "DOWN"
                })
    
    if down_subsystems:
        subsystems_summary = f"{len(down_subsystems)} Down"
        subsystems_detail = down_subsystems
    else:
        subsystems_summary = "None"
        subsystems_detail = []

    record = {
        "entry_id": uuid.uuid4().hex,
        "timestamp": timestamp_str,
        "config_key": str(config_key),
        "lpar": server_name,
        "server": server_name,
        "ip": ip_addr,
        "cpu": sys_info.get("cpu", 0.0),
        "asp": sys_info.get("asp", 0.0),
        "jobs": sys_info.get("jobs", 0),
        "status": sys_info.get("status", "OFFLINE"),
        "subsystems_summary": subsystems_summary,
        "subsystems_detail": subsystems_detail,
        "services_down": services_down_val
    }

    try:
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing_data = json.load(f) or []
                if not isinstance(existing_data, list):
                    existing_data = [existing_data]
            for entry in existing_data:
                if not isinstance(entry, dict):
                    continue
                for rec in entry.get("records", []):
                    if _is_same_dedupe_window_record(rec, server_name, dedupe_seconds, record, now):
                        _merge_and_remove_conflict_logs(filepath, date_str)
                        return "already_recorded"
    except json.JSONDecodeError:
        return "failed"
    except OSError:
        return "file_busy"
    except Exception:
        return "failed"

    entry = {
        "timestamp": timestamp_str,
        "records": [record]
    }

    # OneDrive-safe atomic append; the caller holds the process-wide log lock.
    if not safe_json_append_and_save(filepath, entry):
        return "file_busy"
    _merge_and_remove_conflict_logs(filepath, date_str)
    cleanup_old_logs(days_to_keep=30)
    return "saved"


def _persist_and_emit(runnable, result):
    try:
        runnable.signals.server_fetched.emit(result)
    except RuntimeError as exc:
        if "already been deleted" not in str(exc) and "has been deleted" not in str(exc):
            raise
    try:
        queue_log_persistence(result, SERVER_CONFIGS)
    except Exception as exc:
        print(
            f"[{runnable.server}] Live result emitted, but log persistence failed: {exc}"
        )
        return


class LparWorkerSignals(QObject):
    """Signals for communicating LPAR query execution results safely to GUI widgets."""
    server_fetched = pyqtSignal(dict)
    server_failed = pyqtSignal(dict)


class SingleLparRunnable(QRunnable):
    """Concurrent worker task for fetching metrics from a single LPAR connection."""
    def __init__(self, server, cfg, username, password, cancel_event=None, signal_parent=None):
        super().__init__()
        self.setAutoDelete(False)
        self.server = server
        self.cfg = cfg
        self.username = username
        self.password = password
        self.cancel_event = cancel_event or threading.Event()
        self.signals = LparWorkerSignals(signal_parent)

    def cancel(self):
        self.cancel_event.set()

    def is_cancelled(self):
        return self.cancel_event.is_set()

    def check_cancelled(self):
        if self.is_cancelled():
            return True
        return False

    def run(self):
        conn = None
        started_at = time.monotonic()
        host = self.cfg.get("host", "") if isinstance(self.cfg, dict) else str(self.cfg)
        db = self.cfg.get("db", "*LOCAL") if isinstance(self.cfg, dict) else "*LOCAL"

        if self.check_cancelled():
            return

        try:
            conn = _new_connection(host, db, self.username, self.password)
            cursor = conn.cursor()

            if self.check_cancelled():
                return

            system_name = self.server
            try:
                cursor.execute("SELECT HOST_NAME FROM TABLE(QSYS2.SYSTEM_STATUS())")
                row = cursor.fetchone()
                if row and row[0] is not None:
                    resolved_name = str(row[0]).strip()
                    if resolved_name:
                        system_name = resolved_name
            except Exception:
                pass

            active_jobs = 0
            active_jobs_detail = []
            asp_used = 0.0
            cpu_util = 0.0
            metric_errors = []

            try:
                cursor.execute(
                    """
                    SELECT 
                    (SELECT COUNT(*) FROM TABLE(QSYS2.ACTIVE_JOB_INFO(DETAILED_INFO => 'NONE'))) AS ACTIVE_JOBS,
                    SYSTEM_ASP_USED AS ASP_USED,
                    (SELECT ROUND(AVERAGE_CPU_UTILIZATION, 2) 
                    FROM TABLE(QSYS2.SYSTEM_ACTIVITY_INFO())) AS CPU_UTIL
                    FROM QSYS2.SYSTEM_STATUS_INFO_BASIC
                    WITH NC
                    """
                )
                combined_row = cursor.fetchone()
                if combined_row:
                    if combined_row[0] is not None:
                        active_jobs = int(combined_row[0])
                    if combined_row[1] is not None:
                        asp_used = float(combined_row[1])
                    if combined_row[2] is not None:
                        cpu_util = float(combined_row[2])
            except Exception as e:
                metric_errors.append(f"jobs/ASP/CPU: {e}")

            try:
                cursor.execute(
                    """
                    SELECT
                        JOB_NAME,
                        JOB_STATUS,
                        TEMPORARY_STORAGE,
                        CPU_TIME
                    FROM TABLE(QSYS2.ACTIVE_JOB_INFO(DETAILED_INFO => 'NONE'))
                    """
                )
                for row in cursor.fetchall():
                    active_jobs_detail.append({
                        "job_name": str(row[0]).strip() if row[0] is not None else "",
                        "job_status": str(row[1]).strip() if row[1] is not None else "",
                        "temporary_storage": row[2] if row[2] is not None else 0,
                        "cpu_time": row[3] if row[3] is not None else 0,
                    })
                active_jobs = len(active_jobs_detail)
            except Exception as e:
                metric_errors.append(f"active job details: {e}")

            if self.check_cancelled():
                return

            if asp_used == 0.0:
                try:
                    cursor.execute("SELECT PERCENT_PROCESSING_UNIT_USED FROM QSYS2.SYSTEM_ASP_INFO")
                    asp_row = cursor.fetchone()
                    if asp_row and asp_row[0] is not None:
                        asp_used = float(asp_row[0])
                except Exception as e:
                    metric_errors.append(f"ASP fallback: {e}")

            if self.check_cancelled():
                return

            active_subsystems = []
            try:
                cursor.execute(
                    """
                    SELECT 
                        SUBSYSTEM_DESCRIPTION, 
                        STATUS, 
                        CURRENT_ACTIVE_JOBS, 
                        SIGNON_DEVICE_FILE_LIBRARY, 
                        TEXT_DESCRIPTION 
                    FROM QSYS2.SUBSYSTEM_INFO 
                    WHERE STATUS = 'ACTIVE'
                    """
                )
                for r in cursor.fetchall():
                    active_subsystems.append({
                        "name": str(r[0]).strip() if r[0] else "",
                        "status": str(r[1]).strip() if r[1] else "",
                        "active_jobs": r[2] if r[2] is not None else 0,
                        "library": str(r[3]).strip() if r[3] else "",
                        "description": str(r[4]).strip() if r[4] else ""
                    })
            except Exception as e:
                metric_errors.append(f"subsystems: {e}")

            expected_key = self.cfg.get("expected_subsystems_key", self.server) if isinstance(self.cfg, dict) else self.server
            expected_subs = EXPECTED_SUBSYSTEMS.get(expected_key, [])
            if isinstance(expected_subs, dict):
                expected_names = {str(name).strip().upper() for name in expected_subs.keys() if str(name).strip()}
            elif isinstance(expected_subs, (list, tuple, set)):
                expected_names = {str(name).strip().upper() for name in expected_subs if str(name).strip()}
            else:
                expected_names = set()

            active_names = {str(sub.get("name", "")).strip().upper() for sub in active_subsystems if isinstance(sub, dict) and sub.get("name")}
            for expected_name in sorted(expected_names):
                if expected_name not in active_names:
                    active_subsystems.append({
                        "name": expected_name,
                        "status": "DOWN",
                        "active_jobs": 0,
                        "library": "",
                        "description": "Subsystem Stopped / Down"
                    })

            if self.check_cancelled():
                return

            port_status_list = []
            try:
                target_ports = EXPECTED_PORTS.get(self.server, [])
                if not target_ports and isinstance(MONITORED_PORTS, dict):
                    target_ports = [{"port": p, "name": s} for p, s in MONITORED_PORTS.items()]

                requested_ports = []
                for p_info in target_ports:
                    p_num = p_info.get("port") if isinstance(p_info, dict) else p_info
                    try:
                        requested_ports.append(int(p_num))
                    except (TypeError, ValueError):
                        continue

                if requested_ports:
                    placeholders = ", ".join("?" for _ in requested_ports)
                    cursor.execute(
                        f"""
                        WITH FILTERED_PORTS AS (
                            SELECT LOCAL_PORT, TCP_STATE
                            FROM QSYS2.NETSTAT_INFO
                            WHERE LOCAL_PORT IN ({placeholders})
                        )
                            SELECT DISTINCT LOCAL_PORT
                            FROM FILTERED_PORTS
                            WHERE TCP_STATE = 'LISTEN'
                            ORDER BY LOCAL_PORT
                        """,
                                                *requested_ports,
                    )
                    active_ports = {
                        int(r[0]) for r in cursor.fetchall() if r[0] is not None and str(r[0]).isdigit()
                    }

                    for p_info in target_ports:
                        if self.check_cancelled():
                            return
                        p_num = p_info.get("port") if isinstance(p_info, dict) else p_info
                        p_name = p_info.get("name", f"PORT_{p_num}") if isinstance(p_info, dict) else str(p_num)
                        try:
                            port_number = int(p_num)
                        except (TypeError, ValueError):
                            continue
                        port_status_list.append({
                            "port": port_number,
                            "name": p_name,
                            "service": p_name,
                            "is_up": port_number in active_ports
                        })
            except Exception as e:
                metric_errors.append(f"ports: {e}")

            result = {
                "server": system_name,
                "host_name": system_name,
                "config_key": self.server,
                "status": "DEGRADED" if metric_errors else "ONLINE",
                "cpu": cpu_util,
                "asp": asp_used,
                "jobs": active_jobs,
                "active_jobs_detail": active_jobs_detail,
                "subsystems": active_subsystems,
                "ports": port_status_list,
            }
            if metric_errors:
                result["error"] = "; ".join(metric_errors)

        except Exception as e:
            err_msg = str(e)
            if any(k in err_msg.lower() for k in ["28000", "cwbsy0011", "disabled", "password", "authentication"]):
                result = {
                    "server": self.server,
                    "host_name": self.server,
                    "config_key": self.server,
                    "status": "AUTH_ERROR",
                    "error": f"[{self.server}] {err_msg}",
                    "cpu": 0.0,
                    "asp": 0.0,
                    "jobs": 0,
                    "active_jobs_detail": [],
                    "subsystems": [],
                    "ports": [],
                }
            else:
                result = {
                    "server": self.server,
                    "host_name": self.server,
                    "config_key": self.server,
                    "status": "OFFLINE",
                    "error": f"[{self.server}] {err_msg}",
                    "cpu": 0.0,
                    "asp": 0.0,
                    "jobs": 0,
                    "active_jobs_detail": [],
                    "subsystems": [],
                    "ports": [],
                }
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        if not self.is_cancelled():
            result["sync_duration_ms"] = max(0, int((time.monotonic() - started_at) * 1000))
            result["completed_at"] = time.strftime("%H:%M:%S")
            try:
                maybe_send_server_status_alert(result)
            except Exception:
                pass
            try:
                maybe_send_asp_alert(str(result.get("server") or self.server), float(result.get("asp", 0.0) or 0.0))
            except Exception:
                pass
            _persist_and_emit(self, result)