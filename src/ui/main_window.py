# ui/main_window.py
import sys
import os
import math
import threading
import time
from typing import cast
from collections import deque
from config import APP_VERSION, APP_NAME, load_email_alerts

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt6.QtCore import Qt, QTimer, QThreadPool, QCoreApplication, QPointF, QRectF, QPropertyAnimation, QAbstractAnimation, pyqtProperty, QSize
from PyQt6.QtGui import QColor, QFont, QCursor, QIcon, QPainter, QPen, QPolygonF, QBrush
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout,
    QHBoxLayout, QGroupBox, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QGridLayout, QProgressBar,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QApplication, QSizePolicy, QComboBox
)

from worker import SingleLparRunnable, has_vpn_ip, reset_asp_alert_sound, stop_asp_alert_sound
from ui.log_viewer import LogViewerWidget
from ui.monthly_report import MonthlyReportWidget
from ui.widgets import RefreshStatusWidget, StatusBadgesWidget, SubsystemGridWidget, ThemeLoadingDialog
from dialogs import LparSettingsDialog
from version_worker import VersionCheckWorker
from config import SERVER_CONFIGS, EXPECTED_SUBSYSTEMS, get_resource_path
from ui.styles import DARK_STYLESHEET, LIGHT_STYLESHEET


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
        self.jobs_val_label = QLabel("0")
        self.jobs_val_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        
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
        for label in (self.name_label, self.jobs_val_label):
            label.setStyleSheet(f"color: {value_color}; background-color: transparent;")

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
        subsystems = data.get("subsystems", [])
        ports = data.get("ports", [])

        if status in ("OFFLINE", "AUTH_ERROR", "STALE") and self.current_status not in ("OFFLINE", "AUTH_ERROR", "STALE"):
            last_cpu = float(self.current_cpu) if self.current_cpu else cpu
            last_asp = float(self.current_asp) if self.current_asp else asp
            last_jobs = int(self.current_jobs) if hasattr(self, "current_jobs") and self.current_jobs else jobs
            cpu = last_cpu
            asp = last_asp
            jobs = last_jobs
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
    self.setMinimumHeight(340)  # Tightened height to remove empty space
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
        " font-weight: bold; letter-spacing: 0.5px;'>AS/400 QUANTUM</h2>"
        "<div style='color: #cbd5e1; font-size: 12px; margin-bottom: 12px;'>"
        "<b>System:</b> IBM i (AS/400) Real-time Monitoring & Telemetry<br>"
        "<b>Stack:</b> Python | SQL<br>"
        f"<b>Version:</b> {version_str}"
        "</div>"
        "<hr style='border: none; border-top: 1px solid #334155; margin: 12px"
        " 0;'>"
        "<div style='margin-bottom: 12px;'>"
        "<b style='color: #f1f5f9; font-size: 13px;'>Key Features:</b>"
        "<ul style='margin: 6px 0 0 16px; padding: 0; color: #cbd5e1;"
        " font-size: 12px; line-height: 1.6;'>"
        "<li>Real-Time LPAR Health Monitoring (CPU / ASP / Active Jobs)</li>"
        "<li>System Service & Subsystem Status Tracking</li>"
        "<li>Historical Log Vault & Daily LPAR Summaries</li>"
        "<li>Monthly Performance Analytics & Excel Reporting</li>"
        "</ul>"
        "</div>"
        "<hr style='border: none; border-top: 1px solid #334155; margin: 12px"
        " 0;'>"
        "<div style='color: #94a3b8; font-size: 11px; line-height: 1.5;'>"
        "<b>© 2026 Reymart De Lara.</b> All Rights Reserved.<br>"
        "<span style='color: #cbd5e1;'>Created & Developed by Reymart De"
        " Lara</span><br>"
        "<span style='color: #64748b; font-size: 10px;'>IBM i and AS/400 are"
        " registered trademarks of IBM Corp.</span>"
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

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.live_monitor_widget = QWidget()
        self.init_live_monitor_ui()
        self.tabs.addTab(self.live_monitor_widget, "📊 Live Monitor")

        self.log_viewer_widget = LogViewerWidget()
        self.log_viewer_widget.set_theme(self.is_dark_theme)
        self.tabs.addTab(self.log_viewer_widget, "📜 Log Viewer History")

        self.monthly_report_widget = MonthlyReportWidget()
        self.log_viewer_widget.monthly_report_widget = self.monthly_report_widget
        setattr(self.monthly_report_widget, "parent_log_viewer", self.log_viewer_widget)
        self.monthly_report_widget.set_theme(self.is_dark_theme)
        self.tabs.addTab(self.monthly_report_widget, "📈 Monthly ASP/CPU Report")

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
        self.cards_title.setStyleSheet(f"color: {title_color}; background-color: transparent;")
        self.header_title.setStyleSheet(f"color: {title_color}; background-color: transparent;")
        self.global_alerts.set_theme(self.is_dark_theme)
        self.refresh_widget.set_theme(self.is_dark_theme)
        self.log_viewer_widget.set_theme(self.is_dark_theme)
        if hasattr(self, 'monthly_report_widget'):
            self.monthly_report_widget.set_theme(self.is_dark_theme)
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

    def init_live_monitor_ui(self):
        main_layout = QVBoxLayout(self.live_monitor_widget)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(4)

        self.header_title = QLabel(f"Dashboard Active")
        self.header_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        main_layout.addWidget(self.header_title)

        top_bar_layout = QHBoxLayout()
        top_bar_layout.setSpacing(10)

        cred_group = QGroupBox("IBM i Access Credentials")
        cred_layout = QHBoxLayout(cred_group)
        cred_layout.setContentsMargins(10, 4, 10, 4)
        cred_layout.setSpacing(6)

        lbl_user = QLabel("Username:")
        lbl_user.setStyleSheet("background-color: transparent;")
        cred_layout.addWidget(lbl_user)

        self.user_input = QLineEdit("")
        self.user_input.setPlaceholderText("Username")
        self.user_input.setFont(QFont("Segoe UI", 9))
        self.user_input.setFixedWidth(100)
        cred_layout.addWidget(self.user_input)

        lbl_pass = QLabel("Password:")
        lbl_pass.setStyleSheet("background-color: transparent;")
        cred_layout.addWidget(lbl_pass)

        self.pass_input = QLineEdit("")
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setFont(QFont("Segoe UI", 9))
        self.pass_input.setFixedWidth(100)
        cred_layout.addWidget(self.pass_input)

        self.toggle_btn = QPushButton("Start Auto-Refresh")
        self.toggle_btn.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.toggle_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.toggle_btn.clicked.connect(self.toggle_monitoring)
        cred_layout.addWidget(self.toggle_btn)

        self.settings_btn = QPushButton("⚙️ Settings")
        self.settings_btn.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.settings_btn.clicked.connect(self.open_lpar_settings)
        cred_layout.addWidget(self.settings_btn)

        top_bar_layout.addWidget(cred_group, stretch=0)

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

        self.theme_btn = QPushButton("☀ Light Theme")
        self.theme_btn.setFixedHeight(35)
        self.theme_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.theme_btn.setToolTip("Switch between dark and light themes")
        self.theme_btn.clicked.connect(self.toggle_theme)
        top_bar_layout.addWidget(self.theme_btn, stretch=0, alignment=Qt.AlignmentFlag.AlignVCenter)

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

        self.status_label = QLabel("Status: Idle. Enter credentials and click 'Start Auto-Refresh'.")
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
        if self.is_monitoring:
            if self.is_dark_theme:
                self.toggle_btn.setStyleSheet("""
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
                """)
            else:
                self.toggle_btn.setStyleSheet("""
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
                """)
        else:
            self.toggle_btn.setStyleSheet("""
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
            """)

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
        self.user_input.setEnabled(False)
        self.pass_input.setEnabled(False)
        self.settings_btn.setEnabled(False)
        self.retry_status_label.setVisible(False)
        
        self.toggle_btn.setText("Stop Auto-Refresh")
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

        self.user_input.setEnabled(True)
        self.pass_input.setEnabled(True)
        self.settings_btn.setEnabled(True)
        
        self.toggle_btn.setText("Start Auto-Refresh")
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
            self.tabs.currentWidget() is self.log_viewer_widget
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