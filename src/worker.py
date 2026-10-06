import os
import sys
import json
import html as html_lib
import re
import struct
import threading
import time
import uuid
import subprocess
import smtplib
import queue
import socket
import ctypes
from ctypes import wintypes
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
    EXPECTED_WEB_APPS,
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
_PENDING_SERVER_STATUS_ALERTS = {}
SERVER_STATUS_ALERT_DELAY_SECONDS = 3
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
_OBJECT_STATISTICS_CACHE_TTL_SECONDS = 3 * 60 * 60
_OBJECT_STATISTICS_QUERY_TIMEOUT_SECONDS = 120
_TEMPORARY_STORAGE_JOBS_QUERY_TIMEOUT_SECONDS = 120
_OBJECT_STATISTICS_CACHE_LOCK = threading.Lock()
_OBJECT_STATISTICS_CACHE = {}
_OBJECT_STATISTICS_KEY_LOCKS = {}


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
        FETCH FIRST 3 ROW ONLY
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


class MonthlyBackupFetchThread(QThread):
    host_name_ready = pyqtSignal(str, str)
    data_ready = pyqtSignal(str, list)
    error = pyqtSignal(str, str)

    HOST_NAME_QUERY = DailyBackupFetchThread.HOST_NAME_QUERY
    QUERY = """
        WITH MONTH_START AS (
            SELECT MIN(MESSAGE_TIMESTAMP) AS START_TIME
            FROM QUSRBRM.BRMS_LOG_INFO
            WHERE MESSAGE_TEXT LIKE ?
              AND MESSAGE_ID = 'BRM1380'
              AND MESSAGE_TIMESTAMP >= ?
              AND MESSAGE_TIMESTAMP < ?
        )
        SELECT
            ? AS JOB_NAME,
            MONTH_START.START_TIME,
            MIN(BRM_END.MESSAGE_TIMESTAMP) AS END_TIME
        FROM MONTH_START
        LEFT JOIN QUSRBRM.BRMS_LOG_INFO AS BRM_END
          ON BRM_END.MESSAGE_TEXT LIKE ?
         AND BRM_END.MESSAGE_ID = 'BRM1049'
         AND BRM_END.MESSAGE_TIMESTAMP >= MONTH_START.START_TIME
        GROUP BY MONTH_START.START_TIME
    """

    def __init__(self, server_name, host, db, username, password, control_group_name, month_start, next_month):
        super().__init__()
        self.server_name = server_name
        self.host = host
        self.db = db
        self.username = username
        self.password = password
        self.control_group_name = control_group_name
        self.month_start = month_start
        self.next_month = next_month

    def run(self):
        conn = None
        try:
            conn = _new_connection(self.host, self.db, self.username, self.password)
            cursor = conn.cursor()
            cursor.execute(self.HOST_NAME_QUERY)
            host_row = cursor.fetchone()
            if host_row and host_row[0] is not None:
                host_name = str(host_row[0]).strip()
                if host_name:
                    self.host_name_ready.emit(self.server_name, host_name)

            cursor.execute(
                self.QUERY,
                (
                    f"%{self.control_group_name}%",
                    self.month_start,
                    self.next_month,
                    self.control_group_name,
                    f"%{self.control_group_name}%",
                ),
            )
            columns = [column[0].lower() for column in cursor.description]
            row = cursor.fetchone()
            records = []
            if row and row[1] is not None:
                records.append(dict(zip(columns, row)))
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


def _new_connection(host, db, username, password, query_timeout_seconds=3):
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
        f"QUERY_TIMEOUT={int(query_timeout_seconds)};"
        f"{extra_params}",
        timeout=3,
        autocommit=True,
    )


def check_database_connection(host, db, username, password):
    conn = None
    try:
        conn = _new_connection(host, db, username, password)
        cursor = conn.cursor()
        if hasattr(cursor, "execute"):
            cursor.execute("SELECT 1 FROM SYSIBM.SYSDUMMY1")
        fetchone = getattr(cursor, "fetchone", None)
        if callable(fetchone):
            try:
                fetchone()
            except TypeError:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _object_statistics_cache_key(host, db, username):
    return (
        str(host).strip().lower(),
        str(db).strip().upper(),
        str(username).strip().upper(),
    )


