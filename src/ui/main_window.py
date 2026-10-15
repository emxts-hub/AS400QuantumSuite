# ui/main_window.py
import sys
import os
import json
import math
import re
import subprocess
import threading
import time
from typing import cast
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from config import APP_VERSION, APP_NAME

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt6.QtCore import Qt, QTimer, QThread, QThreadPool, QRunnable, QObject, QPointF, QPropertyAnimation, QAbstractAnimation, pyqtProperty, QSize, QByteArray, pyqtSignal, QEvent
from PyQt6.QtGui import QColor, QFont, QCursor, QIcon, QPixmap, QPainter, QPen, QPolygonF, QBrush, QMouseEvent
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QGroupBox, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QGridLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QApplication, QSizePolicy, QComboBox, QCheckBox, QStackedWidget, QPlainTextEdit, QTabWidget, QMessageBox,
    QDialog, QFormLayout,
)

import worker
from worker import (
    ObjectStatisticsRunnable,
    SingleLparRunnable,
    SystemDetailsRunnable,
    TemporaryStorageJobsRunnable,
    _new_connection,
    has_vpn_ip,
    reset_asp_alert_sound,
    stop_all_server_status_alerts,
    stop_asp_alert_sound,
)
from ui.log_viewer import LogViewerWidget
from ui.monthly_report import MonthlyReportWidget
from ui.backup_manage import BackupManagementWidget
from ui.widgets import RefreshStatusWidget, StatusBadgesWidget, SubsystemGridWidget
from dialogs import (
    ActiveJobsDialog, AppInfoDialog, FramelessDialog, LparSettingsDialog, ObjectStatisticsDialog,
    SubsystemDetailDialog, TestEmailThread, ThemeLoadingDialog, TopCpuJobsDialog,
    WebAppJobsDialog, ask_question_dialog, get_existing_directory, get_open_file_name,
    get_save_file_name, show_critical_dialog,
    show_information_dialog, show_warning_dialog,
)
from version_worker import VersionCheckWorker
from config import SERVER_CONFIGS, EXPECTED_SUBSYSTEMS, EXPECTED_PORTS, EXPECTED_WEB_APPS, ONEDRIVE_SHAREPOINT_PATH, get_resource_path, get_config_path
from ui.setcreds import (
    get_email_password,
    get_ibmi_password,
    load_email_alerts,
    load_login_credentials,
    load_settings_backup,
    save_all_configs,
    save_ibmi_password,
    save_settings_backup,
)
from ui.styles import DARK_STYLESHEET, LIGHT_STYLESHEET
from system_logging import (
    clear_system_logs as clear_persisted_system_logs,
    configure_system_logging,
)

CREDENTIAL_CHECK_TIMEOUT_SECONDS = 180
SYSTEM_LOGGER, SYSTEM_LOG_HANDLER = configure_system_logging()


def build_web_app_rows(active_jobs_detail, ports_data, configured_web_apps=None):
    """Build a summary of web apps by pairing active job names with their monitored ports."""
    jobs = active_jobs_detail or []
    ports = ports_data or []
    configured = configured_web_apps or []
    if not isinstance(jobs, list):
        jobs = []
    if not isinstance(ports, list):
        ports = []
    if not isinstance(configured, list):
        configured = []

    def _normalize_entry(entry):
        if isinstance(entry, dict):
            name = str(entry.get("job_name") or entry.get("name") or entry.get("job") or "").strip().upper()
            port_val = entry.get("port")
            if not name or port_val is None:
                return None
            try:
                port_num = int(port_val)
                if not 1 <= port_num <= 65535:
                    status = "INVALID"
                else:
                    status = "DOWN"
                return {"name": name, "port": port_num, "status": status}
            except (TypeError, ValueError):
                return None
        if isinstance(entry, str):
            item = entry.strip()
            if not item:
                return None
            if ":" in item:
                name_part, port_part = item.split(":", 1)
                try:
                    port_num = int(port_part.strip())
                    status = "DOWN" if 1 <= port_num <= 65535 else "INVALID"
                    return {"name": name_part.strip().upper(), "port": port_num, "status": status}
                except ValueError:
                    return None
        return None

    configured_rows = []
    for item in configured:
        parsed = _normalize_entry(item)
        if parsed:
            configured_rows.append(parsed)

    verified_ports = {}
    for item in ports:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("service") or "").strip().upper()
        try:
            port_num = int(item.get("port"))
        except (TypeError, ValueError):
            continue
        if name:
            verified_ports[(name, port_num)] = item

    for row in configured_rows:
        if row["status"] == "INVALID":
            continue
        port_entry = verified_ports.get((row["name"], row["port"]))
        row["status"] = "RUNNING" if port_entry and port_entry.get("is_up") is True else "DOWN"

    job_counts = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("job_name") or "").strip().upper()
        if not name:
            continue
        job_counts[name] = job_counts.get(name, 0) + 1

    port_by_name = {}
    for item in ports:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("service") or "").strip().upper()
        port_num = item.get("port")
        if name and port_num is not None:
            try:
                port_by_name[name] = int(port_num)
            except (TypeError, ValueError):
                pass

    rows_by_key = {}
    for row in configured_rows:
        rows_by_key[(row["name"], row["port"])] = row

    for app_name, count in sorted(job_counts.items()):
        port_num = port_by_name.get(app_name)
        port_entry = None
        for item in ports:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("service") or "").strip().upper()
            if name == app_name:
                port_entry = item
                port_num = item.get("port")
        if port_num is None:
            continue
        port_num = int(port_num)
        if not 1 <= port_num <= 65535:
            status = "INVALID"
        else:
            status = "RUNNING" if bool(port_entry.get("is_up", False) if isinstance(port_entry, dict) else False) else "DOWN"
        key = (app_name, port_num)
        rows_by_key[key] = {
            "name": app_name,
            "port": port_num,
            "count": max(int(rows_by_key.get(key, {}).get("count", 0)), int(count)),
            "status": status,
        }

    rows = sorted(rows_by_key.values(), key=lambda row: (row["name"], row["port"]))
    for row in rows:
        if "count" not in row:
            row["count"] = 1
    return rows


class CredentialCheckThread(QThread):
    results_ready = pyqtSignal(object)

    def __init__(self, server_configs, username, password, timeout_seconds=CREDENTIAL_CHECK_TIMEOUT_SECONDS):
        super().__init__()
        self.server_configs = dict(server_configs)
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _validate_single_server(server_name, cfg, username, password):
        try:
            host = cfg.get("host", "") if isinstance(cfg, dict) else str(cfg)
            db = cfg.get("db", "*LOCAL") if isinstance(cfg, dict) else "*LOCAL"
            worker.check_database_connection(host, db, username, password)
            return {"status": "OK", "message": "Credentials accepted."}
        except Exception as exc:
            return {"status": "AUTH_ERROR", "message": str(exc)}

    @staticmethod
    def validate_server_credentials(server_configs, username, password, timeout_seconds=CREDENTIAL_CHECK_TIMEOUT_SECONDS):
        results = {}
        server_items = list(dict(server_configs).items())
        if not server_items:
            return results

        deadline = time.monotonic() + timeout_seconds
        with ThreadPoolExecutor(max_workers=min(len(server_items), 8)) as executor:
            futures = {
                executor.submit(CredentialCheckThread._validate_single_server, server_name, cfg, username, password): server_name
                for server_name, cfg in server_items
            }
            for future in futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    server_name = futures[future]
                    results[server_name] = future.result(timeout=min(remaining, 5.0))
                except TimeoutError:
                    server_name = futures[future]
                    results[server_name] = {"status": "AUTH_ERROR", "message": f"Credential validation timed out after {timeout_seconds} seconds."}
        for server_name, _ in server_items:
            if server_name not in results:
                results[server_name] = {"status": "AUTH_ERROR", "message": f"Credential validation timed out after {timeout_seconds} seconds."}
        return results

    def run(self):
        self.results_ready.emit(
            self.validate_server_credentials(
                self.server_configs,
                self.username,
                self.password,
                timeout_seconds=self.timeout_seconds,
            )
        )


def metric_load_color(value):
    if value <= 50:
        return "#10B981"
    if value <= 88:
        return "#F59E0B"
    if value <= 100:
        return "#EF4444"
    if value <= 200:
        return "#991B1B"
    return "#D946EF"


