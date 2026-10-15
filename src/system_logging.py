import logging
import os
import threading
from collections import deque

SYSTEM_LOG_FILE_NAME = "system.log"
_system_log_lock = threading.RLock()


def _read_saved_records(log_path):
    saved_records = deque(maxlen=1000)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as log_file:
            saved_records.extend(line.rstrip("\r\n") for line in log_file)
    except FileNotFoundError:
        pass
    return saved_records


class SystemLogHandler(logging.Handler):
    def __init__(self, saved_records=()):
        logging.Handler.__init__(self, level=logging.INFO)
        self.lock = _system_log_lock
        self.records = deque(maxlen=1000)
        self.records.extend(
            (record_id, message)
            for record_id, message in enumerate(saved_records)
        )
        self.next_record_id = len(self.records)
        self.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    def emit(self, record):
        message = self.format(record)
        self.records.append((self.next_record_id, message))
        self.next_record_id += 1

    def records_since(self, record_id):
        self.acquire()
        try:
            if self.records:
                record_id = max(record_id, self.records[0][0])
            return self.next_record_id, [
                message
                for current_id, message in self.records
                if current_id >= record_id
            ]
        finally:
            self.release()


_system_log_handler = None
_system_log_file_handler = None
SYSTEM_LOGGER = logging.getLogger("AS400QuantumSuite")


def configure_system_logging():
    global _system_log_handler, _system_log_file_handler

    logger = SYSTEM_LOGGER
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from config import get_app_data_dir

    log_path = os.path.join(get_app_data_dir(), SYSTEM_LOG_FILE_NAME)
    if _system_log_handler is None:
        _system_log_handler = SystemLogHandler(_read_saved_records(log_path))
        _system_log_handler.setFormatter(formatter)
        logger.addHandler(_system_log_handler)
    if _system_log_file_handler is None:
        _system_log_file_handler = logging.FileHandler(
            log_path,
            mode="a",
            encoding="utf-8",
        )
        _system_log_file_handler.lock = _system_log_lock
        _system_log_file_handler.setLevel(logging.INFO)
        _system_log_file_handler.setFormatter(formatter)
        logger.addHandler(_system_log_file_handler)
    return logger, _system_log_handler


def clear_system_logs():
    """Clear the system log file and in-memory history as one synchronized operation."""
    global _system_log_handler, _system_log_file_handler

    if _system_log_handler is None or _system_log_file_handler is None:
        configure_system_logging()

    with _system_log_lock:
        file_handler = _system_log_file_handler
        memory_handler = _system_log_handler
        if file_handler is None or memory_handler is None:
            raise RuntimeError("System logging is not configured.")

        file_handler.flush()
        stream = file_handler.stream
        if stream is None:
            raise OSError("The system log file is not open.")
        stream.seek(0)
        stream.truncate(0)
        stream.flush()

        memory_handler.records.clear()
        return memory_handler.next_record_id