def _get_cached_object_statistics(host, db, username):
    cache_key = _object_statistics_cache_key(host, db, username)
    with _OBJECT_STATISTICS_CACHE_LOCK:
        cached = _OBJECT_STATISTICS_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _OBJECT_STATISTICS_CACHE_TTL_SECONDS:
        return cached[1], cached[2], cached[3], cached[4]
    return None


def _fetch_object_statistics_cached(cursor, host, db, username):
    cache_key = _object_statistics_cache_key(host, db, username)
    with _OBJECT_STATISTICS_CACHE_LOCK:
        key_lock = _OBJECT_STATISTICS_KEY_LOCKS.setdefault(cache_key, threading.Lock())

    with key_lock:
        cached_result = _get_cached_object_statistics(host, db, username)
        if cached_result is not None:
            return cached_result
        with _OBJECT_STATISTICS_CACHE_LOCK:
            cached = _OBJECT_STATISTICS_CACHE.get(cache_key)

        try:
            cursor.execute(
                """
                SELECT
                    O.OBJNAME AS OBJECT_NAME,
                    O.OBJLIB AS LIBRARY,
                    O.OBJTYPE AS OBJECT_TYPE,
                    O.OBJATTRIBUTE AS ATTRIBUTE,
                    COALESCE(DECIMAL(O.OBJSIZE / 1073741824.0, 10, 2), 0.00) AS SIZE_GB,
                    COALESCE(DECIMAL(((FLOAT(O.OBJSIZE) / 1073741824.0) / (A.TOTAL_CAPACITY / 1000.0)) * 100, 7, 4), 0.0000) AS PCT_OF_ASP,
                    O.OBJDEFINER AS DEFINER
                FROM TABLE(QSYS2.OBJECT_STATISTICS('*ALLUSR', '*ALL')) O
                CROSS JOIN (
                    SELECT TOTAL_CAPACITY
                    FROM QSYS2.ASP_INFO
                    WHERE ASP_NUMBER = 1
                ) A
                ORDER BY
                    CASE WHEN O.OBJSIZE IS NULL THEN 1 ELSE 0 END ASC,
                    O.OBJSIZE DESC
                FETCH FIRST 5 ROWS ONLY
                WITH UR
                """
            )
            rows = [
                {
                    "object_name": str(row[0]).strip() if row[0] is not None else "",
                    "library": str(row[1]).strip() if row[1] is not None else "",
                    "object_type": str(row[2]).strip() if row[2] is not None else "",
                    "attribute": str(row[3]).strip() if row[3] is not None else "",
                    "size_gb": float(row[4]) if row[4] is not None else 0.0,
                    "pct_of_asp": float(row[5]) if row[5] is not None else 0.0,
                    "definer": str(row[6]).strip() if row[6] is not None else "",
                }
                for row in cursor.fetchall()
            ]
            error = ""
            loaded = True
            updated_at = datetime.now().astimezone().strftime("%m/%d/%Y %H:%M")
        except Exception as exc:
            rows = cached[1] if cached else []
            error = str(exc)
            loaded = bool(cached and cached[3])
            updated_at = cached[4] if cached else ""

        with _OBJECT_STATISTICS_CACHE_LOCK:
            _OBJECT_STATISTICS_CACHE[cache_key] = (time.monotonic(), rows, error, loaded, updated_at)
        return rows, error, loaded, updated_at


def _fetch_storage_pools(cursor):
    cursor.execute(
        """
        SELECT
            POOL_NAME,
            CURRENT_SIZE AS CURRENT_SIZE_MB
        FROM QSYS2.MEMORY_POOL_INFO
        ORDER BY POOL_ID
        """
    )
    return [
        {
            "pool_name": str(row[0]).strip() if row[0] is not None else "",
            "current_size_mb": float(row[1]) if row[1] is not None else 0.0,
        }
        for row in cursor.fetchall()
    ]


def _fetch_pool_threads(cursor):
    cursor.execute(
        """
        SELECT
            POOL_NAME,
            CURRENT_THREADS
        FROM QSYS2.MEMORY_POOL_INFO
        ORDER BY POOL_ID
        """
    )
    return [
        {
            "pool_name": str(row[0]).strip() if row[0] is not None else "",
            "current_threads": float(row[1]) if row[1] is not None else 0.0,
        }
        for row in cursor.fetchall()
    ]


