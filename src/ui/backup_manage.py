import json
import os
import threading
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCursor, QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QHeaderView,
    QSizePolicy,
    QWidget,
)

from config import get_logs_dir, safe_json_save
from worker import DailyBackupFetchThread

_BACKUP_FILE_LOCK = threading.RLock()


class BackupManagementWidget(QWidget):
    ACTIVE_BUTTON_STYLE = """
        QPushButton {
            background-color: #0078D4;
            color: white;
            font-weight: bold;
            border: 1px solid #005A9E;
            border-radius: 4px;
            padding: 5px 12px;
        }
    """
    DEFAULT_BUTTON_STYLE = """
        QPushButton {
            background-color: #F3F3F3;
            color: #333333;
            border: 1px solid #CCCCCC;
            border-radius: 4px;
            padding: 5px 12px;
        }
        QPushButton:hover {
            background-color: #E5E5E5;
        }
    """

    def __init__(self, server_configs, credentials_provider, credentials_validated_provider, status_callback=None, parent=None):
        super().__init__(parent)
        self.server_configs = server_configs
        self.credentials_provider = credentials_provider
        self.credentials_validated_provider = credentials_validated_provider
        self.status_callback = status_callback
        self.backup_selected_server = None
        self.backup_fetch_thread = None
        self.backup_fetch_threads = {}
        self.backup_tables = {}
        self.backup_average_labels = {}
        self.backup_weekly_average_labels = {}
        self.backup_server_buttons = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("Backup Management")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        layout.addWidget(title)
        subtitle = QLabel("Selected systems - Daily averages")
        subtitle.setFont(QFont("Segoe UI", 10, QFont.Weight.Normal))
        layout.addWidget(subtitle)

        # Control Row containing Server Buttons and Month Selector
        self.server_row = QHBoxLayout()
        self.server_row.addWidget(QLabel("Server:"))
        for server_name in sorted(self.server_configs):
            button = QPushButton(server_name)
            button.setProperty("server_key", server_name)
            button.setCheckable(True)
            button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            button.setToolTip(f"View backup data for {server_name}")
            button.setStyleSheet(self.DEFAULT_BUTTON_STYLE)
            button.clicked.connect(lambda checked, name=server_name: self.select_server(name))
            self.server_row.addWidget(button)
            self.backup_server_buttons.append(button)
        if not self.backup_server_buttons:
            self.server_row.addWidget(QLabel("No servers configured"))

        self.server_row.addStretch()

        # Month Selector Dropdown
        self.server_row.addWidget(QLabel("Month:"))
        self.month_combo = QComboBox()
        self.month_combo.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._populate_month_selector()
        self.month_combo.currentIndexChanged.connect(self._on_month_changed)
        self.server_row.addWidget(self.month_combo)

        layout.addLayout(self.server_row)

        tables_layout = QHBoxLayout()
        tables_layout.setSpacing(16)
        tables_layout.addWidget(self._create_backup_panel("Daily Backup", "daily"), stretch=1)
        tables_layout.addWidget(self._create_backup_panel("Journal Backup", "journal"), stretch=1)
        layout.addLayout(tables_layout, stretch=1)

        self.hourly_timer = QTimer(self)
        self.hourly_timer.setInterval(60 * 60 * 1000)
        self.hourly_timer.timeout.connect(self.fetch_all_backup_data)
        if self.backup_server_buttons:
            self.select_server(self.backup_server_buttons[0].property("server_key"))

    def _populate_month_selector(self):
        self.month_combo.clear()
        logs_dir = get_logs_dir()
        found_months = set()

        # Scan log directory for files matching daily_backup_YYYY-MM.json or journal_backup_YYYY-MM.json
        if os.path.exists(logs_dir):
            for filename in os.listdir(logs_dir):
                if filename.endswith(".json") and ("_backup_" in filename):
                    parts = filename.rsplit("_backup_", 1)
                    if len(parts) == 2:
                        month_part = parts[1].replace(".json", "")
                        try:
                            datetime.strptime(month_part, "%Y-%m")
                            found_months.add(month_part)
                        except ValueError:
                            pass

        # Always include current month even if no log file has been created yet
        current_month = datetime.now().strftime("%Y-%m")
        found_months.add(current_month)

        # Sort months descending (newest first)
        sorted_months = sorted(found_months, reverse=True)

        # Populate combo box with formatted Month Year string
        for month_str in sorted_months:
            date_obj = datetime.strptime(month_str, "%Y-%m")
            display_str = date_obj.strftime("%B %Y")
            self.month_combo.addItem(display_str, month_str)

    def _selected_month_prefix(self):
        return self.month_combo.currentData() or datetime.now().strftime("%Y-%m")

    def _on_month_changed(self):
        if self.backup_selected_server:
            for backup_type in self.backup_tables:
                self._load_backup_json(self.backup_selected_server, backup_type)

    def _create_backup_panel(self, title_text, backup_type):
        panel = QGroupBox(title_text)
        panel.setMinimumWidth(420)
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 8, 10, 10)
        panel_layout.setSpacing(8)

        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["Job ID / Name", "Start Time", "End Time", "Duration", "Status"])
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        vertical_header = table.verticalHeader()
        horizontal_header = table.horizontalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        if horizontal_header is not None:
            horizontal_header.setStretchLastSection(True)
            horizontal_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for column in range(1, 5):
                horizontal_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        table.setMinimumHeight(310)
        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        panel_layout.addWidget(table, stretch=1)
        average_label = QLabel("Average: 00h 00m")
        average_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        average_label.setContentsMargins(6, 0, 0, 0)
        panel_layout.addWidget(average_label)
        weekly_average_label = None
        if backup_type == "daily":
            weekly_average_label = QLabel("Weekly Average: 00h 00m")
            weekly_average_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            weekly_average_label.setContentsMargins(6, 0, 0, 0)
            panel_layout.addWidget(weekly_average_label)
        self.backup_tables[backup_type] = table
        self.backup_average_labels[backup_type] = average_label
        if weekly_average_label is not None:
            self.backup_weekly_average_labels[backup_type] = weekly_average_label
        return panel

    def select_server(self, server_name):
        self.backup_selected_server = server_name
        
        # Highlight active server button
        for button in self.backup_server_buttons:
            is_selected = (button.property("server_key") == server_name)
            button.setChecked(is_selected)
            button.setStyleSheet(self.ACTIVE_BUTTON_STYLE if is_selected else self.DEFAULT_BUTTON_STYLE)

        for table in self.backup_tables.values():
            table.setProperty("selected_server", server_name)

        # Refresh months dropdown to pick up newly created monthly logs
        self.month_combo.blockSignals(True)
        self._populate_month_selector()
        self.month_combo.blockSignals(False)

        for backup_type in self.backup_tables:
            self._load_backup_json(server_name, backup_type)
            
        self.fetch_all_backup_data()
        self._set_status(f"Status: Backup data selected for {server_name}.")

    def _job_name_for(self, backup_type, server_name=None):
        target_server = server_name or self.backup_selected_server
        config = self.server_configs.get(target_server, {})

        key = "daily_backup_name" if backup_type == "daily" else "journal_backup_name"
        val = str(config.get(key, "") or "").strip()

        if not val and backup_type == "journal":
            raw_daily = str(config.get("daily_backup_name", "") or "").strip()
            parts = raw_daily.split()
            if len(parts) > 1:
                return parts[1].upper()

        if val:
            parts = val.split()
            if backup_type == "daily":
                return parts[0].upper()
            elif backup_type == "journal":
                return parts[1].upper() if len(parts) > 1 else parts[0].upper()

        return "DAILYSWA" if backup_type == "daily" else "JRNBKUP"

    def _backup_json_path(self, backup_type, month_prefix=None):
        prefix = month_prefix or self._selected_month_prefix()
        return os.path.join(get_logs_dir(), f"{backup_type}_backup_{prefix}.json")

    def _load_backup_json(self, server_name, backup_type):
        records = []
        month_prefix = self._selected_month_prefix()
        path = self._backup_json_path(backup_type, month_prefix)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as file:
                    all_records = (json.load(file) or {}).get(server_name, [])
                    records = [
                        rec for rec in all_records
                        if str(rec.get("start_time", "")).startswith(month_prefix)
                    ]
        except (OSError, ValueError):
            pass
        self._populate_backup_table(self.backup_tables[backup_type], records)

    def _populate_backup_table(self, table, records):
        table.setRowCount(len(records))
        for row_index, record in enumerate(records):
            start_time = record.get("start_time", "")
            end_time = record.get("end_time", "")
            values = [
                record.get("job_name", ""),
                start_time,
                end_time,
                self._backup_duration(start_time, end_time),
                record.get("backup_status", ""),
            ]
            for column_index, value in enumerate(values):
                table.setItem(row_index, column_index, QTableWidgetItem(str(value or "")))
        backup_type = next(
            (kind for kind, backup_table in self.backup_tables.items() if backup_table is table),
            None,
        )
        if backup_type is not None:
            average_title = "Daily Average" if backup_type == "daily" else "Average"
            self.backup_average_labels[backup_type].setText(
                f"{average_title}: "
                f"{self._average_backup_duration(self._weekday_records(records)) if backup_type == 'daily' else self._average_backup_duration(records)}"
            )
            if backup_type == "daily":
                self.backup_weekly_average_labels[backup_type].setText(
                    f"Weekly Average: {self._average_backup_duration(self._sunday_records(records))}"
                )

    @staticmethod
    def _record_start_date(record):
        value = record.get("start_time", "")
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.date()
        except (TypeError, ValueError):
            return None

    @classmethod
    def _weekday_records(cls, records):
        return [record for record in records if (start_date := cls._record_start_date(record)) is not None and start_date.weekday() < 6]

    @classmethod
    def _sunday_records(cls, records):
        return [record for record in records if (start_date := cls._record_start_date(record)) is not None and start_date.weekday() == 6]

    @classmethod
    def _average_backup_duration(cls, records):
        durations = []
        for record in records:
            seconds = cls._backup_duration_seconds(
                record.get("start_time", ""), record.get("end_time", "")
            )
            if seconds is not None:
                durations.append(seconds)
        if not durations:
            return "00h 00m"
        average_seconds = round(sum(durations) / len(durations))
        hours, remainder = divmod(average_seconds, 3600)
        minutes = remainder // 60
        return f"{hours:02d}h {minutes:02d}m"

    @staticmethod
    def _backup_duration_seconds(start_time, end_time):
        if not start_time or not end_time:
            return None
        try:
            start = start_time if isinstance(start_time, datetime) else datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
            end = end_time if isinstance(end_time, datetime) else datetime.fromisoformat(str(end_time).replace("Z", "+00:00"))
            return max(0, int((end - start).total_seconds()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _backup_duration(start_time, end_time):
        seconds = BackupManagementWidget._backup_duration_seconds(start_time, end_time)
        if not start_time:
            return ""
        if not end_time:
            return "RUNNING"
        if seconds is None:
            return ""
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"

    def fetch_selected_daily_backup(self):
        self.fetch_backup_data("daily")

    def fetch_all_backup_data(self):
        self.fetch_backup_data("daily")
        self.fetch_backup_data("journal")

    def fetch_backup_data(self, backup_type):
        if not self.backup_selected_server or not self.credentials_validated_provider():
            return
        thread = self.backup_fetch_threads.get(backup_type)
        if thread is not None and thread.isRunning():
            return
        config = self.server_configs.get(self.backup_selected_server, {})
        host = config.get("host", "") if isinstance(config, dict) else str(config)
        db = config.get("db", "*LOCAL") if isinstance(config, dict) else "*LOCAL"
        username, password = self.credentials_provider()
        job_name = self._job_name_for(backup_type, self.backup_selected_server)
        thread = DailyBackupFetchThread(
            self.backup_selected_server, host, db, username, password, job_name
        )
        self.backup_fetch_threads[backup_type] = thread
        thread.data_ready.connect(
            lambda server_name, records, kind=backup_type: self._handle_backup_data(kind, server_name, records)
        )
        thread.host_name_ready.connect(self._handle_host_name)
        thread.error.connect(
            lambda server_name, message, kind=backup_type: self._handle_backup_error(kind, server_name, message)
        )
        thread.start()

    def _handle_host_name(self, server_name, host_name):
        for button in self.backup_server_buttons:
            if button.property("server_key") == server_name:
                button.setText(host_name)
                button.setToolTip(f"View backup data for {host_name} ({server_name})")
                break

        self.backup_server_buttons.sort(key=lambda btn: btn.text())

        for button in self.backup_server_buttons:
            self.server_row.removeWidget(button)

        for i in range(len(self.backup_server_buttons)):
            self.server_row.insertWidget(i + 1, self.backup_server_buttons[i])

    def _handle_backup_data(self, backup_type, server_name, records):
        current_month = datetime.now().strftime("%Y-%m")
        path = self._backup_json_path(backup_type, current_month)
        with _BACKUP_FILE_LOCK:
            data = self._read_backup_file(path)
            existing_records = data.get(server_name, [])

            combined = {
                (rec.get("job_name"), str(rec.get("start_time"))): rec
                for rec in existing_records + records
            }
            updated_records = sorted(
                combined.values(),
                key=lambda x: str(x.get("start_time", "")),
                reverse=True,
            )
            data[server_name] = updated_records

            if not safe_json_save(path, data):
                self._handle_backup_error(
                    backup_type,
                    server_name,
                    "Could not save backup history; the destination is unavailable.",
                )
                return

        if server_name == self.backup_selected_server:
            selected_month = self._selected_month_prefix()
            if selected_month == current_month:
                self._populate_backup_table(self.backup_tables[backup_type], updated_records)

    def _handle_backup_error(self, backup_type, server_name, message):
        current_month = datetime.now().strftime("%Y-%m")
        path = self._backup_json_path(backup_type, current_month)
        with _BACKUP_FILE_LOCK:
            data = self._read_backup_file(path)
            data.setdefault(server_name, [])
            data.setdefault("_errors", {})[server_name] = {
                "message": message,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
            safe_json_save(path, data)
        self._set_status(f"Backup error for {server_name}: {message}")

    @staticmethod
    def _read_backup_file(path):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as file:
                    data = json.load(file) or {}
                    return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass
        return {}

    def _set_status(self, message):
        if self.status_callback is not None:
            self.status_callback(message)

    def start_hourly_refresh(self):
        self.hourly_timer.start()
        self.fetch_all_backup_data()

    def stop_refresh(self):
        self.hourly_timer.stop()