class MetricSparklineWidget(QWidget):
    """Trend line for a single CPU or ASP metric."""

    def __init__(self, metric, max_points=100, parent=None):
        super().__init__(parent)
        self.metric = metric
        self.history = deque(maxlen=max_points)
        self.setFixedHeight(20)

        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True

    def add_value(self, value):
        self.history.append(float(value))
        self.update()

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        self.update()

    def _line_color(self, value):
        return QColor(metric_load_color(value))

    def paintEvent(self, event):
        if len(self.history) < 2:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        h = float(self.height())
        margin = 2.0
        max_val = max(100.0, max(self.history))
        step_x = (w - 2 * margin) / max(1, len(self.history) - 1)

        points = []
        for i, value in enumerate(self.history):
            x = margin + i * step_x
            y = (h - margin) - ((value / max_val) * (h - 2 * margin))
            points.append(QPointF(x, y))

        line_color = self._line_color(self.history[-1])

        painter.setPen(QPen(line_color, 1.2))
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])

        fill_color = QColor(line_color)
        fill_color.setAlpha(30)
        poly_points = [
            QPointF(points[0].x(), h),
            *points,
            QPointF(points[-1].x(), h),
        ]
        painter.setBrush(QBrush(fill_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF(poly_points))

class LinearGauge(QWidget):
    clicked = pyqtSignal()

    def __init__(self, title, initial_value=0.0, parent=None, decimals=1):
        super().__init__(parent)
        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        self.is_uncapped = False
        self.clickable = False
        self.decimals = int(decimals)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        self.title_label = QLabel(title)
        self.title_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.title_label.setStyleSheet("color: #8b949e; background-color: transparent;")

        zero_format = f"0.{self.decimals}f%" if self.decimals > 0 else "0%"
        self.val_label = QLabel(zero_format.replace("0.", "0." if False else "0."))
        self.val_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.val_label.setMinimumWidth(78)
        self.val_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        top_h = QHBoxLayout()
        top_h.addWidget(self.title_label)
        top_h.addStretch()
        top_h.addWidget(self.val_label)
        layout.addLayout(top_h)

        self.sparkline = MetricSparklineWidget(title)
        layout.addWidget(self.sparkline)

        for widget in (self, self.title_label, self.val_label):
            widget.installEventFilter(self)

        self.set_value(initial_value)

    def set_clickable(self, clickable, tooltip=None):
        self.clickable = bool(clickable)
        self.click_tooltip = tooltip or "Click to view the top CPU-consuming jobs"
        cursor = QCursor(Qt.CursorShape.PointingHandCursor if self.clickable else Qt.CursorShape.ArrowCursor)
        tooltip = self.click_tooltip if self.clickable else ""
        for widget in (self, self.title_label, self.val_label):
            widget.setCursor(cursor)
            widget.setToolTip(tooltip)

    def eventFilter(self, a0, a1):
        if (
            self.clickable
            and isinstance(a1, QMouseEvent)
            and a1.type() == QEvent.Type.MouseButtonRelease
            and a1.button() == Qt.MouseButton.LeftButton
        ):
            self.clicked.emit()
            return True
        return super().eventFilter(a0, a1)

    def set_value(self, val, is_uncapped=False):
        val_float = float(val)
        self.value = val_float
        self.is_uncapped = is_uncapped or val_float > 100.0

        display_value = f"{val_float:.{self.decimals}f}"
        if self.is_uncapped and val_float > 100.0:
            self.val_label.setText(f"{display_value}% ⚡")
            value_tooltip = "Uncapped CPU capacity in use (borrowing processing power)"
        else:
            self.val_label.setText(f"{display_value}%")
            value_tooltip = ""
        tooltips = [text for text in (self.click_tooltip if self.clickable else "", value_tooltip) if text]
        for widget in (self, self.title_label, self.val_label):
            widget.setToolTip("\n".join(tooltips))

        value_color = metric_load_color(val_float)
        self.val_label.setStyleSheet(
            f"color: {value_color}; background-color: transparent;"
        )

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        title_color = "#8b949e" if is_dark_theme else "#57606a"
        self.title_label.setStyleSheet(f"color: {title_color}; background-color: transparent;")
        self.set_value(self.value, self.is_uncapped)
        self.sparkline.set_theme(is_dark_theme)


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SystemDetailsDialog(FramelessDialog):
    DETAIL_FIELDS = (
        ("host_name", "Host name"),
        ("partition_name", "Partition"),
        ("machine_type", "Machine type"),
        ("machine_model", "Machine model"),
        ("machine_serial_number", "Serial number"),
        ("system_asp_storage", "System ASP storage"),
        ("current_cpu_capacity", "Current CPU capacity"),
        ("cpu_sharing_attribute", "CPU sharing"),
    )

    def __init__(self, server_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{server_name} - System Details")
        self.resize(830, 740)
        self.setMinimumSize(620, 560)

        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        background = "#1e1e1e" if is_dark else "#ffffff"
        header = "#252525" if is_dark else "#f3f4f6"
        surface = "#1e1e1e" if is_dark else "#ffffff"
        text = "#e1e3e6" if is_dark else "#1f2328"
        muted = "#a0a0a0" if is_dark else "#57606a"
        border = "#333333" if is_dark else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {background}; color: {text}; }}
            QFrame#systemDetailsContent {{ background-color: {surface}; }}
            QLabel#systemDetailsStatus {{
                color: {muted};
                font-size: 16.5pt;
                background: transparent;
            }}
            QLabel#systemDetailsFieldLabel {{
                color: {muted};
                font-size: 16.5pt;
                background: transparent;
            }}
            QLabel#systemDetailsFieldValue {{
                color: {text};
                font-size: 16.5pt;
                font-weight: 600;
                background: transparent;
            }}
            QFrame#systemDetailsFooter {{
                background-color: {surface};
                border-top: 1px solid {border};
            }}
            QPushButton#systemDetailsCloseButton {{
                background-color: {header};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 10px 26px;
                font-size: 14px;
                font-weight: 600;
            }}
            QPushButton#systemDetailsCloseButton:hover {{
                background-color: {border};
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        content = QFrame(self)
        content.setObjectName("systemDetailsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(36, 34, 36, 32)
        content_layout.setSpacing(22)

        self.status_label = QLabel(f"Loading system details for {server_name}...")
        self.status_label.setObjectName("systemDetailsStatus")
        self.status_label.setWordWrap(True)
        content_layout.addWidget(self.status_label)

        self.details_widget = QWidget(content)
        self.details_widget.setStyleSheet("background: transparent;")
        details_layout = QFormLayout(self.details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setHorizontalSpacing(42)
        details_layout.setVerticalSpacing(22)
        details_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        details_layout.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        self.value_labels = {}
        for field_name, field_label in self.DETAIL_FIELDS:
            value_label = QLabel("—")
            value_label.setObjectName("systemDetailsFieldValue")
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label = QLabel(f"{field_label}:")
            label.setObjectName("systemDetailsFieldLabel")
            self.value_labels[field_name] = value_label
            details_layout.addRow(label, value_label)
        self.details_widget.hide()
        content_layout.addWidget(self.details_widget)
        content_layout.addStretch(1)
        layout.addWidget(content, stretch=1)

        footer = QFrame(self)
        footer.setObjectName("systemDetailsFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(28, 18, 36, 18)
        footer_layout.addStretch(1)
        close_button = QPushButton("Close")
        close_button.setObjectName("systemDetailsCloseButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.setMinimumSize(126, 52)
        close_button.clicked.connect(self.accept)
        footer_layout.addWidget(close_button)
        layout.addWidget(footer)

    def showEvent(self, a0):
        super().showEvent(a0)
        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        title_text = "#e1e3e6" if is_dark else "#1f2328"
        muted = "#a0a0a0" if is_dark else "#57606a"
        close_hover_bg = "#3c1618" if is_dark else "#ffebe9"
        close_hover_text = "#f85149" if is_dark else "#cf222e"
        title_bar = self._dialog_title_bar
        if title_bar is not None:
            title_bar.setFixedHeight(96)
            title_label = self._dialog_title_label
            if title_label is not None:
                title_label.setStyleSheet(
                    f"color: {title_text}; background: transparent; border: none; "
                    "font-family: 'Segoe UI', sans-serif; font-size: 16.5pt; font-weight: 600;"
                )
            close_button = title_bar.findChild(QPushButton, "dialogCloseButton")
            if close_button is not None:
                close_button.setFixedSize(38, 38)
                close_button.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent;
                        color: {muted};
                        border: none;
                        border-radius: 6px;
                        padding: 0;
                        font-size: 28px;
                        font-weight: 400;
                    }}
                    QPushButton:hover {{
                        background-color: {close_hover_bg};
                        color: {close_hover_text};
                    }}
                """)

    @staticmethod
    def _display_value(value):
        if value is None or not str(value).strip():
            return "—"
        return str(value).strip()

    def set_result(self, result):
        error = str(result.get("error") or "").strip()
        if error:
            self.status_label.setText(f"Unable to load system details: {error}")
            self.status_label.setStyleSheet("color: #f85149;")
            return

        details = result.get("system_details") or {}
        for field_name, _field_label in self.DETAIL_FIELDS:
            value = details.get(field_name)
            if field_name == "system_asp_storage" and value is not None:
                try:
                    storage_mb = float(value)
                    storage_tb = (
                        storage_mb
                        / 1000
                        / 1024
                        * 9
                        / 8
                        * 1000000000
                        / 1073741824
                    )
                    value = f"{storage_mb:,.0f} MB ({storage_tb:.2f} TB)"
                except (TypeError, ValueError):
                    pass
            if field_name == "current_cpu_capacity" and value is not None:
                try:
                    value = f"{float(value):.2f} cores"
                except (TypeError, ValueError):
                    value = f"{self._display_value(value)} cores"
            self.value_labels[field_name].setText(self._display_value(value))

        self.status_label.setText("System details loaded")
        self.status_label.setStyleSheet("color: #2ea043;")
        self.details_widget.show()


class LparCardWidget(QFrame):
    server_name_clicked = pyqtSignal(str)

    def __init__(self, server_name, parent=None):
        super().__init__(parent)
        self.server_name = server_name
        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        self.current_status = "OFFLINE"
        self.current_is_critical = True
        self.current_is_warning = False
        self.current_cpu = 0.0
        self.current_asp = 0.0
        self.current_jobs = 0
        self.current_active_jobs_detail = []
        self.current_object_statistics = []
        self.current_object_statistics_error = ""
        self.current_object_statistics_loaded = False
        self.current_object_statistics_updated_at = ""
        self.current_storage_pools = []
        self.current_pool_threads = []
        self.current_storage_pools_loaded = False
        self.current_top_temporary_storage_jobs = []
        self.current_top_temporary_storage_jobs_error = ""
        self.current_top_temporary_storage_jobs_loaded = False
        self.current_web_app_jobs_detail = {}
        self.current_subsystems_data = []
        self.current_ports_data = []
        self.current_webapps_data = []
        self.current_web_app_ports = []
        self.config_key = server_name
        self.detail_expanded = True
        self.last_success_ts = None
        self.retry_count = 0
        self.sync_duration_ms = 0
        self.refresh_countdown_text = ""
        self.last_error_reason = ""
        self.stale_after_seconds = 90
        self._last_subsystem_signature = None
        self._last_ports_signature = None
        self._last_update_signature = None
        self._last_card_style_key = None
        self._last_status_badge_key = None
        self._alert_pulse = 0.0
        self._alert_animation = QPropertyAnimation(self, b"alertPulse", self)
        self._alert_animation.setDuration(900)
        self._alert_animation.setStartValue(0.0)
        self._alert_animation.setKeyValueAt(0.5, 1.0)
        self._alert_animation.setEndValue(0.0)
        self._alert_animation.setLoopCount(-1)
        self.active_jobs_dialog = None
        self.cpu_jobs_dialog = None
        self.object_statistics_dialog = None
        self.webapp_jobs_dialog = None
        
        self.setMinimumWidth(0)
        self.setFixedHeight(360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(16, 16, 16, 16)
        self.main_layout.setSpacing(6)
        self.section_dividers = []

        header_layout = QHBoxLayout()
        self.name_label = ClickableLabel(server_name)
        self.name_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.name_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.name_label.setToolTip("Click to view IBM i system details")
        self.name_label.clicked.connect(
            lambda: self.server_name_clicked.emit(self.config_key)
        )

        self.uncapped_badge = QLabel("UNCAPPED")
        self.uncapped_badge.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        self.uncapped_badge.setStyleSheet("""
            background-color: transparent;
            color: #6e40c9;
            border: none;
            padding: 0px;
        """)
        self.uncapped_badge.setToolTip("Partition configured with UNCAPPED CPU attribute")
        self.uncapped_badge.hide()

        self.status_badge = QLabel("ONLINE")
        self.status_badge.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.status_badge.setFixedWidth(82)
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        header_layout.addWidget(self.name_label)
        header_layout.addWidget(self.uncapped_badge)
        header_layout.addStretch()
        header_layout.addWidget(self.status_badge)
        self.main_layout.addLayout(header_layout)

        gauges_layout = QVBoxLayout()
        gauges_layout.setSpacing(2)
        self.cpu_gauge = LinearGauge("CPU", decimals=1)
        self.cpu_gauge.set_clickable(True)
        self.cpu_gauge.clicked.connect(self.show_top_cpu_jobs)
        self.asp_gauge = LinearGauge("ASP", decimals=2)
        self.asp_gauge.set_clickable(True, "Click to view the largest ASP objects")
        self.asp_gauge.clicked.connect(self.show_object_statistics)
        gauges_layout.addWidget(self.cpu_gauge)
        gauges_layout.addWidget(self.asp_gauge)
        self.main_layout.addLayout(gauges_layout)

        jobs_layout = QHBoxLayout()
        self.jobs_title_label = QLabel("Active Jobs")
        self.jobs_title_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.jobs_val_label = QPushButton("0")
        self.jobs_val_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.jobs_val_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.jobs_val_label.setToolTip("View active jobs")
        self.jobs_val_label.setFixedWidth(70)
        self.jobs_val_label.clicked.connect(self.show_active_jobs)
        
        jobs_layout.addWidget(self.jobs_title_label)
        jobs_layout.addStretch()
        jobs_layout.addWidget(self.jobs_val_label)
        self.main_layout.addLayout(jobs_layout)

        self.health_label = QLabel("Last success: -- | Retries: 0")
        self.health_label.setFont(QFont("Segoe UI", 6))
        self.health_label.setToolTip("Server health summary")
        self.health_label.setWordWrap(True)
        self.main_layout.addWidget(self.health_label)
        self.main_layout.addWidget(self._create_section_divider())

        self.subsystem_container = QWidget()
        self.subsys_layout = QVBoxLayout(self.subsystem_container)
        self.subsys_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.subsystem_container)
        self.main_layout.addWidget(self._create_section_divider())

        self.webapps_header_layout = QHBoxLayout()
        self.webapps_title_label = QLabel("Web Applications")
        self.webapps_title_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.webapps_header_layout.addWidget(self.webapps_title_label)
        self.webapps_header_layout.addStretch()
        self.webapps_count_label = QLabel("• 0 / 0 Running")
        self.webapps_count_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.webapps_header_layout.addWidget(self.webapps_count_label)
        self.webapps_expand_button = QPushButton("Expand ▼")
        self.webapps_expand_button.setFixedSize(70, 21)
        self.webapps_expand_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.webapps_expand_button.clicked.connect(self.show_web_apps)
        self.webapps_header_layout.addWidget(self.webapps_expand_button)
        self.main_layout.addLayout(self.webapps_header_layout)
        self.main_layout.addWidget(self._create_section_divider())

        self.network_title_label = QLabel("Network Services")
        self.network_title_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.main_layout.addWidget(self.network_title_label)

        self.ports_container = QWidget()
        self.ports_layout = QVBoxLayout(self.ports_container)
        self.ports_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.ports_container)

        self.set_theme(self.is_dark_theme)

    def _create_section_divider(self):
        divider = QFrame(self)
        divider.setFrameShape(QFrame.Shape.NoFrame)
        divider.setFixedHeight(1)
        self.section_dividers.append(divider)
        return divider

    def _clear_detail_widgets(self):
        for layout in (self.subsys_layout, self.ports_layout):
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.setParent(None)

    def _refresh_webapps_widget(self, active_jobs_detail, ports, configured_web_apps=None):
        web_apps = build_web_app_rows(active_jobs_detail, ports, configured_web_apps)
        if not self.detail_expanded:
            return

        total = len(web_apps)
        running = sum(1 for app in web_apps if app.get("status") == "RUNNING")
        self.webapps_count_label.setText(f"• {running} / {total} Running")
        self.webapps_count_label.setStyleSheet("color: #3fb950; background-color: transparent;")
        self.current_webapps_data = web_apps

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        self.cpu_gauge.set_theme(is_dark_theme)
        self.asp_gauge.set_theme(is_dark_theme)

        label_color = "#c9d1d9" if is_dark_theme else "#57606a"
        value_color = "#ffffff" if is_dark_theme else "#1f2328"
        for label in (self.name_label,):
            hover_color = "#58a6ff" if is_dark_theme else "#0969da"
            label.setStyleSheet(
                f"QLabel {{ color: {value_color}; background-color: transparent; }}"
                f"QLabel:hover {{ color: {hover_color}; }}"
            )

        self.jobs_val_label.setStyleSheet(
            f"QPushButton {{ color: {value_color}; background-color: transparent; border: none; }}"
            "QPushButton:hover { color: #58a6ff; }"
        )

        for label in (
            self.jobs_title_label,
            self.webapps_title_label,
            self.network_title_label,
            self.health_label,
        ):
            label.setStyleSheet(f"color: {label_color}; background-color: transparent;")
        self.webapps_expand_button.setStyleSheet(
            "QPushButton { background-color: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 2px 8px; }"
            "QPushButton:hover { border-color: #58a6ff; }"
        )
        divider_color = "#30363d" if is_dark_theme else "#e1e4e8"
        for divider in self.section_dividers:
            divider.setStyleSheet(
                f"QFrame {{ background-color: {divider_color}; border: none; }}"
            )
        self._last_ports_signature = None
        self._refresh_ports_widget(self.current_ports_data)

        self.name_label.setText(self.server_name)

        self._last_card_style_key = None
        self._last_status_badge_key = None
        self.set_card_style(is_critical=self.current_is_critical, is_warning=self.current_is_warning)
        self.set_status(self.current_status)

    def _get_alert_pulse(self):
        return self._alert_pulse

    def _set_alert_pulse(self, value):
        self._alert_pulse = float(value)
        self.set_card_style(
            is_critical=self.current_is_critical,
            is_warning=self.current_is_warning,
            force=True,
        )

    alertPulse = pyqtProperty(float, fget=_get_alert_pulse, fset=_set_alert_pulse)

    def _update_alert_animation(self):
        should_pulse = (self.current_is_critical or self.current_is_warning) and self.current_status in ("ONLINE", "DEGRADED")
        if should_pulse and self._alert_animation.state() != QAbstractAnimation.State.Running:
            self._alert_animation.start()
        elif not should_pulse and self._alert_animation.state() != QAbstractAnimation.State.Stopped:
            self._alert_animation.stop()
            self._set_alert_pulse(0.0)

    def open_subsystem_modal(self, server_name):
        dialog = SubsystemDetailDialog(
            server_name=self.server_name, 
            subsystem_data=self.current_subsystems_data, 
            expected_key=self.config_key,
            parent=self
        )
        dialog.show_centered()

    def show_active_jobs(self):
        if self.active_jobs_dialog is None:
            self.active_jobs_dialog = ActiveJobsDialog(
                self.server_name,
                self.current_active_jobs_detail,
                parent=self,
            )
            self.active_jobs_dialog.finished.connect(self._clear_active_jobs_dialog)
        self.active_jobs_dialog.update_jobs(self.current_active_jobs_detail)
        self.active_jobs_dialog.show()
        self.active_jobs_dialog.raise_()
        self.active_jobs_dialog.activateWindow()

    def show_top_cpu_jobs(self):
        if self.cpu_jobs_dialog is None:
            self.cpu_jobs_dialog = TopCpuJobsDialog(
                self.server_name,
                self.current_active_jobs_detail,
                parent=self,
            )
            self.cpu_jobs_dialog.finished.connect(self._clear_cpu_jobs_dialog)
        self.cpu_jobs_dialog.update_jobs(self.current_active_jobs_detail)
        self.cpu_jobs_dialog.show()
        self.cpu_jobs_dialog.raise_()
        self.cpu_jobs_dialog.activateWindow()

    def show_object_statistics(self):
        if self.object_statistics_dialog is None:
            self.object_statistics_dialog = ObjectStatisticsDialog(
                self.server_name,
                rows=self.current_object_statistics,
                error=self.current_object_statistics_error,
                loaded=self.current_object_statistics_loaded,
                updated_at=self.current_object_statistics_updated_at,
                storage_pools=self.current_storage_pools,
                pool_threads=self.current_pool_threads,
                pool_data_loaded=self.current_storage_pools_loaded,
                top_temporary_storage_jobs=self.current_top_temporary_storage_jobs,
                top_temporary_storage_jobs_error=self.current_top_temporary_storage_jobs_error,
                top_temporary_storage_jobs_loaded=self.current_top_temporary_storage_jobs_loaded,
                parent=self,
            )
            self.object_statistics_dialog.finished.connect(self._clear_object_statistics_dialog)
        self.object_statistics_dialog.update_data(
            self.current_object_statistics,
            self.current_object_statistics_error,
            self.current_object_statistics_loaded,
            self.current_object_statistics_updated_at,
            self.current_storage_pools,
            self.current_pool_threads,
            pool_data_loaded=self.current_storage_pools_loaded,
        )
        self.object_statistics_dialog.update_temporary_storage_jobs(
            self.current_top_temporary_storage_jobs,
            self.current_top_temporary_storage_jobs_error,
            self.current_top_temporary_storage_jobs_loaded,
        )
        self.object_statistics_dialog.show()
        self.object_statistics_dialog.raise_()
        self.object_statistics_dialog.activateWindow()

    def set_object_statistics(self, rows, error="", loaded=False, updated_at=""):
        if loaded or rows:
            self.current_object_statistics = rows if isinstance(rows, list) else []
        self.current_object_statistics_error = error
        self.current_object_statistics_loaded = loaded or bool(self.current_object_statistics)
        if updated_at:
            self.current_object_statistics_updated_at = updated_at
        if self.object_statistics_dialog is not None:
            self.object_statistics_dialog.update_data(
                self.current_object_statistics,
                self.current_object_statistics_error,
                self.current_object_statistics_loaded,
                self.current_object_statistics_updated_at,
                self.current_storage_pools,
                self.current_pool_threads,
                pool_data_loaded=self.current_storage_pools_loaded,
            )

    def set_top_temporary_storage_jobs(self, jobs, error="", loaded=True):
        self.current_top_temporary_storage_jobs = jobs if isinstance(jobs, list) else []
        self.current_top_temporary_storage_jobs_error = error
        self.current_top_temporary_storage_jobs_loaded = loaded
        if self.object_statistics_dialog is not None:
            self.object_statistics_dialog.update_temporary_storage_jobs(
                self.current_top_temporary_storage_jobs,
                self.current_top_temporary_storage_jobs_error,
                self.current_top_temporary_storage_jobs_loaded,
            )

    def _web_app_jobs_for_display(self):
        jobs_by_app = {
            str(app_name).strip().upper(): [job for job in jobs if isinstance(job, dict)]
            for app_name, jobs in (self.current_web_app_jobs_detail or {}).items()
            if isinstance(jobs, list)
        }

        for app in self.current_webapps_data or []:
            if not isinstance(app, dict):
                continue
            app_name = str(app.get("name") or "").strip().upper()
            if not app_name:
                continue
            app_jobs = jobs_by_app.setdefault(app_name, [])
            known_names = {
                str(job.get("job_name") or "").strip().upper()
                for job in app_jobs
            }
            for job in self.current_active_jobs_detail or []:
                if not isinstance(job, dict):
                    continue
                job_name = str(job.get("job_name") or "").strip()
                if job_name.rsplit("/", 1)[-1].upper() != app_name:
                    continue
                if job_name.upper() not in known_names:
                    app_jobs.append(job)
                    known_names.add(job_name.upper())

        return jobs_by_app

    def show_web_apps(self):
        jobs_by_app = self._web_app_jobs_for_display()
        if self.webapp_jobs_dialog is None:
            self.webapp_jobs_dialog = WebAppJobsDialog(
                self.server_name,
                self.current_webapps_data,
                jobs_by_app,
                parent=self,
            )
            self.webapp_jobs_dialog.finished.connect(self._clear_webapp_jobs_dialog)
        else:
            self.webapp_jobs_dialog.update_data(
                self.current_webapps_data,
                jobs_by_app,
            )
        self.webapp_jobs_dialog.show()
        self.webapp_jobs_dialog.raise_()
        self.webapp_jobs_dialog.activateWindow()

    def _clear_webapp_jobs_dialog(self):
        self.webapp_jobs_dialog = None

    def _clear_active_jobs_dialog(self):
        self.active_jobs_dialog = None

    def _clear_cpu_jobs_dialog(self):
        self.cpu_jobs_dialog = None

    def _clear_object_statistics_dialog(self):
        self.object_statistics_dialog = None

    def set_card_style(self, is_critical=False, is_warning=False, force=False):
        key = (self.is_dark_theme, bool(is_critical), bool(is_warning))
        if self._last_card_style_key == key and not force:
            return
        self._last_card_style_key = key

        if not self.is_dark_theme:
            if is_critical:
                border_color = self._alert_border_color("#d0d7de", "#cf222e")
            elif is_warning:
                border_color = self._alert_border_color("#d0d7de", "#bf8700")
            else:
                border_color = "#d0d7de"
            self.setStyleSheet(f"""
                LparCardWidget {{
                    background-color: #ffffff;
                    border: 2px solid {border_color};
                    border-radius: 10px;
                }}
                LparCardWidget QWidget {{
                    background-color: transparent;
                }}
                QLabel {{
                    background-color: transparent;
                }}
            """)
            return

        if is_critical:
            border_color = self._alert_border_color("#34383d", "#f85149")
            self.setStyleSheet("""
                LparCardWidget {
                    background-color: #202225;
                    border: 2px solid %s;
                    border-radius: 10px;
                }
                LparCardWidget QWidget {
                    background-color: transparent;
                }
                QLabel {
                    background-color: transparent;
                }
            """ % border_color)
        elif is_warning:
            border_color = self._alert_border_color("#34383d", "#e3b341")
            self.setStyleSheet("""
                LparCardWidget {
                    background-color: #202225;
                    border: 2px solid %s;
                    border-radius: 10px;
                }
                LparCardWidget QWidget {
                    background-color: transparent;
                }
                QLabel {
                    background-color: transparent;
                }
            """ % border_color)
        else:
            self.setStyleSheet("""
                LparCardWidget {
                    background-color: #202225;
                    border: 2px solid #34383d;
                    border-radius: 10px;
                }
                LparCardWidget QWidget {
                    background-color: transparent;
                }
                QLabel {
                    background-color: transparent;
                }
            """)

    def _alert_border_color(self, base_color, alert_color):
        base = QColor(base_color)
        alert = QColor(alert_color)
        pulse = self._alert_pulse
        red = round(base.red() + (alert.red() - base.red()) * pulse)
        green = round(base.green() + (alert.green() - base.green()) * pulse)
        blue = round(base.blue() + (alert.blue() - base.blue()) * pulse)
        return QColor(red, green, blue).name()

    def _sync_health_summary(self):
        last_success_text = f"Last success: {self.last_success_ts}" if self.last_success_ts else "Last success: never"
        retry_text = f" | Retries: {self.retry_count}"
        sync_text = f" | Sync: {self.sync_duration_ms}ms" if self.sync_duration_ms else ""
        refresh_text = f" | Next refresh: {self.refresh_countdown_text}" if self.refresh_countdown_text else ""
        reason_text = f" | {self.last_error_reason}" if self.last_error_reason else ""
        summary = f"{last_success_text}{retry_text}{sync_text}{refresh_text}{reason_text}"
        if len(summary) > 120:
            summary = summary[:117] + "..."
        self.health_label.setText(summary)

    def set_refresh_countdown(self, seconds=None, refreshing=False):
        if refreshing:
            self.refresh_countdown_text = "refreshing"
        elif seconds is None:
            self.refresh_countdown_text = ""
        else:
            self.refresh_countdown_text = f"{max(0, int(seconds))}s"
        self._sync_health_summary()

    def set_status(self, status):
        self.current_status = status
        self.set_card_style(
            is_critical=self.current_is_critical and status not in ("CONNECTING", "SYNCING"),
            is_warning=self.current_is_warning and status not in ("CONNECTING", "SYNCING"),
        )
        self._update_alert_animation()
        self._sync_health_summary()

        if status == "CONNECTING":
            key = (status, self.is_dark_theme)
            if self._last_status_badge_key == key:
                return
            self._last_status_badge_key = key
            connecting_color = "#58a6ff" if self.is_dark_theme else "#0969da"
            self.status_badge.setText("CONNECTING ●")
            self.status_badge.setStyleSheet(
                f"color: {connecting_color}; font-weight: bold; background-color: transparent;"
            )
        elif status == "SYNCING":
            key = (status, self.is_dark_theme)
            if self._last_status_badge_key == key:
                return
            self._last_status_badge_key = key
            sync_color = "#58a6ff" if self.is_dark_theme else "#0969da"
            self.status_badge.setText("SYNCING ●")
            self.status_badge.setStyleSheet(
                f"color: {sync_color}; font-weight: bold; background-color: transparent;"
            )
        elif status == "STALE":
            key = (status, self.is_dark_theme)
            if self._last_status_badge_key == key:
                return
            self._last_status_badge_key = key
            self.status_badge.setText("STALE ●")
            self.status_badge.setStyleSheet(
                "color: #e3b341; font-weight: bold; background-color: transparent;"
            )
        elif status in ("AUTH_ERROR", "OFFLINE"):
            key = (status, self.is_dark_theme)
            if self._last_status_badge_key == key:
                return
            self._last_status_badge_key = key
            self.status_badge.setText(f"{status} ●")
            self.status_badge.setStyleSheet(
                "color: #f85149; font-weight: bold; background-color: transparent;"
            )
        elif status == "STOPPED":
            key = (status, self.is_dark_theme)
            if self._last_status_badge_key == key:
                return
            self._last_status_badge_key = key
            self.status_badge.setText("STOPPED ●")
            stopped_color = "#8b949e" if self.is_dark_theme else "#57606a"
            self.status_badge.setStyleSheet(
                f"color: {stopped_color}; font-weight: bold; background-color: transparent;"
            )

    def _refresh_subsystem_widget(self, subsystems):
        if not self.detail_expanded:
            return
        subsystem_signature = repr(subsystems)
        if getattr(self, "_last_subsystem_signature", None) == subsystem_signature:
            return
        self._last_subsystem_signature = subsystem_signature

        for i in reversed(range(self.subsys_layout.count())):
            layout_item = self.subsys_layout.itemAt(i)
            w = layout_item.widget() if layout_item is not None else None
            if w:
                w.setParent(None)

        if not subsystems:
            no_subs_lbl = QLabel("No subsystem data")
            no_subs_lbl.setFont(QFont("Segoe UI", 8))
            no_subs_lbl.setStyleSheet("color: #6e7681; font-style: italic; background-color: transparent;")
            self.subsys_layout.addWidget(no_subs_lbl)
            return

        sub_widget = SubsystemGridWidget(
            server_name=self.server_name,
            active_subsystems=subsystems,
            expected_key=self.config_key,
            on_expand_callback=self.open_subsystem_modal,
            parent=self
        )
        self.subsys_layout.addWidget(sub_widget)

    def _refresh_ports_widget(self, ports):
        if not self.detail_expanded:
            return
        port_signature = repr(ports)
        if getattr(self, "_last_ports_signature", None) == port_signature:
            return
        self._last_ports_signature = port_signature

        for i in reversed(range(self.ports_layout.count())):
            layout_item = self.ports_layout.itemAt(i)
            w = layout_item.widget() if layout_item is not None else None
            if w:
                w.setParent(None)

        if not ports:
            no_ports_lbl = QLabel("No monitored services")
            no_ports_lbl.setFont(QFont("Segoe UI", 8))
            no_ports_lbl.setStyleSheet("color: #6e7681; font-style: italic; background-color: transparent;")
            self.ports_layout.addWidget(no_ports_lbl)
            return

        badges_widget = StatusBadgesWidget(ports)
        self.ports_layout.addWidget(badges_widget)

    def update_data(self, data):
        display_name = str(data.get("host_name") or data.get("server") or self.server_name).strip()
        if display_name and display_name != self.server_name:
            self.server_name = display_name
            if hasattr(self, "name_label"):
                self.name_label.setText(display_name)

        status = str(data.get("status", "OFFLINE")).upper()
        error_reason = str(data.get("error") or "")
        completed_at = str(data.get("completed_at") or "").strip()
        cpu = float(data.get("cpu", 0.0))
        asp = float(data.get("asp", 0.0))
        jobs = int(data.get("jobs", 0))
        active_jobs_detail = data.get("active_jobs_detail", [])
        object_statistics = data.get("object_statistics", self.current_object_statistics)
        object_statistics_error = str(
            data.get("object_statistics_error", self.current_object_statistics_error) or ""
        )
        object_statistics_loaded = bool(data.get("object_statistics_loaded", self.current_object_statistics_loaded))
        object_statistics_updated_at = str(
            data.get("object_statistics_updated_at", self.current_object_statistics_updated_at) or ""
        )
        storage_pools = data.get("storage_pools", self.current_storage_pools)
        pool_threads = data.get("pool_threads", self.current_pool_threads)
        storage_pools_loaded = self.current_storage_pools_loaded
        web_app_jobs_detail = data.get("web_app_jobs_detail", {})
        web_app_ports = data.get("web_app_ports", [])
        subsystems = data.get("subsystems", [])
        ports = data.get("ports", [])

        if status in ("OFFLINE", "AUTH_ERROR", "STALE") and self.current_status not in ("OFFLINE", "AUTH_ERROR", "STALE"):
            last_cpu = float(self.current_cpu) if self.current_cpu else cpu
            last_asp = float(self.current_asp) if self.current_asp else asp
            last_jobs = int(self.current_jobs) if hasattr(self, "current_jobs") and self.current_jobs else jobs
            cpu = last_cpu
            asp = last_asp
            jobs = last_jobs
            active_jobs_detail = self.current_active_jobs_detail
            object_statistics = self.current_object_statistics
            object_statistics_error = self.current_object_statistics_error
            object_statistics_loaded = self.current_object_statistics_loaded
            object_statistics_updated_at = self.current_object_statistics_updated_at
            storage_pools = self.current_storage_pools
            pool_threads = self.current_pool_threads
            storage_pools_loaded = self.current_storage_pools_loaded
            web_app_jobs_detail = self.current_web_app_jobs_detail
            web_app_ports = self.current_web_app_ports
        elif status in ("ONLINE", "DEGRADED"):
            if "storage_pools" in data or "pool_threads" in data:
                storage_pools_loaded = True
            if completed_at:
                self.last_success_ts = completed_at
            elif self.last_success_ts is None or self.current_status not in ("ONLINE", "DEGRADED"):
                self.last_success_ts = time.strftime("%H:%M:%S")
            self.last_error_reason = ""

        self.retry_count = int(data.get("retry_count", getattr(self, "retry_count", 0)))
        self.last_error_reason = error_reason if status in ("OFFLINE", "AUTH_ERROR", "STALE") else ""
        self.sync_duration_ms = int(data.get("sync_duration_ms", getattr(self, "sync_duration_ms", 0)))

        cpu_sharing = str(data.get("cpu_sharing_attribute", "")).upper()
        is_uncapped = "UNCAPPED" in cpu_sharing or cpu > 100.0
        alert_cfg = load_email_alerts()
        configured_threshold = float(alert_cfg.get("threshold_percent", 40.0) or 40.0)
        warning_threshold = float(
            alert_cfg.get("warning_threshold")
            if alert_cfg.get("warning_threshold") is not None
            else configured_threshold * 0.98
        )
        critical_threshold = float(alert_cfg.get("critical_threshold", configured_threshold) or configured_threshold)
        if critical_threshold < warning_threshold:
            warning_threshold, critical_threshold = critical_threshold, warning_threshold
        is_critical = asp >= critical_threshold
        is_warning = warning_threshold <= asp < critical_threshold

        signature = (
            status,
            cpu,
            asp,
            int(jobs),
            repr(active_jobs_detail),
            repr(object_statistics),
            object_statistics_error,
            object_statistics_loaded,
            object_statistics_updated_at,
            repr(storage_pools),
            repr(pool_threads),
            repr(web_app_jobs_detail),
            repr(web_app_ports),
            repr(subsystems),
            repr(ports),
            is_uncapped,
            is_critical,
            is_warning,
            warning_threshold,
            critical_threshold,
            self.last_error_reason,
            self.retry_count,
            self.sync_duration_ms,
        )
        if self._last_update_signature == signature:
            return
        self._last_update_signature = signature

        self.current_status = status
        self.config_key = str(data.get("config_key") or self.config_key).strip()
        self.current_cpu = cpu
        self.current_asp = asp
        self.current_jobs = jobs
        self.current_active_jobs_detail = active_jobs_detail if isinstance(active_jobs_detail, list) else []
        self.current_object_statistics = object_statistics if isinstance(object_statistics, list) else []
        self.current_object_statistics_error = object_statistics_error
        self.current_object_statistics_loaded = object_statistics_loaded
        self.current_object_statistics_updated_at = object_statistics_updated_at
        self.current_storage_pools = storage_pools if isinstance(storage_pools, list) else []
        self.current_pool_threads = pool_threads if isinstance(pool_threads, list) else []
        self.current_storage_pools_loaded = storage_pools_loaded
        self.current_web_app_jobs_detail = web_app_jobs_detail if isinstance(web_app_jobs_detail, dict) else {}
        self.current_web_app_ports = web_app_ports if isinstance(web_app_ports, list) else []
        self.current_subsystems_data = subsystems
        self.current_ports_data = ports

        if is_uncapped:
            self.uncapped_badge.show()
        else:
            self.uncapped_badge.hide()

        self.current_is_critical = is_critical
        self.current_is_warning = is_warning
        self.set_card_style(is_critical=is_critical, is_warning=is_warning)
        self._update_alert_animation()

        label_key = (status, is_critical, is_warning, self.is_dark_theme)
        if self._last_status_badge_key != label_key:
            self._last_status_badge_key = label_key
            if status in ("ONLINE", "DEGRADED"):
                if is_critical:
                    self.status_badge.setText("CRITICAL ●")
                    self.status_badge.setStyleSheet(
                        "color: #f85149; font-weight: bold; background-color: transparent;"
                    )
                elif is_warning:
                    self.status_badge.setText("WARNING ●")
                    self.status_badge.setStyleSheet(
                        "color: #e3b341; font-weight: bold; background-color: transparent;"
                    )
                elif status == "DEGRADED":
                    self.status_badge.setText("DEGRADED ●")
                    self.status_badge.setStyleSheet(
                        "color: #e3b341; font-weight: bold; background-color: transparent;"
                    )
                else:
                    self.status_badge.setText("ONLINE ●")
                    self.status_badge.setStyleSheet(
                        "color: #3fb950; font-weight: bold; background-color: transparent;"
                    )
            elif status == "SYNCING":
                self.status_badge.setText("SYNCING ●")
                sync_color = "#58a6ff" if self.is_dark_theme else "#0969da"
                self.status_badge.setStyleSheet(
                    f"color: {sync_color}; font-weight: bold; background-color: transparent;"
                )
            elif status == "STALE":
                self.status_badge.setText("STALE ●")
                self.status_badge.setStyleSheet(
                    "color: #e3b341; font-weight: bold; background-color: transparent;"
                )
            else:
                self.status_badge.setText(f"{status} ●")
                status_color = "#f85149" if status in ("AUTH_ERROR", "OFFLINE") else "#8b949e"
                self.status_badge.setStyleSheet(
                    f"color: {status_color}; font-weight: bold; background-color: transparent;"
                )

        if not math.isclose(self.cpu_gauge.value, cpu, rel_tol=0.0, abs_tol=1e-9):
            self.cpu_gauge.set_value(cpu, is_uncapped=is_uncapped)
        if not math.isclose(self.asp_gauge.value, asp, rel_tol=0.0, abs_tol=1e-9):
            self.asp_gauge.set_value(asp)
        self.cpu_gauge.sparkline.add_value(cpu)
        self.asp_gauge.sparkline.add_value(asp)

        jobs_text = f"{jobs:,}"
        if self.jobs_val_label.text() != jobs_text:
            self.jobs_val_label.setText(jobs_text)
        if self.active_jobs_dialog is not None:
            self.active_jobs_dialog.update_jobs(self.current_active_jobs_detail)
        if self.cpu_jobs_dialog is not None:
            self.cpu_jobs_dialog.update_jobs(self.current_active_jobs_detail)
        if self.object_statistics_dialog is not None:
            self.object_statistics_dialog.update_data(
                self.current_object_statistics,
                self.current_object_statistics_error,
                self.current_object_statistics_loaded,
                self.current_object_statistics_updated_at,
                self.current_storage_pools,
                self.current_pool_threads,
                pool_data_loaded=self.current_storage_pools_loaded,
            )
        if self.webapp_jobs_dialog is not None:
            self.webapp_jobs_dialog.update_data(
                self.current_webapps_data,
                self._web_app_jobs_for_display(),
            )

        self._sync_health_summary()
        self._refresh_subsystem_widget(self.current_subsystems_data)
        self._refresh_webapps_widget(
            self.current_active_jobs_detail,
            self.current_ports_data + self.current_web_app_ports,
            EXPECTED_WEB_APPS.get(str(self.config_key or self.server_name).strip().upper(), []),
        )
        self._refresh_ports_widget(ports)


class GlobalAlertsWidget(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Global Alerts and Status", parent)
        self.overloaded_servers = []
        self.active_lpars = 0
        self.configured_lpars = 0
        self._last_summary_signature = None
        
        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)

        m1_layout = QHBoxLayout()
        m1_layout.setSpacing(8)
        m1_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.down_count_lbl = QLabel("0")
        self.down_count_lbl.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))

        self.down_desc = QLabel("Total Services\nDown")
        self.down_desc.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.down_desc.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        m1_layout.addWidget(self.down_count_lbl)
        m1_layout.addWidget(self.down_desc)

        m2_layout = QHBoxLayout()
        m2_layout.setSpacing(8)
        m2_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.overload_count_lbl = QLabel("0")
        self.overload_count_lbl.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))

        self.overload_desc = QLabel("Server Overloaded")
        self.overload_desc.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.overload_desc.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        m2_layout.addWidget(self.overload_count_lbl)
        m2_layout.addWidget(self.overload_desc)

        m3_layout = QHBoxLayout()
        m3_layout.setSpacing(8)
        m3_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stale_count_lbl = QLabel("0")
        self.stale_count_lbl.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))

        self.stale_desc = QLabel("Stale\nServers")
        self.stale_desc.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.stale_desc.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        m3_layout.addWidget(self.stale_count_lbl)
        m3_layout.addWidget(self.stale_desc)

        m4_layout = QHBoxLayout()
        m4_layout.setSpacing(8)
        m4_layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.sub_count_lbl = QLabel("(0/0)")
        self.sub_count_lbl.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))

        self.sub_status_desc = QLabel("Servers Active")
        self.sub_status_desc.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.sub_status_desc.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        m4_layout.addWidget(self.sub_count_lbl)
        m4_layout.addWidget(self.sub_status_desc)

        layout.addLayout(m1_layout)
        layout.addStretch(1)
        layout.addLayout(m2_layout)
        layout.addStretch(1)
        layout.addLayout(m3_layout)
        layout.addStretch(1)
        layout.addLayout(m4_layout)

        self.set_theme(self.is_dark_theme)

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        if is_dark_theme:
            self.setStyleSheet("""
                QGroupBox {
                    font-weight: bold;
                    color: #8b949e;
                    border: 1px solid #30363d;
                    border-radius: 6px;
                    margin-top: 6px;
                    background-color: #161b22;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                    color: #8b949e;
                }
            """)
            red_color = "#f85149"
            yellow_color = "#e3b341"
            green_color = "#3fb950"
        else:
            self.setStyleSheet("""
                QGroupBox {
                    font-weight: bold;
                    color: #57606a;
                    border: 1px solid #d0d7de;
                    border-radius: 6px;
                    margin-top: 6px;
                    background-color: #ffffff;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                    color: #57606a;
                }
            """)
            red_color = "#cf222e"
            yellow_color = "#d97706"
            green_color = "#1a7f37"

        self.down_count_lbl.setStyleSheet(f"color: {red_color}; background-color: transparent;")
        self.down_desc.setStyleSheet(f"color: {red_color}; background-color: transparent;")

        if self.overloaded_servers:
            self.overload_count_lbl.setStyleSheet(f"color: {red_color}; background-color: transparent;")
            self.overload_desc.setStyleSheet(f"color: {red_color}; background-color: transparent;")
        else:
            self.overload_count_lbl.setStyleSheet(f"color: {yellow_color}; background-color: transparent;")
            self.overload_desc.setStyleSheet(f"color: {yellow_color}; background-color: transparent;")

        self.stale_count_lbl.setStyleSheet(f"color: {yellow_color}; background-color: transparent;")
        self.stale_desc.setStyleSheet(f"color: {yellow_color}; background-color: transparent;")

        if self.active_lpars == self.configured_lpars and self.configured_lpars > 0:
            self.sub_count_lbl.setStyleSheet(f"color: {green_color}; background-color: transparent;")
            self.sub_status_desc.setStyleSheet(f"color: {green_color}; background-color: transparent;")
        else:
            self.sub_count_lbl.setStyleSheet(f"color: {red_color}; background-color: transparent;")
            self.sub_status_desc.setStyleSheet(f"color: {red_color}; background-color: transparent;")

    def set_stale_count(self, count):
        self.stale_count_lbl.setText(str(count))
        self.set_theme(self.is_dark_theme)

    def update_summary(self, data_list, total_lpars=None):
        total_down_services = 0
        overloaded_servers = []
        active_lpars = 0
        listed_lpars = len(data_list)
        configured_lpars = total_lpars if total_lpars is not None else listed_lpars
        threshold_percent = float(load_email_alerts().get("threshold_percent", 40.0) or 40.0)

        for sys_info in data_list:
            status = str(sys_info.get("status", "")).upper()
            if status not in ("ONLINE", "DEGRADED", "OFFLINE", "AUTH_ERROR", "STALE"):
                status = "OFFLINE"

            if status in ("ONLINE", "DEGRADED"):
                active_lpars += 1

            ports = sys_info.get("ports", [])
            down_ports = [p for p in ports if isinstance(p, dict) and p.get("is_up") is False]
            total_down_services += len(down_ports)

            if float(sys_info.get("asp", 0.0) or 0.0) >= threshold_percent and status in ("ONLINE", "DEGRADED"):
                overloaded_servers.append(str(sys_info.get("server") or sys_info.get("host_name") or ""))

        signature = (
            total_down_services,
            tuple(sorted(overloaded_servers)),
            active_lpars,
            configured_lpars,
            threshold_percent,
            tuple(sorted((sys_info.get("server", ""), sys_info.get("status", ""), round(float(sys_info.get("asp", 0.0)), 1), round(float(sys_info.get("cpu", 0.0)), 1)) for sys_info in data_list))
        )
        if self._last_summary_signature == signature:
            return
        self._last_summary_signature = signature

        self.overloaded_servers = overloaded_servers
        self.active_lpars = active_lpars
        self.configured_lpars = configured_lpars

        self.down_count_lbl.setText(str(total_down_services))

        if self.overloaded_servers:
            srv_str = ", ".join(self.overloaded_servers)
            self.overload_count_lbl.setText(str(len(self.overloaded_servers)))
            self.overload_desc.setText(f"Server Overloaded\n({srv_str})")
        else:
            self.overload_count_lbl.setText("0")
            self.overload_desc.setText("Server Overloaded")

        self.sub_count_lbl.setText(f"({self.active_lpars}/{self.configured_lpars})")
        self.sub_status_desc.setText("Servers Active")

        self.set_theme(self.is_dark_theme)


def resource_path(relative_path):
    return get_resource_path(relative_path)


PING_TARGET_IP = worker.VPN_PING_TARGET_IP
GATEWAY_PING_INTERVAL_MS = 5000
GATEWAY_PING_MAX_AGE_SECONDS = 10
GATEWAY_PING_MAX_ATTEMPTS = 3
_ACTIVE_GATEWAY_PING_RUNNABLES = set()


class GatewayPingSignals(QObject):
    result_ready = pyqtSignal(object)


class GatewayPingRunnable(QRunnable):
    def __init__(self, measure_ping):
        super().__init__()
        self.measure_ping = measure_ping
        self.signals = GatewayPingSignals()

    def run(self):
        try:
            latency_ms = self.measure_ping(PING_TARGET_IP)
        except Exception:
            latency_ms = None
        try:
            self.signals.result_ready.emit(latency_ms)
        finally:
            _ACTIVE_GATEWAY_PING_RUNNABLES.discard(self)


class IBMiDashboard(QMainWindow):
    _version_worker: VersionCheckWorker | None

    def __init__(self, version_str: str = APP_VERSION):
        super().__init__()
        self.system_log_handler = SYSTEM_LOG_HANDLER
        self._version_worker = None
        self.version_str = version_str
        self.setWindowTitle(f"{APP_NAME} - v{version_str}")
        self.setGeometry(100, 100, 900, 600)

        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        self._theme_loading_dialog = None

        self.setWindowIcon(QIcon(resource_path("logo.png")))
        self.resize(1750, 950)

        self.is_monitoring = False
        self.credentials_validated = False
        self.card_widgets = {}
        self.active_server_configs = dict(SERVER_CONFIGS)
        self.latest_results_cache = {}
        self.refresh_generation = 0
        self.active_runnables = set()
        self._log_history_refresh_timer = QTimer(self)
        self._log_history_refresh_timer.setSingleShot(True)
        self._log_history_refresh_timer.setInterval(750)
        self._log_history_refresh_timer.timeout.connect(self._refresh_log_history_after_results)
        self.auto_refresh_paused = False
        self._last_gateway_latency_ms = None
        self._last_gateway_ping_at = 0.0
        self._gateway_ping_started_at = 0.0
        self._gateway_ping_pending = False
        self._gateway_ping_runnable = None
        self._gateway_ping_attempts = 0
        self._pending_monitoring_start = False
        self._vpn_disconnect_alerted = False
        self.last_refresh_success_at = None
        self.last_refresh_started_at = None
        self._last_global_summary_signature = None
        self._last_card_layout_signature = None

        self.thread_pool = cast(QThreadPool, QThreadPool.globalInstance())
        self.thread_pool.setMaxThreadCount(8)
        self.gateway_ping_thread_pool = QThreadPool(self)
        self.gateway_ping_thread_pool.setMaxThreadCount(1)
        self.gateway_ping_timer = QTimer(self)
        self.gateway_ping_timer.setInterval(GATEWAY_PING_INTERVAL_MS)
        self.gateway_ping_timer.timeout.connect(self._check_monitoring_gateway)
        self.object_statistics_thread_pool = QThreadPool(self)
        self.object_statistics_thread_pool.setMaxThreadCount(2)
        self.object_statistics_runnables = {}
        self.object_statistics_next_fetch = {}
        self.system_details_dialogs = {}
        self.system_details_runnables = {}
        self.temporary_storage_thread_pool = QThreadPool(self)
        self.temporary_storage_thread_pool.setMaxThreadCount(2)
        self.temporary_storage_runnables = {}
        try:
            configured_refresh_interval_ms = int(
                load_email_alerts().get("refresh_interval_ms", 0) or 0
            )
        except (TypeError, ValueError):
            configured_refresh_interval_ms = 0
        self.refresh_interval_ms = (
            configured_refresh_interval_ms
            if configured_refresh_interval_ms in (0, 3000, 5000, 10000)
            else 0
        )
        self.server_refresh_timers = {}  # Per-server refresh timers for independent refresh
        self.refresh_countdown_timer = QTimer(self)
        self.refresh_countdown_timer.setInterval(1000)
        self.refresh_countdown_timer.timeout.connect(self._update_card_refresh_countdowns)
        self._refresh_in_progress = False
        self._refresh_queued = False
        self.server_retry_counts = {}
        self.server_last_success = {}
        self.server_error_reasons = {}
        self.server_sync_durations_ms = {}
        self.server_last_fetch_time = {}  # Track when each server last completed fetch
        self._system_logged_lpar_status = {}
        self.stale_after_seconds = 90
        self.retry_backoff_seconds = 15
        self.max_retry_backoff_seconds = 120

        self.central_widget = QWidget(self)
        self.central_layout = QHBoxLayout(self.central_widget)
        self.central_layout.setContentsMargins(0, 0, 0, 0)
        self.central_layout.setSpacing(0)
        self.setCentralWidget(self.central_widget)

        self.sidebar = QWidget(self.central_widget)
        self.sidebar_collapsed = True
        self.sidebar.setFixedWidth(78)
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        self.sidebar_layout.setContentsMargins(10, 10, 10, 10)
        self.sidebar_layout.setSpacing(12)

        self.sidebar_header = QWidget(self.sidebar)
        self.sidebar_header_layout = QHBoxLayout(self.sidebar_header)
        self.sidebar_header_layout.setContentsMargins(8, 8, 8, 8)
        self.sidebar_header_layout.setSpacing(8)

        self.sidebar_logo = QLabel()
        self.sidebar_logo.setFixedSize(28, 28)
        self.sidebar_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sidebar_logo.setStyleSheet("background-color: transparent;")
        self.sidebar_logo.setPixmap(
            QPixmap(resource_path("logo.png")).scaled(
                28, 28,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.sidebar_header_layout.addWidget(self.sidebar_logo)

        self.sidebar_brand = QLabel("AS400 Qua")
        self.sidebar_brand.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        self.sidebar_brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.sidebar_brand.setFixedHeight(32)
        self.sidebar_brand.setWordWrap(False)
        self.sidebar_header_layout.addWidget(self.sidebar_brand)

        self.sidebar_toggle_btn = QPushButton("›")
        self.sidebar_toggle_btn.setFixedWidth(28)
        self.sidebar_toggle_btn.setFixedHeight(28)
        self.sidebar_toggle_btn.setStyleSheet("QPushButton { color: #a1a1a1; font-size: 24px; font-weight: bold; background: transparent; border: none; padding: 0; }")
        self.sidebar_toggle_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.sidebar_toggle_btn.clicked.connect(self.toggle_sidebar)
        self.sidebar_header_layout.addWidget(self.sidebar_toggle_btn, 0, Qt.AlignmentFlag.AlignRight)
        self.sidebar_layout.addWidget(self.sidebar_header)

        self.ping_status_label = QLabel("No ping")
        self.ping_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ping_status_label.setMinimumHeight(28)
        self.ping_status_label.setFixedHeight(28)
        self.ping_status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.ping_status_label.setStyleSheet(
            "QLabel {"
            "  color: #8b949e;"
            "  font-weight: bold;"
            "  font-size: 10px;"
            "  background-color: rgba(15, 23, 42, 0.78);"
            "  border: 1px solid rgba(148, 163, 184, 0.35);"
            "  border-radius: 14px;"
            "  padding: 0 10px;"
            "}"
        )
        self.ping_status_label.setToolTip(f"Gateway ping to {PING_TARGET_IP}")

        self.nav_buttons = {}
        self.nav_button_labels = {}
        nav_items = [
            ("Live Monitor", "monitor", "dashboard.svg"),
            ("Logs && Analytics", "log_viewer", "logs.svg"),
            ("Backup Management", "backup_management", "backup.svg"),
            ("Settings && Credentials", "settings", "settings.svg")
        ]
        self.nav_icon_files = {key: icon_file for _, key, icon_file in nav_items}
        for label, key, icon_file in nav_items:
            btn = QPushButton(label)
            btn.setIcon(QIcon(resource_path(icon_file)))
            btn.setIconSize(QSize(24, 24))
            btn.setCheckable(True)
            btn.setObjectName(f"nav_{key}")
            btn.setToolTip(label)
            btn.clicked.connect(lambda checked, page_key=key: self.set_current_page(page_key))
            btn.setFixedHeight(56)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.setStyleSheet("QPushButton { text-align: left; padding-left: 18px; padding-right: 18px; border: none; border-radius: 16px; spacing: 12px; }")
            self.nav_buttons[key] = btn
            self.nav_button_labels[key] = label
            self.sidebar_layout.addWidget(btn)

        self.sidebar_layout.addStretch(1)
        self.sidebar_layout.addWidget(self.ping_status_label, alignment=Qt.AlignmentFlag.AlignLeft)

        self.content_stack = QStackedWidget(self.central_widget)
        self.content_stack.setObjectName("contentStack")

        self.live_monitor_widget = QWidget()
        self.init_live_monitor_ui()
        self.content_stack.addWidget(self.live_monitor_widget)

        self.log_viewer_widget = LogViewerWidget()
        self.log_viewer_widget.set_theme(self.is_dark_theme)
        self.logs_analytics_widget = QWidget()
        logs_analytics_layout = QVBoxLayout(self.logs_analytics_widget)
        logs_analytics_layout.setContentsMargins(0, 0, 0, 0)
        logs_header, self.logs_header_title = self._create_page_header(
            self.logs_analytics_widget,
            "Logs & Analytics",
        )
        logs_analytics_layout.addWidget(logs_header)
        logs_analytics_divider = QFrame(self.logs_analytics_widget)
        logs_analytics_divider.setObjectName("pageHeaderDivider")
        logs_analytics_divider.setFrameShape(QFrame.Shape.HLine)
        logs_analytics_layout.addWidget(logs_analytics_divider)
        self.logs_analytics_tabs = QTabWidget(self.logs_analytics_widget)
        self.logs_analytics_tabs.setObjectName("logsAnalyticsTabs")
        self.logs_analytics_tabs.setDocumentMode(True)
        self.analytics_tab_icon_files = ("logs.svg", "monthly.svg")
        self.logs_analytics_tabs.addTab(
            self.log_viewer_widget,
            QIcon(),
            "System Logs",
        )
        logs_analytics_layout.addWidget(self.logs_analytics_tabs)
        self.content_stack.addWidget(self.logs_analytics_widget)

        self.settings_widget = QWidget()
        self._init_settings_page()
        self._system_log_record_id = 0
        self._system_log_timer = QTimer(self)
        self._system_log_timer.setInterval(250)
        self._system_log_timer.timeout.connect(self._drain_system_log)
        self._drain_system_log()
        self._system_log_timer.start()
        SYSTEM_LOGGER.info(
            "Dashboard ready with %d configured LPAR(s)",
            len(self.active_server_configs),
        )
        self._settings_saved_snapshot = self._capture_settings_snapshot()
        self._update_settings_save_state()
        self.content_stack.addWidget(self.settings_widget)

        self.backup_management_widget = BackupManagementWidget(
            self.active_server_configs,
            self._backup_credentials,
            lambda: self.credentials_validated,
            self._set_backup_status,
        )
        self.content_stack.addWidget(self.backup_management_widget)

        self.monthly_report_widget = MonthlyReportWidget()
        self.log_viewer_widget.monthly_report_widget = self.monthly_report_widget
        setattr(self.monthly_report_widget, "parent_log_viewer", self.log_viewer_widget)
        self.monthly_report_widget.set_theme(self.is_dark_theme)
        self.logs_analytics_tabs.addTab(
            self.monthly_report_widget,
            QIcon(),
            "Monthly ASP/CPU",
        )
        self.logs_analytics_tabs.tabBar().setIconSize(QSize(18, 18))
        self.logs_analytics_tabs.currentChanged.connect(
            self._refresh_analytics_tab_icons
        )
        self._refresh_analytics_tab_icons()

        self.central_layout.addWidget(self.sidebar)
        self.central_layout.addWidget(self.content_stack)
        self.set_current_page("monitor")
        self.sidebar_collapsed = True
        self._apply_sidebar_state()

        self.apply_theme_state()

        self._create_startup_loading_overlay()

        # Postpone background log loading to after the UI loop initializes
        QTimer.singleShot(300, self.post_init_tasks)

    def _create_startup_loading_overlay(self):
        self.startup_loading_overlay = QFrame(self)
        self.startup_loading_overlay.setStyleSheet(
            "QFrame { background-color: rgba(246, 248, 250, 245); }"
            "QLabel { color: #1f2937; background-color: #ffffff; "
            "border: 1px solid #d0d7de; border-radius: 10px; "
            "padding: 28px 56px; font-size: 18px; font-weight: bold; }"
        )
        overlay_layout = QVBoxLayout(self.startup_loading_overlay)
        overlay_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        loading_label = QLabel("Loading dashboard...")
        loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addWidget(loading_label)
        self.startup_loading_overlay.setGeometry(self.rect())
        self.startup_loading_overlay.raise_()
        self.startup_loading_overlay.show()

    def _current_nav_key(self):
        current_widget = self.content_stack.currentWidget()
        widget_map = {
            "monitor": self.live_monitor_widget,
            "log_viewer": self.logs_analytics_widget,
            "backup_management": self.backup_management_widget,
            "settings": self.settings_widget,
        }
        for key, widget in widget_map.items():
            if widget is current_widget:
                return key
        return "monitor"

    @staticmethod
    def _create_page_header(parent, title_text):
        header = QFrame(parent)
        header.setObjectName("pageHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(32, 14, 32, 14)
        title = QLabel(title_text, header)
        title.setObjectName("pageHeaderTitle")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        header_layout.addWidget(title)
        header_layout.addStretch()
        return header, title

    def set_current_page(self, page_key):
        mapping = {
            "monitor": 0,
            "log_viewer": 1,
            "settings": 2,
            "backup_management": 3,
        }
        if page_key != "settings" and self._current_nav_key() == "settings":
            if not self._confirm_leave_settings():
                return
        if page_key in mapping:
            self.content_stack.setCurrentIndex(mapping[page_key])
            for key, button in self.nav_buttons.items():
                button.setChecked(key == page_key)
            self._update_nav_button_styles(page_key)
            if page_key == "log_viewer":
                self.log_viewer_widget._setup_file_watcher()
                self.log_viewer_widget.load_log_history(silent=True)
            if page_key == "settings" and self.is_monitoring:
                self.status_label.setText("Status: Stop monitoring to edit settings.")
                self.status_label.setStyleSheet("color: #b45309; font-weight: bold; font-size: 11px; background-color: transparent;")
            if page_key == "backup_management" and self.credentials_validated:
                self.backup_management_widget.start_hourly_refresh()

    def _capture_settings_snapshot(self):
        def table_values(table):
            return [
                tuple(
                    table.item(row, column).text() if table.item(row, column) else ""
                    for column in range(table.columnCount())
                )
                for row in range(table.rowCount())
            ]

        return {
            "username": self.user_input.text(),
            "password": self.pass_input.text(),
            "remember": self.remember_creds_checkbox.isChecked(),
            "lpar_rows": table_values(self.lpar_table),
            "logs_root": self.logs_root_input.text(),
            "smtp_enabled": self.smtp_enabled.isChecked(),
            "smtp_server": self.smtp_server_input.text(),
            "smtp_port": self.smtp_port_input.text(),
            "smtp_tls": self.smtp_tls_checkbox.isChecked(),
            "smtp_username": self.smtp_username_input.text(),
            "smtp_password": self.smtp_password_input.text(),
            "smtp_from": self.smtp_from_input.text(),
            "smtp_to": self.smtp_to_input.text(),
            "smtp_threshold": self.smtp_threshold_input.text(),
            "smtp_cooldown": self.smtp_cooldown_input.text(),
            "refresh_interval": self.refresh_interval_combo.currentIndex(),
            "log_dedupe": self.log_dedupe_combo.currentIndex(),
        }

    def _restore_settings_snapshot(self):
        snapshot = self._settings_saved_snapshot
        self.user_input.setText(snapshot["username"])
        self.pass_input.setText(snapshot["password"])
        self.remember_creds_checkbox.setChecked(snapshot["remember"])
        self.logs_root_input.setText(snapshot["logs_root"])
        self.smtp_enabled.setChecked(snapshot["smtp_enabled"])
        self.smtp_server_input.setText(snapshot["smtp_server"])
        self.smtp_port_input.setText(snapshot["smtp_port"])
        self.smtp_tls_checkbox.setChecked(snapshot["smtp_tls"])
        self.smtp_username_input.setText(snapshot["smtp_username"])
        self.smtp_password_input.setText(snapshot["smtp_password"])
        self.smtp_from_input.setText(snapshot["smtp_from"])
        self.smtp_to_input.setText(snapshot["smtp_to"])
        self.smtp_threshold_input.setText(snapshot["smtp_threshold"])
        self.smtp_cooldown_input.setText(snapshot["smtp_cooldown"])
        self.refresh_interval_combo.setCurrentIndex(snapshot["refresh_interval"])
        self.log_dedupe_combo.setCurrentIndex(snapshot["log_dedupe"])

        self.lpar_table.setRowCount(0)
        for row_values in snapshot["lpar_rows"]:
            row = self.lpar_table.rowCount()
            self.lpar_table.insertRow(row)
            for column, value in enumerate(row_values):
                self.lpar_table.setItem(row, column, QTableWidgetItem(value))

    def _settings_have_unsaved_changes(self):
        return self._capture_settings_snapshot() != self._settings_saved_snapshot

    def _confirm_leave_settings(self):
        if not self._settings_have_unsaved_changes():
            return True

        choice = ask_question_dialog(
            self,
            "Unsaved Settings",
            "You have unsaved Settings changes. Save them before leaving?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            self.save_settings_page()
            return not self._settings_have_unsaved_changes()
        if choice == QMessageBox.StandardButton.Discard:
            self._restore_settings_snapshot()
            return True
        return False

    def toggle_sidebar(self):
        self.sidebar_collapsed = not self.sidebar_collapsed
        self._apply_sidebar_state()

    def _apply_sidebar_state(self):
        self.sidebar.setFixedWidth(78 if self.sidebar_collapsed else 290)
        self.sidebar_logo.setVisible(not self.sidebar_collapsed)
        self.sidebar_brand.setVisible(not self.sidebar_collapsed)
        if not self.sidebar_collapsed:
            self.sidebar_logo.setFixedSize(28, 28)
            self.sidebar_logo.setPixmap(
                QPixmap(resource_path("logo.png")).scaled(
                    28, 28,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.sidebar_brand.setText("AS400 Quantum")
            self.sidebar_brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.sidebar_toggle_btn.setText("《" if not self.sidebar_collapsed else "》")

        for key, button in self.nav_buttons.items():
            label = self.nav_button_labels.get(key, key)
            icon_file = self.nav_icon_files.get(key, "logo.png")
            button.setIcon(QIcon(resource_path(icon_file)))
            button.setIconSize(QSize(26, 26))
            button.setToolTip(label)
            button.setText(label if not self.sidebar_collapsed else "")
            button.setFixedWidth(54 if self.sidebar_collapsed else 200)
            button.setStyleSheet(
                "QPushButton { text-align: left; padding-left: 10px; padding-right: 10px; }"
                if not self.sidebar_collapsed else
                "QPushButton { text-align: center; padding-left: 0; padding-right: 0; icon-size: 30px; }"
            )

        self._update_nav_button_styles(self._current_nav_key())

    def _tinted_nav_icon(self, icon_file, color):
        pixmap = QIcon(resource_path(icon_file)).pixmap(QSize(26, 26))
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(color))
        painter.end()
        return QIcon(pixmap)

    def _update_nav_button_styles(self, active_key):
        dark = self.is_dark_theme
        if dark:
            sidebar_bg = "#0b0b0b"
            sidebar_inner = "#1d2430"
            active_bg = "#2d5dff"
            collapsed_active_bg = "#111621"
            active_text = "#f3f6ff"
            inactive_bg = "#151719"
            inactive_text = "#e1e3e6"
            hover_bg = "#202225"
            brand_text = "#f8fbff"
        else:
            sidebar_bg = "#edf2f8" if self.sidebar_collapsed else "#f6f8fa"
            sidebar_inner = "#f3f6fa"
            active_bg = "#2d5dff"
            collapsed_active_bg = "#e0e7ff"
            active_text = "#ffffff"
            inactive_bg = "#ffffff"
            inactive_text = "#1f2328"
            hover_bg = "#f3f4f6"
            brand_text = "#1f2937"

        self.sidebar.setStyleSheet(
            f"QWidget {{ background-color: {sidebar_bg}; border: none; border-radius: 0; }}"
        )
        self.sidebar_brand.setStyleSheet(
            f"color: {brand_text}; background-color: transparent; padding: 8px 0; font-weight: bold;"
        )

        for key, button in self.nav_buttons.items():
            is_active = key == active_key
            button.setIconSize(QSize(20, 20))
            button.setText(self.nav_button_labels.get(key, key) if not self.sidebar_collapsed else "")
            if self.sidebar_collapsed:
                icon_color = ("#79a7ff" if dark else "#4f7fff") if is_active else "#6b7280"
                button.setIcon(self._tinted_nav_icon(self.nav_icon_files[key], icon_color))
                button.setFixedWidth(64)
                collapsed_bg = collapsed_active_bg if is_active else "transparent"
                button.setStyleSheet(
                    f"QPushButton {{ background-color: {collapsed_bg}; color: {active_text if is_active else inactive_text}; "
                    f"border: none; border-radius: 18px; padding: 0; font-weight: 400; font-size: 12px; "
                    f"text-align: center; qproperty-iconSize: 26px; }} "
                    f"QPushButton:hover {{ background-color: {collapsed_active_bg if is_active else 'transparent'}; color: {active_text if is_active else inactive_text}; }}"
                )
            else:
                button.setIcon(QIcon(resource_path(self.nav_icon_files[key])))
                button.setFixedWidth(220)
                button.setStyleSheet(
                    f"QPushButton {{ background-color: {'#2d5dff' if is_active else inactive_bg}; color: {active_text if is_active else inactive_text}; "
                    f"border: none; border-radius: 18px; padding: 0 18px; font-weight: 400; font-size: 12px; "
                    f"text-align: left; spacing: 12px; }} "
                    f"QPushButton:hover {{ background-color: {active_bg if is_active else hover_bg}; color: {active_text if is_active else inactive_text}; }}"
                )
            button.setFixedHeight(56)
            button.setMinimumWidth(42)

    def _init_settings_page(self):
        layout = QVBoxLayout(self.settings_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        settings_header_frame = QFrame(self.settings_widget)
        settings_header_frame.setObjectName("settingsHeader")
        settings_header = QHBoxLayout(settings_header_frame)
        settings_header.setContentsMargins(32, 14, 32, 14)
        settings_header.setSpacing(12)
        title = QLabel("Settings & Credential Configuration")
        title.setObjectName("settingsTitle")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        settings_header.addWidget(title)
        settings_header.addStretch()

        self.theme_btn = QPushButton("☀ Light Theme")
        self.theme_btn.setObjectName("settingsThemeButton")
        self.theme_btn.setFixedHeight(35)
        self.theme_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.theme_btn.setToolTip("Switch between dark and light themes")
        self.theme_btn.clicked.connect(self.toggle_theme)
        settings_header.addWidget(self.theme_btn)

        self.settings_lock_label = QLabel("Stop monitoring to edit settings")
        self.settings_lock_label.setObjectName("settingsLockLabel")
        self.settings_lock_label.setVisible(False)
        settings_header.addWidget(self.settings_lock_label)

        layout.addWidget(settings_header_frame)

        line = QFrame()
        line.setObjectName("settingsHeaderDivider")
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        self.settings_tab_widget = QTabWidget(self.settings_widget)
        self.settings_tab_widget.setObjectName("settingsTabs")
        self.settings_tab_widget.setDocumentMode(True)
        self.credentials_page = QWidget()
        self.credentials_page_layout = QVBoxLayout(self.credentials_page)
        self.credentials_page_layout.setContentsMargins(32, 26, 32, 24)
        self.credentials_page_layout.setSpacing(18)
        self.smtp_page = QWidget()
        self.smtp_page_layout = QVBoxLayout(self.smtp_page)
        self.smtp_page_layout.setContentsMargins(32, 26, 32, 24)
        self.smtp_page_layout.setSpacing(18)
        self.lpar_page = QWidget()
        self.lpar_page_layout = QVBoxLayout(self.lpar_page)
        self.lpar_page_layout.setContentsMargins(32, 26, 32, 24)
        self.lpar_page_layout.setSpacing(18)
        self.storage_page = QWidget()
        self.storage_page_layout = QVBoxLayout(self.storage_page)
        self.storage_page_layout.setContentsMargins(32, 26, 32, 24)
        self.storage_page_layout.setSpacing(22)
        self.settings_tab_icon_files = ("Cred.svg", "smtp.svg", "configure.svg", "BnR.svg")
        tab_pages = (
            (self.credentials_page, "Credentials"),
            (self.smtp_page, "SMTP Config"),
            (self.lpar_page, "LPAR Configuration"),
            (self.storage_page, "Storage && System"),
        )
        for index, (page, title) in enumerate(tab_pages):
            self.settings_tab_widget.addTab(
                page,
                self._settings_tab_icon(index),
                title,
            )
        self.settings_tab_widget.tabBar().setIconSize(QSize(18, 18))
        self.settings_tab_widget.currentChanged.connect(self._refresh_settings_tab_icons)
        layout.addWidget(self.settings_tab_widget, stretch=1)

        form_row = QHBoxLayout()
        form_row.setSpacing(24)

        user_wrapper = QVBoxLayout()
        user_wrapper.setSpacing(8)
        user_label = QLabel("Username")
        user_label.setObjectName("settingsFieldLabel")
        user_wrapper.addWidget(user_label)

        self.user_input = QLineEdit("")
        self.user_input.setObjectName("settingsInput")
        self.user_input.setPlaceholderText("Username")
        self.user_input.setFont(QFont("Segoe UI", 11))
        self.user_input.setMinimumHeight(38)
        user_wrapper.addWidget(self.user_input)
        form_row.addLayout(user_wrapper, stretch=1)

        pass_wrapper = QVBoxLayout()
        pass_wrapper.setSpacing(8)
        pass_label = QLabel("Password")
        pass_label.setObjectName("settingsFieldLabel")
        pass_wrapper.addWidget(pass_label)

        self.pass_input = QLineEdit("")
        self.pass_input.setObjectName("settingsInput")
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setFont(QFont("Segoe UI", 11))
        self.pass_input.setMinimumHeight(38)
        pass_wrapper.addWidget(self.pass_input)
        form_row.addLayout(pass_wrapper, stretch=1)

        saved_login = load_login_credentials()
        if saved_login.get("remember") and saved_login.get("username"):
            self.user_input.setText(saved_login["username"])
            self.pass_input.setText(get_ibmi_password(saved_login["username"]))
        self.remember_creds_checkbox = QCheckBox("Remember credentials")
        self.remember_creds_checkbox.setChecked(bool(saved_login.get("remember", False)))

        self.toggle_btn = QPushButton("Check Login Creds")
        self.toggle_btn.setObjectName("settingsPrimaryButton")
        self.toggle_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.toggle_btn.setFixedHeight(38)
        self.toggle_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.toggle_btn.clicked.connect(self.validate_login_credentials_async)

        self.settings_btn = QPushButton("Save && Apply")
        self.settings_btn.setObjectName("settingsSaveButton")
        self.settings_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.settings_btn.setFixedHeight(38)
        self.settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.settings_btn.clicked.connect(self.save_settings_page)
        self.settings_btn.setText("Save Changes")
        settings_header.addWidget(self.settings_btn)

        credential_card = QFrame(self.credentials_page)
        credential_card.setObjectName("settingsCard")
        credential_layout = QVBoxLayout(credential_card)
        credential_layout.setContentsMargins(22, 20, 22, 20)
        credential_layout.setSpacing(18)
        credential_layout.addLayout(form_row)
        credential_options_row = QHBoxLayout()
        credential_options_row.addWidget(self.remember_creds_checkbox)
        credential_options_row.addStretch()
        credential_options_row.addWidget(self.toggle_btn)
        credential_layout.addLayout(credential_options_row)
        self.credentials_page_layout.addWidget(credential_card)

        credential_tab = QGroupBox("System Logs")
        credential_layout = QVBoxLayout(credential_tab)
        credential_tab.setObjectName("settingsLogCard")
        credential_layout.setContentsMargins(16, 14, 16, 14)

        self.clear_system_logs_btn = QPushButton("Clear All Logs")
        self.clear_system_logs_btn.setObjectName("settingsClearLogsButton")
        self.clear_system_logs_btn.setFixedHeight(32)
        self.clear_system_logs_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.clear_system_logs_btn.clicked.connect(self.clear_system_logs)
        credential_layout.addWidget(
            self.clear_system_logs_btn,
            alignment=Qt.AlignmentFlag.AlignRight,
        )

        self.cred_log = QPlainTextEdit()
        self.cred_log.setObjectName("settingsLog")
        self.cred_log.setReadOnly(True)
        self.cred_log.setMinimumHeight(210)
        self.cred_log.setMaximumBlockCount(1000)
        self.cred_log.setPlaceholderText("System events will appear here as they occur.")
        credential_layout.addWidget(self.cred_log)
        self.credentials_page_layout.addWidget(credential_tab, stretch=1)

        lpar_tab = QGroupBox("LPAR Configuration")
        lpar_tab.setObjectName("settingsCard")
        lpar_layout = QVBoxLayout(lpar_tab)
        lpar_layout.setContentsMargins(18, 16, 18, 16)

        self.lpar_table = QTableWidget()
        self.lpar_table.setObjectName("settingsLparTable")
        self.lpar_table.setColumnCount(8)
        self.lpar_table.setHorizontalHeaderLabels([
            "IP / Hostname", "Database Name", "Daily Backup Name",
            "Journal Backup Name", "Monthly Backup Name", "Expected Subsystems",
            "Monitored Ports (Port:Name)", "Web Apps (Job:Port)"
        ])
        self.lpar_table.setAlternatingRowColors(True)
        self.lpar_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lpar_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.lpar_table.setEditTriggers(QTableWidget.EditTrigger.AllEditTriggers)
        self.lpar_table.setWordWrap(True)
        self.lpar_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.lpar_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.lpar_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.lpar_table.horizontalHeader().setMinimumSectionSize(140)
        self.lpar_table.horizontalHeader().setMinimumHeight(46)
        for column, width in enumerate((180, 160, 175, 200, 180, 260, 300, 300)):
            self.lpar_table.setColumnWidth(column, width)
        self.lpar_table.horizontalHeader().setStretchLastSection(True)
        self._populate_lpar_table()
        lpar_layout.addWidget(self.lpar_table)

        lpar_buttons = QHBoxLayout()
        self.add_lpar_btn = QPushButton("+ Add LPAR")
        self.add_lpar_btn.setObjectName("settingsAddButton")
        self.add_lpar_btn.clicked.connect(self._add_lpar_row)
        self.remove_lpar_btn = QPushButton("Remove Selected")
        self.remove_lpar_btn.setObjectName("settingsRemoveButton")
        self.remove_lpar_btn.clicked.connect(self._remove_lpar_row)
        lpar_buttons.addWidget(self.add_lpar_btn)
        lpar_buttons.addWidget(self.remove_lpar_btn)
        lpar_buttons.addStretch()
        lpar_layout.addLayout(lpar_buttons)
        lpar_tab.setMinimumHeight(250)

        storage_tab = QGroupBox("Log Storage")
        storage_tab.setObjectName("settingsCard")
        storage_layout = QGridLayout(storage_tab)
        storage_layout.setContentsMargins(18, 16, 18, 16)
        storage_layout.setHorizontalSpacing(12)
        storage_layout.setVerticalSpacing(8)
        storage_label = QLabel("Log Storage Root Folder")
        storage_label.setObjectName("settingsFieldLabel")
        storage_layout.addWidget(storage_label, 0, 0, 1, 2)
        self.logs_root_input = QLineEdit(ONEDRIVE_SHAREPOINT_PATH)
        self.logs_root_input.setObjectName("settingsInput")
        self.logs_root_input.setMinimumHeight(38)
        self.logs_root_input.setPlaceholderText("Choose a folder for monthly logs")
        storage_layout.addWidget(self.logs_root_input, 1, 0)
        self.browse_logs_btn = QPushButton("Browse...")
        self.browse_logs_btn.setObjectName("settingsSecondaryButton")
        self.browse_logs_btn.setMinimumHeight(38)
        self.browse_logs_btn.clicked.connect(self._browse_logs_root)
        storage_layout.addWidget(self.browse_logs_btn, 1, 1)

        smtp_tab = QGroupBox("SMTP / Mail Configuration")
        smtp_tab.setObjectName("settingsCard")
        smtp_layout = QVBoxLayout(smtp_tab)
        smtp_layout.setContentsMargins(18, 16, 18, 16)
        smtp_layout.setSpacing(16)
        email_cfg = load_email_alerts()

        smtp_options_row = QHBoxLayout()
        self.smtp_enabled = QCheckBox("Enable email alerts")
        self.smtp_enabled.setChecked(bool(email_cfg.get("enabled", False)))
        smtp_options_row.addWidget(self.smtp_enabled)
        self.smtp_tls_checkbox = QCheckBox("Use TLS")
        self.smtp_tls_checkbox.setChecked(bool(email_cfg.get("use_tls", True)))
        smtp_options_row.addWidget(self.smtp_tls_checkbox)
        smtp_options_row.addStretch()
        smtp_layout.addLayout(smtp_options_row)

        smtp_fields_grid = QGridLayout()
        smtp_fields_grid.setHorizontalSpacing(24)
        smtp_fields_grid.setVerticalSpacing(18)

        def add_smtp_field(row, column, label_text, control):
            field_layout = QVBoxLayout()
            field_layout.setSpacing(8)
            field_label = QLabel(label_text)
            field_label.setObjectName("settingsFieldLabel")
            control.setObjectName("settingsInput")
            control.setMinimumHeight(38)
            field_layout.addWidget(field_label)
            field_layout.addWidget(control)
            smtp_fields_grid.addLayout(field_layout, row, column)

        self.smtp_server_input = QLineEdit(str(email_cfg.get("smtp_server", "")))
        self.smtp_server_input.setPlaceholderText("smtp.office365.com")
        self.smtp_port_input = QLineEdit(str(email_cfg.get("port", 587)))
        add_smtp_field(0, 0, "SMTP Server", self.smtp_server_input)
        add_smtp_field(0, 1, "Port", self.smtp_port_input)
        self.smtp_username_input = QLineEdit(str(email_cfg.get("username", "")))
        self.smtp_password_input = QLineEdit()
        self.smtp_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_password_input.setPlaceholderText("Leave blank to keep saved password")
        add_smtp_field(1, 0, "Username", self.smtp_username_input)
        add_smtp_field(1, 1, "Password", self.smtp_password_input)
        self.smtp_from_input = QLineEdit(str(email_cfg.get("from_address", "")))
        self.smtp_to_input = QLineEdit(", ".join(email_cfg.get("to_addresses", [])))
        self.smtp_to_input.setPlaceholderText("recipient@example.com, ...")
        add_smtp_field(2, 0, "From Address", self.smtp_from_input)
        add_smtp_field(2, 1, "To Addresses", self.smtp_to_input)
        self.smtp_threshold_input = QLineEdit(str(email_cfg.get("threshold_percent", 40)))
        self.smtp_cooldown_input = QLineEdit(str(email_cfg.get("cooldown_minutes", 10)))
        add_smtp_field(3, 0, "Threshold %", self.smtp_threshold_input)
        add_smtp_field(3, 1, "Cooldown minutes", self.smtp_cooldown_input)
        self.refresh_interval_combo = QComboBox()
        self.refresh_interval_combo.addItems(["Instantly", "3s", "5s", "10s"])
        refresh_interval_ms = int(email_cfg.get("refresh_interval_ms", 0) or 0)
        self.refresh_interval_combo.setCurrentIndex({0: 0, 3000: 1, 5000: 2, 10000: 3}.get(refresh_interval_ms, 0))
        self.refresh_interval_combo.setMinimumHeight(38)
        self.log_dedupe_combo = QComboBox()
        self.log_dedupe_combo.addItems(["Off", "30s", "1m", "5m"])
        log_dedupe_seconds = int(email_cfg.get("log_dedupe_seconds", 60) or 60)
        self.log_dedupe_combo.setCurrentIndex({0: 0, 30: 1, 60: 2, 300: 3}.get(log_dedupe_seconds, 2))
        self.log_dedupe_combo.setMinimumHeight(38)
        add_smtp_field(4, 0, "Refresh Interval", self.refresh_interval_combo)
        add_smtp_field(4, 1, "Log Dedupe", self.log_dedupe_combo)
        smtp_layout.addLayout(smtp_fields_grid)

        smtp_actions_row = QHBoxLayout()
        smtp_actions_row.addStretch()
        self.test_email_btn = QPushButton("Send Test Email")
        self.test_email_btn.setObjectName("settingsSecondaryButton")
        self.test_email_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.test_email_btn.clicked.connect(self.send_test_email)
        smtp_actions_row.addWidget(self.test_email_btn)
        smtp_layout.addLayout(smtp_actions_row)
        smtp_layout.addStretch()
        self.smtp_page_layout.addWidget(smtp_tab)
        self.smtp_page_layout.addStretch(1)
        self.lpar_page_layout.addWidget(lpar_tab, stretch=1)
        self.storage_page_layout.addWidget(storage_tab)

        system_tab = QGroupBox("System")
        system_tab.setObjectName("settingsCard")
        system_layout = QHBoxLayout(system_tab)
        system_layout.setContentsMargins(18, 16, 18, 16)
        system_copy = QVBoxLayout()
        system_title = QLabel("System Backup & Restore")
        system_title.setObjectName("settingsSectionTitle")
        system_description = QLabel("Backup or restore the entire application configuration.")
        system_description.setObjectName("settingsMutedText")
        system_copy.addWidget(system_title)
        system_copy.addWidget(system_description)
        system_layout.addLayout(system_copy)
        system_layout.addStretch()

        self.backup_settings_btn = QPushButton("Backup")
        self.backup_settings_btn.setText("Backup Configuration")
        self.backup_settings_btn.setFixedHeight(35)
        self.backup_settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.backup_settings_btn.clicked.connect(self.backup_settings)
        system_layout.addWidget(self.backup_settings_btn)

        self.restore_settings_btn = QPushButton("Restore")
        self.restore_settings_btn.setFixedHeight(35)
        self.restore_settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.restore_settings_btn.clicked.connect(self.restore_settings)
        system_layout.addWidget(self.restore_settings_btn)
        self.storage_page_layout.addWidget(system_tab)
        self.storage_page_layout.addStretch(1)

        self._settings_editable_widgets = [
            self.backup_settings_btn,
            self.restore_settings_btn,
            self.user_input,
            self.pass_input,
            self.remember_creds_checkbox,
            self.toggle_btn,
            self.settings_btn,
            self.lpar_table,
            self.add_lpar_btn,
            self.remove_lpar_btn,
            self.logs_root_input,
            self.browse_logs_btn,
            self.smtp_enabled,
            self.smtp_server_input,
            self.smtp_port_input,
            self.smtp_tls_checkbox,
            self.smtp_username_input,
            self.smtp_password_input,
            self.smtp_from_input,
            self.smtp_to_input,
            self.smtp_threshold_input,
            self.smtp_cooldown_input,
            self.refresh_interval_combo,
            self.log_dedupe_combo,
            self.test_email_btn,
        ]
        for widget in self._settings_editable_widgets:
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._update_settings_save_state)
            elif isinstance(widget, QCheckBox):
                widget.stateChanged.connect(self._update_settings_save_state)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._update_settings_save_state)
        self.lpar_table.itemChanged.connect(self._on_lpar_table_item_changed)
        self._set_settings_editable(True)

    def _set_settings_editable(self, editable):
        for widget in self._settings_editable_widgets:
            widget.setEnabled(editable)
        self.settings_lock_label.setVisible(not editable)
        self.settings_btn.setToolTip(
            "Save and apply settings" if editable else "Stop monitoring to edit settings"
        )
        if not editable:
            self.status_label.setText("Status: Stop monitoring to edit settings.")
            self.status_label.setStyleSheet("color: #b45309; font-weight: bold; font-size: 11px; background-color: transparent;")
        self._update_settings_save_state()

    def _apply_settings_theme(self):
        if not hasattr(self, "settings_tab_widget"):
            return

        if self.is_dark_theme:
            background = "#171819"
            surface = "#202225"
            header = "#202225"
            text = "#e1e3e6"
            muted = "#aeb4bc"
            border = "#34383d"
            accent = "#79aaff"
            hover = "#2a2c2f"
            input_background = "#202225"
            log_background = "#171819"
            log_text = "#e1e3e6"
            danger = "#f85149"
        else:
            background = "#f6f8fa"
            surface = "#ffffff"
            header = "#ffffff"
            text = "#1f2328"
            muted = "#57606a"
            border = "#d0d7de"
            accent = "#5b7ff5"
            hover = "#f3f4f6"
            input_background = "#ffffff"
            log_background = "#f6f8fa"
            log_text = "#1f2328"
            danger = "#cf222e"

        self.settings_widget.setStyleSheet(f"""
            QWidget#settingsTabs {{
                background-color: {background};
                color: {text};
            }}
            QFrame#settingsHeader {{
                background-color: {header};
                border-bottom: 1px solid {border};
            }}
            QLabel#settingsTitle {{
                color: {text};
                background: transparent;
            }}
            QFrame#settingsHeaderDivider {{
                max-height: 1px;
                background-color: {border};
                border: none;
            }}
            QTabWidget#settingsTabs::pane {{
                background-color: {background};
                border: none;
            }}
            QTabWidget#settingsTabs QTabBar::tab {{
                background: transparent;
                color: {muted};
                border: none;
                border-bottom: 2px solid transparent;
                padding: 14px 16px;
                margin-right: 8px;
                font-size: 10pt;
                font-weight: 600;
            }}
            QTabWidget#settingsTabs QTabBar::tab:selected {{
                color: {accent};
                border-bottom: 2px solid {accent};
            }}
            QTabWidget#settingsTabs QTabBar::tab:hover:!selected {{
                color: {text};
                background-color: {hover};
            }}
            QFrame#settingsCard, QGroupBox#settingsCard, QGroupBox#settingsLogCard {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QGroupBox#settingsCard::title, QGroupBox#settingsLogCard::title {{
                color: {text};
                subcontrol-origin: margin;
                left: 16px;
                padding: 0 6px;
                font-weight: 600;
            }}
            QLabel#settingsFieldLabel {{
                color: {muted};
                background: transparent;
                font-size: 9.5pt;
            }}
            QLabel#settingsSectionTitle {{
                color: {text};
                background: transparent;
                font-size: 12pt;
                font-weight: 600;
            }}
            QLabel#settingsMutedText {{
                color: {muted};
                background: transparent;
            }}
            QLineEdit#settingsInput, QComboBox#settingsInput {{
                background-color: {input_background};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 8px 10px;
                selection-background-color: {accent};
            }}
            QLineEdit#settingsInput:focus, QComboBox#settingsInput:focus {{
                border: 1px solid {accent};
            }}
            QPlainTextEdit#settingsLog {{
                background-color: {log_background};
                color: {log_text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 12px;
                font-family: Consolas, 'Cascadia Mono', monospace;
                font-size: 9.5pt;
            }}
            QTableWidget#settingsLparTable {{
                background-color: {surface};
                color: {text};
                alternate-background-color: {hover};
                border: 1px solid {border};
                gridline-color: {border};
                selection-background-color: {accent};
                selection-color: #ffffff;
            }}
            QTableWidget#settingsLparTable::item {{
                padding: 10px 12px;
                border-bottom: 1px solid {border};
            }}
            QTableWidget#settingsLparTable QHeaderView::section {{
                background-color: {background};
                color: {muted};
                border: none;
                border-bottom: 1px solid {border};
                padding: 10px 12px;
                font-size: 9pt;
                font-weight: 600;
            }}
            QPushButton#settingsSaveButton, QPushButton#settingsPrimaryButton {{
                background-color: {accent};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 0 18px;
                font-weight: 600;
            }}
            QPushButton#settingsSaveButton:hover, QPushButton#settingsPrimaryButton:hover {{
                background-color: {'#456be0' if self.is_dark_theme else '#466be0'};
            }}
            QPushButton#settingsSaveButton:disabled, QPushButton#settingsPrimaryButton:disabled {{
                background-color: {'#34383d' if self.is_dark_theme else '#d8dee4'};
                color: {muted};
            }}
            QPushButton#settingsSecondaryButton, QPushButton#settingsThemeButton {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 0 16px;
                font-weight: 600;
            }}
            QPushButton#settingsClearLogsButton {{
                background-color: transparent;
                color: {danger};
                border: 1px solid {danger};
                border-radius: 6px;
                padding: 0 12px;
                font-weight: 600;
            }}
            QPushButton#settingsClearLogsButton:hover {{
                background-color: {danger};
                color: #ffffff;
            }}
            QPushButton#settingsSecondaryButton:hover, QPushButton#settingsThemeButton:hover {{
                background-color: {hover};
            }}
            QPushButton#settingsAddButton {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
            }}
            QPushButton#settingsRemoveButton {{
                background-color: {danger};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
            }}
            QLabel#settingsLockLabel {{
                color: {'#fbbf24' if self.is_dark_theme else '#92400e'};
                background-color: {'#3b2f12' if self.is_dark_theme else '#fef3c7'};
                border: 1px solid {'#805d13' if self.is_dark_theme else '#f59e0b'};
                border-radius: 6px;
                padding: 6px 10px;
                font-weight: 600;
            }}
        """)

        self._refresh_settings_tab_icons()

    def _settings_tab_icon(self, index):
        icon_path = resource_path(self.settings_tab_icon_files[index])
        with open(icon_path, "r", encoding="utf-8") as icon_file:
            svg_markup = icon_file.read()
        icon_color = "#79aaff" if self.is_dark_theme else "#5b7ff5"
        if index != self.settings_tab_widget.currentIndex():
            icon_color = "#aeb4bc" if self.is_dark_theme else "#57606a"
        renderer = QSvgRenderer(
            QByteArray(svg_markup.replace("currentColor", icon_color).encode("utf-8"))
        )
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)

    def _refresh_settings_tab_icons(self, *_args):
        if not hasattr(self, "settings_tab_icon_files"):
            return
        for index in range(self.settings_tab_widget.count()):
            self.settings_tab_widget.setTabIcon(index, self._settings_tab_icon(index))

    def _analytics_tab_icon(self, index):
        icon_path = resource_path(self.analytics_tab_icon_files[index])
        with open(icon_path, "r", encoding="utf-8") as icon_file:
            svg_markup = icon_file.read()
        icon_color = "#79aaff" if self.is_dark_theme else "#0969da"
        if index != self.logs_analytics_tabs.currentIndex():
            icon_color = "#a4a8ae" if self.is_dark_theme else "#57606a"
        renderer = QSvgRenderer(
            QByteArray(
                svg_markup.replace("#6b7280", icon_color)
                .replace("currentColor", icon_color)
                .encode("utf-8")
            )
        )
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)

    def _refresh_analytics_tab_icons(self, *_args):
        if not hasattr(self, "analytics_tab_icon_files"):
            return
        for index in range(self.logs_analytics_tabs.count()):
            self.logs_analytics_tabs.setTabIcon(
                index,
                self._analytics_tab_icon(index),
            )

    def _append_system_log(self, message):
        self.cred_log.appendPlainText(message)
        scrollbar = self.cred_log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _drain_system_log(self):
        next_record_id, messages = self.system_log_handler.records_since(
            self._system_log_record_id
        )
        self._system_log_record_id = next_record_id
        for message in messages:
            self._append_system_log(message)

    def clear_system_logs(self):
        choice = ask_question_dialog(
            self,
            "Clear System Logs",
            "Permanently delete all saved system logs and clear the log display?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        try:
            self._system_log_record_id = clear_persisted_system_logs()
        except (OSError, RuntimeError) as exc:
            show_critical_dialog(
                self,
                "Could Not Clear System Logs",
                f"The saved system log could not be cleared:\n{exc}",
            )
            return

        self.cred_log.clear()

    def _update_settings_save_state(self, *_args):
        if not hasattr(self, "_settings_saved_snapshot"):
            return
        self.settings_btn.setEnabled(
            not self.is_monitoring and self._settings_have_unsaved_changes()
        )

    def _backup_credentials(self):
        return self.user_input.text().strip(), self.pass_input.text()

    def _browse_logs_root(self):
        selected = get_existing_directory(
            self,
            "Choose log storage folder",
            self.logs_root_input.text().strip() or os.path.expanduser("~"),
        )
        if selected:
            self.logs_root_input.setText(selected)

    def backup_settings(self):
        target_path, _ = get_save_file_name(
            self,
            "Backup settings and credentials",
            "AS400 Quantum Suite.conf",
            "Configuration files (*.conf);;All files (*)",
        )
        if not target_path:
            return
        if not target_path.lower().endswith(".conf"):
            target_path += ".conf"

        try:
            config_path = get_config_path()
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as file:
                    payload: dict[str, object] = json.load(file)
            else:
                payload = {
                    "SERVER_CONFIGS": dict(self.active_server_configs),
                    "EXPECTED_SUBSYSTEMS": dict(EXPECTED_SUBSYSTEMS),
                    "EXPECTED_PORTS": dict(EXPECTED_PORTS),
                    "EXPECTED_WEB_APPS": dict(EXPECTED_WEB_APPS),
                    "EMAIL_ALERTS": {},
                }
            email_alerts = dict(cast(dict, payload.get("EMAIL_ALERTS", {})))
            email_alerts["password"] = self.smtp_password_input.text() or get_email_password(
                self.smtp_username_input.text().strip()
            )
            payload["EMAIL_ALERTS"] = email_alerts
            payload["LOGIN_CREDENTIALS"] = {
                "remember": self.remember_creds_checkbox.isChecked(),
                "username": self.user_input.text().strip(),
                "password": self.pass_input.text(),
            }
            payload["LOGS_ROOT"] = self.logs_root_input.text().strip()
            save_settings_backup(target_path, payload)
        except (OSError, TypeError, ValueError) as exc:
            SYSTEM_LOGGER.error("Settings backup failed")
            show_critical_dialog(self, "Backup Failed", f"Could not create the settings backup:\n{exc}")
            return
        SYSTEM_LOGGER.info("Settings backup created")
        show_information_dialog(self, "Backup Created", f"Settings backup created:\n{target_path}")

    def restore_settings(self):
        source_path, _ = get_open_file_name(
            self,
            "Restore settings and credentials",
            os.path.expanduser("~"),
            "Configuration files (*.conf);;All files (*)",
        )
        if not source_path:
            return
        try:
            payload = load_settings_backup(source_path)
            email_alerts = payload["EMAIL_ALERTS"]
            login_credentials = payload["LOGIN_CREDENTIALS"]
            self.user_input.setText(str(login_credentials.get("username", "")))
            self.pass_input.setText(str(login_credentials.get("password", "")))
            self.remember_creds_checkbox.setChecked(bool(login_credentials.get("remember", False)))
            self.logs_root_input.setText(str(payload.get("LOGS_ROOT", ONEDRIVE_SHAREPOINT_PATH)))
            self.smtp_enabled.setChecked(bool(email_alerts.get("enabled", False)))
            self.smtp_server_input.setText(str(email_alerts.get("smtp_server", "")))
            self.smtp_port_input.setText(str(email_alerts.get("port", 587)))
            self.smtp_tls_checkbox.setChecked(bool(email_alerts.get("use_tls", True)))
            self.smtp_username_input.setText(str(email_alerts.get("username", "")))
            self.smtp_password_input.setText(str(email_alerts.get("password", "")))
            self.smtp_from_input.setText(str(email_alerts.get("from_address", "")))
            self.smtp_to_input.setText(", ".join(email_alerts.get("to_addresses", [])))
            self.smtp_threshold_input.setText(str(email_alerts.get("threshold_percent", 40)))
            self.smtp_cooldown_input.setText(str(email_alerts.get("cooldown_minutes", 10)))
            self.refresh_interval_combo.setCurrentIndex({0: 0, 3000: 1, 5000: 2, 10000: 3}.get(int(email_alerts.get("refresh_interval_ms", 0) or 0), 0))
            self.log_dedupe_combo.setCurrentIndex({0: 0, 30: 1, 60: 2, 300: 3}.get(int(email_alerts.get("log_dedupe_seconds", 60) or 60), 2))
            restored_subsystems = payload["EXPECTED_SUBSYSTEMS"]
            restored_ports = payload["EXPECTED_PORTS"]
            restored_web_apps = payload.get("EXPECTED_WEB_APPS", {})
            self.active_server_configs.clear()
            self.active_server_configs.update(payload["SERVER_CONFIGS"])
            EXPECTED_SUBSYSTEMS.clear()
            EXPECTED_SUBSYSTEMS.update(restored_subsystems)
            EXPECTED_PORTS.clear()
            EXPECTED_PORTS.update(restored_ports)
            EXPECTED_WEB_APPS.clear()
            EXPECTED_WEB_APPS.update(restored_web_apps)
            self._populate_lpar_table()
            self._settings_saved_snapshot = self._capture_settings_snapshot()
            self.save_settings_page()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            SYSTEM_LOGGER.error("Settings restore failed")
            show_critical_dialog(self, "Restore Failed", f"Could not restore the settings backup:\n{exc}")

    def _set_backup_status(self, message):
        if hasattr(self, "status_label"):
            self.status_label.setText(message)

    def _populate_lpar_table(self):
        self.lpar_table.setRowCount(len(self.active_server_configs))
        for row, (server_name, cfg) in enumerate(sorted(self.active_server_configs.items())):
            host = cfg.get("host", "") if isinstance(cfg, dict) else str(cfg)
            db = cfg.get("db", "*LOCAL") if isinstance(cfg, dict) else "*LOCAL"
            subsystems = EXPECTED_SUBSYSTEMS.get(server_name, [])
            subsystems_str = ", ".join(subsystems) if isinstance(subsystems, (list, tuple)) else str(subsystems)
            ports = EXPECTED_PORTS.get(server_name, [])
            port_parts = []
            for entry in ports:
                if isinstance(entry, dict):
                    port_parts.append(f"{entry.get('port')}:{entry.get('name')}")
                else:
                    port_parts.append(str(entry))
            web_apps = EXPECTED_WEB_APPS.get(server_name, [])
            web_app_parts = []
            for entry in web_apps:
                if isinstance(entry, dict):
                    app_name = str(entry.get("job_name") or entry.get("name") or entry.get("job") or "").strip().upper()
                    port_num = entry.get("port")
                    if app_name and port_num is not None:
                        web_app_parts.append(f"{app_name}:{port_num}")
                elif isinstance(entry, str):
                    item = entry.strip()
                    if item:
                        web_app_parts.append(item)
            daily_name = cfg.get("daily_backup_name", "DAILYSWA") if isinstance(cfg, dict) else "DAILYSWA"
            journal_name = cfg.get("journal_backup_name", "DAILYSWA") if isinstance(cfg, dict) else "DAILYSWA"
            monthly_name = cfg.get("monthly_backup_name", "MONTHLYBKU") if isinstance(cfg, dict) else "MONTHLYBKU"
            values = (
                host,
                db,
                str(daily_name or "DAILYSWA"),
                str(journal_name or "DAILYSWA"),
                str(monthly_name or "MONTHLYBKU"),
                subsystems_str,
                ", ".join(port_parts),
                ", ".join(web_app_parts),
            )
            for column, value in enumerate(values):
                self.lpar_table.setItem(row, column, self._make_lpar_table_item(value))
        self.lpar_table.resizeRowsToContents()

    @staticmethod
    def _make_lpar_table_item(value):
        item = QTableWidgetItem(str(value))
        item.setToolTip(str(value))
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        return item

    def _on_lpar_table_item_changed(self, item):
        item.setToolTip(item.text())
        self.lpar_table.resizeRowToContents(item.row())
        self._update_settings_save_state()

    def _add_lpar_row(self):
        row = self.lpar_table.rowCount()
        self.lpar_table.insertRow(row)
        values = (
            "192.168.1.1",
            "*LOCAL",
            "DAILYSWA",
            "DAILYSWA",
            "MONTHLYBKU",
            "QINTER, QBATCH, QSYSWRK",
            "21:FTP, 22:SSH",
            "DLRWEB:10078",
        )
        for column, value in enumerate(values):
            self.lpar_table.setItem(row, column, self._make_lpar_table_item(value))
        self.lpar_table.resizeRowToContents(row)
        self._update_settings_save_state()

    def _remove_lpar_row(self):
        row = self.lpar_table.currentRow()
        if row >= 0:
            self.lpar_table.removeRow(row)
            self._update_settings_save_state()

    def send_test_email(self):
        smtp_server = self.smtp_server_input.text().strip()
        username = self.smtp_username_input.text().strip()
        password = self.smtp_password_input.text() or get_email_password(username)
        from_address = self.smtp_from_input.text().strip() or username
        to_addresses = [address.strip() for address in self.smtp_to_input.text().split(",") if address.strip()]

        try:
            port = int(self.smtp_port_input.text().strip() or 587)
        except ValueError:
            show_warning_dialog(self, "Invalid SMTP Port", "Please enter a valid SMTP port.")
            return

        if not smtp_server or not to_addresses or "@" not in from_address:
            show_warning_dialog(
                self,
                "Incomplete SMTP Settings",
                "Enter an SMTP server, a valid From Address, and at least one recipient.",
            )
            return
        if any("@" not in address for address in to_addresses):
            show_warning_dialog(self, "Invalid Recipient", "Please check the recipient email addresses.")
            return

        self.test_email_btn.setEnabled(False)
        self.test_email_thread = TestEmailThread(
            smtp_server=smtp_server,
            port=port,
            use_tls=self.smtp_tls_checkbox.isChecked(),
            username=username,
            password=password,
            from_address=from_address,
            to_addresses=to_addresses,
        )
        self.test_email_thread.result_ready.connect(self._handle_test_email_result)
        self.test_email_thread.start()

    def _handle_test_email_result(self, success, message):
        self.test_email_btn.setEnabled(True)
        if success:
            SYSTEM_LOGGER.info("SMTP test email sent successfully")
            show_information_dialog(self, "Test Email", message)
        else:
            SYSTEM_LOGGER.warning("SMTP test email failed")
            show_critical_dialog(self, "Test Email Failed", message)

    def save_settings_page(self):
        if self.is_monitoring:
            self.status_label.setText("Status: Stop monitoring to edit settings.")
            self.status_label.setStyleSheet("color: #b45309; font-weight: bold; font-size: 11px; background-color: transparent;")
            return

        new_configs = {}
        new_subsystems = {}
        new_ports = {}
        new_web_apps = {}
        for row in range(self.lpar_table.rowCount()):
            host_item = self.lpar_table.item(row, 0)
            if not host_item or not host_item.text().strip():
                continue
            host = host_item.text().strip()
            db = self.lpar_table.item(row, 1).text().strip() if self.lpar_table.item(row, 1) else "*LOCAL"
            daily_backup_name = self.lpar_table.item(row, 2).text().strip().upper() if self.lpar_table.item(row, 2) else "DAILYSWA"
            journal_backup_name = self.lpar_table.item(row, 3).text().strip().upper() if self.lpar_table.item(row, 3) else "DAILYSWA"
            monthly_backup_name = self.lpar_table.item(row, 4).text().strip().upper() if self.lpar_table.item(row, 4) else "MONTHLYBKU"
            subsystems = self.lpar_table.item(row, 5).text().strip() if self.lpar_table.item(row, 5) else ""
            ports = self.lpar_table.item(row, 6).text().strip() if self.lpar_table.item(row, 6) else ""
            web_apps = self.lpar_table.item(row, 7).text().strip() if self.lpar_table.item(row, 7) else ""
            new_configs[host.upper()] = {
                "host": host,
                "db": db,
                "daily_backup_name": daily_backup_name or "DAILYSWA",
                "journal_backup_name": journal_backup_name or "DAILYSWA",
                "monthly_backup_name": monthly_backup_name or "MONTHLYBKU",
            }
            new_subsystems[host.upper()] = [s.strip().upper() for s in subsystems.split(",") if s.strip()]
            parsed_ports = []
            for part in ports.split(","):
                item = part.strip()
                if not item:
                    continue
                if ":" in item:
                    port_num, port_name = item.split(":", 1)
                    if port_num.strip().isdigit():
                        parsed_ports.append({"port": int(port_num.strip()), "name": port_name.strip().upper()})
                elif item.isdigit():
                    parsed_ports.append({"port": int(item), "name": f"PORT_{item}"})
            parsed_web_apps = []
            for part in web_apps.split(","):
                item = part.strip()
                if not item:
                    continue
                if ":" in item:
                    app_name, port_num = item.split(":", 1)
                    if port_num.strip().isdigit():
                        parsed_web_apps.append({"job_name": app_name.strip().upper(), "port": int(port_num.strip())})
            new_ports[host.upper()] = parsed_ports
            new_web_apps[host.upper()] = parsed_web_apps

        try:
            smtp_port = int(self.smtp_port_input.text().strip() or 587)
        except ValueError:
            smtp_port = 587
        try:
            threshold_percent = float(self.smtp_threshold_input.text().strip() or 40)
        except ValueError:
            threshold_percent = 40
        try:
            cooldown_minutes = int(self.smtp_cooldown_input.text().strip() or 10)
        except ValueError:
            cooldown_minutes = 10
        refresh_interval_ms = {
            "Instantly": 0,
            "3s": 3000,
            "5s": 5000,
            "10s": 10000,
        }.get(self.refresh_interval_combo.currentText(), 0)
        log_dedupe_seconds = {
            "Off": 0,
            "30s": 30,
            "1m": 60,
            "5m": 300,
        }.get(self.log_dedupe_combo.currentText(), 60)
        email_alerts = {
            "enabled": self.smtp_enabled.isChecked(),
            "smtp_server": self.smtp_server_input.text().strip(),
            "port": smtp_port,
            "use_tls": self.smtp_tls_checkbox.isChecked(),
            "username": self.smtp_username_input.text().strip(),
            "from_address": self.smtp_from_input.text().strip(),
            "to_addresses": [address.strip() for address in self.smtp_to_input.text().split(",") if address.strip()],
            "threshold_percent": threshold_percent,
            "cooldown_minutes": cooldown_minutes,
            "refresh_interval_ms": refresh_interval_ms,
            "log_dedupe_seconds": log_dedupe_seconds,
        }
        smtp_password = self.smtp_password_input.text()
        if smtp_password:
            email_alerts["password"] = smtp_password

        if not new_configs:
            self.status_label.setText("Error: Add at least one LPAR before saving.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            return

        logs_root = self.logs_root_input.text().strip()
        if not logs_root:
            self.status_label.setText("Error: Choose a log storage folder before saving.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            return

        remember_credentials = self.remember_creds_checkbox.isChecked()
        login_username = self.user_input.text().strip()
        login_password = self.pass_input.text()
        if remember_credentials and login_username and login_password:
            save_ibmi_password(login_username, login_password)
        elif not remember_credentials and login_username:
            save_ibmi_password(login_username, "")
        if not save_all_configs(
            new_configs,
            new_subsystems,
            new_ports,
            expected_web_apps=new_web_apps,
            email_alerts=email_alerts,
            login_credentials={"remember": remember_credentials, "username": login_username},
            log_root=logs_root,
        ):
            SYSTEM_LOGGER.error("Settings save failed")
            self.status_label.setText("Error: Save failed. Please try again.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            return

        self.active_server_configs.clear()
        self.active_server_configs.update(new_configs)
        self.refresh_interval_ms = refresh_interval_ms
        self.backup_management_widget.server_configs = self.active_server_configs
        SERVER_CONFIGS.clear(); SERVER_CONFIGS.update(new_configs)
        EXPECTED_SUBSYSTEMS.clear(); EXPECTED_SUBSYSTEMS.update(new_subsystems)
        EXPECTED_PORTS.clear(); EXPECTED_PORTS.update(new_ports)
        EXPECTED_WEB_APPS.clear(); EXPECTED_WEB_APPS.update(new_web_apps)
        self.rebuild_server_cards()
        self.status_label.setText("Status: Settings saved and applied.")
        self.status_label.setStyleSheet("color: #2ea043; font-weight: bold; font-size: 11px; background-color: transparent;")
        SYSTEM_LOGGER.info("Settings saved and applied")
        self._settings_saved_snapshot = self._capture_settings_snapshot()
        self._update_settings_save_state()
        show_information_dialog(self, "Settings Applied", "Settings were saved and applied successfully.")

    def _update_start_controls_state(self):
        enabled = self.credentials_validated and bool(self.active_server_configs)
        if hasattr(self, "top_start_btn"):
            self.top_start_btn.setEnabled(enabled)
            if self.is_monitoring:
                self.top_start_btn.setStyleSheet(
                    "QPushButton {"
                    "  background-color: #21262d;"
                    "  color: #f85149;"
                    "  border: 1px solid #30363d;"
                    "  font-weight: bold;"
                    "  padding: 5px 8px;"
                    "  border-radius: 6px;"
                    "  opacity: 1;"
                    "}"
                    "QPushButton:hover {"
                    "  background-color: #361718;"
                    "  border-color: #f85149;"
                    "}"
                    "QPushButton:disabled {"
                    "  background-color: #6b7280;"
                    "  color: #e5e7eb;"
                    "  border: 1px solid #6b7280;"
                    "  opacity: 0.7;"
                    "}"
                    "QPushButton:disabled:hover {"
                    "  background-color: #6b7280;"
                    "  border-color: #6b7280;"
                    "}"
                )
            else:
                self.top_start_btn.setStyleSheet(
                    "QPushButton {"
                    "  background-color: #238636;"
                    "  color: #ffffff;"
                    "  border: 1px solid #2ea043;"
                    "  border-radius: 6px;"
                    "  font-weight: bold;"
                    "  font-size: 8pt;"
                    "  padding: 5px 8px;"
                    "  opacity: 1;"
                    "}"
                    "QPushButton:hover {"
                    "  background-color: #2ea043;"
                    "}"
                    "QPushButton:disabled {"
                    "  background-color: #6b7280;"
                    "  color: #e5e7eb;"
                    "  border: 1px solid #6b7280;"
                    "  opacity: 0.7;"
                    "}"
                    "QPushButton:disabled:hover {"
                    "  background-color: #6b7280;"
                    "  border-color: #6b7280;"
                    "}"
                )

    def validate_login_credentials(self):
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        if not username or not password:
            return {}

        if not self.active_server_configs:
            return {}

        if not self._credential_vpn_preflight():
            return {}

        result = CredentialCheckThread.validate_server_credentials(
            self.active_server_configs,
            username,
            password,
            timeout_seconds=CREDENTIAL_CHECK_TIMEOUT_SECONDS,
        )
        self._credential_check_results = result
        self._handle_credential_check_results(result)
        return result

    def _credential_vpn_preflight(self):
        if has_vpn_ip():
            return True

        message = "VPN connection not detected. Connect to the VPN and confirm your IPv4 address starts with 10.212."
        SYSTEM_LOGGER.warning("Credential validation skipped because the required VPN was unavailable")
        self.credentials_validated = False
        self.status_label.setText(f"Warning: {message}")
        self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
        self._update_start_controls_state()
        show_warning_dialog(self, "Check VPN Network", message)
        return False

    def _update_credential_countdown(self):
        deadline = getattr(self, "_credential_check_deadline", None)
        if deadline is None:
            return

        remaining = max(0, int(deadline - time.monotonic()))
        self.status_label.setText(f"Status: Checking credentials... {remaining}s remaining")
        self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")

    def _start_credential_countdown(self):
        self._credential_check_deadline = time.monotonic() + CREDENTIAL_CHECK_TIMEOUT_SECONDS
        if hasattr(self, "_credential_check_countdown_timer") and self._credential_check_countdown_timer is not None:
            self._credential_check_countdown_timer.stop()
            self._credential_check_countdown_timer.deleteLater()

        self._credential_check_countdown_timer = QTimer(self)
        self._credential_check_countdown_timer.setInterval(1000)
        self._credential_check_countdown_timer.timeout.connect(self._update_credential_countdown)
        self._credential_check_countdown_timer.start()
        self._update_credential_countdown()

    def _stop_credential_countdown(self):
        self._credential_check_deadline = None
        timer = getattr(self, "_credential_check_countdown_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self._credential_check_countdown_timer = None

    def validate_login_credentials_async(self):
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.setText("Checking...")
        self.status_label.setText("Status: Checking credentials...")
        self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")

        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        if not username or not password:
            self.toggle_btn.setEnabled(True)
            self.toggle_btn.setText("Check Login Creds")
            self.status_label.setText("Error: Please enter both Username and Password.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            return {}

        if not self.active_server_configs:
            self.toggle_btn.setEnabled(True)
            self.toggle_btn.setText("Check Login Creds")
            self.status_label.setText("Error: Configure at least one LPAR before validating credentials.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            return {}

        if not self._credential_vpn_preflight():
            self.toggle_btn.setEnabled(True)
            self.toggle_btn.setText("Check Login Creds")
            return {}

        SYSTEM_LOGGER.info(
            "Credential validation started for %d configured LPAR(s)",
            len(self.active_server_configs),
        )
        self.credentials_validated = False
        self._credential_check_results = {}
        self._start_credential_countdown()
        self.credential_check_thread = CredentialCheckThread(
            self.active_server_configs,
            username,
            password,
            timeout_seconds=CREDENTIAL_CHECK_TIMEOUT_SECONDS,
        )
        self.credential_check_thread.results_ready.connect(self._handle_credential_check_results)
        self.credential_check_thread.finished.connect(self._on_credential_check_finished)
        self.credential_check_thread.start()
        return self._credential_check_results

    def _on_credential_check_finished(self):
        self._stop_credential_countdown()
        if hasattr(self, "credential_check_thread"):
            self.credential_check_thread.deleteLater()
            self.credential_check_thread = None

    def _handle_credential_check_results(self, results):
        self._credential_check_results = results
        ok_servers = []
        auth_error_servers = []
        for server_name, result in results.items():
            if result.get("status") == "OK":
                ok_servers.append(server_name)
                SYSTEM_LOGGER.info("Credential validation succeeded for LPAR %s", server_name)
            else:
                auth_error_servers.append(server_name)
                SYSTEM_LOGGER.warning("Credential validation failed for LPAR %s", server_name)

        if ok_servers and not auth_error_servers:
            details = ", ".join(ok_servers)
            self.credentials_validated = True
            self.status_label.setText(f"Login check passed for: {details}.")
            self.status_label.setStyleSheet("color: #2ea043; font-weight: bold; font-size: 11px; background-color: transparent;")
        elif auth_error_servers and not ok_servers:
            details = ", ".join(auth_error_servers)
            self.credentials_validated = False
            self.status_label.setText(f"Authentication failed for: {details}. Check credentials or user profile.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
        else:
            ok_details = ", ".join(ok_servers)
            auth_details = ", ".join(auth_error_servers)
            self.credentials_validated = bool(ok_servers)
            self.status_label.setText(
                f"Login check results: OK on {ok_details}; Authentication failed on {auth_details}."
            )
            self.status_label.setStyleSheet("color: #e3b341; font-weight: bold; font-size: 11px; background-color: transparent;")

        self._update_start_controls_state()
        self.toggle_btn.setEnabled(True)
        self.toggle_btn.setText("Check Login Creds")
        if self.credentials_validated:
            self.backup_management_widget.start_hourly_refresh()

    def _show_sync_loading(self, message="Syncing data..."):
        self.status_label.setText(f"Status: {message}")
        self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")

    def _hide_sync_loading(self):
        if self.is_monitoring:
            self.status_label.setText("Status: Live Metrics Updated. Cards refresh independently...")
            self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")
        else:
            self.status_label.setText("Status: Monitoring stopped. Credentials unlocked for editing.")
            self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")

    def _schedule_log_history_refresh(self):
        if self.log_viewer_widget is not None:
            self._log_history_refresh_timer.start()

    def _refresh_log_history_after_results(self):
        if self.content_stack.currentWidget() is not self.logs_analytics_widget:
            return
        live_server_names = {
            str(data.get("server") or key): {}
            for key, data in self.latest_results_cache.items()
        }
        if live_server_names:
            self.log_viewer_widget.load_log_history(
                live_server_names,
                silent=True,
            )

    def post_init_tasks(self):
        """Perform non-blocking operations after UI layout is painted."""
        QTimer.singleShot(1200, self._finish_startup_loading)

    def _finish_startup_loading(self):
        if hasattr(self, "startup_loading_overlay"):
            self.startup_loading_overlay.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "startup_loading_overlay"):
            self.startup_loading_overlay.setGeometry(self.rect())

    def apply_theme_state(self):
        page_background = "#171819" if self.is_dark_theme else "#f6f8fa"
        page_header_surface = "#202225" if self.is_dark_theme else "#ffffff"
        page_header_text = "#e1e3e6" if self.is_dark_theme else "#1f2328"
        page_header_border = "#34383d" if self.is_dark_theme else "#d0d7de"
        for page in (
            self.live_monitor_widget,
            self.logs_analytics_widget,
            self.backup_management_widget,
            self.settings_widget,
        ):
            page.setStyleSheet(f"""
                QWidget {{
                    background-color: {page_background};
                }}
                QFrame#pageHeader {{
                    background-color: {page_header_surface};
                    border: none;
                }}
                QLabel#pageHeaderTitle {{
                    color: {page_header_text};
                    background-color: transparent;
                }}
                QFrame#pageHeaderDivider {{
                    max-height: 1px;
                    background-color: {page_header_border};
                    border: none;
                }}
            """)
        if hasattr(self, "logs_analytics_tabs"):
            tab_muted = "#a4a8ae" if self.is_dark_theme else "#57606a"
            tab_border = "#34383d" if self.is_dark_theme else "#d0d7de"
            tab_accent = "#79aaff" if self.is_dark_theme else "#0969da"
            hover = "#2a2c2f" if self.is_dark_theme else "#eaeef2"
            self.logs_analytics_tabs.setStyleSheet(f"""
                QTabWidget#logsAnalyticsTabs::pane {{
                    background-color: {page_background};
                    border: none;
                    border-top: 1px solid {tab_border};
                }}
                QTabWidget#logsAnalyticsTabs QTabBar {{
                    background-color: {page_background};
                    border: none;
                }}
                QTabWidget#logsAnalyticsTabs QTabBar::tab {{
                    background: transparent;
                    color: {tab_muted};
                    border: none;
                    border-bottom: 2px solid transparent;
                    padding: 12px 18px;
                    margin-right: 8px;
                    font-size: 10pt;
                    font-weight: 600;
                }}
                QTabWidget#logsAnalyticsTabs QTabBar::tab:selected {{
                    color: {tab_accent};
                    border-bottom: 2px solid {tab_accent};
                }}
                QTabWidget#logsAnalyticsTabs QTabBar::tab:hover:!selected {{
                    color: {tab_accent};
                    background-color: {hover};
                }}
            """)
            self._refresh_analytics_tab_icons()
        self._apply_settings_theme()

        title_color = "#ffffff" if self.is_dark_theme else "#1f2328"
        if hasattr(self, "cards_title"):
            self.cards_title.setStyleSheet(f"color: {title_color}; background-color: transparent;")
        self.global_alerts.set_theme(self.is_dark_theme)
        self.refresh_widget.set_theme(self.is_dark_theme)
        if hasattr(self, "log_viewer_widget"):
            self.log_viewer_widget.set_theme(self.is_dark_theme)
        if hasattr(self, 'monthly_report_widget'):
            self.monthly_report_widget.set_theme(self.is_dark_theme)
        if hasattr(self, "theme_btn"):
            self.theme_btn.setText("☀ Light Theme" if self.is_dark_theme else "🌙 Dark Theme")
            self.info_btn.setStyleSheet(
                "QPushButton {"
                "  background-color: transparent;"
                "  color: #f0f6fc;"
                "  border: 1px solid #30363d;"
                "  border-radius: 8px;"
                "  font-size: 17px;"
                "  font-weight: bold;"
                "  padding: 0;"
                "}"
                "QPushButton:hover {"
                "  background-color: #21262d;"
                "  border-color: #58a6ff;"
                "}"
                if self.is_dark_theme else
                "QPushButton {"
                "  background-color: transparent;"
                "  color: #1f2328;"
                "  border: 1px solid #d0d7de;"
                "  border-radius: 8px;"
                "  font-size: 17px;"
                "  font-weight: bold;"
                "  padding: 0;"
                "}"
                "QPushButton:hover {"
                "  background-color: #f3f4f6;"
                "  border-color: #0969da;"
                "}"
            )
            self.info_btn.setText("🛈")
            self.update_toggle_button_style()
        if hasattr(self, "nav_buttons"):
            self._update_nav_button_styles(self._current_nav_key())

    def init_live_monitor_ui(self):
        main_layout = QVBoxLayout(self.live_monitor_widget)
        main_layout.setContentsMargins(0, 0, 0, 14)
        main_layout.setSpacing(0)

        dashboard_header, self.header_title = self._create_page_header(
            self.live_monitor_widget,
            "Dashboard Active",
        )
        main_layout.addWidget(dashboard_header)
        dashboard_divider = QFrame(self.live_monitor_widget)
        dashboard_divider.setObjectName("pageHeaderDivider")
        dashboard_divider.setFrameShape(QFrame.Shape.HLine)
        main_layout.addWidget(dashboard_divider)
        dashboard_content = QWidget(self.live_monitor_widget)
        dashboard_content_layout = QVBoxLayout(dashboard_content)
        dashboard_content_layout.setContentsMargins(14, 10, 14, 0)
        dashboard_content_layout.setSpacing(4)
        main_layout.addWidget(dashboard_content, stretch=1)

        top_bar_layout = QHBoxLayout()
        top_bar_layout.setSpacing(10)

        self.global_alerts = GlobalAlertsWidget()
        self.global_alerts.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_bar_layout.addWidget(self.global_alerts, stretch=1)

        self.refresh_widget = RefreshStatusWidget()
        self.refresh_widget.setFixedHeight(85)
        top_bar_layout.addWidget(self.refresh_widget, stretch=0)

        self.retry_status_label = QLabel("")
        self.retry_status_label.setStyleSheet("color: #8b949e; font-size: 10px; background-color: transparent;")
        self.retry_status_label.setVisible(False)
        dashboard_content_layout.addWidget(self.retry_status_label)

        self.top_start_btn = QPushButton("Start Auto-Refresh")
        self.top_start_btn.setFixedHeight(35)
        self.top_start_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.top_start_btn.setToolTip("Start or stop automated refresh")
        self.top_start_btn.clicked.connect(self.toggle_monitoring)
        self.top_start_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: #238636;"
            "  color: #ffffff;"
            "  border: 1px solid #2ea043;"
            "  border-radius: 8px;"
            "  font-size: 11px;"
            "  font-weight: bold;"
            "  padding: 0 12px;"
            "}"
            "QPushButton:hover {"
            "  background-color: #2ea043;"
            "}"
        )
        top_bar_layout.addWidget(self.top_start_btn, stretch=0, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.info_btn = QPushButton("🛈")
        self.info_btn.setFixedSize(36, 35)
        self.info_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.info_btn.setToolTip("App information")
        self.info_btn.clicked.connect(self.show_app_info)
        self.info_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: transparent;"
            "  color: #f0f6fc;"
            "  border: 1px solid #30363d;"
            "  border-radius: 8px;"
            "  font-size: 17px;"
            "  font-weight: bold;"
            "  padding: 0;"
            "}"
            "QPushButton:hover {"
            "  background-color: #21262d;"
            "  border-color: #58a6ff;"
            "}"
        )
        top_bar_layout.addWidget(self.info_btn, stretch=0, alignment=Qt.AlignmentFlag.AlignVCenter)

        dashboard_content_layout.addLayout(top_bar_layout)

        self.status_label = QLabel("Status: Idle. Enter credentials and click 'Check Login Creds'.")
        self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")
        dashboard_content_layout.addWidget(self.status_label)

        filter_bar_layout = QHBoxLayout()
        filter_bar_layout.setSpacing(10)

        self.cards_title = QLabel("Server Health Cards")
        self.cards_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        filter_bar_layout.addWidget(self.cards_title)

        filter_bar_layout.addStretch()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Filter by server name...")
        self.search_input.setFixedWidth(200)
        self.search_input.textChanged.connect(self.filter_and_sort_cards)
        filter_bar_layout.addWidget(self.search_input)

        self.status_filter_combo = QComboBox()
        self.status_filter_combo.addItems(["Filter: All Statuses", "Filter: Critical Only", "Filter: Online Only", "Filter: Offline Only"])
        self.status_filter_combo.currentIndexChanged.connect(self.filter_and_sort_cards)
        filter_bar_layout.addWidget(self.status_filter_combo)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Sort: Name (A-Z)", "Sort: CPU High-to-Low", "Sort: ASP High-to-Low", "Sort: Status Critical First"])
        self.sort_combo.currentIndexChanged.connect(self.filter_and_sort_cards)
        filter_bar_layout.addWidget(self.sort_combo)

        self.group_combo = QComboBox()
        self.group_combo.addItems(["Grouping: Prefix", "Grouping: Status", "Grouping: None (Grid)"])
        self.group_combo.currentIndexChanged.connect(self.filter_and_sort_cards)
        filter_bar_layout.addWidget(self.group_combo)

        dashboard_content_layout.addLayout(filter_bar_layout)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setStyleSheet("QScrollArea { background-color: transparent; }")

        scroll_content = QWidget()
        scroll_content.setMinimumWidth(0)
        scroll_content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.cards_grid = QGridLayout(scroll_content)
        self.cards_grid.setSpacing(10)
        self.cards_grid.setContentsMargins(2, 2, 2, 2)

        self.rebuild_server_cards()

        scroll_area.setWidget(scroll_content)
        dashboard_content_layout.addWidget(scroll_area, stretch=1)
        self._update_ping_indicator()

    def _latency_color_for_ms(self, latency_ms):
        if latency_ms is None:
            return "#8b949e"
        if latency_ms <= 100:
            return "#2ea043"
        if latency_ms <= 250:
            return "#e3b341"
        return "#f85149"

    def _measure_gateway_ping_ms(self, target_ip=PING_TARGET_IP):
        if sys.platform.startswith("win"):
            command = ["ping", "-n", "1", "-w", "1200", target_ip]
        else:
            command = ["ping", "-c", "1", "-W", "1.2", target_ip]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        match = re.search(r"time[<>= ]+(\d+(?:\.\d+)?)\s*ms", output, re.IGNORECASE)
        if match:
            return int(round(float(match.group(1))))
        return None

    def _update_ping_indicator(self):
        if not hasattr(self, "ping_status_label"):
            return None

        self._request_gateway_ping()
        latency_ms = self._last_gateway_latency_ms
        if latency_ms is None:
            text = "No ping"
            color = "#8b949e"
        else:
            text = f"{latency_ms} ms"
            color = self._latency_color_for_ms(latency_ms)

        self.ping_status_label.setText(text)
        self.ping_status_label.setStyleSheet(
            "QLabel {"
            f"  color: {color};"
            "  font-weight: bold;"
            "  font-size: 10px;"
            "  background-color: rgba(15, 23, 42, 0.78);"
            "  border: 1px solid rgba(148, 163, 184, 0.35);"
            "  border-radius: 14px;"
            "  padding: 0 10px;"
            "}"
        )
        self.ping_status_label.setToolTip(f"Gateway ping: {text} to {PING_TARGET_IP}")
        return latency_ms

    def _request_gateway_ping(self, force=False):
        if self._gateway_ping_pending:
            return

        now = time.monotonic()
        if not force and now - self._gateway_ping_started_at < GATEWAY_PING_INTERVAL_MS / 1000:
            return

        runnable = GatewayPingRunnable(self._measure_gateway_ping_ms)
        runnable.signals.result_ready.connect(self._on_gateway_ping_finished)
        self._gateway_ping_runnable = runnable
        self._gateway_ping_pending = True
        self._gateway_ping_started_at = now
        _ACTIVE_GATEWAY_PING_RUNNABLES.add(runnable)
        self.gateway_ping_thread_pool.start(runnable)

    def _on_gateway_ping_finished(self, latency_ms):
        self._gateway_ping_pending = False
        self._gateway_ping_runnable = None
        self._last_gateway_latency_ms = latency_ms
        self._last_gateway_ping_at = time.monotonic()
        self._update_ping_indicator()

        if latency_ms is None:
            if not (self._pending_monitoring_start or self.is_monitoring):
                self._gateway_ping_attempts = 0
                return

            self._gateway_ping_attempts += 1
            if self._gateway_ping_attempts < GATEWAY_PING_MAX_ATTEMPTS:
                next_attempt = self._gateway_ping_attempts + 1
                self.status_label.setText(
                    f"Warning: Gateway ping failed. Reconnecting "
                    f"(attempt {next_attempt} of {GATEWAY_PING_MAX_ATTEMPTS})..."
                )
                self.status_label.setStyleSheet(
                    "color: #e3b341; font-weight: bold; font-size: 11px; background-color: transparent;"
                )
                self._request_gateway_ping(force=True)
                return

            self._gateway_ping_attempts = 0
            if self._pending_monitoring_start:
                self._pending_monitoring_start = False
                self._show_vpn_connection_warning()
            else:
                self._handle_vpn_disconnect()
            return

        self._gateway_ping_attempts = 0
        if self._pending_monitoring_start:
            self._pending_monitoring_start = False
            self.start_monitoring()

    def _check_monitoring_gateway(self):
        if self.is_monitoring:
            self._request_gateway_ping(force=True)

    def _show_vpn_connection_warning(self):
        message = "Gateway latency is unavailable. Check your VPN connection before monitoring."
        self.status_label.setText(f"Warning: {message}")
        self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
        show_warning_dialog(self, "VPN Connection Required", message)

    def _handle_vpn_disconnect(self):
        if self._vpn_disconnect_alerted:
            return

        self._vpn_disconnect_alerted = True
        self.stop_monitoring()
        self.status_label.setText("Warning: Gateway ping failed. Auto-refresh stopped. Check your VPN connection.")
        self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
        show_warning_dialog(
            self,
            "VPN Connection Lost",
            "Gateway ping failed. Auto-refresh has stopped. Check your VPN connection, then restart monitoring.",
        )

    def _refresh_network_available(self):
        if has_vpn_ip():
            return True
        ping_is_fresh = time.monotonic() - self._last_gateway_ping_at <= GATEWAY_PING_MAX_AGE_SECONDS
        if self._last_gateway_latency_ms is not None and ping_is_fresh:
            return True
        self._request_gateway_ping()
        return False

    def _server_refresh_interval_ms(self, server_name, result=None):
        base = self.refresh_interval_ms
        retry_count = self.server_retry_counts.get(server_name, 0)
        if retry_count > 0:
            base = min(60000, max(8000, base + retry_count * 5000))
        if result is not None:
            status = str(result.get("status", "OFFLINE")).upper()
            if status in ("OFFLINE", "AUTH_ERROR"):
                base = min(base, 20000)
        return max(0, int(base))

    def _next_retry_delay_ms(self):
        if not self.latest_results_cache:
            return max(8000, self.refresh_interval_ms)
        candidate_intervals = []
        for server_name, result in self.latest_results_cache.items():
            candidate_intervals.append(self._server_refresh_interval_ms(server_name, result))
        return max(8000, min(candidate_intervals)) if candidate_intervals else max(8000, self.refresh_interval_ms)

    def _gateway_ping_reachable(self):
        return (
            self._last_gateway_latency_ms is not None
            and time.monotonic() - self._last_gateway_ping_at <= GATEWAY_PING_MAX_AGE_SECONDS
        )

    def _register_server_result(self, server_name, result):
        status = str(result.get("status", "OFFLINE")).upper()
        if status == "OFFLINE" and self._gateway_ping_reachable():
            status = "STALE"
            result["status"] = status
            result["error"] = str(result.get("error") or "No fresh server data")

        if result.get("sync_duration_ms") is not None:
            sync_duration_ms = int(result.get("sync_duration_ms"))
        elif self.last_refresh_started_at is not None:
            sync_duration_ms = max(0, int((time.monotonic() - self.last_refresh_started_at) * 1000))
        else:
            sync_duration_ms = 0
        self.server_sync_durations_ms[server_name] = sync_duration_ms
        if status in ("ONLINE", "DEGRADED"):
            self.server_retry_counts.pop(server_name, None)
            self.server_last_success[server_name] = time.monotonic()
            self.server_error_reasons.pop(server_name, None)
        elif status in ("OFFLINE", "AUTH_ERROR", "STALE"):
            self.server_retry_counts[server_name] = self.server_retry_counts.get(server_name, 0) + 1
            self.server_last_success.setdefault(server_name, time.monotonic())
            self.server_error_reasons[server_name] = str(result.get("error") or status)

    def _refresh_global_status_summary(self):
        self.global_alerts.update_summary(
            list(self.latest_results_cache.values()),
            total_lpars=len(self.active_server_configs),
        )
        stale_count = sum(
            1 for server_name, card in self.card_widgets.items()
            if getattr(card, "current_status", "") == "STALE"
        )
        self.global_alerts.set_stale_count(stale_count)

        retry_delay_ms = self._next_retry_delay_ms()
        all_unreachable = bool(self.latest_results_cache) and all(
            str(data.get("status", "OFFLINE")).upper() == "OFFLINE"
            for data in self.latest_results_cache.values()
        )
        auth_error_systems = [
            srv for srv, data in self.latest_results_cache.items()
            if str(data.get("status", "OFFLINE")).upper() == "AUTH_ERROR"
        ]

        self._update_ping_indicator()

        if self.auto_refresh_paused:
            self.status_label.setText("Status: Auto-refresh paused. Resume when you are ready.")
            self.status_label.setStyleSheet("color: #e3b341; font-weight: bold; font-size: 11px; background-color: transparent;")
            self.retry_status_label.setVisible(False)
        elif all_unreachable:
            self.status_label.setText("⚠️ Network unreachable on all LPARs. Check your VPN connection.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            self.retry_status_label.setText(f"Retrying in {max(5, retry_delay_ms // 1000)}s")
            self.retry_status_label.setVisible(True)
        elif auth_error_systems:
            err_servers_str = ", ".join(auth_error_systems)
            self.status_label.setText(
                f"Error: Authentication failed / User profile disabled on: {err_servers_str}. Retrying in {max(5, retry_delay_ms // 1000)}s..."
            )
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            self.retry_status_label.setText(f"Retry backoff: {max(5, retry_delay_ms // 1000)}s")
            self.retry_status_label.setVisible(True)
        elif self.is_monitoring:
            self.status_label.setText("Status: Live Metrics Updated. Cards refresh independently...")
            self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")
            self.retry_status_label.setVisible(False)
        else:
            self.status_label.setText("Status: Monitoring stopped. Credentials unlocked for editing.")
            self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")
            self.retry_status_label.setVisible(False)

    def update_stale_server_states(self):
        now = time.monotonic()
        for server_name, card in self.card_widgets.items():
            if not hasattr(card, "current_status"):
                continue
            last_success = self.server_last_success.get(server_name)
            if last_success is None:
                if getattr(card, "current_status", "OFFLINE") not in ("OFFLINE", "AUTH_ERROR", "STALE"):
                    card.set_status("STALE")
                continue
            stale = (now - last_success) > self.stale_after_seconds
            if stale:
                card.set_status("STALE")
            elif getattr(card, "current_status", "OFFLINE") == "STALE":
                card.set_status("ONLINE")

    def filter_and_sort_cards(self):
        query = self.search_input.text().strip().lower()
        status_filter = self.status_filter_combo.currentIndex()
        sort_mode = self.sort_combo.currentIndex()
        group_mode = self.group_combo.currentIndex()

        filtered_servers = []
        for srv, card in self.card_widgets.items():
            if query and query not in srv.lower() and query not in card.server_name.lower():
                continue

            if status_filter == 1 and not card.current_is_critical:
                continue
            elif status_filter == 2 and card.current_status not in ("ONLINE", "DEGRADED", "SYNCING"):
                continue
            elif status_filter == 3 and card.current_status not in ("OFFLINE", "AUTH_ERROR", "STALE"):
                continue

            filtered_servers.append(srv)

        if sort_mode == 0:
            filtered_servers.sort(key=lambda s: self.card_widgets[s].server_name.lower())
        elif sort_mode == 1:
            filtered_servers.sort(key=lambda s: (self.card_widgets[s].current_cpu, self.card_widgets[s].server_name.lower()), reverse=True)
        elif sort_mode == 2:
            filtered_servers.sort(key=lambda s: (self.card_widgets[s].current_asp, self.card_widgets[s].server_name.lower()), reverse=True)
        elif sort_mode == 3:
            filtered_servers.sort(key=lambda s: (not self.card_widgets[s].current_is_critical, self.card_widgets[s].server_name.lower()))

        layout_signature = (
            query,
            status_filter,
            sort_mode,
            group_mode,
            tuple(filtered_servers),
        )
        if self._last_card_layout_signature == layout_signature:
            return
        self._last_card_layout_signature = layout_signature

        while self.cards_grid.count():
            item = self.cards_grid.takeAt(0)
            if item is not None:
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)

        cols = 4
        current_row = 0

        if group_mode == 0:
            groups = {}
            for srv in filtered_servers:
                display_name = self.card_widgets[srv].server_name
                prefix = "".join([c for c in display_name if not c.isdigit()]) or "OTHER"
                groups.setdefault(prefix, []).append(srv)

            if len(groups) <= 1:
                for idx, srv in enumerate(filtered_servers):
                    r = idx // cols
                    c = idx % cols
                    self.cards_grid.addWidget(self.card_widgets[srv], r, c, Qt.AlignmentFlag.AlignTop)
            else:
                for group_name, srv_list in sorted(groups.items()):
                    group_lbl = QLabel(f"📁 {group_name} Environment ({len(srv_list)})")
                    group_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
                    group_lbl.setStyleSheet("color: #388bfd; font-weight: bold; margin-top: 6px; background: transparent;")
                    self.cards_grid.addWidget(group_lbl, current_row, 0, 1, cols)
                    current_row += 1

                    for idx, srv in enumerate(srv_list):
                        r = current_row + (idx // cols)
                        c = idx % cols
                        self.cards_grid.addWidget(self.card_widgets[srv], r, c, Qt.AlignmentFlag.AlignTop)
                    current_row += (len(srv_list) + cols - 1) // cols

        elif group_mode == 1:
            groups = {"Critical / Offline": [], "Healthy Online": []}
            for srv in filtered_servers:
                card = self.card_widgets[srv]
                if card.current_is_critical or card.current_status in ("OFFLINE", "AUTH_ERROR"):
                    groups["Critical / Offline"].append(srv)
                else:
                    groups["Healthy Online"].append(srv)

            visible_groups = {k: v for k, v in groups.items() if v}
            if len(visible_groups) <= 1:
                for idx, srv in enumerate(filtered_servers):
                    r = idx // cols
                    c = idx % cols
                    self.cards_grid.addWidget(self.card_widgets[srv], r, c, Qt.AlignmentFlag.AlignTop)
            else:
                for group_name, srv_list in groups.items():
                    if not srv_list:
                        continue
                    group_lbl = QLabel(f"🔴 {group_name}" if "Critical" in group_name else f"🟢 {group_name}")
                    group_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
                    group_lbl.setStyleSheet("color: #388bfd; font-weight: bold; margin-top: 6px; background: transparent;")
                    self.cards_grid.addWidget(group_lbl, current_row, 0, 1, cols)
                    current_row += 1

                    for idx, srv in enumerate(srv_list):
                        r = current_row + (idx // cols)
                        c = idx % cols
                        self.cards_grid.addWidget(self.card_widgets[srv], r, c, Qt.AlignmentFlag.AlignTop)
                    current_row += (len(srv_list) + cols - 1) // cols

        else:
            for idx, srv in enumerate(filtered_servers):
                r = idx // cols
                c = idx % cols
                self.cards_grid.addWidget(self.card_widgets[srv], r, c, Qt.AlignmentFlag.AlignTop)

        for c in range(cols):
            self.cards_grid.setColumnStretch(c, 1)

    def toggle_monitoring(self):
        if not self.is_monitoring:
            self.start_monitoring()
        else:
            self.stop_monitoring()

    def _apply_theme_toggle(self, new_theme, loading_dialog=None):
        app = QApplication.instance()
        if app is None:
            self._theme_loading_dialog = None
            return

        self.setUpdatesEnabled(False)
        try:
            self.is_dark_theme = new_theme
            app.setProperty("is_dark_theme", self.is_dark_theme)
            stylesheet = DARK_STYLESHEET if self.is_dark_theme else LIGHT_STYLESHEET
            cast(QApplication, app).setStyleSheet(stylesheet)

            for card in self.card_widgets.values():
                card.set_theme(self.is_dark_theme)

            self.apply_theme_state()
        finally:
            self.setUpdatesEnabled(True)
            self.repaint()
            if loading_dialog is not None:
                loading_dialog.close()
            self._theme_loading_dialog = None

    def toggle_theme(self):
        app = QApplication.instance()
        if app is None or self._theme_loading_dialog is not None:
            return

        loading_dialog = ThemeLoadingDialog(self)
        self._theme_loading_dialog = loading_dialog
        loading_dialog.move(
            self.geometry().center() - loading_dialog.rect().center()
        )
        loading_dialog.show()
        loading_dialog.raise_()
        QApplication.processEvents()

        target_theme = not self.is_dark_theme
        QTimer.singleShot(0, lambda: self._apply_theme_toggle(target_theme, loading_dialog))

    def show_app_info(self):
        dialog = AppInfoDialog(self.version_str, self)
        dialog.move(self.geometry().center() - dialog.rect().center())
        dialog.exec()

    def update_toggle_button_style(self):
        self._update_start_controls_state()

        if hasattr(self, "top_start_btn"):
            button = self.top_start_btn
            if self.is_monitoring:
                if self.is_dark_theme:
                    button.setStyleSheet("""
                        QPushButton {
                            background-color: #21262d; 
                            color: #f85149;
                            border: 1px solid #30363d; 
                            font-weight: bold; 
                            padding: 5px 8px;
                            border-radius: 6px;
                        }
                        QPushButton:hover { 
                            background-color: #361718; 
                            border-color: #f85149; 
                        }
                        QPushButton:disabled {
                            background-color: #6b7280;
                            color: #e5e7eb;
                            border: 1px solid #6b7280;
                            opacity: 0.7;
                        }
                        QPushButton:disabled:hover {
                            background-color: #6b7280;
                            border-color: #6b7280;
                        }
                    """)
                else:
                    button.setStyleSheet("""
                        QPushButton {
                            background-color: #c2410c; 
                            color: #ffffff;
                            border: 1px solid #9a3412; 
                            font-weight: bold; 
                            padding: 5px 8px;
                            border-radius: 6px;
                        }
                        QPushButton:hover { 
                            background-color: #ea580c; 
                            border-color: #c2410c; 
                        }
                        QPushButton:disabled {
                            background-color: #6b7280;
                            color: #e5e7eb;
                            border: 1px solid #6b7280;
                            opacity: 0.7;
                        }
                        QPushButton:disabled:hover {
                            background-color: #6b7280;
                            border-color: #6b7280;
                        }
                    """)
            else:
                button.setStyleSheet("""
                    QPushButton {
                        background-color: #238636; 
                        color: #ffffff;
                        border: 1px solid #2ea043; 
                        font-weight: bold; 
                        font-size: 8pt;
                        padding: 5px 8px;
                        border-radius: 6px;
                    }
                    QPushButton:hover { 
                        background-color: #2ea043; 
                    }
                    QPushButton:disabled {
                        background-color: #6b7280;
                        color: #e5e7eb;
                        border: 1px solid #6b7280;
                        opacity: 0.7;
                    }
                    QPushButton:disabled:hover {
                        background-color: #6b7280;
                        border-color: #6b7280;
                    }
                """)

            self.top_start_btn.setText("Stop Auto-Refresh" if self.is_monitoring else "Start Auto-Refresh")

    def open_lpar_settings(self):
        dialog = LparSettingsDialog(self.active_server_configs, self)
        if dialog.exec():
            self.active_server_configs = dialog.configs
             
            SERVER_CONFIGS.clear()
            SERVER_CONFIGS.update(self.active_server_configs)

            self.rebuild_server_cards()
            self.log_viewer_widget.load_log_history()

    def show_cards_sequentially(self):
        for index, card in enumerate(self.card_widgets.values()):
            card.hide()
            QTimer.singleShot(index * 80, lambda c=card: c.show())

    def show_server_details(self, server_name):
        existing_dialog = self.system_details_dialogs.get(server_name)
        if existing_dialog is not None:
            existing_dialog.raise_()
            existing_dialog.activateWindow()
            return

        dialog = SystemDetailsDialog(server_name, self)
        self.system_details_dialogs[server_name] = dialog
        dialog.finished.connect(
            lambda _result, key=server_name, current=dialog:
                self._discard_system_details_dialog(key, current)
        )

        username = self.user_input.text().strip()
        password = self.pass_input.text()
        cfg = self.active_server_configs.get(server_name)
        if not username or not password:
            dialog.set_result({
                "error": "Enter your IBM i username and password to load system details."
            })
            dialog.show()
            return
        if cfg is None:
            dialog.set_result({"error": "Server configuration is no longer available."})
            dialog.show()
            return

        runnable = SystemDetailsRunnable(
            server_name,
            cfg,
            username,
            password,
            signal_parent=self,
        )
        runnable.signals.details_ready.connect(self._handle_system_details_result)
        self.system_details_runnables[server_name] = runnable
        dialog.show()
        self.thread_pool.start(runnable)

    def _discard_system_details_dialog(self, server_name, dialog):
        if self.system_details_dialogs.get(server_name) is dialog:
            self.system_details_dialogs.pop(server_name, None)

    def _handle_system_details_result(self, result):
        server_name = result.get("config_key")
        self.system_details_runnables.pop(server_name, None)
        dialog = self.system_details_dialogs.get(server_name)
        if dialog is not None:
            dialog.set_result(result)

    def rebuild_server_cards(self):
        while self.cards_grid.count():
            item = self.cards_grid.takeAt(0)
            if item is not None:
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)

        self.card_widgets.clear()
        self._last_card_layout_signature = None

        if not self.active_server_configs and SERVER_CONFIGS:
            self.active_server_configs = dict(SERVER_CONFIGS)

        servers = sorted(self.active_server_configs.keys())

        if not servers:
            empty_lbl = QLabel("No LPAR connections found. Click '⚙️ Settings' to configure servers.")
            empty_lbl.setFont(QFont("Segoe UI", 11))
            empty_lbl.setStyleSheet("color: #8b949e; margin: 20px; background-color: transparent;")
            self.cards_grid.addWidget(empty_lbl, 0, 0)
            return

        for idx, srv in enumerate(servers):
            card = LparCardWidget(srv)
            card.set_theme(self.is_dark_theme)
            card.server_name_clicked.connect(self.show_server_details)
            card.setVisible(False)
            self.card_widgets[srv] = card

        self.filter_and_sort_cards()
        self.show_cards_sequentially()

    def start_monitoring(self):
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        if not username or not password:
            self.status_label.setText("Error: Please enter both Username and Password.")
            self.status_label.setStyleSheet("color: #f85149; font-size: 11px; background-color: transparent;")
            return

        if not self.active_server_configs:
            self.status_label.setText("Error: Configure at least one LPAR before starting monitoring.")
            self.status_label.setStyleSheet("color: #f85149; font-size: 11px; background-color: transparent;")
            return

        if not self._refresh_network_available():
            self._pending_monitoring_start = True
            self.status_label.setText("Status: Checking VPN gateway latency before starting monitoring...")
            self.status_label.setStyleSheet("color: #e3b341; font-size: 11px; background-color: transparent;")
            self._request_gateway_ping(force=True)
            return

        self._pending_monitoring_start = False
        self._vpn_disconnect_alerted = False
        SYSTEM_LOGGER.info(
            "Monitoring started for %d configured LPAR(s)",
            len(self.active_server_configs),
        )
        self.refresh_generation += 1
        self.is_monitoring = True
        self.auto_refresh_paused = False
        self.gateway_ping_timer.start()
        self.refresh_countdown_timer.start()
        reset_asp_alert_sound()
        self._set_settings_editable(False)
        self.retry_status_label.setVisible(False)
        
        self.update_toggle_button_style()

        for card in self.card_widgets.values():
            card.set_status("CONNECTING")

        self.refresh_widget.set_active_state(True)
        self._show_sync_loading("Starting independent server refreshes...")
        self.fetch_data(force=True)

    def stop_monitoring(self):
        was_monitoring = self.is_monitoring
        self.is_monitoring = False
        self.auto_refresh_paused = False
        self._pending_monitoring_start = False
        self._gateway_ping_attempts = 0
        self.gateway_ping_timer.stop()
        self.refresh_countdown_timer.stop()
        self.refresh_generation += 1
        self._refresh_in_progress = False
        self._refresh_queued = False
        stop_asp_alert_sound()
        stop_all_server_status_alerts()
        for timer in self.server_refresh_timers.values():
            timer.stop()
        self.server_refresh_timers.clear()
        self.retry_status_label.setVisible(False)

        for runnable in self.active_runnables:
            runnable.cancel()
        self.active_runnables.clear()

        for card in self.card_widgets.values():
            card.set_refresh_countdown()
            card.set_status("STOPPED")
        self.global_alerts.update_summary(
            list(self.latest_results_cache.values()),
            total_lpars=len(self.active_server_configs),
        )

        self._set_settings_editable(True)
        
        self.update_toggle_button_style()
        self._hide_sync_loading()

        self.refresh_widget.set_active_state(False)
        self.status_label.setText("Status: Monitoring stopped. Credentials unlocked for editing.")
        self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")
        if was_monitoring:
            SYSTEM_LOGGER.info("Monitoring stopped")

    def _reschedule_server_timer(self, server_name, delay_ms=None):
        """Create or refresh the timer for a single server without leaving a dead timer behind."""
        if not self.is_monitoring or server_name not in self.active_server_configs:
            return

        if server_name in self.server_refresh_timers:
            self.server_refresh_timers[server_name].stop()
            self.server_refresh_timers[server_name].deleteLater()

        timer = QTimer(self)
        timer.setSingleShot(True)
        if delay_ms is None:
            if self.server_retry_counts.get(server_name, 0) > 0:
                delay_ms = self._next_retry_delay_ms()
            else:
                delay_ms = self.refresh_interval_ms
        timer.setInterval(delay_ms)
        timer.timeout.connect(lambda srv=server_name: self._refresh_single_server(srv))
        timer.start()
        self.server_refresh_timers[server_name] = timer
        self._update_card_refresh_countdowns()

    def _update_card_refresh_countdowns(self):
        if not self.is_monitoring:
            for card in self.card_widgets.values():
                card.set_refresh_countdown()
            return

        running_servers = {
            getattr(runnable, "server", None)
            for runnable in self.active_runnables
        }
        for server_name, card in self.card_widgets.items():
            timer = self.server_refresh_timers.get(server_name)
            if timer is not None and timer.isActive():
                remaining_ms = max(0, timer.remainingTime())
                card.set_refresh_countdown(math.ceil(remaining_ms / 1000))
            elif server_name in running_servers:
                card.set_refresh_countdown(refreshing=True)
            else:
                card.set_refresh_countdown()

    def _schedule_independent_refreshes(self):
        """Schedule independent per-server refresh timers based on fetch completion time."""
        for server_name in self.active_server_configs:
            base_delay = self.refresh_interval_ms
            if self.server_retry_counts.get(server_name, 0) > 0:
                base_delay = self._next_retry_delay_ms()
            self._reschedule_server_timer(server_name, base_delay)

    def _ensure_server_timers_alive(self):
        if not self.is_monitoring:
            return
        for server_name in list(self.active_server_configs.keys()):
            timer = self.server_refresh_timers.get(server_name)
            if timer is None or not timer.isActive():
                self._reschedule_server_timer(server_name)

    def _refresh_single_server(self, server_name, network_checked=False):
        """Refresh a single server independently."""
        if not self.is_monitoring or self.auto_refresh_paused:
            return
        
        if server_name not in self.active_server_configs:
            return

        if not network_checked and not self._refresh_network_available():
            self._reschedule_server_timer(server_name, self._next_retry_delay_ms())
            return
        
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()
        
        if not username or not password:
            return

        card = self.card_widgets.get(server_name)
        if card is not None:
            card.set_refresh_countdown(refreshing=True)
        
        cfg = self.active_server_configs[server_name]
        runnable = SingleLparRunnable(
            server_name,
            cfg,
            username,
            password,
            cancel_event=threading.Event(),
            signal_parent=self,
        )
        self.active_runnables.add(runnable)
        generation = self.refresh_generation
        runnable.signals.server_fetched.connect(
            lambda data, gen=generation, task=runnable, srv=server_name:
                self._on_single_server_fetched_independent(data, gen, task, srv)
        )
        runnable.signals.server_failed.connect(
            lambda data, gen=generation, task=runnable, srv=server_name:
                self._on_single_server_failed(data, gen, task, srv)
        )
        self.thread_pool.start(runnable)

    def _schedule_object_statistics_fetch(self, server_name, cfg, username, password):
        host = cfg.get("host", "") if isinstance(cfg, dict) else str(cfg)
        db = cfg.get("db", "*LOCAL") if isinstance(cfg, dict) else "*LOCAL"
        cache_key = (
            server_name,
            str(host).strip().lower(),
            str(db).strip().upper(),
            str(username).strip().upper(),
        )
        if cache_key in self.object_statistics_runnables:
            return

        card = self.card_widgets.get(server_name)
        now = time.monotonic()
        if now < self.object_statistics_next_fetch.get(cache_key, 0.0):
            cached = worker._get_cached_object_statistics(host, db, username)
            if cached is not None:
                if card is not None:
                    card.set_object_statistics(*cached)
                return

        runnable = ObjectStatisticsRunnable(
            server_name,
            cfg,
            username,
            password,
            signal_parent=self,
        )
        self.object_statistics_runnables[cache_key] = runnable
        runnable.signals.statistics_ready.connect(
            lambda data, key=cache_key, task=runnable:
                self._on_object_statistics_fetched(data, key, task)
        )
        self.object_statistics_thread_pool.start(runnable)

    def _on_object_statistics_fetched(self, data, cache_key, runnable):
        if self.object_statistics_runnables.get(cache_key) is runnable:
            self.object_statistics_runnables.pop(cache_key, None)
        self.object_statistics_next_fetch[cache_key] = (
            time.monotonic() + worker._OBJECT_STATISTICS_CACHE_TTL_SECONDS
        )
        config_key = data.get("config_key") or cache_key[0]
        card = self.card_widgets.get(config_key)
        if card is not None:
            card.set_object_statistics(
                data.get("object_statistics", []),
                str(data.get("object_statistics_error") or ""),
                bool(data.get("object_statistics_loaded", False)),
                str(data.get("object_statistics_updated_at") or ""),
            )

    def _schedule_temporary_storage_jobs_fetch(self, server_name, cfg, username, password):
        host = cfg.get("host", "") if isinstance(cfg, dict) else str(cfg)
        db = cfg.get("db", "*LOCAL") if isinstance(cfg, dict) else "*LOCAL"
        cache_key = (
            server_name,
            str(host).strip().lower(),
            str(db).strip().upper(),
            str(username).strip().upper(),
        )
        if cache_key in self.temporary_storage_runnables:
            return

        runnable = TemporaryStorageJobsRunnable(
            server_name,
            cfg,
            username,
            password,
            signal_parent=self,
        )
        self.temporary_storage_runnables[cache_key] = runnable
        runnable.signals.jobs_ready.connect(
            lambda data, key=cache_key, task=runnable:
                self._on_temporary_storage_jobs_fetched(data, key, task)
        )
        self.temporary_storage_thread_pool.start(runnable)

    def _on_temporary_storage_jobs_fetched(self, data, cache_key, runnable):
        if self.temporary_storage_runnables.get(cache_key) is runnable:
            self.temporary_storage_runnables.pop(cache_key, None)
        config_key = data.get("config_key") or cache_key[0]
        card = self.card_widgets.get(config_key)
        if card is not None:
            card.set_top_temporary_storage_jobs(
                data.get("top_temporary_storage_jobs", []),
                str(data.get("top_temporary_storage_jobs_error") or ""),
            )

    def _on_single_server_fetched_independent(self, lpar_data, generation, runnable, server_name):
        """Handle individual server fetch completion and schedule next refresh for that server."""
        self.active_runnables.discard(runnable)
        if not self.is_monitoring or generation != self.refresh_generation:
            return
        if not self._refresh_network_available():
            self._reschedule_server_timer(server_name, self._next_retry_delay_ms())
            return

        config_key = lpar_data.get("config_key") or lpar_data.get("server") or runnable.server
        self.latest_results_cache[config_key] = lpar_data
        self.server_last_fetch_time[server_name] = time.monotonic()
        self._register_server_result(config_key, lpar_data)

        self._schedule_log_history_refresh()

        if config_key in self.card_widgets:
            card = self.card_widgets[config_key]
            card.retry_count = self.server_retry_counts.get(config_key, 0)
            card.last_error_reason = self.server_error_reasons.get(config_key, str(lpar_data.get("error") or ""))
            card.sync_duration_ms = int(self.server_sync_durations_ms.get(config_key, 0))
            if str(lpar_data.get("status", "OFFLINE")).upper() in ("ONLINE", "DEGRADED"):
                completed_at = str(lpar_data.get("completed_at") or "").strip()
                card.last_success_ts = completed_at or time.strftime("%H:%M:%S")
            card.update_data(lpar_data)
            card._sync_health_summary()

        if str(lpar_data.get("status", "OFFLINE")).upper() in ("ONLINE", "DEGRADED"):
            self._schedule_object_statistics_fetch(
                server_name,
                runnable.cfg,
                runnable.username,
                runnable.password,
            )
            self._schedule_temporary_storage_jobs_fetch(
                server_name,
                runnable.cfg,
                runnable.username,
                runnable.password,
            )

        self._refresh_global_status_summary()

        retry_delay = self._next_retry_delay_ms() if self.server_retry_counts.get(server_name, 0) > 0 else self.refresh_interval_ms
        self._reschedule_server_timer(server_name, retry_delay)
        self._ensure_server_timers_alive()
        self._hide_sync_loading()

    def _on_single_server_failed(self, failure, generation, runnable, server_name):
        self.active_runnables.discard(runnable)
        if not self.is_monitoring or generation != self.refresh_generation:
            return
        if not self._refresh_network_available():
            self._reschedule_server_timer(server_name, self._next_retry_delay_ms())
            return

        config_key = failure.get("server") or runnable.server
        previous_status = self._system_logged_lpar_status.get(config_key)
        if previous_status != "OFFLINE":
            SYSTEM_LOGGER.warning("LPAR %s refresh failed; retry scheduled", config_key)
            self._system_logged_lpar_status[config_key] = "OFFLINE"
        self._register_server_result(config_key, failure)
        card = self.card_widgets.get(config_key)
        if card is not None:
            card.last_error_reason = str(failure.get("error") or "Fetch failed")
            card.set_status("OFFLINE")
        self._refresh_global_status_summary()
        self._reschedule_server_timer(server_name, self._next_retry_delay_ms())

    def fetch_data(self, force=False):
        if not self.is_monitoring:
            return

        if self.auto_refresh_paused and not force:
            return

        if not self._refresh_network_available():
            return

        if not getattr(self.log_viewer_widget, 'active_lpars', None):
            self.log_viewer_widget.active_lpars = sorted({
                self.log_viewer_widget._normalize_server_name(name)
                for name in self.active_server_configs.keys()
                if self.log_viewer_widget._normalize_server_name(name)
            })

        self._refresh_in_progress = False
        self._refresh_queued = False
        self._show_sync_loading("Starting independent server refreshes...")
        self.last_refresh_started_at = time.monotonic()

        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()

        if force:
            self.status_label.setText("Status: Manual refresh triggered. Fetching latest metrics...")
            self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")
        else:
            self.status_label.setText("Status: Authenticating & fetching metrics concurrently...")
            self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")

        self.completed_threads_count = 0
        self.pending_lpar_count = len(self.active_server_configs)
        self.latest_results_cache = {}
        self.active_runnables.clear()

        for timer in list(self.server_refresh_timers.values()):
            timer.stop()
            timer.deleteLater()
        self.server_refresh_timers.clear()

        for _, card in self.card_widgets.items():
            card.set_status("SYNCING")

        for server_name in self.active_server_configs.keys():
            self._refresh_single_server(server_name, network_checked=True)

        self._hide_sync_loading()

    def on_single_lpar_failed(self, failure, generation, runnable):
        self.active_runnables.discard(runnable)
        if not self.is_monitoring or generation != self.refresh_generation:
            return
        config_key = failure.get("server") or runnable.server
        self._register_server_result(config_key, failure)
        card = self.card_widgets.get(config_key)
        if card is not None:
            card.last_error_reason = str(failure.get("error") or "Fetch failed")
            card.set_status("OFFLINE")
        self.completed_threads_count += 1
        self._reschedule_server_timer(runnable.server, self._next_retry_delay_ms())
        if self.completed_threads_count >= self.pending_lpar_count:
            self.on_all_lpars_finished()

    def on_single_lpar_fetched(self, lpar_data, generation, runnable):
        self.active_runnables.discard(runnable)
        if not self.is_monitoring or generation != self.refresh_generation:
            return

        config_key = lpar_data.get("config_key") or lpar_data.get("server") or runnable.server
        server_name = lpar_data.get("server") or config_key
        status = str(lpar_data.get("status", "OFFLINE")).upper()
        previous_status = self._system_logged_lpar_status.get(config_key)
        if status != previous_status:
            if status in ("DEGRADED", "OFFLINE", "AUTH_ERROR", "STALE"):
                SYSTEM_LOGGER.warning("LPAR %s status changed to %s", server_name, status)
            else:
                SYSTEM_LOGGER.info("LPAR %s status changed to %s", server_name, status)
            self._system_logged_lpar_status[config_key] = status
        self.latest_results_cache[config_key] = lpar_data
        self._register_server_result(config_key, lpar_data)
        self.completed_threads_count += 1

        self._schedule_log_history_refresh()

        if config_key in self.card_widgets:
            card = self.card_widgets[config_key]
            card.retry_count = self.server_retry_counts.get(config_key, 0)
            card.last_error_reason = self.server_error_reasons.get(config_key, str(lpar_data.get("error") or ""))
            card.sync_duration_ms = int(self.server_sync_durations_ms.get(config_key, 0))
            if str(lpar_data.get("status", "OFFLINE")).upper() in ("ONLINE", "DEGRADED"):
                completed_at = str(lpar_data.get("completed_at") or "").strip()
                card.last_success_ts = completed_at or time.strftime("%H:%M:%S")
            card.update_data(lpar_data)
            card._sync_health_summary()

        self._reschedule_server_timer(runnable.server, self.refresh_interval_ms)
        if self.completed_threads_count >= self.pending_lpar_count:
            self.on_all_lpars_finished()

    def on_all_lpars_finished(self):
        self._refresh_in_progress = False
        self.last_refresh_success_at = time.monotonic()
        for server_name, lpar_data in self.latest_results_cache.items():
            if server_name in self.card_widgets:
                card = self.card_widgets[server_name]
                card.update_data(lpar_data)
                card.retry_count = self.server_retry_counts.get(server_name, 0)
                card.last_error_reason = self.server_error_reasons.get(server_name, str(lpar_data.get("error") or ""))
                card.sync_duration_ms = int(self.server_sync_durations_ms.get(server_name, 0))
                card._sync_health_summary()

        self.update_stale_server_states()
        self._refresh_global_status_summary()

        if self.content_stack.currentWidget() is self.logs_analytics_widget:
            self.log_viewer_widget.load_log_history()
        self.refresh_widget.update_timestamp()
        if not self._refresh_in_progress and not self.active_runnables:
            self._hide_sync_loading()

        if self.is_monitoring and not self.auto_refresh_paused:
            self._schedule_independent_refreshes()

        if self._refresh_queued:
            self._refresh_queued = False
            self.fetch_data()