def _fetch_top_temporary_storage_jobs(cursor):
    cursor.execute(
        """
        SELECT
            J.JOB_NAME,
            J.AUTHORIZATION_NAME AS USER_NAME,
            J.TEMPORARY_STORAGE AS TEMP_STORAGE_MB,
            COALESCE(DECIMAL(J.TEMPORARY_STORAGE / 1024.0, 10, 2), 0.00) AS TEMP_STORAGE_GB,
            VARCHAR(COALESCE(DECIMAL((FLOAT(J.TEMPORARY_STORAGE) / A.TOTAL_CAPACITY) * 100, 7, 4), 0.0000)) || '%' AS PCT_OF_ASP
        FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) J
        CROSS JOIN (
            SELECT TOTAL_CAPACITY
            FROM QSYS2.ASP_INFO
            WHERE ASP_NUMBER = 1
        ) A
        ORDER BY J.TEMPORARY_STORAGE DESC
        FETCH FIRST 20 ROWS ONLY
        WITH UR
        """
    )
    return [
        {
            "job_name": str(row[0]).strip() if row[0] is not None else "",
            "user_name": str(row[1]).strip() if row[1] is not None else "",
            "temp_storage_mb": row[2] if row[2] is not None else 0,
            "temp_storage_gb": float(row[3]) if row[3] is not None else 0.0,
            "pct_of_asp": str(row[4]).strip() if row[4] is not None else "0.0000%",
        }
        for row in cursor.fetchall()
    ]


def _fetch_web_app_jobs(cursor, configured_web_apps):
    app_names = set()
    for entry in configured_web_apps or []:
        if isinstance(entry, dict):
            app_name = entry.get("job_name") or entry.get("name") or entry.get("job") or ""
        elif isinstance(entry, str):
            app_name = entry.split(":", 1)[0]
        else:
            continue
        normalized_name = str(app_name).strip().upper()
        if normalized_name:
            app_names.add(normalized_name)

    query = """
        SELECT
            JOB_NAME,
            JOB_STATUS,
            TEMPORARY_STORAGE,
            CPU_TIME
        FROM TABLE(QSYS2.ACTIVE_JOB_INFO(
            JOB_NAME_FILTER => ?,
            DETAILED_INFO => 'NONE'
        ))
    """
    jobs_by_app = {}
    for app_name in sorted(app_names):
        cursor.execute(query, (f"{app_name}*",))
        matching_jobs = []
        for row in cursor.fetchall():
            job = {
                "job_name": str(row[0]).strip() if row[0] is not None else "",
                "job_status": str(row[1]).strip() if row[1] is not None else "",
                "temporary_storage": row[2] if row[2] is not None else 0,
                "cpu_time": row[3] if row[3] is not None else 0,
            }
            if job["job_name"].rsplit("/", 1)[-1].strip().upper() == app_name:
                matching_jobs.append(job)
        jobs_by_app[app_name] = matching_jobs
    return jobs_by_app


def _active_job_detail_from_row(row):
    return {
        "job_name": str(row[0]).strip() if row[0] is not None else "",
        "authorization_name": str(row[1]).strip() if row[1] is not None else "",
        "cpu_time": row[2] if row[2] is not None else 0,
        "elapsed_cpu_percentage": row[3] if row[3] is not None else 0,
        "run_priority": row[4] if row[4] is not None else "",
        "job_status": str(row[5]).strip() if row[5] is not None else "",
        "temporary_storage": row[6] if row[6] is not None else 0,
    }


def _fetch_web_app_ports(cursor, configured_web_apps):
    apps = []
    valid_ports = set()
    for entry in configured_web_apps or []:
        if isinstance(entry, dict):
            app_name = entry.get("job_name") or entry.get("name") or entry.get("job") or ""
            port_value = entry.get("port")
        elif isinstance(entry, str) and ":" in entry:
            app_name, port_value = entry.split(":", 1)
        else:
            continue

        app_name = str(app_name).strip().upper()
        try:
            port = int(port_value)
        except (TypeError, ValueError):
            continue
        if not app_name:
            continue
        is_valid = 1 <= port <= 65535
        apps.append({"name": app_name, "port": port, "is_valid": is_valid})
        if is_valid:
            valid_ports.add(port)

    active_ports = set()
    if valid_ports:
        port_values = sorted(valid_ports)
        placeholders = ", ".join("?" for _ in port_values)
        cursor.execute(
            f"""
            SELECT DISTINCT LOCAL_PORT
            FROM QSYS2.NETSTAT_INFO
            WHERE LOCAL_PORT IN ({placeholders})
              AND TCP_STATE = 'LISTEN'
            ORDER BY LOCAL_PORT
            """,
            *port_values,
        )
        active_ports = {
            int(row[0]) for row in cursor.fetchall()
            if row[0] is not None and str(row[0]).isdigit()
        }

    return [
        {
            "name": app["name"],
            "port": app["port"],
            "is_up": app["is_valid"] and app["port"] in active_ports,
        }
        for app in apps
    ]


