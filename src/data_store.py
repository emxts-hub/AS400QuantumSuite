import json
import os

from config import get_logs_dir, get_all_logs_dirs


def _read_local_log_file(file_path):
    if not os.path.isfile(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else ([] if data is None else [data])


def write_log(entry):
    """Persist log entries locally; this module does not provide cloud sync."""
    if not isinstance(entry, dict):
        return False

    timestamp = str(entry.get("timestamp") or "")
    if not timestamp:
        return False

    date_key = timestamp[:10] if len(timestamp) >= 10 else ""
    if not date_key:
        return False

    log_dir = get_logs_dir()
    os.makedirs(log_dir, exist_ok=True)
    file_path = os.path.join(log_dir, f"lpar_history_{date_key}.json")

    existing = _read_local_log_file(file_path)
    entry_signature = json.dumps(entry, sort_keys=True, default=str)
    if not any(
        json.dumps(item, sort_keys=True, default=str) == entry_signature
        for item in existing
    ):
        existing.append(entry)

    temp_path = f"{file_path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2)
        os.replace(temp_path, file_path)
        return True
    except OSError:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return False


def sync_pending_logs():
    """Return zero because cloud synchronization is not part of this build."""
    return 0


def read_recent_logs(limit=500):
    try:
        limit_value = max(1, int(limit))
    except (TypeError, ValueError):
        limit_value = 500
    limit_value = min(limit_value, 1000)

    entries = []
    for log_dir in get_all_logs_dirs():
        if not os.path.isdir(log_dir):
            continue
        for file_name in sorted(os.listdir(log_dir), reverse=True):
            if not file_name.startswith("lpar_history_") or not file_name.endswith(".json"):
                continue
            file_path = os.path.join(log_dir, file_name)
            for item in _read_local_log_file(file_path):
                if isinstance(item, dict):
                    entries.append(item)
                if len(entries) >= limit_value:
                    return entries
    return entries[:limit_value]


def delete_logs_older_than(cutoff_timestamp):
    """Local offline cleanup: remove daily history files older than the cutoff date."""
    log_dir = get_logs_dir()
    if not os.path.isdir(log_dir):
        return 0

    cutoff_text = str(cutoff_timestamp).split(" ", 1)[0] if cutoff_timestamp is not None else ""
    if not cutoff_text:
        return 0

    removed = 0
    for file_name in os.listdir(log_dir):
        if not file_name.startswith("lpar_history_") or not file_name.endswith(".json"):
            continue
        file_date = file_name[len("lpar_history_"):-5]
        if file_date < cutoff_text:
            file_path = os.path.join(log_dir, file_name)
            try:
                os.remove(file_path)
                removed += 1
            except OSError:
                pass
    return removed
