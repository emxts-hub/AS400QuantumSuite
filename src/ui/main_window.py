# ui/main_window.py
import sys
import os
import json
import math
import threading
import time
from typing import cast
from collections import deque
from config import APP_VERSION, APP_NAME

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt6.QtCore import Qt, QTimer, QThread, QThreadPool, QEventLoop, QCoreApplication, QPointF, QRectF, QPropertyAnimation, QAbstractAnimation, pyqtProperty, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QCursor, QIcon, QPixmap, QPainter, QPen, QPolygonF, QBrush
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QGroupBox, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QGridLayout, QProgressBar,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QApplication, QSizePolicy, QComboBox, QCheckBox, QStackedWidget, QPlainTextEdit, QTabWidget, QMessageBox, QFileDialog
)

from worker import (
    SingleLparRunnable,
    has_vpn_ip,
    reset_asp_alert_sound,
    stop_all_server_status_alerts,
    stop_asp_alert_sound,
)
from ui.log_viewer import LogViewerWidget
from ui.monthly_report import MonthlyReportWidget
from ui.backup_manage import BackupManagementWidget
from ui.widgets import RefreshStatusWidget, StatusBadgesWidget, SubsystemGridWidget, ThemeLoadingDialog
from dialogs import ActiveJobsDialog, LparSettingsDialog, TestEmailThread
from version_worker import VersionCheckWorker
from config import SERVER_CONFIGS, EXPECTED_SUBSYSTEMS, EXPECTED_PORTS, ONEDRIVE_SHAREPOINT_PATH, get_resource_path, get_config_path
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


class CredentialCheckThread(QThread):
    results_ready = pyqtSignal(object)

    def __init__(self, server_configs, username, password):
        super().__init__()
        self.server_configs = dict(server_configs)
        self.username = username
        self.password = password

    def run(self):
        results = {}
        for server_name, cfg in self.server_configs.items():
            conn = None
            try:
                host = cfg.get("host", "") if isinstance(cfg, dict) else str(cfg)
                db = cfg.get("db", "*LOCAL") if isinstance(cfg, dict) else "*LOCAL"
                conn = __import__("worker")._new_connection(host, db, self.username, self.password)
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM SYSIBM.SYSDUMMY1")
                cursor.fetchone()
                results[server_name] = {"status": "OK", "message": "Credentials accepted."}
            except Exception as exc:
                results[server_name] = {"status": "AUTH_ERROR", "message": str(exc)}
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        self.results_ready.emit(results)