def _fetch_web_app_jobs_after_port_check(cursor, web_app_ports, active_jobs_detail=None):
    jobs_by_app = _fetch_web_app_jobs(cursor, web_app_ports or [])
    active_jobs = active_jobs_detail if isinstance(active_jobs_detail, list) else []

    for app in web_app_ports or []:
        if not isinstance(app, dict):
            continue
        app_name = str(app.get("name") or "").strip().upper()
        if not app_name:
            continue

        app_jobs = jobs_by_app.setdefault(app_name, [])
        known_job_names = {
            str(job.get("job_name") or "").strip().upper()
            for job in app_jobs
            if isinstance(job, dict)
        }
        for job in active_jobs:
            if not isinstance(job, dict):
                continue
            job_name = str(job.get("job_name") or "").strip()
            unqualified_name = job_name.rsplit("/", 1)[-1].upper()
            normalized_job_name = job_name.upper()
            if unqualified_name == app_name and normalized_job_name not in known_job_names:
                app_jobs.append(job)
                known_job_names.add(normalized_job_name)

    return jobs_by_app


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
                with _ALERT_STATE_LOCK:
                    if not _SERVER_ALERTING_SERVERS:
                        return
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
        pending_alerts = list(_PENDING_SERVER_STATUS_ALERTS.values())
        _PENDING_SERVER_STATUS_ALERTS.clear()
    for timer in pending_alerts:
        timer.cancel()
    stop_server_down_alert_sound()


def _clear_server_down_alert_state(server_name):
    with _ALERT_STATE_LOCK:
        _LAST_SERVER_STATUS.pop(server_name, None)
        _SERVER_ALERTING_SERVERS.discard(server_name)
        pending_alert = _PENDING_SERVER_STATUS_ALERTS.pop(server_name, None)
        should_stop_sound = not _SERVER_ALERTING_SERVERS
    if pending_alert is not None:
        pending_alert.cancel()
    if should_stop_sound:
        stop_server_down_alert_sound()


def _send_delayed_server_status_alert(server_name, error, timer):
    with _ALERT_STATE_LOCK:
        if _PENDING_SERVER_STATUS_ALERTS.get(server_name) is not timer:
            return
        if _LAST_SERVER_STATUS.get(server_name) != "OFFLINE":
            _PENDING_SERVER_STATUS_ALERTS.pop(server_name, None)
            return

    if not has_vpn_ip():
        _clear_server_down_alert_state(server_name)
        return

    with _ALERT_STATE_LOCK:
        if (
            _PENDING_SERVER_STATUS_ALERTS.get(server_name) is not timer
            or _LAST_SERVER_STATUS.get(server_name) != "OFFLINE"
        ):
            return
        _PENDING_SERVER_STATUS_ALERTS.pop(server_name, None)

    send_server_status_alert(server_name, "OFFLINE", error)


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


def send_asp_alert(server_name, asp_value, threshold_percent, severity="WARNING", server_ip=""):
    """Sends an SMTP notification when ASP usage crosses the warning/critical escalation thresholds."""
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
    severity_label = str(severity or "WARNING").upper()

    try:
        msg = EmailMessage()
        subject_server = str(server_name).strip()
        if server_ip and server_ip.casefold() != subject_server.casefold():
            subject_server = f"{subject_server} {server_ip}"
        msg["Subject"] = f"ASP {severity_label} Alert - {subject_server}"
        msg["From"] = from_address
        msg["To"] = ", ".join(recipients)
        safe_server_name = html_lib.escape(str(server_name))
        threshold_text = f"{float(threshold_percent):g}%"
        current_usage_text = f"{asp_value:.2f}%"

        if severity_label == "CRITICAL":
            text_body = (
                "Hello Team,\n\n"
                f"A CRITICAL alert has been triggered for {server_name} as the ASP usage has reached the configured threshold.\n\n"
                f"Server: {server_name}\n"
                f"Current ASP Usage: {current_usage_text}\n"
                f"ASP Threshold: {threshold_text}\n"
                "Status: CRITICAL\n\n"
                "The ASP usage has reached or exceeded the configured threshold. Please take the necessary action to prevent further increase and potential impact to the system.\n\n"
                "This notification was generated automatically by the IBM i Monitoring Dashboard."
            )
            html_body = (
                "<p>Hello Team,</p>"
                f"<p>A <strong>CRITICAL</strong> alert has been triggered for <strong>{safe_server_name}</strong> as the ASP usage has reached the configured threshold.</p>"
                f"<p><strong>Server:</strong> {safe_server_name}<br>"
                f"<strong>Current ASP Usage:</strong> {current_usage_text}<br>"
                f"<strong>ASP Threshold:</strong> {threshold_text}<br>"
                "<strong>Status:</strong> CRITICAL</p>"
                "<p>The ASP usage has reached or exceeded the configured threshold. Please take the necessary action to prevent further increase and potential impact to the system.</p>"
                "<p>This notification was generated automatically by the <strong>IBM i Monitoring Dashboard</strong>.</p>"
            )
        else:
            text_body = (
                "Hello Team,\n\n"
                f"A WARNING alert has been triggered for {server_name} due to high ASP usage.\n\n"
                f"Server: {server_name}\n"
                f"Current ASP Usage: {current_usage_text}\n"
                f"ASP Threshold: {threshold_text}\n"
                "Status: WARNING\n\n"
                "Please monitor the ASP usage and take the necessary action if it continues to increase.\n\n"
                "This notification was generated automatically by the IBM i Monitoring Dashboard."
            )
            html_body = (
                "<p>Hello Team,</p>"
                f"<p>A <strong>WARNING</strong> alert has been triggered for <strong>{safe_server_name}</strong> due to high ASP usage.</p>"
                f"<p><strong>Server:</strong> {safe_server_name}<br>"
                f"<strong>Current ASP Usage:</strong> {current_usage_text}<br>"
                f"<strong>ASP Threshold:</strong> {threshold_text}<br>"
                "<strong>Status:</strong> WARNING</p>"
                "<p>Please monitor the ASP usage and take the necessary action if it continues to increase.</p>"
                "<p>This notification was generated automatically by the <strong>IBM i Monitoring Dashboard</strong>.</p>"
            )

        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")

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
    if str(status).upper() == "OFFLINE" and not has_vpn_ip():
        return False

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
    # Use the configured key for state tracking. The reported IBM i host name
    # can differ between successful and failed connection results.
    server_name = str(result.get("config_key") or result.get("server") or "Unknown server")
    status = str(result.get("status", "OFFLINE")).upper()
    is_down = status == "OFFLINE"
    is_up = status in {"ONLINE", "DEGRADED"}

    if is_down and not has_vpn_ip():
        _clear_server_down_alert_state(server_name)
        return False

    cancelled_alert = None
    with _ALERT_STATE_LOCK:
        previous_status = _LAST_SERVER_STATUS.get(server_name)
        _LAST_SERVER_STATUS[server_name] = status
        if is_down:
            _SERVER_ALERTING_SERVERS.add(server_name)
        elif is_up:
            _SERVER_ALERTING_SERVERS.discard(server_name)
            cancelled_alert = _PENDING_SERVER_STATUS_ALERTS.pop(server_name, None)
        should_stop_sound = not _SERVER_ALERTING_SERVERS
    if cancelled_alert is not None:
        cancelled_alert.cancel()

    if is_down and previous_status != "OFFLINE":
        # Recheck immediately before alerting so a VPN disconnect cannot turn
        # a server connection failure into a false server-down notification.
        if not has_vpn_ip():
            _clear_server_down_alert_state(server_name)
            return False

        play_server_down_alert_sound()
        if not has_vpn_ip():
            _clear_server_down_alert_state(server_name)
            return False

        timer_ref = {}
        timer = threading.Timer(
            SERVER_STATUS_ALERT_DELAY_SECONDS,
            lambda: _send_delayed_server_status_alert(
                server_name, str(result.get("error", "")), timer_ref["timer"]
            ),
        )
        timer_ref["timer"] = timer
        timer.daemon = True
        with _ALERT_STATE_LOCK:
            if _LAST_SERVER_STATUS.get(server_name) != "OFFLINE":
                return False
            previous_timer = _PENDING_SERVER_STATUS_ALERTS.get(server_name)
            _PENDING_SERVER_STATUS_ALERTS[server_name] = timer
        if previous_timer is not None:
            previous_timer.cancel()
        timer.start()
        return True

    if is_up and previous_status == "OFFLINE":
        if should_stop_sound:
            stop_server_down_alert_sound()
        return play_alert_sound("up.wav")

    return False