class DualSparklineWidget(QWidget):
    """Stacked dual sparkline chart displaying CPU (Upper) and ASP (Lower) trends."""
    def __init__(self, max_points=45, parent=None):
        super().__init__(parent)
        self.max_points = max_points
        self.cpu_history = deque(maxlen=max_points)
        self.asp_history = deque(maxlen=max_points)
        self.setFixedHeight(48)
        
        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True

    def add_values(self, cpu_val, asp_val=None):
        self.cpu_history.append(float(cpu_val))
        if asp_val is not None:
            self.asp_history.append(float(asp_val))
        self.update()

    def add_value(self, val):
        self.add_values(val)

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        self.update()

    def _draw_subgraph(self, painter, history, rect, default_color):
        if len(history) < 2:
            return

        w = rect.width()
        h = rect.height()
        margin = 2.0

        max_val = max(100.0, max(history))
        step_x = (w - 2 * margin) / max(1, len(history) - 1)

        points = []
        for i, val in enumerate(history):
            x = rect.left() + margin + i * step_x
            y = (rect.bottom() - margin) - ((val / max_val) * (h - 2 * margin))
            points.append(QPointF(x, y))

        latest_val = history[-1]
        line_color = QColor("#f85149") if latest_val >= 90 else default_color

        painter.setPen(QPen(line_color, 1.2))
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i+1])

        fill_color = QColor(line_color)
        fill_color.setAlpha(30)
        poly_points = [QPointF(points[0].x(), rect.bottom())] + points + [QPointF(points[-1].x(), rect.bottom())]
        painter.setBrush(QBrush(fill_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF(poly_points))

    def paintEvent(self, a0):
        if len(self.cpu_history) < 2 and len(self.asp_history) < 2:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        half_h = (h - 2) / 2.0

        cpu_rect = QRectF(0, 0, w, half_h)
        asp_rect = QRectF(0, half_h + 2, w, half_h)

        sep_color = QColor("#30363d" if self.is_dark_theme else "#d0d7de")
        painter.setPen(QPen(sep_color, 0.5, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(0, half_h + 1), QPointF(w, half_h + 1))

        cpu_color = QColor("#58a6ff" if self.is_dark_theme else "#0969da")
        asp_color = QColor("#a371f7" if self.is_dark_theme else "#8250df")

        if len(self.cpu_history) >= 2:
            self._draw_subgraph(painter, self.cpu_history, cpu_rect, cpu_color)

        if len(self.asp_history) >= 2:
            self._draw_subgraph(painter, self.asp_history, asp_rect, asp_color)


class SubsystemDetailDialog(QDialog):
    def __init__(self, server_name, subsystem_data=None, expected_key=None, timestamp_str="", parent=None):
        super().__init__(parent)
        self.server_name = server_name
        self.expected_key = expected_key or server_name
        self.subsystem_data = subsystem_data or []
        self.setWindowTitle(f"{server_name} - Detailed Subsystem Status")
        self.resize(850, 520)
        app = QApplication.instance()
        is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        dialog_bg = "#0d1117" if is_dark_theme else "#f6f8fa"
        table_bg = "#161b22" if is_dark_theme else "#ffffff"
        surface = "#21262d" if is_dark_theme else "#eaeef2"
        text = "#c9d1d9" if is_dark_theme else "#1f2328"
        muted = "#8b949e" if is_dark_theme else "#57606a"
        border = "#30363d" if is_dark_theme else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; border: 2px solid #2ea043; border-radius: 12px; }}
            QLabel {{ color: {text}; background-color: transparent; }}
            QTableWidget {{ background-color: {table_bg}; border: 1px solid {border}; gridline-color: {border}; color: {text}; border-radius: 6px; }}
            QHeaderView::section {{ background-color: {surface}; color: {muted}; font-weight: bold; border: none; padding: 8px; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title_str = f"{server_name} Detailed Subsystem Status"
        if timestamp_str:
            title_str += f" ({timestamp_str})"
        title_lbl = QLabel(title_str)
        title_lbl.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {'#ffffff' if is_dark_theme else '#1f2328'}; background-color: transparent;")
        layout.addWidget(title_lbl)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Subsystem Description ▲", "Status", "Current Active Jobs", "Library", "Text Description"
        ])
        
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        
        header = cast(QHeaderView, self.table.horizontalHeader())
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 110)
        
        cast(QHeaderView, self.table.verticalHeader()).setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        self.populate_subsystem_details()
        layout.addWidget(self.table)

    def show_centered(self):
        if self.parent():
            parent = self.parent()
            top_level = cast(QWidget, parent).window() if parent is not None else None
            if top_level is None:
                return self.exec()
            parent_geo = top_level.geometry()
            
            x = parent_geo.x() + (parent_geo.width() - self.width()) // 2
            y = parent_geo.y() + (parent_geo.height() - self.height()) // 2
            self.move(x, y)
        else:
            screen = QApplication.primaryScreen()
            if screen:
                screen_geo = screen.availableGeometry()
                x = screen_geo.x() + (screen_geo.width() - self.width()) // 2
                y = screen_geo.y() + (screen_geo.height() - self.height()) // 2
                self.move(x, y)
                
        self.exec()

    def populate_subsystem_details(self):
        expected_list = EXPECTED_SUBSYSTEMS.get(self.expected_key, [])
        active_dict = {}

        for sub in self.subsystem_data:
            if isinstance(sub, dict):
                s_name = sub.get("name", "").upper()
                active_dict[s_name] = sub
            elif isinstance(sub, str):
                s_name = sub.upper()
                active_dict[s_name] = {"name": s_name, "status": "ACTIVE", "active_jobs": 0, "library": "QSYS", "description": ""}

        all_display_rows = []
        
        for exp_name in expected_list:
            exp_upper = exp_name.upper()
            if exp_upper in active_dict:
                all_display_rows.append(active_dict[exp_upper])
            else:
                all_display_rows.append({
                    "name": exp_upper,
                    "status": "INACTIVE",
                    "active_jobs": 0,
                    "library": "QSYS",
                    "description": "Subsystem Stopped / Down"
                })

        for s_name, data in active_dict.items():
            if s_name not in [e.upper() for e in expected_list]:
                all_display_rows.append(data)

        self.table.setRowCount(len(all_display_rows))
        
        for row, sub in enumerate(all_display_rows):
            name = sub.get("name", "")
            status = str(sub.get("status", "ACTIVE")).upper()
            active_jobs = str(sub.get("active_jobs", 0))
            library = sub.get("library", "")
            desc = sub.get("description", "")

            is_inactive = status in ["INACTIVE", "DOWN", "INACTIVE/OFF"]

            items = [
                QTableWidgetItem(name),
                QTableWidgetItem(status),
                QTableWidgetItem(active_jobs),
                QTableWidgetItem(library),
                QTableWidgetItem(desc)
            ]

            items[1].setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            items[2].setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            for col, item in enumerate(items):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if is_inactive:
                    item.setForeground(QColor("#f85149"))
                    item.setBackground(QColor("#361718"))
                    item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                self.table.setItem(row, col, item)


class LinearGauge(QWidget):
    def __init__(self, title, initial_value=0.0, parent=None, decimals=1):
        super().__init__(parent)
        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        self.is_uncapped = False
        self.decimals = int(decimals)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.title_label = QLabel(title)
        self.title_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.title_label.setStyleSheet("color: #8b949e; background-color: transparent;")

        zero_format = f"0.{self.decimals}f%" if self.decimals > 0 else "0%"
        self.val_label = QLabel(zero_format.replace("0.", "0." if False else "0."))
        self.val_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.val_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        top_h = QHBoxLayout()
        top_h.addWidget(self.title_label)
        top_h.addStretch()
        top_h.addWidget(self.val_label)
        layout.addLayout(top_h)

        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(8)
        self.pbar.setTextVisible(False)
        layout.addWidget(self.pbar)

        self.set_value(initial_value)

    def set_value(self, val, is_uncapped=False):
        val_float = float(val)
        self.value = val_float
        self.is_uncapped = is_uncapped or val_float > 100.0

        display_value = f"{val_float:.{self.decimals}f}"
        if self.is_uncapped and val_float > 100.0:
            self.val_label.setText(f"{display_value}% ⚡")
            self.setToolTip("Uncapped CPU capacity in use (borrowing processing power)")
        else:
            self.val_label.setText(f"{display_value}%")
            self.setToolTip("")

        self.pbar.setValue(min(100, int(val_float)))

        if val_float > 100.0:
            bar_color = "#bc8cff" if self.is_dark_theme else "#8250df"
            self.val_label.setStyleSheet(f"color: {bar_color}; background-color: transparent;")
        elif val_float >= 90.0:
            bar_color = "#f85149"
            self.val_label.setStyleSheet("color: #f85149; background-color: transparent;")
        elif val_float >= 80.0:
            bar_color = "#e3b341"
            self.val_label.setStyleSheet("color: #9a6700; background-color: transparent;" if not self.is_dark_theme else "color: #e3b341; background-color: transparent;")
        else:
            bar_color = "#388bfd"
            value_color = "#0969da" if not self.is_dark_theme else "#ffffff"
            self.val_label.setStyleSheet(f"color: {value_color}; background-color: transparent;")

        self.pbar.setStyleSheet(f"""
            QProgressBar {{
                background-color: {"#21262d" if self.is_dark_theme else "#e1e4e8"};
                border: none;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background-color: {bar_color};
                border-radius: 4px;
            }}
        """)

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        title_color = "#8b949e" if is_dark_theme else "#57606a"
        self.title_label.setStyleSheet(f"color: {title_color}; background-color: transparent;")
        self.set_value(self.value, self.is_uncapped)


class LparCardWidget(QFrame):
    def __init__(self, server_name, parent=None):
        super().__init__(parent)
        self.server_name = server_name
        app = QApplication.instance()
        self.is_dark_theme = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        self.current_status = "OFFLINE"
        self.current_is_critical = True
        self.current_cpu = 0.0
        self.current_asp = 0.0
        self.current_jobs = 0
        self.current_active_jobs_detail = []
        self.current_subsystems_data = []
        self.current_ports_data = []
        self.config_key = server_name
        self.detail_expanded = True
        self.last_success_ts = None
        self.retry_count = 0
        self.sync_duration_ms = 0
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
        
        self.setMinimumWidth(0)
        self.setFixedHeight(350)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setSpacing(2)

        header_layout = QHBoxLayout()
        self.name_label = QLabel(server_name)
        self.name_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))

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

        gauges_layout = QHBoxLayout()
        gauges_layout.setSpacing(8)
        self.cpu_gauge = LinearGauge("CPU", decimals=1)
        self.asp_gauge = LinearGauge("ASP", decimals=2)
        gauges_layout.addWidget(self.cpu_gauge, stretch=1)
        gauges_layout.addWidget(self.asp_gauge, stretch=1)
        self.main_layout.addLayout(gauges_layout)

        self.sparkline = DualSparklineWidget(max_points=35)
        self.main_layout.addWidget(self.sparkline)

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

        sub_header = QHBoxLayout()
        self.subsystems_title_label = QLabel("Subsystems")
        self.subsystems_title_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        sub_header.addWidget(self.subsystems_title_label)
        sub_header.addStretch()
        self.main_layout.addLayout(sub_header)

        self.subsystem_container = QWidget()
        self.subsys_layout = QVBoxLayout(self.subsystem_container)
        self.subsys_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.subsystem_container)

        self.network_title_label = QLabel("Network Services")
        self.network_title_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.main_layout.addWidget(self.network_title_label)

        self.ports_container = QWidget()
        self.ports_layout = QVBoxLayout(self.ports_container)
        self.ports_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.ports_container)

        self.set_theme(self.is_dark_theme)

    def _clear_detail_widgets(self):
        for layout in (self.subsys_layout, self.ports_layout):
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.setParent(None)

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        self.cpu_gauge.set_theme(is_dark_theme)
        self.asp_gauge.set_theme(is_dark_theme)
        self.sparkline.set_theme(is_dark_theme)

        label_color = "#c9d1d9" if is_dark_theme else "#57606a"
        value_color = "#ffffff" if is_dark_theme else "#1f2328"
        for label in (self.name_label,):
            label.setStyleSheet(f"color: {value_color}; background-color: transparent;")

        self.jobs_val_label.setStyleSheet(
            f"QPushButton {{ color: {value_color}; background-color: transparent; border: none; }}"
            "QPushButton:hover { color: #58a6ff; }"
        )

        for label in (self.jobs_title_label, self.subsystems_title_label, self.network_title_label, self.health_label):
            label.setStyleSheet(f"color: {label_color}; background-color: transparent;")

        self.name_label.setText(self.server_name)

        self._last_card_style_key = None
        self._last_status_badge_key = None
        self.set_card_style(is_critical=self.current_is_critical)
        self.set_status(self.current_status)

    def _get_alert_pulse(self):
        return self._alert_pulse

    def _set_alert_pulse(self, value):
        self._alert_pulse = float(value)
        self.set_card_style(is_critical=self.current_is_critical, force=True)

    alertPulse = pyqtProperty(float, fget=_get_alert_pulse, fset=_set_alert_pulse)

    def _update_alert_animation(self):
        should_pulse = self.current_is_critical and self.current_status in ("ONLINE", "DEGRADED")
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

    def _clear_active_jobs_dialog(self):
        self.active_jobs_dialog = None

    def set_card_style(self, is_critical=False, force=False):
        key = (self.is_dark_theme, bool(is_critical))
        if self._last_card_style_key == key and not force:
            return
        self._last_card_style_key = key

        if not self.is_dark_theme:
            border_color = self._alert_border_color("#d0d7de", "#cf222e") if is_critical else "#d0d7de"
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
            border_color = self._alert_border_color("#30363d", "#f85149")
            self.setStyleSheet("""
                LparCardWidget {
                    background-color: #161b22;
                    border: 2px solid %s;
                    border-radius: 10px;
                }
                QLabel {
                    background-color: transparent;
                }
            """ % border_color)
        else:
            self.setStyleSheet("""
                LparCardWidget {
                    background-color: #161b22;
                    border: 2px solid #30363d;
                    border-radius: 10px;
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
        reason_text = f" | {self.last_error_reason}" if self.last_error_reason else ""
        summary = f"{last_success_text}{retry_text}{sync_text}{reason_text}"
        if len(summary) > 120:
            summary = summary[:117] + "..."
        self.health_label.setText(summary)

    def set_status(self, status):
        self.current_status = status
        self.set_card_style(
            is_critical=self.current_is_critical and status not in ("CONNECTING", "SYNCING")
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
        elif status in ("ONLINE", "DEGRADED"):
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
        threshold_percent = float(load_email_alerts().get("threshold_percent", 40.0) or 40.0)
        is_critical = asp >= threshold_percent

        signature = (
            status,
            cpu,
            asp,
            int(jobs),
            repr(active_jobs_detail),
            repr(subsystems),
            repr(ports),
            is_uncapped,
            is_critical,
            threshold_percent,
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
        self.current_subsystems_data = subsystems
        self.current_ports_data = ports

        if is_uncapped:
            self.uncapped_badge.show()
        else:
            self.uncapped_badge.hide()

        self.current_is_critical = is_critical
        self.set_card_style(is_critical=is_critical)
        self._update_alert_animation()

        label_key = (status, is_critical, self.is_dark_theme)
        if self._last_status_badge_key != label_key:
            self._last_status_badge_key = label_key
            if status in ("ONLINE", "DEGRADED"):
                if is_critical:
                    self.status_badge.setText("CRITICAL ●")
                    self.status_badge.setStyleSheet(
                        "color: #f85149; font-weight: bold; background-color: transparent;"
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
        if not self.sparkline.cpu_history or not math.isclose(self.sparkline.cpu_history[-1], cpu, rel_tol=0.0, abs_tol=1e-9) or not math.isclose(self.sparkline.asp_history[-1], asp, rel_tol=0.0, abs_tol=1e-9):
            self.sparkline.add_values(cpu, asp)

        jobs_text = f"{jobs:,}"
        if self.jobs_val_label.text() != jobs_text:
            self.jobs_val_label.setText(jobs_text)
        if self.active_jobs_dialog is not None:
            self.active_jobs_dialog.update_jobs(self.current_active_jobs_detail)

        self._sync_health_summary()
        self._refresh_subsystem_widget(self.current_subsystems_data)
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


class AppInfoDialog(QDialog):

    def __init__(self, version_str: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About this App")
        self.setModal(True)
        self.setFixedWidth(460)
        self.setMinimumHeight(350)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.info_label = QLabel(self._build_info_text(version_str))
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.info_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(self.info_label)

        self.setStyleSheet(
            "QDialog { background-color: #0f172a; border: 1px solid #1e293b;"
            " border-radius: 10px; }"
            "QLabel { background-color: transparent; color: #f8fafc; padding: 4px;"
            " font-size: 13px; line-height: 1.5; }"
        )

    @staticmethod
    def _build_info_text(version_str: str) -> str:
        return (
            "<div style='font-family: Segoe UI, sans-serif; color: #e2e8f0;'>"
            "<h2 style='margin: 0 0 6px 0; color: #38bdf8; font-size: 18px;"
            " font-weight: bold; letter-spacing: 0.5px;'>AS400 QUANTUM SUITE</h2>"
            "<div style='color: #cbd5e1; font-size: 12px; margin-bottom: 12px;'>"
            "<b>System:</b> IBM i (AS/400) Real-time Monitoring & Telemetry<br>"
            "<b>Stack:</b> Python | PyQt6 | DB2 ODBC | Local JSON storage<br>"
            f"<b>Version:</b> {version_str}"
            "</div>"
            "<hr style='border: none; border-top: 1px solid #334155; margin: 12px 0;'>"
            "<div style='margin-bottom: 12px;'>"
            "<b style='color: #f1f5f9; font-size: 13px;'>Key Features:</b>"
            "<ul style='margin: 6px 0 0 16px; padding: 0; color: #cbd5e1;"
            " font-size: 12px; line-height: 1.6;'>"
            "<li>Real-Time LPAR Health Monitoring (CPU / ASP / Active Jobs)</li>"
            "<li>Automated Backup Management & Job Duration Analytics</li>"
            "<li>Subsystem, Network Port & Service Status Tracking</li>"
            "<li>Monthly Historical JSON Vault & Deduplicated Logging</li>"
            "<li>Monthly Performance Analytics & Excel Reporting</li>"
            "</ul>"
            "</div>"
            "<hr style='border: none; border-top: 1px solid #334155; margin: 12px 0;'>"
            "<div style='color: #94a3b8; font-size: 11px; line-height: 1.5;'>"
            "<b>© 2026 Reymart De Lara.</b> All Rights Reserved.<br>"
            "<span style='color: #cbd5e1;'>Created & Developed by Reymart De Lara</span><br>"
            "<span style='color: #64748b; font-size: 10px;'>IBM i and AS/400 are registered trademarks of IBM Corp.</span>"
            "</div>"
            "</div>"
        )


class IBMiDashboard(QMainWindow):
    _version_worker: VersionCheckWorker | None

    def __init__(self, version_str: str = APP_VERSION):
        super().__init__()
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
        self.last_log_history_refresh = 0.0
        self._log_history_refresh_timer = QTimer(self)
        self._log_history_refresh_timer.setSingleShot(True)
        self._log_history_refresh_timer.setInterval(750)
        self._log_history_refresh_timer.timeout.connect(self._refresh_log_history_after_results)
        self.auto_refresh_paused = False
        self.last_refresh_success_at = None
        self.last_refresh_started_at = None
        self._last_global_summary_signature = None
        self._last_card_layout_signature = None

        self.thread_pool = cast(QThreadPool, QThreadPool.globalInstance())
        self.thread_pool.setMaxThreadCount(8)
        self.min_refresh_interval_ms = 5000  # Refresh live ASP/CPU data every 5s for near real-time monitoring
        self.refresh_interval_ms = self.min_refresh_interval_ms
        self.server_refresh_timers = {}  # Per-server refresh timers for independent refresh
        self._refresh_in_progress = False
        self._refresh_queued = False
        self.server_retry_counts = {}
        self.server_last_success = {}
        self.server_error_reasons = {}
        self.server_sync_durations_ms = {}
        self.server_last_fetch_time = {}  # Track when each server last completed fetch
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

        self.nav_buttons = {}
        self.nav_button_labels = {}
        nav_items = [
            ("Live Monitor", "monitor", "monitor.png"),
            ("Logs && Analytics", "log_viewer", "logs.png"),
            ("Backup Management", "backup_management", "backup.png"),
            ("Settings && Credentials", "settings", "settings.png")
        ]
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
        self.logs_analytics_tabs = QTabWidget()
        self.logs_analytics_tabs.addTab(self.log_viewer_widget, "System Logs")
        logs_analytics_layout.addWidget(self.logs_analytics_tabs)
        self.content_stack.addWidget(self.logs_analytics_widget)

        self.settings_widget = QWidget()
        self._init_settings_page()
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
        self.logs_analytics_tabs.addTab(self.monthly_report_widget, "Monthly ASP/CPU")

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

        choice = QMessageBox.question(
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

        nav_items = {
            "monitor": ("Live Monitor", "monitor.png"),
            "log_viewer": ("Logs && Analytics", "logs.png"),
            "backup_management": ("Backup Management", "backup.png"),
            "settings": ("Settings && Credentials", "settings.png")
        }

        for key, button in self.nav_buttons.items():
            label, icon_file = nav_items.get(key, (key, "logo.png"))
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

    def _update_nav_button_styles(self, active_key):
        dark = self.is_dark_theme
        if dark:
            sidebar_bg = "#171f2e"
            sidebar_inner = "#1d2430"
            active_bg = "#2d5dff"
            active_text = "#f3f6ff"
            inactive_bg = "#202b39"
            inactive_text = "#dfe7ff"
            hover_bg = "#24314d"
            brand_text = "#f8fbff"
        else:
            sidebar_bg = "#edf2f8"
            sidebar_inner = "#f3f6fa"
            active_bg = "#2d5dff"
            active_text = "#ffffff"
            inactive_bg = "#f3f6fa"
            inactive_text = "#111827"
            hover_bg = "#e7edf8"
            brand_text = "#1f2937"

        self.sidebar.setStyleSheet(
            f"QWidget {{ background-color: {sidebar_bg}; border: none; border-radius: 0; }}"
        )
        self.sidebar_brand.setStyleSheet(
            f"color: {brand_text}; background-color: transparent; padding: 8px 0; font-weight: bold;"
        )

        for key, button in self.nav_buttons.items():
            is_active = key == active_key
            button.setIconSize(QSize(60, 60) if is_active else QSize(30, 30))
            button.setText(self.nav_button_labels.get(key, key) if not self.sidebar_collapsed else "")
            if self.sidebar_collapsed:
                button.setFixedWidth(64)
                button.setStyleSheet(
                    f"QPushButton {{ background-color: {'#2d5dff' if is_active else inactive_bg}; color: {active_text if is_active else inactive_text}; "
                    f"border: none; border-radius: 18px; padding: 0; font-weight: 400; font-size: 12px; "
                    f"text-align: center; qproperty-iconSize: {60 if is_active else 30}px; }} "
                    f"QPushButton:hover {{ background-color: {active_bg if is_active else hover_bg}; color: {active_text if is_active else inactive_text}; }}"
                )
            else:
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
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        settings_header = QHBoxLayout()
        title = QLabel("Settings & Credential configuration")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #1f2937; background-color: transparent;")
        settings_header.addWidget(title)
        settings_header.addStretch()

        self.theme_btn = QPushButton("☀ Light Theme")
        self.theme_btn.setFixedHeight(35)
        self.theme_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.theme_btn.setToolTip("Switch between dark and light themes")
        self.theme_btn.clicked.connect(self.toggle_theme)
        settings_header.addWidget(self.theme_btn)

        self.settings_lock_label = QLabel("Stop monitoring to edit settings")
        self.settings_lock_label.setStyleSheet(
            "color: #b45309; background-color: #fef3c7; border: 1px solid #f59e0b; "
            "border-radius: 6px; padding: 6px 10px; font-weight: bold;"
        )
        self.settings_lock_label.setVisible(False)
        settings_header.addWidget(self.settings_lock_label)

        layout.addLayout(settings_header)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #d0d7de; background-color: transparent;")
        layout.addWidget(line)

        form_row = QHBoxLayout()
        form_row.setSpacing(6)

        user_wrapper = QHBoxLayout()
        user_wrapper.setSpacing(6)
        user_label = QLabel("Username:")
        user_label.setFixedWidth(68)
        user_label.setStyleSheet("color: #1f2937; background-color: transparent; font-size: 14px; font-weight: 500;")
        user_wrapper.addWidget(user_label)

        self.user_input = QLineEdit("")
        self.user_input.setPlaceholderText("Username")
        self.user_input.setFont(QFont("Segoe UI", 11))
        self.user_input.setFixedWidth(190)
        self.user_input.setStyleSheet(
            "QLineEdit { border: 1px solid #c7d1db; border-radius: 8px; padding: 10px 12px; background-color: #ffffff; color: #1f2937; }"
        )
        user_wrapper.addWidget(self.user_input)
        form_row.addLayout(user_wrapper)

        pass_wrapper = QHBoxLayout()
        pass_wrapper.setSpacing(6)
        pass_label = QLabel("Password:")
        pass_label.setFixedWidth(68)
        pass_label.setStyleSheet("color: #1f2937; background-color: transparent; font-size: 14px; font-weight: 500;")
        pass_wrapper.addWidget(pass_label)

        self.pass_input = QLineEdit("")
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setFont(QFont("Segoe UI", 11))
        self.pass_input.setFixedWidth(190)
        self.pass_input.setStyleSheet(
            "QLineEdit { border: 1px solid #c7d1db; border-radius: 8px; padding: 10px 12px; background-color: #ffffff; color: #1f2937; }"
        )
        pass_wrapper.addWidget(self.pass_input)
        form_row.addLayout(pass_wrapper)

        saved_login = load_login_credentials()
        if saved_login.get("remember") and saved_login.get("username"):
            self.user_input.setText(saved_login["username"])
            self.pass_input.setText(get_ibmi_password(saved_login["username"]))
        self.remember_creds_checkbox = QCheckBox("Remember credentials")
        self.remember_creds_checkbox.setChecked(bool(saved_login.get("remember", False)))
        form_row.addWidget(self.remember_creds_checkbox)

        self.toggle_btn = QPushButton("Check Login Creds")
        self.toggle_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.toggle_btn.setFixedHeight(42)
        self.toggle_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.toggle_btn.clicked.connect(self.validate_login_credentials)
        self.toggle_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; border: none; border-radius: 8px; padding: 0 18px; }"
            "QPushButton:hover { background-color: #1d4ed8; }"
        )
        form_row.addWidget(self.toggle_btn)

        self.settings_btn = QPushButton("Save && Apply")
        self.settings_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.settings_btn.setFixedHeight(42)
        self.settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.settings_btn.clicked.connect(self.save_settings_page)
        self.settings_btn.setStyleSheet(
            "QPushButton { background-color: #16a34a; color: white; border: none; border-radius: 8px; padding: 0 18px; }"
            "QPushButton:hover { background-color: #15803d; }"
        )
        form_row.addWidget(self.settings_btn)

        form_row.addStretch()
        layout.addLayout(form_row)

        self.settings_tab_widget = QWidget()
        settings_sections_layout = QVBoxLayout(self.settings_tab_widget)
        settings_sections_layout.setContentsMargins(0, 0, 0, 0)
        settings_sections_layout.setSpacing(8)

        auth_mail_row = QHBoxLayout()
        auth_mail_row.setSpacing(8)

        credential_tab = QGroupBox("Credential Validation")
        credential_layout = QVBoxLayout(credential_tab)
        credential_layout.setContentsMargins(10, 8, 10, 8)
        credential_tab.setMinimumWidth(360)

        self.cred_log = QPlainTextEdit()
        self.cred_log.setReadOnly(True)
        self.cred_log.setMinimumHeight(92)
        self.cred_log.setPlaceholderText("Credential validation log will appear here...")
        self.cred_log.setStyleSheet(
            "QPlainTextEdit {"
            "  background-color: #f8fafc;"
            "  color: #1f2937;"
            "  border: 1px solid #d0d7de;"
            "  border-radius: 8px;"
            "  padding: 10px;"
            "}"
        )
        self.cred_log.setPlainText("Credential validation log will appear here...")
        credential_layout.addWidget(self.cred_log)
        auth_mail_row.addWidget(credential_tab, stretch=1)

        lpar_tab = QGroupBox("LPAR Configuration")
        lpar_layout = QVBoxLayout(lpar_tab)
        lpar_layout.setContentsMargins(10, 8, 10, 8)

        self.lpar_table = QTableWidget()
        self.lpar_table.setColumnCount(6)
        self.lpar_table.setHorizontalHeaderLabels([
            "IP / Hostname", "Database Name", "Daily Backup Name",
            "Journal Backup Name", "Expected Subsystems", "Monitored Ports (Port:Name)"
        ])
        self.lpar_table.setAlternatingRowColors(True)
        self.lpar_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lpar_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.lpar_table.setEditTriggers(QTableWidget.EditTrigger.AllEditTriggers)
        self.lpar_table.setWordWrap(False)
        self.lpar_table.horizontalHeader().setStretchLastSection(True)
        self._populate_lpar_table()
        lpar_layout.addWidget(self.lpar_table)

        lpar_buttons = QHBoxLayout()
        self.add_lpar_btn = QPushButton("+ Add LPAR")
        self.add_lpar_btn.clicked.connect(self._add_lpar_row)
        self.remove_lpar_btn = QPushButton("Remove Selected")
        self.remove_lpar_btn.clicked.connect(self._remove_lpar_row)
        lpar_buttons.addWidget(self.add_lpar_btn)
        lpar_buttons.addWidget(self.remove_lpar_btn)
        lpar_buttons.addStretch()
        lpar_layout.addLayout(lpar_buttons)
        lpar_tab.setMinimumHeight(250)

        storage_tab = QGroupBox("Log Storage")
        storage_layout = QHBoxLayout(storage_tab)
        storage_layout.setContentsMargins(10, 8, 10, 8)
        storage_layout.addWidget(QLabel("Root folder:"))
        self.logs_root_input = QLineEdit(ONEDRIVE_SHAREPOINT_PATH)
        self.logs_root_input.setPlaceholderText("Choose a folder for monthly logs")
        storage_layout.addWidget(self.logs_root_input, stretch=1)
        self.browse_logs_btn = QPushButton("Browse...")
        self.browse_logs_btn.clicked.connect(self._browse_logs_root)
        storage_layout.addWidget(self.browse_logs_btn)

        smtp_tab = QGroupBox("SMTP / Mail Configuration")
        smtp_layout = QVBoxLayout(smtp_tab)
        smtp_layout.setContentsMargins(10, 8, 10, 8)
        smtp_tab.setMinimumWidth(520)
        email_cfg = load_email_alerts()

        self.smtp_enabled = QCheckBox("Enable email alerts")
        self.smtp_enabled.setChecked(bool(email_cfg.get("enabled", False)))
        smtp_layout.addWidget(self.smtp_enabled)

        smtp_server_row = QHBoxLayout()
        smtp_server_row.addWidget(QLabel("SMTP Server:"))
        self.smtp_server_input = QLineEdit(str(email_cfg.get("smtp_server", "")))
        self.smtp_server_input.setPlaceholderText("smtp.office365.com")
        smtp_server_row.addWidget(self.smtp_server_input)
        smtp_server_row.addWidget(QLabel("Port:"))
        self.smtp_port_input = QLineEdit(str(email_cfg.get("port", 587)))
        self.smtp_port_input.setFixedWidth(75)
        smtp_server_row.addWidget(self.smtp_port_input)
        self.smtp_tls_checkbox = QCheckBox("Use TLS")
        self.smtp_tls_checkbox.setChecked(bool(email_cfg.get("use_tls", True)))
        smtp_server_row.addWidget(self.smtp_tls_checkbox)
        smtp_layout.addLayout(smtp_server_row)

        smtp_auth_row = QHBoxLayout()
        smtp_auth_row.addWidget(QLabel("Username:"))
        self.smtp_username_input = QLineEdit(str(email_cfg.get("username", "")))
        smtp_auth_row.addWidget(self.smtp_username_input)
        smtp_auth_row.addWidget(QLabel("Password:"))
        self.smtp_password_input = QLineEdit()
        self.smtp_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_password_input.setPlaceholderText("Leave blank to keep saved password")
        smtp_auth_row.addWidget(self.smtp_password_input)
        smtp_layout.addLayout(smtp_auth_row)

        smtp_address_row = QHBoxLayout()
        smtp_address_row.addWidget(QLabel("From Address:"))
        self.smtp_from_input = QLineEdit(str(email_cfg.get("from_address", "")))
        smtp_address_row.addWidget(self.smtp_from_input)
        smtp_address_row.addWidget(QLabel("To Addresses:"))
        self.smtp_to_input = QLineEdit(", ".join(email_cfg.get("to_addresses", [])))
        self.smtp_to_input.setPlaceholderText("recipient@example.com, ...")
        smtp_address_row.addWidget(self.smtp_to_input)
        smtp_layout.addLayout(smtp_address_row)

        alert_options_row = QHBoxLayout()
        alert_options_row.addWidget(QLabel("Threshold %:"))
        self.smtp_threshold_input = QLineEdit(str(email_cfg.get("threshold_percent", 40)))
        self.smtp_threshold_input.setFixedWidth(75)
        alert_options_row.addWidget(self.smtp_threshold_input)
        alert_options_row.addWidget(QLabel("Cooldown minutes:"))
        self.smtp_cooldown_input = QLineEdit(str(email_cfg.get("cooldown_minutes", 10)))
        self.smtp_cooldown_input.setFixedWidth(75)
        alert_options_row.addWidget(self.smtp_cooldown_input)
        alert_options_row.addStretch()
        smtp_layout.addLayout(alert_options_row)

        runtime_options_row = QHBoxLayout()
        runtime_options_row.addWidget(QLabel("Refresh Interval:"))
        self.refresh_interval_combo = QComboBox()
        self.refresh_interval_combo.addItems(["Instantly", "3s", "5s", "10s"])
        refresh_interval_ms = int(email_cfg.get("refresh_interval_ms", 0) or 0)
        self.refresh_interval_combo.setCurrentIndex({0: 0, 3000: 1, 5000: 2, 10000: 3}.get(refresh_interval_ms, 0))
        runtime_options_row.addWidget(self.refresh_interval_combo)

        runtime_options_row.addWidget(QLabel("Log Dedupe:"))
        self.log_dedupe_combo = QComboBox()
        self.log_dedupe_combo.addItems(["Off", "30s", "1m", "5m"])
        log_dedupe_seconds = int(email_cfg.get("log_dedupe_seconds", 60) or 60)
        self.log_dedupe_combo.setCurrentIndex({0: 0, 30: 1, 60: 2, 300: 3}.get(log_dedupe_seconds, 2))
        runtime_options_row.addWidget(self.log_dedupe_combo)
        runtime_options_row.addStretch()
        smtp_layout.addLayout(runtime_options_row)

        smtp_actions_row = QHBoxLayout()
        smtp_actions_row.addStretch()
        self.test_email_btn = QPushButton("Send Test Email")
        self.test_email_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.test_email_btn.clicked.connect(self.send_test_email)
        smtp_actions_row.addWidget(self.test_email_btn)
        smtp_layout.addLayout(smtp_actions_row)
        smtp_layout.addStretch()
        auth_mail_row.addWidget(smtp_tab, stretch=2)
        settings_sections_layout.addLayout(auth_mail_row)
        settings_sections_layout.addWidget(lpar_tab, stretch=1)
        settings_sections_layout.addWidget(storage_tab)

        system_tab = QGroupBox("System")
        system_layout = QHBoxLayout(system_tab)
        system_layout.setContentsMargins(10, 8, 10, 8)
        system_layout.addWidget(QLabel("Backup or restore a configuration"))
        system_layout.addStretch()

        self.backup_settings_btn = QPushButton("Backup")
        self.backup_settings_btn.setFixedHeight(35)
        self.backup_settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.backup_settings_btn.clicked.connect(self.backup_settings)
        system_layout.addWidget(self.backup_settings_btn)

        self.restore_settings_btn = QPushButton("Restore")
        self.restore_settings_btn.setFixedHeight(35)
        self.restore_settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.restore_settings_btn.clicked.connect(self.restore_settings)
        system_layout.addWidget(self.restore_settings_btn)
        settings_sections_layout.addWidget(system_tab)
        layout.addWidget(self.settings_tab_widget)

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
        self.lpar_table.itemChanged.connect(self._update_settings_save_state)
        self._set_settings_editable(True)

    def _set_settings_editable(self, editable):
        for widget in self._settings_editable_widgets:
            widget.setEnabled(editable)
        self.settings_lock_label.setVisible(not editable)
        self.settings_btn.setToolTip(
            "Save and apply settings" if editable else "Stop monitoring to edit settings"
        )
        if editable:
            self.settings_btn.setStyleSheet(
                "QPushButton { background-color: #16a34a; color: white; border: none; border-radius: 8px; padding: 0 18px; }"
                "QPushButton:hover { background-color: #15803d; }"
                "QPushButton:disabled { background-color: #94a3b8; color: #e2e8f0; border: none; }"
            )
        else:
            self.settings_btn.setStyleSheet(
                "QPushButton { background-color: #b45309; color: #fff7ed; border: 1px solid #f59e0b; "
                "border-radius: 8px; padding: 0 18px; font-weight: bold; }"
                "QPushButton:disabled { background-color: #b45309; color: #fff7ed; border: 1px solid #f59e0b; }"
            )
            self.status_label.setText("Status: Stop monitoring to edit settings.")
            self.status_label.setStyleSheet("color: #b45309; font-weight: bold; font-size: 11px; background-color: transparent;")
        self._update_settings_save_state()

    def _update_settings_save_state(self, *_args):
        if not hasattr(self, "_settings_saved_snapshot"):
            return
        self.settings_btn.setEnabled(
            not self.is_monitoring and self._settings_have_unsaved_changes()
        )

    def _backup_credentials(self):
        return self.user_input.text().strip(), self.pass_input.text()

    def _browse_logs_root(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose log storage folder",
            self.logs_root_input.text().strip() or os.path.expanduser("~"),
        )
        if selected:
            self.logs_root_input.setText(selected)

    def backup_settings(self):
        target_path, _ = QFileDialog.getSaveFileName(
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
            QMessageBox.critical(self, "Backup Failed", f"Could not create the settings backup:\n{exc}")
            return
        QMessageBox.information(self, "Backup Created", f"Settings backup created:\n{target_path}")

    def restore_settings(self):
        source_path, _ = QFileDialog.getOpenFileName(
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
            self.active_server_configs.clear()
            self.active_server_configs.update(payload["SERVER_CONFIGS"])
            EXPECTED_SUBSYSTEMS.clear()
            EXPECTED_SUBSYSTEMS.update(restored_subsystems)
            EXPECTED_PORTS.clear()
            EXPECTED_PORTS.update(restored_ports)
            self._populate_lpar_table()
            self._settings_saved_snapshot = self._capture_settings_snapshot()
            self.save_settings_page()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Restore Failed", f"Could not restore the settings backup:\n{exc}")

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
            daily_name = cfg.get("daily_backup_name", "DAILYSWA") if isinstance(cfg, dict) else "DAILYSWA"
            journal_name = cfg.get("journal_backup_name", "DAILYSWA") if isinstance(cfg, dict) else "DAILYSWA"
            self.lpar_table.setItem(row, 0, QTableWidgetItem(host))
            self.lpar_table.setItem(row, 1, QTableWidgetItem(db))
            self.lpar_table.setItem(row, 2, QTableWidgetItem(str(daily_name or "DAILYSWA")))
            self.lpar_table.setItem(row, 3, QTableWidgetItem(str(journal_name or "DAILYSWA")))
            self.lpar_table.setItem(row, 4, QTableWidgetItem(subsystems_str))
            self.lpar_table.setItem(row, 5, QTableWidgetItem(", ".join(port_parts)))

    def _add_lpar_row(self):
        row = self.lpar_table.rowCount()
        self.lpar_table.insertRow(row)
        self.lpar_table.setItem(row, 0, QTableWidgetItem("192.168.1.1"))
        self.lpar_table.setItem(row, 1, QTableWidgetItem("*LOCAL"))
        self.lpar_table.setItem(row, 2, QTableWidgetItem("DAILYSWA"))
        self.lpar_table.setItem(row, 3, QTableWidgetItem("DAILYSWA"))
        self.lpar_table.setItem(row, 4, QTableWidgetItem("QINTER, QBATCH, QSYSWRK"))
        self.lpar_table.setItem(row, 5, QTableWidgetItem("21:FTP, 22:SSH"))
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
            QMessageBox.warning(self, "Invalid SMTP Port", "Please enter a valid SMTP port.")
            return

        if not smtp_server or not to_addresses or "@" not in from_address:
            QMessageBox.warning(
                self,
                "Incomplete SMTP Settings",
                "Enter an SMTP server, a valid From Address, and at least one recipient.",
            )
            return
        if any("@" not in address for address in to_addresses):
            QMessageBox.warning(self, "Invalid Recipient", "Please check the recipient email addresses.")
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
            QMessageBox.information(self, "Test Email", message)
        else:
            QMessageBox.critical(self, "Test Email Failed", message)

    def save_settings_page(self):
        if self.is_monitoring:
            self.status_label.setText("Status: Stop monitoring to edit settings.")
            self.status_label.setStyleSheet("color: #b45309; font-weight: bold; font-size: 11px; background-color: transparent;")
            return

        new_configs = {}
        new_subsystems = {}
        new_ports = {}
        for row in range(self.lpar_table.rowCount()):
            host_item = self.lpar_table.item(row, 0)
            if not host_item or not host_item.text().strip():
                continue
            host = host_item.text().strip()
            db = self.lpar_table.item(row, 1).text().strip() if self.lpar_table.item(row, 1) else "*LOCAL"
            daily_backup_name = self.lpar_table.item(row, 2).text().strip().upper() if self.lpar_table.item(row, 2) else "DAILYSWA"
            journal_backup_name = self.lpar_table.item(row, 3).text().strip().upper() if self.lpar_table.item(row, 3) else "DAILYSWA"
            subsystems = self.lpar_table.item(row, 4).text().strip() if self.lpar_table.item(row, 4) else ""
            ports = self.lpar_table.item(row, 5).text().strip() if self.lpar_table.item(row, 5) else ""
            new_configs[host.upper()] = {
                "host": host,
                "db": db,
                "daily_backup_name": daily_backup_name or "DAILYSWA",
                "journal_backup_name": journal_backup_name or "DAILYSWA",
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
            new_ports[host.upper()] = parsed_ports

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
            email_alerts=email_alerts,
            login_credentials={"remember": remember_credentials, "username": login_username},
            log_root=logs_root,
        ):
            self.status_label.setText("Error: Save failed. Please try again.")
            self.status_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 11px; background-color: transparent;")
            return

        self.active_server_configs.clear()
        self.active_server_configs.update(new_configs)
        self.backup_management_widget.server_configs = self.active_server_configs
        SERVER_CONFIGS.clear(); SERVER_CONFIGS.update(new_configs)
        EXPECTED_SUBSYSTEMS.clear(); EXPECTED_SUBSYSTEMS.update(new_subsystems)
        EXPECTED_PORTS.clear(); EXPECTED_PORTS.update(new_ports)
        self.rebuild_server_cards()
        self.status_label.setText("Status: Settings saved and applied.")
        self.status_label.setStyleSheet("color: #2ea043; font-weight: bold; font-size: 11px; background-color: transparent;")
        self._settings_saved_snapshot = self._capture_settings_snapshot()
        self._update_settings_save_state()
        QMessageBox.information(self, "Settings Applied", "Settings were saved and applied successfully.")

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

        self.credentials_validated = False
        self._credential_check_results = {}
        self.credential_check_thread = CredentialCheckThread(
            self.active_server_configs,
            username,
            password,
        )
        event_loop = QEventLoop()
        self.credential_check_thread.results_ready.connect(self._handle_credential_check_results)
        self.credential_check_thread.finished.connect(event_loop.quit)
        self.credential_check_thread.start()
        event_loop.exec()
        self.credential_check_thread.deleteLater()
        return self._credential_check_results

    def _handle_credential_check_results(self, results):
        self._credential_check_results = results
        ok_servers = []
        auth_error_servers = []
        log_lines = ["Credential validation log:"]
        for server_name, result in results.items():
            if result.get("status") == "OK":
                ok_servers.append(server_name)
                log_lines.append(f"[{server_name}] OK - Credentials accepted.")
            else:
                auth_error_servers.append(server_name)
                log_lines.append(f"[{server_name}] AUTH_ERROR - {result.get('message', '')}")

        self.cred_log.setPlainText("\n".join(log_lines))

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
        title_color = "#ffffff" if self.is_dark_theme else "#1f2328"
        if hasattr(self, "cards_title"):
            self.cards_title.setStyleSheet(f"color: {title_color}; background-color: transparent;")
        if hasattr(self, "header_title"):
            self.header_title.setStyleSheet(f"color: {title_color}; background-color: transparent;")
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
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(4)

        self.header_title = QLabel(f"Dashboard Active")
        self.header_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        main_layout.addWidget(self.header_title)

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
        main_layout.addWidget(self.retry_status_label)

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

        main_layout.addLayout(top_bar_layout)

        self.status_label = QLabel("Status: Idle. Enter credentials and click 'Check Login Creds'.")
        self.status_label.setStyleSheet("color: #8b949e; font-size: 11px; background-color: transparent;")
        main_layout.addWidget(self.status_label)

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
        self.group_combo.addItems(["Grouping: Prefix (JDAD/JDAP)", "Grouping: Status", "Grouping: None (Grid)"])
        self.group_combo.currentIndexChanged.connect(self.filter_and_sort_cards)
        filter_bar_layout.addWidget(self.group_combo)

        main_layout.addLayout(filter_bar_layout)

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
        main_layout.addWidget(scroll_area, stretch=1)

    def _server_refresh_interval_ms(self, server_name, result=None):
        base = self.min_refresh_interval_ms
        retry_count = self.server_retry_counts.get(server_name, 0)
        if retry_count > 0:
            base = min(max(8000, self.min_refresh_interval_ms // 2), 60000)
            base = min(60000, max(8000, base + retry_count * 5000))
        if result is not None:
            status = str(result.get("status", "OFFLINE")).upper()
            if status in ("OFFLINE", "AUTH_ERROR"):
                base = min(base, 20000)
            duration_ms = int(result.get("sync_duration_ms") or 0)
            if duration_ms >= 7000:
                base = min(base, max(12000, self.min_refresh_interval_ms // 2))
        return max(8000, int(base))

    def _next_retry_delay_ms(self):
        if not self.latest_results_cache:
            return self.min_refresh_interval_ms
        candidate_intervals = []
        for server_name, result in self.latest_results_cache.items():
            candidate_intervals.append(self._server_refresh_interval_ms(server_name, result))
        return max(8000, min(candidate_intervals)) if candidate_intervals else self.min_refresh_interval_ms

    def _register_server_result(self, server_name, result):
        status = str(result.get("status", "OFFLINE")).upper()
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
        elif status in ("OFFLINE", "AUTH_ERROR"):
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

        if not has_vpn_ip():
            self.status_label.setText("Error: VPN connection not detected. Please check your VPN before starting monitoring.")
            self.status_label.setStyleSheet("color: #f85149; font-size: 11px; background-color: transparent;")
            return

        self.refresh_generation += 1
        self.is_monitoring = True
        self.auto_refresh_paused = False
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
        self.is_monitoring = False
        self.auto_refresh_paused = False
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
                delay_ms = self.min_refresh_interval_ms
        timer.setInterval(delay_ms)
        timer.timeout.connect(lambda srv=server_name: self._refresh_single_server(srv))
        timer.start()
        self.server_refresh_timers[server_name] = timer

    def _schedule_independent_refreshes(self):
        """Schedule independent per-server refresh timers based on fetch completion time."""
        for idx, server_name in enumerate(self.active_server_configs.keys()):
            base_delay = self.min_refresh_interval_ms + (idx * 500)
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

    def _refresh_single_server(self, server_name):
        """Refresh a single server independently."""
        if not self.is_monitoring or self.auto_refresh_paused:
            return
        
        if server_name not in self.active_server_configs:
            return
        
        username = self.user_input.text().strip()
        password = self.pass_input.text().strip()
        
        if not username or not password:
            return
        
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

    def _on_single_server_fetched_independent(self, lpar_data, generation, runnable, server_name):
        """Handle individual server fetch completion and schedule next refresh for that server."""
        self.active_runnables.discard(runnable)
        if not self.is_monitoring or generation != self.refresh_generation:
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

        self._refresh_global_status_summary()

        retry_delay = self._next_retry_delay_ms() if self.server_retry_counts.get(server_name, 0) > 0 else self.min_refresh_interval_ms
        self._reschedule_server_timer(server_name, retry_delay)
        self._ensure_server_timers_alive()
        self._hide_sync_loading()

    def _on_single_server_failed(self, failure, generation, runnable, server_name):
        self.active_runnables.discard(runnable)
        if not self.is_monitoring or generation != self.refresh_generation:
            return

        config_key = failure.get("server") or runnable.server
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
            self._refresh_single_server(server_name)

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

        self._reschedule_server_timer(runnable.server, self.min_refresh_interval_ms)
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

        now = time.monotonic()
        should_refresh_history = (
            self.content_stack.currentWidget() is self.logs_analytics_widget
            or (now - self.last_log_history_refresh) >= 30.0
        )
        if should_refresh_history:
            self.log_viewer_widget.load_log_history()
            self.last_log_history_refresh = now
        self.refresh_widget.update_timestamp()
        if not self._refresh_in_progress and not self.active_runnables:
            self._hide_sync_loading()

        if self.is_monitoring and not self.auto_refresh_paused:
            self._schedule_independent_refreshes()

        if self._refresh_queued:
            self._refresh_queued = False
            self.fetch_data()