def maybe_send_asp_alert(server_name, asp_value, display_name=None):
    """Escalates ASP alerts using the configured threshold as the critical point.

    When no explicit warning threshold is configured, the warning is set to 2%
    below the configured threshold to preserve the intended warning/critical
    escalation model.
    """
    alert_cfg = load_email_alerts()
    email_enabled = bool(alert_cfg.get("enabled"))
    display_name = str(display_name or server_name).strip()

    try:
        asp_value = float(asp_value if asp_value is not None else 0.0)
    except (TypeError, ValueError):
        asp_value = 0.0

    configured_threshold = float(alert_cfg.get("threshold_percent", 90.0) or 90.0)
    warning_threshold = float(alert_cfg.get("warning_threshold") if alert_cfg.get("warning_threshold") is not None else configured_threshold * 0.98)
    critical_threshold = float(alert_cfg.get("critical_threshold", configured_threshold) or configured_threshold)
    if critical_threshold < warning_threshold:
        warning_threshold, critical_threshold = critical_threshold, warning_threshold

    threshold_percent = warning_threshold
    cooldown_seconds = max(0, int(float(alert_cfg.get("cooldown_minutes", 10) or 10) * 60))

    if asp_value >= critical_threshold:
        severity = "CRITICAL"
        threshold_percent = critical_threshold
    elif asp_value >= warning_threshold:
        severity = "WARNING"
        threshold_percent = warning_threshold
    else:
        severity = None

    email_should_send = False
    should_play_sound = False
    should_stop_sound = False

    with _ALERT_STATE_LOCK:
        now = time.monotonic()

        if severity is None:
            previous_state = _LAST_ASP_ALERT_STATE.get(server_name)
            previous_severity = previous_state.get("severity") if isinstance(previous_state, dict) else None
            if previous_severity is not None:
                _LAST_ASP_ALERT_STATE[server_name] = {"armed": False, "severity": None, "sent_at": 0.0}
            else:
                _LAST_ASP_ALERT_STATE.pop(server_name, None)

            any_armed = any(st.get("armed", False) for st in _LAST_ASP_ALERT_STATE.values())
            should_stop_sound = not any(
                st.get("severity") == "CRITICAL"
                for st in _LAST_ASP_ALERT_STATE.values()
                if isinstance(st, dict)
            )
            if not any_armed:
                _LAST_ASP_ALERT_STATE.clear()
        else:
            previous_state = _LAST_ASP_ALERT_STATE.get(server_name)
            previous_severity = previous_state.get("severity") if isinstance(previous_state, dict) else None
            _LAST_ASP_ALERT_STATE[server_name] = {"armed": True, "severity": severity, "sent_at": now}
            should_play_sound = severity == "CRITICAL"
            should_stop_sound = not any(
                st.get("severity") == "CRITICAL"
                for st in _LAST_ASP_ALERT_STATE.values()
                if isinstance(st, dict)
            )

            if email_enabled:
                email_state = _LAST_ASP_EMAIL_STATE.get(server_name, {"sent_at": None})
                last_sent = email_state.get("sent_at")
                if previous_severity != severity or last_sent is None or (now - last_sent) >= cooldown_seconds:
                    _LAST_ASP_EMAIL_STATE[server_name] = {"sent_at": now}
                    email_should_send = True

    sound_played = False
    if should_play_sound:
        sound_played = play_asp_alert_sound()
    elif should_stop_sound:
        stop_asp_alert_sound()

    if email_should_send:
        server_config = SERVER_CONFIGS.get(server_name, {})
        server_ip = str(server_config.get("host", "")).strip() if isinstance(server_config, dict) else ""
        email_sent = send_asp_alert(
            display_name,
            asp_value,
            critical_threshold,
            severity=severity,
            server_ip=server_ip,
        )
        return email_sent or sound_played

    return sound_played


AF_INET = 2
ERROR_BUFFER_OVERFLOW = 111
IF_OPER_STATUS_UP = 1


class _SocketAddress(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.c_void_p),
        ("iSockaddrLength", ctypes.c_int),
    ]


class _IpAdapterUnicastAddress(ctypes.Structure):
    pass


_IpAdapterUnicastAddress._fields_ = [
    ("Length", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Next", ctypes.POINTER(_IpAdapterUnicastAddress)),
    ("Address", _SocketAddress),
]


class _IpAdapterAddresses(ctypes.Structure):
    pass


_IpAdapterAddresses._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.ULONG),
    ("Next", ctypes.POINTER(_IpAdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IpAdapterUnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.ULONG),
    ("OperStatus", wintypes.ULONG),
]


def has_vpn_ip(prefix="10.212."):
    """Return whether an active Windows adapter owns an IPv4 address in the VPN range."""
    if sys.platform != "win32":
        return False

    try:
        iphlpapi = ctypes.windll.iphlpapi
        get_adapters = iphlpapi.GetAdaptersAddresses
        get_adapters.argtypes = [
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.ULONG),
        ]
        get_adapters.restype = wintypes.ULONG

        # Skip anycast, multicast, and DNS entries, but keep unicast addresses.
        flags = 0x0002 | 0x0004 | 0x0008
        buffer_len = wintypes.ULONG(15 * 1024)
        buffer = ctypes.create_string_buffer(buffer_len.value)
        result = get_adapters(
            AF_INET,
            flags,
            None,
            buffer,
            ctypes.byref(buffer_len),
        )

        if result == ERROR_BUFFER_OVERFLOW:
            buffer = ctypes.create_string_buffer(buffer_len.value)
            result = get_adapters(
                AF_INET,
                flags,
                None,
                buffer,
                ctypes.byref(buffer_len),
            )
        if result != 0:
            return False

        adapter = ctypes.cast(buffer, ctypes.POINTER(_IpAdapterAddresses))
        while adapter:
            current = adapter.contents
            if current.OperStatus == IF_OPER_STATUS_UP:
                unicast = current.FirstUnicastAddress
                while unicast:
                    address = unicast.contents.Address
                    if address.lpSockaddr and address.iSockaddrLength >= 8:
                        raw_sockaddr = ctypes.string_at(address.lpSockaddr, 8)
                        if int.from_bytes(raw_sockaddr[:2], "little") == AF_INET:
                            ip_address = socket.inet_ntoa(raw_sockaddr[4:8])
                            if ip_address.startswith(prefix):
                                return True
                    unicast = unicast.contents.Next
            adapter = current.Next
        return False
    except (AttributeError, OSError, ValueError):
        return False


def cleanup_old_logs(days_to_keep=30):
    """Delete canonical daily log files older than the retention period in all archives.

    Keep the current month and previous month available so the monthly report graph
    still has enough data for the most recent reporting period even when daily
    retention is enabled.
    """
    now = datetime.now()
    cutoff_date = now - timedelta(days=days_to_keep)
    current_month = now.strftime("%Y-%m")
    previous_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
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
                month_key = file_date.strftime("%Y-%m")
                if month_key in {current_month, previous_month}:
                    continue
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


class ObjectStatisticsSignals(QObject):
    statistics_ready = pyqtSignal(dict)


class ObjectStatisticsRunnable(QRunnable):
    def __init__(self, server, cfg, username, password, signal_parent=None):
        super().__init__()
        self.server = server
        self.cfg = cfg
        self.username = username
        self.password = password
        self.signals = ObjectStatisticsSignals(signal_parent)

    def run(self):
        host = self.cfg.get("host", "") if isinstance(self.cfg, dict) else str(self.cfg)
        db = self.cfg.get("db", "*LOCAL") if isinstance(self.cfg, dict) else "*LOCAL"
        result = {
            "config_key": self.server,
            "object_statistics": [],
            "object_statistics_error": "",
            "object_statistics_loaded": False,
            "object_statistics_updated_at": "",
        }
        conn = None
        try:
            cached = _get_cached_object_statistics(host, db, self.username)
            if cached is None:
                conn = _new_connection(
                    host,
                    db,
                    self.username,
                    self.password,
                    _OBJECT_STATISTICS_QUERY_TIMEOUT_SECONDS,
                )
                cursor = conn.cursor()
                cached = _fetch_object_statistics_cached(cursor, host, db, self.username)
            rows, error, loaded, updated_at = cached
            result["object_statistics"] = rows
            result["object_statistics_error"] = error
            result["object_statistics_loaded"] = loaded
            result["object_statistics_updated_at"] = updated_at
        except Exception as exc:
            result["object_statistics_error"] = str(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self.signals.statistics_ready.emit(result)


class TemporaryStorageJobsSignals(QObject):
    jobs_ready = pyqtSignal(dict)


class TemporaryStorageJobsRunnable(QRunnable):
    def __init__(self, server, cfg, username, password, signal_parent=None):
        super().__init__()
        self.server = server
        self.cfg = cfg
        self.username = username
        self.password = password
        self.signals = TemporaryStorageJobsSignals(signal_parent)

    def run(self):
        host = self.cfg.get("host", "") if isinstance(self.cfg, dict) else str(self.cfg)
        db = self.cfg.get("db", "*LOCAL") if isinstance(self.cfg, dict) else "*LOCAL"
        result = {
            "config_key": self.server,
            "top_temporary_storage_jobs": [],
            "top_temporary_storage_jobs_error": "",
        }
        conn = None
        try:
            conn = _new_connection(
                host,
                db,
                self.username,
                self.password,
                _TEMPORARY_STORAGE_JOBS_QUERY_TIMEOUT_SECONDS,
            )
            result["top_temporary_storage_jobs"] = _fetch_top_temporary_storage_jobs(
                conn.cursor()
            )
        except Exception as exc:
            result["top_temporary_storage_jobs_error"] = str(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self.signals.jobs_ready.emit(result)


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
            storage_pools = None
            pool_threads = None

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
                        AUTHORIZATION_NAME,
                        CPU_TIME,
                        ELAPSED_CPU_PERCENTAGE,
                        RUN_PRIORITY,
                        JOB_STATUS,
                        TEMPORARY_STORAGE
                    FROM TABLE(QSYS2.ACTIVE_JOB_INFO(DETAILED_INFO => 'NONE'))
                    """
                )
                for row in cursor.fetchall():
                    active_jobs_detail.append(_active_job_detail_from_row(row))
                active_jobs = len(active_jobs_detail)
            except Exception as e:
                metric_errors.append(f"active job details: {e}")

            try:
                storage_pools = _fetch_storage_pools(cursor)
            except Exception:
                pass
            try:
                pool_threads = _fetch_pool_threads(cursor)
            except Exception:
                pass

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

            web_app_ports = []
            try:
                web_app_ports = _fetch_web_app_ports(
                    cursor,
                    EXPECTED_WEB_APPS.get(self.server, []),
                )
            except Exception as e:
                metric_errors.append(f"web app ports: {e}")

            web_app_jobs_detail = {}
            try:
                web_app_jobs_detail = _fetch_web_app_jobs_after_port_check(
                    cursor,
                    web_app_ports,
                    active_jobs_detail,
                )
            except Exception as e:
                metric_errors.append(f"web app job details: {e}")

            result = {
                "server": system_name,
                "host_name": system_name,
                "config_key": self.server,
                "status": "DEGRADED" if metric_errors else "ONLINE",
                "cpu": cpu_util,
                "asp": asp_used,
                "jobs": active_jobs,
                "active_jobs_detail": active_jobs_detail,
                "web_app_jobs_detail": web_app_jobs_detail,
                "web_app_ports": web_app_ports,
                "subsystems": active_subsystems,
                "ports": port_status_list,
            }
            if storage_pools is not None:
                result["storage_pools"] = storage_pools
            if pool_threads is not None:
                result["pool_threads"] = pool_threads
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
                    "web_app_jobs_detail": {},
                    "web_app_ports": [],
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
                    "web_app_jobs_detail": {},
                    "web_app_ports": [],
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
                maybe_send_asp_alert(
                    str(result.get("config_key") or self.server),
                    float(result.get("asp", 0.0) or 0.0),
                    display_name=str(result.get("host_name") or result.get("server") or "").strip(),
                )
            except Exception:
                pass
            _persist_and_emit(self, result)