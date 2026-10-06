import paramiko
import ast
import re
import sys
import smtplib
import webbrowser
from math import atan2, degrees, hypot
from typing import Optional, cast
from email.message import EmailMessage
from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt, QRectF
from PyQt6.QtGui import QColor, QCursor, QFont, QMouseEvent, QPainter, QPen
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView,
    QApplication, QMessageBox, QGroupBox, QCheckBox,
    QWidget, QTabWidget, QGridLayout, QProgressBar, QScrollArea, QFrame
)
import config
from config import (
    SERVER_CONFIGS,
    EXPECTED_SUBSYSTEMS,
    EXPECTED_PORTS,
)
from ui.setcreds import load_email_alerts, save_all_configs


def show_information_dialog(parent, title, text, *args, **kwargs):
    return QMessageBox.information(parent, title, text, *args, **kwargs)


def show_warning_dialog(parent, title, text, *args, **kwargs):
    return QMessageBox.warning(parent, title, text, *args, **kwargs)


def show_critical_dialog(parent, title, text, *args, **kwargs):
    return QMessageBox.critical(parent, title, text, *args, **kwargs)


def ask_question_dialog(parent, title, text, *args, **kwargs):
    return QMessageBox.question(parent, title, text, *args, **kwargs)


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
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

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
            "System: IBM i (AS/400) Real-time Monitoring & Telemetry<br>"
            "Developer: Reymart De Lara<br>"
            "<h2 style='margin: 0 0 6px 0; color: #38bdf8; font-size: 18px;"
            " font-weight: bold; letter-spacing: 0.5px;'>AS400 QUANTUM SUITE</h2>"
            "<div style='color: #cbd5e1; font-size: 12px; margin-bottom: 12px;'>"
            "Stack: Python | SQL<br>"
            "Stack Runtime: Python | PyQt6 | SQL | DB2/ODBC<br>"
            f"Version: {version_str}"
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


class ThemeLoadingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(240, 90)

        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app is not None else True
        bg_clr = "#161b22" if is_dark else "#ffffff"
        text_clr = "#ffffff" if is_dark else "#1f2328"
        border_clr = "#30363d" if is_dark else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {bg_clr}; border: 1px solid {border_clr}; border-radius: 8px; }}
            QLabel {{ color: {text_clr}; font-family: "Segoe UI", sans-serif; font-size: 12px; font-weight: bold; }}
            QProgressBar {{ border: none; background-color: {"#21262d" if is_dark else "#e1e4e8"}; height: 4px; border-radius: 2px; }}
            QProgressBar::chunk {{ background-color: #238636; border-radius: 2px; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel("Switching Theme...", self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        self.pbar = QProgressBar(self)
        self.pbar.setRange(0, 0)
        layout.addWidget(self.pbar)


class ItemDetailDialog(QDialog):
    def __init__(self, title_text, status_bool, command_text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Command Detail")
        self.setFixedSize(320, 200)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)

        app = QApplication.instance()
        is_dark_theme = bool(app.property("is_dark_theme")) if app is not None else True
        dialog_bg = "#161b22" if is_dark_theme else "#ffffff"
        text_clr = "#ffffff" if is_dark_theme else "#1f2328"
        muted_clr = "#8b949e" if is_dark_theme else "#57606a"
        surface = "#21262d" if is_dark_theme else "#eaeef2"
        border = "#30363d" if is_dark_theme else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; border: 1px solid {border}; border-radius: 8px; }}
        """)

        self.reset_timer = QTimer(self)
        self.reset_timer.setSingleShot(True)
        self.reset_timer.timeout.connect(self.reset_button_text)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        title_label = QLabel(title_text)
        title_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        title_label.setStyleSheet(f"color: {text_clr}; background: transparent; border: none;")
        layout.addWidget(title_label)

        status_str = "UP" if status_bool else "DOWN"
        status_color = "#3fb950" if status_bool else "#f85149"
        status_label = QLabel(f"Status: {status_str}")
        status_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        status_label.setStyleSheet(f"color: {status_color}; background: transparent; border: none;")
        layout.addWidget(status_label)
        cmd_label = QLabel(f"Cmd: {command_text}")
        cmd_label.setFont(QFont("Consolas", 9))
        cmd_label.setWordWrap(True)
        cmd_label.setStyleSheet(f"color: {muted_clr}; background: transparent; border: none;")
        layout.addWidget(cmd_label)
        layout.addStretch()

        self.copy_btn = QPushButton("Copy Start Command")
        self.copy_btn.setFixedHeight(30)
        self.copy_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.copy_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {surface}; color: {text_clr}; border: 1px solid {border}; border-radius: 6px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {border}; color: {'#ffffff' if is_dark_theme else '#1f2328'}; }}
        """)
        self.copy_btn.clicked.connect(lambda: self.copy_command(command_text))
        layout.addWidget(self.copy_btn)

    def show_smart(self):
        cursor_pos = QCursor.pos()
        screen = QApplication.screenAt(cursor_pos) or QApplication.primaryScreen()
        if screen is None:
            self.exec()
            return
        screen_geo = screen.availableGeometry()
        dialog_w = self.width()
        dialog_h = self.height()
        x = cursor_pos.x() - (dialog_w // 2)
        y = cursor_pos.y() - (dialog_h // 2)
        margin = 10
        x = max(screen_geo.left() + margin, min(x, screen_geo.right() - dialog_w - margin))
        y = max(screen_geo.top() + margin, min(y, screen_geo.bottom() - dialog_h - margin))
        self.move(x, y)
        self.exec()

    def copy_command(self, cmd):
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(cmd)
        self.copy_btn.setText("✓ Copied!")
        self.reset_timer.start(1500)

    def reset_button_text(self):
        if hasattr(self, "copy_btn") and self.copy_btn:
            self.copy_btn.setText("Copy Start Command")

    def reject(self):
        if hasattr(self, "reset_timer"):
            self.reset_timer.stop()
        super().reject()


class SubsystemStatusDialog(QDialog):
    def __init__(self, lpar_name, subsystems, is_dark_theme, title_font, parent=None):
        super().__init__(parent)
        self.has_items = False
        self.setWindowTitle(f"Subsystem Status - {lpar_name}")
        self.setMinimumWidth(480)
        dialog_bg = "#161b22" if is_dark_theme else "#ffffff"
        dialog_text = "#c9d1d9" if is_dark_theme else "#1f2328"
        self.setStyleSheet(f"QDialog {{ background-color: {dialog_bg}; color: {dialog_text}; }}")

        all_display_items = []
        for sub in subsystems:
            sub_name = ""
            status = "ACTIVE"
            if isinstance(sub, dict):
                sub_name = sub.get("name", "")
                status = str(sub.get("status", "ACTIVE")).upper()
            elif isinstance(sub, str):
                s_str = sub.strip()
                if s_str.startswith("{") and s_str.endswith("}"):
                    try:
                        parsed = ast.literal_eval(s_str)
                        if isinstance(parsed, dict):
                            sub_name = parsed.get("name", "")
                            status = str(parsed.get("status", "ACTIVE")).upper()
                    except Exception:
                        sub_name = s_str
                else:
                    sub_name = s_str
            else:
                sub_name = str(sub)
            clean_name = str(sub_name).strip().upper()
            if clean_name:
                all_display_items.append((clean_name, status in ["INACTIVE", "DOWN", "INACTIVE/OFF", "OFF"]))

        if not all_display_items:
            return
        self.has_items = True

        layout = QVBoxLayout(self)
        title_label = QLabel(f"Subsystems status on {lpar_name}:")
        title_label.setFont(title_font)
        title_label.setStyleSheet(
            "color: #ffffff; margin-bottom: 8px;"
            if is_dark_theme
            else "color: #1f2328; margin-bottom: 8px;"
        )
        layout.addWidget(title_label)
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setSpacing(6)
        for idx, (sub_name, is_down) in enumerate(all_display_items):
            if is_down:
                badge_style = (
                    "background-color: #3c1618; color: #f85149; border: 1px solid #f85149; "
                    "border-radius: 4px; padding: 4px 8px; font-weight: bold; font-size: 11px;"
                )
                badge_text = f"[DOWN] {sub_name}"
            else:
                badge_style = (
                    "background-color: #0d281e; color: #3fb950; border: 1px solid #1e4b33; "
                    "border-radius: 4px; padding: 4px 8px; font-weight: bold; font-size: 11px;"
                )
                badge_text = f"[ACTIVE] {sub_name}"
            badge = QLabel(badge_text)
            badge.setStyleSheet(badge_style)
            grid.addWidget(badge, idx // 3, idx % 3)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(grid_widget)
        layout.addWidget(scroll)


class ActiveJobTableItem(QTableWidgetItem):
    def __init__(self, text, sort_value=None):
        super().__init__(text)
        self.sort_value = sort_value

    def __lt__(self, other):
        if isinstance(other, ActiveJobTableItem):
            if isinstance(self.sort_value, (int, float)) and isinstance(other.sort_value, (int, float)):
                return self.sort_value < other.sort_value
        return super().__lt__(other)


class ActiveJobsDialog(QDialog):
    COLUMNS = [
        ("Job Number / Job User / Job Name", "job_name"),
        ("JOB_STATUS", "job_status"),
        ("TEMPORARY_STORAGE", "temporary_storage"),
        ("CPU_TIME", "cpu_time"),
    ]

    def __init__(self, server_name, jobs=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{server_name} - Active Jobs")
        self.resize(790, 560)
        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        dialog_bg = "#0d1117" if is_dark else "#f6f8fa"
        surface = "#161b22" if is_dark else "#ffffff"
        header_bg = "#21262d" if is_dark else "#eaeef2"
        text = "#c9d1d9" if is_dark else "#1f2328"
        muted = "#8b949e" if is_dark else "#57606a"
        border = "#30363d" if is_dark else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; color: {text}; }}
            QLineEdit {{ background-color: {surface}; color: {text}; border: 1px solid {border}; padding: 6px 10px; }}
            QTableWidget {{ background-color: {surface}; color: {text}; gridline-color: {border}; border: 1px solid {border}; }}
            QTableWidget::item {{ color: {text}; padding: 6px; }}
            QTableWidget::item:selected {{ background-color: #1f6feb; color: #ffffff; }}
            QHeaderView::section {{ background-color: {header_bg}; color: {muted}; padding: 8px; border: none; border-bottom: 1px solid {border}; }}
        """)
        layout = QVBoxLayout(self)

        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search active jobs")
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self._apply_search)
        self.search_input.returnPressed.connect(self._apply_search)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_button)
        layout.addLayout(search_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([label for label, _ in self.COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        cast(QHeaderView, self.table.verticalHeader()).setVisible(False)
        self.table.setSortingEnabled(True)
        header = cast(QHeaderView, self.table.horizontalHeader())
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((250, 150, 200, 150)):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        layout.addWidget(self.table)
        self._jobs = []
        self.update_jobs(jobs or [])

    def update_jobs(self, jobs):
        self._jobs = [job for job in jobs if isinstance(job, dict)]
        self.setWindowTitle(f"{self.windowTitle().split(' - ')[0]} - {len(self._jobs):,} Active Jobs")
        self._apply_search()

    def _apply_search(self):
        search_text = self.search_input.text().strip().lower()
        rows = [
            job for job in self._jobs
            if not search_text or search_text in " ".join(
                str(job.get(key, "")) for _, key in self.COLUMNS
            ).lower()
        ]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row_index, job in enumerate(rows):
            for column_index, (_, key) in enumerate(self.COLUMNS):
                value = job.get(key, "")
                sort_value = None
                if key in {"temporary_storage", "cpu_time"}:
                    try:
                        sort_value = float(value)
                    except (TypeError, ValueError):
                        pass
                self.table.setItem(
                    row_index,
                    column_index,
                    ActiveJobTableItem(str(value), sort_value),
                )
        self.table.setSortingEnabled(True)


class StoragePoolDonutCanvas(QWidget):
    COLORS = ("#7caf50", "#d5bd30", "#4169d8", "#df8b2e", "#d95462", "#41a6a6")

    def __init__(self, parent=None):
        super().__init__(parent)
        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        self.background_color = QColor("#161b22" if is_dark else "#ffffff")
        self.entries = []
        self.value_key = ""
        self.loaded = False
        self.setMinimumHeight(170)
        self.setMouseTracking(True)

    def set_data(self, entries, value_key, loaded):
        self.entries = [entry for entry in entries if isinstance(entry, dict)]
        self.value_key = value_key
        self.loaded = loaded
        self.update()

    def _values_and_total(self):
        values = []
        for entry in self.entries:
            try:
                value = float(entry.get(self.value_key, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                values.append((entry, value))
        return values, sum(value for _, value in values)

    def _tooltip_at(self, position):
        values, total = self._values_and_total()
        if total <= 0:
            return ""

        diameter = max(80.0, min(self.width() * 0.55, self.height() - 12))
        center_x = self.width() / 2
        center_y = self.height() / 2
        distance = hypot(position.x() - center_x, position.y() - center_y)
        if distance < diameter * 0.55 / 2 or distance > diameter / 2:
            return ""

        angle = (degrees(atan2(center_y - position.y(), position.x() - center_x)) - 90) % 360
        start_angle = 0.0
        for entry, value in values:
            span_angle = value / total * 360
            if start_angle <= angle < start_angle + span_angle:
                if self.value_key == "current_size_mb":
                    metric = f"Current size: {value:,.2f} MB"
                else:
                    formatted_value = f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
                    metric = f"Threads: {formatted_value}"
                return f"{entry.get('pool_name', '')}\n{metric}\nShare: {value / total:.1%}"
            start_angle += span_angle
        return ""

    def mouseMoveEvent(self, a0: QMouseEvent | None):
        if a0 is not None:
            self.setToolTip(self._tooltip_at(a0.position()))
        super().mouseMoveEvent(a0)

    def paintEvent(self, a0):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        values, total = self._values_and_total()
        if total <= 0:
            message = "No pool data" if self.loaded else "Gathering data..."
            painter.setPen(self.palette().color(self.foregroundRole()))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)
            return

        diameter = min(self.width() * 0.55, self.height() - 12)
        diameter = max(80.0, diameter)
        left = (self.width() - diameter) / 2
        top = (self.height() - diameter) / 2
        pie_rect = QRectF(left, top, diameter, diameter)
        start_angle = 90 * 16
        for index, (_, value) in enumerate(values):
            span_angle = round(value / total * 360 * 16)
            painter.setPen(QPen(self.background_color, 1.5))
            painter.setBrush(QColor(self.COLORS[index % len(self.COLORS)]))
            painter.drawPie(pie_rect, start_angle, span_angle)
            start_angle += span_angle

        hole_size = diameter * 0.55
        hole_rect = QRectF(
            pie_rect.center().x() - hole_size / 2,
            pie_rect.center().y() - hole_size / 2,
            hole_size,
            hole_size,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.background_color)
        painter.drawEllipse(hole_rect)


class StoragePoolDonutChart(QGroupBox):
    def __init__(self, title, value_key, parent=None):
        super().__init__(title, parent)
        self.value_key = value_key
        self.entries = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(3)
        self.canvas = StoragePoolDonutCanvas(self)
        layout.addWidget(self.canvas, stretch=1)
        self.legend_layout = QHBoxLayout()
        self.legend_layout.setContentsMargins(0, 0, 0, 0)
        self.legend_layout.setSpacing(8)
        layout.addLayout(self.legend_layout)

    def set_data(self, entries, loaded):
        self.entries = [entry for entry in entries if isinstance(entry, dict)]
        self.canvas.set_data(self.entries, self.value_key, loaded)
        while self.legend_layout.count():
            item = self.legend_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for index, entry in enumerate(self.entries):
            swatch = QWidget()
            swatch.setFixedSize(8, 8)
            swatch.setStyleSheet(
                f"background-color: {StoragePoolDonutCanvas.COLORS[index % len(StoragePoolDonutCanvas.COLORS)]};"
            )
            label = QLabel(str(entry.get("pool_name", "")))
            label.setToolTip(f"{entry.get('pool_name', '')}: {entry.get(self.value_key, 0)}")
            self.legend_layout.addWidget(swatch)
            self.legend_layout.addWidget(label)
        self.legend_layout.addStretch()


class ObjectStatisticsDialog(QDialog):
    COLUMNS = [
        ("OBJECT_NAME", "object_name"),
        ("LIBRARY", "library"),
        ("OBJECT_TYPE", "object_type"),
        ("ATTRIBUTE", "attribute"),
        ("SIZE_GB", "size_gb"),
        ("PCT_OF_ASP", "pct_of_asp"),
        ("DEFINER", "definer"),
    ]

    def __init__(
        self,
        server_name,
        rows=None,
        error="",
        loaded=False,
        updated_at="",
        storage_pools=None,
        pool_threads=None,
        parent=None,
        top_temporary_storage_jobs=None,
        top_temporary_storage_jobs_error="",
        top_temporary_storage_jobs_loaded=None,
        pool_data_loaded=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"{server_name} - System Memory & ASP Storage")
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            geometry = screen.availableGeometry()
            self.resize(int(geometry.width() * 0.5), int(geometry.height() * 0.75))
        else:
            self.resize(1080, 620)

        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        dialog_bg = "#0d1117" if is_dark else "#f6f8fa"
        surface = "#161b22" if is_dark else "#ffffff"
        header_bg = "#21262d" if is_dark else "#eaeef2"
        text = "#c9d1d9" if is_dark else "#1f2328"
        muted = "#8b949e" if is_dark else "#57606a"
        border = "#30363d" if is_dark else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; color: {text}; }}
            QTableWidget {{ background-color: {surface}; color: {text}; gridline-color: {border}; border: 1px solid {border}; }}
            QTableWidget::item {{ color: {text}; padding: 6px; }}
            QTableWidget::item:selected {{ background-color: #1f6feb; color: #ffffff; }}
            QHeaderView::section {{ background-color: {header_bg}; color: {muted}; padding: 8px; border: none; border-bottom: 1px solid {border}; }}
        """)

        layout = QVBoxLayout(self)
        status_layout = QHBoxLayout()
        self.status_label = QLabel()
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        self.last_update_label = QLabel()
        self.last_update_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_layout.addWidget(self.last_update_label)
        pool_charts_layout = QHBoxLayout()
        pool_charts_layout.setSpacing(8)
        self.pool_size_chart = StoragePoolDonutChart("Pool current size", "current_size_mb", self)
        self.pool_threads_chart = StoragePoolDonutChart("Number of threads", "current_threads", self)
        pool_charts_layout.addWidget(self.pool_size_chart, stretch=1)
        pool_charts_layout.addWidget(self.pool_threads_chart, stretch=1)
        layout.addLayout(pool_charts_layout)
        self.temp_storage_title = QLabel(
            "Top 20 Jobs by Temporary Storage (refreshes with live cards)"
        )
        self.temp_storage_status_label = QLabel()
        self.temp_storage_table = QTableWidget()
        self.temp_storage_table.setColumnCount(5)
        self.temp_storage_table.setHorizontalHeaderLabels([
            "JOB_NAME", "USER_NAME", "TEMP_STORAGE_MB", "TEMP_STORAGE_GB", "PCT_OF_ASP"
        ])
        self.temp_storage_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.temp_storage_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.temp_storage_table.setWordWrap(False)
        self.temp_storage_table.setAlternatingRowColors(True)
        self.temp_storage_table.setMaximumHeight(230)
        cast(QHeaderView, self.temp_storage_table.verticalHeader()).setVisible(False)
        temp_header = cast(QHeaderView, self.temp_storage_table.horizontalHeader())
        temp_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        temp_header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.temp_storage_title)
        layout.addWidget(self.temp_storage_status_label)
        layout.addWidget(self.temp_storage_table)
        self.status_layout = status_layout
        layout.addLayout(self.status_layout)
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([label for label, _ in self.COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        cast(QHeaderView, self.table.verticalHeader()).setVisible(False)
        header = cast(QHeaderView, self.table.horizontalHeader())
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.table)
        self.update_data(
            rows or [],
            error,
            loaded,
            updated_at,
            storage_pools or [],
            pool_threads or [],
            pool_data_loaded=pool_data_loaded,
        )
        self.update_temporary_storage_jobs(
            top_temporary_storage_jobs or [],
            top_temporary_storage_jobs_error,
            loaded if top_temporary_storage_jobs_loaded is None else top_temporary_storage_jobs_loaded,
        )

    def update_data(
        self, rows, error="", loaded=False, updated_at="", storage_pools=None,
        pool_threads=None, top_temporary_storage_jobs=None,
        top_temporary_storage_jobs_error="", pool_data_loaded=None,
    ):
        self.last_update_label.setText(
            f"Pool charts refresh with live cards | Object statistics update every 3 hours | "
            f"Last update: {updated_at or 'Not available'}"
        )
        charts_loaded = loaded if pool_data_loaded is None else pool_data_loaded
        self.pool_size_chart.set_data(storage_pools or [], charts_loaded)
        self.pool_threads_chart.set_data(pool_threads or [], charts_loaded)
        if top_temporary_storage_jobs is not None or top_temporary_storage_jobs_error:
            self.update_temporary_storage_jobs(
                top_temporary_storage_jobs or [],
                top_temporary_storage_jobs_error,
                loaded,
            )

        file_rows = [row for row in rows if isinstance(row, dict)]
        if error and file_rows:
            self.status_label.setText(f"Showing cached data; refresh failed: {error}")
        elif error:
            self.status_label.setText(f"Could not load file statistics: {error}")
        elif not loaded:
            self.status_label.setText("Gathering data...")
        elif not file_rows:
            self.status_label.setText("No object statistics found.")
        else:
            self.status_label.setText(f"{len(file_rows):,} objects")

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(file_rows))
        for row_index, file_row in enumerate(file_rows):
            for column_index, (_, key) in enumerate(self.COLUMNS):
                value = file_row.get(key, "")
                sort_value = None
                if key in {"size_gb", "pct_of_asp"}:
                    try:
                        sort_value = float(value)
                        decimals = 2 if key == "size_gb" else 4
                        value = f"{sort_value:.{decimals}f}"
                        if key == "pct_of_asp":
                            value += "%"
                    except (TypeError, ValueError):
                        value = str(value)
                item = ActiveJobTableItem(str(value), sort_value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, column_index, item)
        self.table.setSortingEnabled(True)
        if file_rows:
            self.table.sortItems(4, Qt.SortOrder.DescendingOrder)

    def update_temporary_storage_jobs(self, jobs, error="", loaded=False):
        temporary_jobs = [row for row in (jobs or []) if isinstance(row, dict)]
        if error:
            self.temp_storage_status_label.setText(
                f"Could not load active job storage: {error}"
            )
        elif not loaded:
            self.temp_storage_status_label.setText("Gathering active job storage...")
        elif not temporary_jobs:
            self.temp_storage_status_label.setText("No active job storage data found.")
        else:
            self.temp_storage_status_label.clear()
        self.temp_storage_status_label.setVisible(bool(self.temp_storage_status_label.text()))
        self.temp_storage_table.setRowCount(len(temporary_jobs))
        for row_index, job in enumerate(temporary_jobs):
            values = (
                job.get("job_name", ""),
                job.get("user_name", ""),
                job.get("temp_storage_mb", 0),
                job.get("temp_storage_gb", 0.0),
                job.get("pct_of_asp", ""),
            )
            for column_index, value in enumerate(values):
                if column_index == 2:
                    try:
                        value = f"{float(value):,.0f}"
                    except (TypeError, ValueError):
                        value = str(value)
                elif column_index == 3:
                    try:
                        value = f"{float(value):,.2f}"
                    except (TypeError, ValueError):
                        value = str(value)
                item = ActiveJobTableItem(str(value).strip())
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.temp_storage_table.setItem(row_index, column_index, item)


class TopCpuJobsDialog(QDialog):
    COLUMNS = [
        ("JOB_NAME", "job_name"),
        ("AUTHORIZATION_NAME", "authorization_name"),
        ("CPU_TIME", "cpu_time"),
        ("ELAPSED_CPU_PERCENTAGE", "elapsed_cpu_percentage"),
        ("RUN_PRIORITY", "run_priority"),
        ("JOB_STATUS", "job_status"),
    ]

    def __init__(self, server_name, jobs=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{server_name} - Top CPU Jobs")
        screen = self.screen() or QApplication.primaryScreen()
        dialog_width = int(screen.availableGeometry().width() * 0.9) if screen else 1080
        self.resize(dialog_width, 520)
        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        dialog_bg = "#0d1117" if is_dark else "#f6f8fa"
        surface = "#161b22" if is_dark else "#ffffff"
        header_bg = "#21262d" if is_dark else "#eaeef2"
        text = "#c9d1d9" if is_dark else "#1f2328"
        muted = "#8b949e" if is_dark else "#57606a"
        border = "#30363d" if is_dark else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; color: {text}; }}
            QTableWidget {{ background-color: {surface}; color: {text}; gridline-color: {border}; border: 1px solid {border}; }}
            QTableWidget::item {{ color: {text}; padding: 6px; }}
            QTableWidget::item:selected {{ background-color: #1f6feb; color: #ffffff; }}
            QHeaderView::section {{ background-color: {header_bg}; color: {muted}; padding: 8px; border: none; border-bottom: 1px solid {border}; }}
        """)
        layout = QVBoxLayout(self)
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([label for label, _ in self.COLUMNS])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        cast(QHeaderView, self.table.verticalHeader()).setVisible(False)
        self.table.setSortingEnabled(True)
        header = cast(QHeaderView, self.table.horizontalHeader())
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.table)
        self.update_jobs(jobs or [])

    def update_jobs(self, jobs):
        top_jobs = sorted(
            (
                job for job in jobs
                if isinstance(job, dict)
                and _numeric_sort_value(job.get("elapsed_cpu_percentage")) > 0
            ),
            key=lambda job: _numeric_sort_value(job.get("elapsed_cpu_percentage")),
            reverse=True,
        )
        self.setWindowTitle(f"{self.windowTitle().split(' - ')[0]} - Top CPU Jobs ({len(top_jobs)})")
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(top_jobs))
        for row_index, job in enumerate(top_jobs):
            for column_index, (_, key) in enumerate(self.COLUMNS):
                value = job.get(key, "")
                sort_value = _numeric_sort_value(value) if key in {"cpu_time", "elapsed_cpu_percentage", "run_priority"} else None
                if key == "elapsed_cpu_percentage" and value != "":
                    value = f"{value}%"
                item = ActiveJobTableItem(str(value), sort_value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, column_index, item)
        self.table.setSortingEnabled(True)


def _numeric_sort_value(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class WebAppJobsDialog(QDialog):
    COLUMNS = [
        "Web App",
        "Job Number / Job User / Job Name",
        "JOB_STATUS",
        "TEMPORARY_STORAGE",
        "CPU_TIME",
        "PORT",
    ]

    def __init__(self, server_name, web_apps=None, jobs_by_app=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{server_name} - Web apps")
        self.resize(1280, 620)

        app = QApplication.instance()
        is_dark = bool(app.property("is_dark_theme")) if app and app.property("is_dark_theme") is not None else True
        dialog_bg = "#0d1117" if is_dark else "#f6f8fa"
        surface = "#161b22" if is_dark else "#ffffff"
        header_bg = "#21262d" if is_dark else "#eaeef2"
        text = "#c9d1d9" if is_dark else "#1f2328"
        muted = "#8b949e" if is_dark else "#57606a"
        border = "#30363d" if is_dark else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; color: {text}; }}
            QTableWidget {{ background-color: {surface}; color: {text}; gridline-color: {border}; border: 1px solid {border}; }}
            QTableWidget::item {{ color: {text}; padding: 6px; }}
            QHeaderView::section {{ background-color: {header_bg}; color: {muted}; padding: 8px; border: none; border-bottom: 1px solid {border}; }}
        """)

        layout = QVBoxLayout(self)
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        cast(QHeaderView, self.table.verticalHeader()).setVisible(False)
        header = cast(QHeaderView, self.table.horizontalHeader())
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((130, 340, 140, 210, 140, 100)):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        layout.addWidget(self.table)
        self.update_data(web_apps or [], jobs_by_app or {})

    def update_data(self, web_apps, jobs_by_app):
        apps_by_name = {}
        for app in web_apps or []:
            if isinstance(app, dict):
                name = str(app.get("name") or "").strip().upper()
                if name:
                    apps_by_name[name] = app

        app_names = sorted(set(apps_by_name) | {str(name).strip().upper() for name in (jobs_by_app or {}) if str(name).strip()})
        grouped_rows = []
        for app_name in app_names:
            app = apps_by_name.get(app_name, {})
            app_jobs = (jobs_by_app or {}).get(app_name, [])
            app_jobs = [job for job in app_jobs if isinstance(job, dict)]
            port = app.get("port", "")
            if not app_jobs:
                app_jobs = [{
                    "job_name": "No active jobs",
                    "job_status": "",
                    "temporary_storage": "",
                    "cpu_time": "",
                }]
            grouped_rows.append((app_name, str(port), app_jobs))

        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(sum(len(rows) for _, _, rows in grouped_rows))
        row_index = 0
        separator_rows = set()
        for app_name, port, app_jobs in grouped_rows:
            group_start = row_index
            if group_start:
                separator_rows.add(group_start - 1)
            for job in app_jobs:
                values = (
                    app_name,
                    job.get("job_name", ""),
                    job.get("job_status", ""),
                    job.get("temporary_storage", ""),
                    job.get("cpu_time", ""),
                    port,
                )
                for column, value in enumerate(values):
                    if column in (0, 5) and row_index > group_start:
                        continue
                    item = QTableWidgetItem(str(value))
                    if column in (0, 5):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.table.setItem(row_index, column, item)
                row_index += 1
            group_size = len(app_jobs)
            if group_size > 1:
                self.table.setSpan(group_start, 0, group_size, 1)
                self.table.setSpan(group_start, 5, group_size, 1)
        self.table.separator_rows = separator_rows
        self.table.setSortingEnabled(False)


class TestEmailThread(QThread):
    result_ready = pyqtSignal(bool, str)

    def __init__(self, smtp_server, port, use_tls, username, password, from_address, to_addresses):
        super().__init__()
        self.smtp_server = smtp_server
        self.port = port
        self.use_tls = use_tls
        self.username = username
        self.password = password
        self.from_address = from_address
        self.to_addresses = to_addresses

    def run(self):
        try:
            msg = LparSettingsDialog._build_test_message(self.from_address, self.to_addresses)

            if self.use_tls:
                smtp = smtplib.SMTP(self.smtp_server, self.port, timeout=15)
                smtp.starttls()
            else:
                smtp = smtplib.SMTP(self.smtp_server, self.port, timeout=15)

            try:
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(msg)
                self.result_ready.emit(True, "Test email sent successfully.")
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
        except Exception as exc:
            self.result_ready.emit(False, f"Sending test email failed: {str(exc)}")


class AppExpirationDialog(QDialog):
    def __init__(self, title: str, message: str, download_url: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.download_url = download_url

        self.setWindowTitle(title)
        self.setFixedSize(420, 220)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setModal(True)

        self.setStyleSheet("""
            QDialog {
                background-color: #0d1117;
                color: #c9d1d9;
            }
            QLabel {
                background-color: transparent;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton {
                border-radius: 6px;
                padding: 8px 16px;
                font-size: 12px;
                font-weight: bold;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Build clean HTML content with transparent background
        full_message = (
            f"<div style='text-align: center; color: #f85149; font-size: 14px; font-weight: bold;'>"
            f"{message}</div>"
            f"<div style='text-align: center; color: #8b949e; font-size: 12px; margin-top: 10px;'>"
            f"Please contact the developer for a new build or access renewal.</div>"
        )

        self.msg_label = QLabel(full_message)
        self.msg_label.setWordWrap(True)
        self.msg_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.msg_label)

        btn_layout = QHBoxLayout()

        if self.download_url:
            self.update_btn = QPushButton("Download Update")
            self.update_btn.setStyleSheet("""
                QPushButton {
                    background-color: #238636;
                    color: #ffffff;
                    border: 1px solid #2ea043;
                }
                QPushButton:hover {
                    background-color: #2ea043;
                }
            """)
            self.update_btn.clicked.connect(self._open_download_page)
            btn_layout.addWidget(self.update_btn)

        self.exit_btn = QPushButton("Exit")
        self.exit_btn.setStyleSheet("""
            QPushButton {
                background-color: #21262d;
                color: #c9d1d9;
                border: 1px solid #30363d;
            }
            QPushButton:hover {
                background-color: #30363d;
            }
        """)
        self.exit_btn.clicked.connect(self._exit_application)
        btn_layout.addWidget(self.exit_btn)

        layout.addLayout(btn_layout)

    def _open_download_page(self):
        if self.download_url:
            webbrowser.open(self.download_url)
        sys.exit(0)

    def _exit_application(self):
        sys.exit(0)

    def reject(self):
        """Prevent escaping out of the mandatory modal via ESC key."""
        pass



class LparSettingsDialog(QDialog):
    """Modal dialog allowing users to dynamically configure LPAR IPs, Database names, Subsystems, and Network Ports."""
    def __init__(self, current_configs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure LPAR Connections, Subsystems & Ports")
        self.resize(1000, 450)
        self.configs = current_configs.copy()
        app = QApplication.instance()
        is_dark_theme = bool(app and app.property("is_dark_theme"))
        dialog_bg = "#161b22" if is_dark_theme else "#ffffff"
        table_bg = "#0d1117" if is_dark_theme else "#f6f8fa"
        surface = "#21262d" if is_dark_theme else "#eaeef2"
        text = "#c9d1d9" if is_dark_theme else "#1f2328"
        muted = "#8b949e" if is_dark_theme else "#57606a"
        border = "#30363d" if is_dark_theme else "#d0d7de"

        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; color: {text}; }}
            QLabel {{ color: {text}; font-weight: bold; }}
            QTableWidget {{
                background-color: {table_bg};
                border: 1px solid {border};
                gridline-color: {border};
                color: {text};
                border-radius: 6px;
            }}
            QHeaderView::section {{
                background-color: {surface};
                color: {muted};
                font-weight: bold;
                border: none;
                padding: 6px;
            }}
            QPushButton {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {border}; }}
            QPushButton#saveBtn {{
                background-color: #238636;
                color: #ffffff;
                border-color: #2ea043;
            }}
            QPushButton#saveBtn:hover {{ background-color: #2ea043; }}
        """)

        layout = QVBoxLayout(self)

        #lbl = QLabel("Manage Server Connections, Expected Subsystems & Monitored Ports:")
        #layout.addWidget(lbl)

        # Table View: IP / Hostname, Database Name, Expected Subsystems, Expected Ports
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels([
            "IP / Hostname", "Database Name", "Expected Subsystems", "Monitored Ports (Port:Name)"
        ])
        
        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        
        vertical_header = self.table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        
        self.populate_table()
        
        # Connect itemChanged to validate IP cell edits in real-time
        self.table.itemChanged.connect(self.validate_table_cell)
        
        # Build tabs: LPAR Configuration and SMTP/Mail Configuration
        email_cfg = load_email_alerts()

        email_group = QGroupBox("System - Email Sender")
        email_layout = QVBoxLayout()

        # SMTP Server (choices)
        h_smtp = QHBoxLayout()
        h_smtp.addWidget(QLabel("SMTP Server:"))
        self.smtp_combo = QComboBox()
        self.smtp_combo.setEditable(True)
        self.smtp_combo.addItems(["smtp.office365.com", "smtp.gmail.com"])
        # set current
        current_server = str(email_cfg.get("smtp_server", "")).strip()
        if current_server:
            idx = self.smtp_combo.findText(current_server)
            if idx >= 0:
                self.smtp_combo.setCurrentIndex(idx)
            else:
                self.smtp_combo.setEditText(current_server)
        h_smtp.addWidget(self.smtp_combo, stretch=1)
        email_layout.addLayout(h_smtp)

        # Port and TLS
        h_port = QHBoxLayout()
        h_port.addWidget(QLabel("Port:"))
        self.port_input = QLineEdit(str(email_cfg.get("port", 587)))
        self.port_input.setMaximumWidth(80)
        h_port.addWidget(self.port_input)
        h_port.addWidget(QLabel("Use TLS:"))
        self.tls_checkbox = QCheckBox()
        self.tls_checkbox.setChecked(bool(email_cfg.get("use_tls", True)))
        h_port.addWidget(self.tls_checkbox)
        h_port.addStretch()
        email_layout.addLayout(h_port)

        # Username / Password
        h_auth = QHBoxLayout()
        h_auth.addWidget(QLabel("Username:"))
        self.username_input = QLineEdit(str(email_cfg.get("username", "")))
        h_auth.addWidget(self.username_input)
        h_auth.addWidget(QLabel("Password:"))
        self.password_input = QLineEdit(str(email_cfg.get("password", "")))
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        h_auth.addWidget(self.password_input)
        email_layout.addLayout(h_auth)

        # From address
        h_from = QHBoxLayout()
        h_from.addWidget(QLabel("From Address:"))
        self.from_input = QLineEdit(str(email_cfg.get("from_address", "")))
        h_from.addWidget(self.from_input)
        email_layout.addLayout(h_from)

        # To addresses
        h_to = QHBoxLayout()
        h_to.addWidget(QLabel("To Addresses (comma-separated):"))
        self.to_input = QLineEdit(", ".join(email_cfg.get("to_addresses", [])))
        h_to.addWidget(self.to_input)
        email_layout.addLayout(h_to)

        # Threshold and cooldown
        h_thresh = QHBoxLayout()
        h_thresh.addWidget(QLabel("Threshold %:"))
        self.threshold_input = QLineEdit(str(email_cfg.get("threshold_percent", 40)))
        self.threshold_input.setMaximumWidth(80)
        h_thresh.addWidget(self.threshold_input)
        h_thresh.addWidget(QLabel("Cooldown minutes:"))
        self.cooldown_input = QLineEdit(str(email_cfg.get("cooldown_minutes", 10)))
        self.cooldown_input.setMaximumWidth(80)
        h_thresh.addWidget(self.cooldown_input)
        h_thresh.addStretch()
        email_layout.addLayout(h_thresh)

        h_refresh = QHBoxLayout()
        h_refresh.addWidget(QLabel("Refresh Interval:"))
        self.refresh_interval_combo = QComboBox()
        self.refresh_interval_combo.addItems(["Instantly", "3s", "5s", "10s"])
        current_interval_ms = int(email_cfg.get("refresh_interval_ms", 0) or 0)
        if current_interval_ms == 0:
            self.refresh_interval_combo.setCurrentIndex(0)
        elif current_interval_ms == 3000:
            self.refresh_interval_combo.setCurrentIndex(1)
        elif current_interval_ms == 5000:
            self.refresh_interval_combo.setCurrentIndex(2)
        else:
            self.refresh_interval_combo.setCurrentIndex(3)
        h_refresh.addWidget(self.refresh_interval_combo)

        h_refresh.addWidget(QLabel("Log Dedupe:"))
        self.log_dedupe_combo = QComboBox()
        self.log_dedupe_combo.addItems(["Off", "30s", "1m", "5m"])
        current_dedupe_seconds = int(email_cfg.get("log_dedupe_seconds", 60) or 60)
        if current_dedupe_seconds == 0:
            self.log_dedupe_combo.setCurrentIndex(0)
        elif current_dedupe_seconds == 30:
            self.log_dedupe_combo.setCurrentIndex(1)
        elif current_dedupe_seconds == 60:
            self.log_dedupe_combo.setCurrentIndex(2)
        else:
            self.log_dedupe_combo.setCurrentIndex(3)
        h_refresh.addWidget(self.log_dedupe_combo)
        h_refresh.addStretch()
        email_layout.addLayout(h_refresh)

        email_group.setLayout(email_layout)

        # LPAR tab
        tab_widget = QTabWidget()
        lpar_tab = QWidget()
        lpar_layout = QVBoxLayout()
        lpar_layout.addWidget(self.table)

        lpar_btn_layout = QHBoxLayout()
        add_btn = QPushButton("+ Add LPAR")
        add_btn.clicked.connect(self.add_row)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self.remove_row)
        lpar_btn_layout.addWidget(add_btn)
        lpar_btn_layout.addWidget(remove_btn)
        lpar_btn_layout.addStretch()
        lpar_layout.addLayout(lpar_btn_layout)
        lpar_tab.setLayout(lpar_layout)
        tab_widget.addTab(lpar_tab, "LPAR Configuration")

        # SMTP tab
        smtp_tab = QWidget()
        smtp_layout = QVBoxLayout()
        smtp_layout.addWidget(email_group)

        test_btn_layout = QHBoxLayout()
        self.test_email_btn = QPushButton("Send Test Email")
        self.test_email_btn.clicked.connect(self.send_test_email)
        test_btn_layout.addStretch()
        test_btn_layout.addWidget(self.test_email_btn)
        smtp_layout.addLayout(test_btn_layout)

        smtp_tab.setLayout(smtp_layout)
        tab_widget.addTab(smtp_tab, "SMTP / Mail Configuration")

        layout.addWidget(tab_widget)

        # Action Buttons (Save only)
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save & Apply")
        save_btn.setObjectName("saveBtn")
        save_btn.clicked.connect(self.save_and_close)
        btn_layout.addStretch()
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def populate_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.configs))
        for row, (srv_name, cfg) in enumerate(sorted(self.configs.items())):
            host = cfg.get("host", "") if isinstance(cfg, dict) else str(cfg)
            db = cfg.get("db", "*LOCAL") if isinstance(cfg, dict) else "*LOCAL"

            subsystems = EXPECTED_SUBSYSTEMS.get(srv_name, [])
            subsystems_str = ", ".join(subsystems)

            ports_list = EXPECTED_PORTS.get(srv_name, [])
            ports_str_items = []
            for p in ports_list:
                if isinstance(p, dict):
                    ports_str_items.append(f"{p.get('port')}:{p.get('name')}")
                else:
                    ports_str_items.append(str(p))
            ports_str = ", ".join(ports_str_items)

            self.table.setItem(row, 0, QTableWidgetItem(host))
            self.table.setItem(row, 1, QTableWidgetItem(db))
            self.table.setItem(row, 2, QTableWidgetItem(subsystems_str))
            self.table.setItem(row, 3, QTableWidgetItem(ports_str))
        self.table.blockSignals(False)

    def validate_table_cell(self, item):
        """Validates IP/Hostname cell edits to prevent duplicate IP addresses."""
        if item.column() != 0:
            return

        current_row = item.row()
        entered_ip = item.text().strip()

        if not entered_ip:
            return

        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            if row == current_row:
                continue

            other_item = self.table.item(row, 0)
            if other_item and other_item.text().strip().lower() == entered_ip.lower():
                QMessageBox.warning(
                    self,
                    "Duplicate IP Detected",
                    f"The IP / Hostname '{entered_ip}' is already assigned to another entry in row {row + 1}.",
                    QMessageBox.StandardButton.Ok
                )
                break
        self.table.blockSignals(False)

    def add_row(self):
        row = self.table.rowCount()
        
        # Determine an unused IP address sequentially
        existing_ips = set()
        for r in range(row):
            item = self.table.item(r, 1)
            if item and item.text().strip():
                existing_ips.add(item.text().strip().lower())

        base_ip = "192.168.1."
        ip_num = 1
        candidate_ip = f"{base_ip}{ip_num}"
        while candidate_ip.lower() in existing_ips:
            ip_num += 1
            candidate_ip = f"{base_ip}{ip_num}"

        self.table.blockSignals(True)
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(candidate_ip))
        self.table.setItem(row, 1, QTableWidgetItem("*LOCAL"))
        self.table.setItem(row, 2, QTableWidgetItem("QINTER, QBATCH, QSERVER, QSYSWRK"))
        self.table.setItem(row, 3, QTableWidgetItem("21:FTP, 22:SSH, 8471:DDM"))
        self.table.blockSignals(False)

    def remove_row(self):
        current_row = self.table.currentRow()
        if current_row >= 0:
            self.table.removeRow(current_row)

    @staticmethod
    def _build_test_message(from_address: str, to_addresses):
        msg = EmailMessage()
        msg["Subject"] = "[Test] IBM i Dashboard SMTP Test"
        msg["From"] = from_address
        msg["To"] = ", ".join(to_addresses)
        msg.set_content("This is a test email sent from the IBM i Dashboard to validate SMTP settings.")
        return msg

    def _show_loading_state(self, is_loading: bool):
        if is_loading:
            if not hasattr(self, "_loading_dialog"):
                self._loading_dialog = QMessageBox(self)
                self._loading_dialog.setWindowTitle("Sending test mail")
                self._loading_dialog.setText("Sending test mail...")
                self._loading_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
                self._loading_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
                self._loading_dialog.setModal(True)
            self._loading_dialog.show()
            self._loading_dialog.raise_()
            self._loading_dialog.activateWindow()
        elif hasattr(self, "_loading_dialog"):
            self._loading_dialog.hide()

    def _is_valid_email(self, addr: str) -> bool:
        if not addr:
            return False
        # Simple validation — sufficient for common use; can be strengthened if needed
        return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr) is not None

    def _handle_test_email_result(self, success: bool, message: str):
        self.test_email_btn.setEnabled(True)
        self._show_loading_state(False)
        if success:
            QMessageBox.information(self, "Test Email", message)
        else:
            QMessageBox.critical(self, "Test Email Failed", message)

    def send_test_email(self):
        # Gather SMTP settings
        smtp_server = str(self.smtp_combo.currentText()).strip()
        try:
            port = int(self.port_input.text().strip() or 587)
        except Exception:
            port = 587
        use_tls = bool(self.tls_checkbox.isChecked())
        username = str(self.username_input.text()).strip()
        password = str(self.password_input.text()).strip() or config.get_email_password(username)
        from_address = str(self.from_input.text()).strip() or username or "test@example.com"
        to_addresses_raw = str(self.to_input.text()).strip()
        to_addresses = [a.strip() for a in to_addresses_raw.split(",") if a.strip()]

        # Validate addresses
        if from_address and not self._is_valid_email(from_address):
            QMessageBox.warning(self, "Invalid From Address", "Please enter a valid From email address.")
            return
        if not to_addresses:
            QMessageBox.warning(self, "No Recipients", "Please specify at least one recipient in To Addresses.")
            return
        for a in to_addresses:
            if not self._is_valid_email(a):
                QMessageBox.warning(self, "Invalid Recipient", f"The recipient address '{a}' does not look valid.")
                return

        self.test_email_btn.setEnabled(False)
        self._show_loading_state(True)

        self.test_thread = TestEmailThread(
            smtp_server=smtp_server,
            port=port,
            use_tls=use_tls,
            username=username,
            password=password,
            from_address=from_address,
            to_addresses=to_addresses,
        )
        self.test_thread.result_ready.connect(self._handle_test_email_result)
        self.test_thread.start()

    def save_and_close(self):
        new_configs = {}
        new_subsystems = {}
        new_ports = {}
        seen_ips = {}

        # Pre-save validation for duplicate IPs
        for row in range(self.table.rowCount()):
            host_item = self.table.item(row, 0)
            if host_item and host_item.text().strip():
                ip_str = host_item.text().strip().lower()
                if ip_str in seen_ips:
                    QMessageBox.warning(
                        self,
                        "Duplicate IP Error",
                        f"Duplicate IP / Hostname '{host_item.text().strip()}' found for row {seen_ips[ip_str]} and row {row + 1}.\n\nEach LPAR must have a unique IP address.",
                        QMessageBox.StandardButton.Ok
                    )
                    return
                seen_ips[ip_str] = row + 1

        for row in range(self.table.rowCount()):
            host_item = self.table.item(row, 0)
            db_item = self.table.item(row, 1)
            sub_item = self.table.item(row, 2)
            port_item = self.table.item(row, 3)

            if host_item and host_item.text().strip():
                host_val = host_item.text().strip()
                srv_name = host_val.upper()
                db_val = db_item.text().strip() if db_item and db_item.text().strip() else "*LOCAL"

                # Parse Subsystems
                sub_text = sub_item.text().strip() if sub_item else ""
                parsed_subsystems = [s.strip().upper() for s in sub_text.split(",") if s.strip()]

                # Parse Ports
                port_text = port_item.text().strip() if port_item else ""
                parsed_ports = []
                for p_entry in port_text.split(","):
                    p_entry = p_entry.strip()
                    if not p_entry:
                        continue
                    if ":" in p_entry:
                        parts = p_entry.split(":", 1)
                        if parts[0].strip().isdigit():
                            parsed_ports.append({
                                "port": int(parts[0].strip()),
                                "name": parts[1].strip().upper()
                            })
                    elif p_entry.isdigit():
                        parsed_ports.append({
                            "port": int(p_entry),
                            "name": f"PORT_{p_entry}"
                        })

                new_configs[srv_name] = {
                    "host": host_val,
                    "db": db_val
                }
                new_subsystems[srv_name] = parsed_subsystems
                new_ports[srv_name] = parsed_ports

        # Collect email/system settings from the dialog
        try:
            smtp_server = str(self.smtp_combo.currentText()).strip()
            port = int(self.port_input.text().strip() or 587)
            use_tls = bool(self.tls_checkbox.isChecked())
        except Exception:
            smtp_server = str(self.smtp_combo.currentText()).strip()
            port = 587
            use_tls = True

        username = str(self.username_input.text()).strip()
        password = str(self.password_input.text()).strip()
        from_address = str(self.from_input.text()).strip() or username or ""
        to_addresses_raw = str(self.to_input.text()).strip()
        to_addresses = [a.strip() for a in to_addresses_raw.split(",") if a.strip()]

        try:
            threshold_percent = float(self.threshold_input.text().strip() or 40.0)
        except Exception:
            threshold_percent = 40.0
        try:
            cooldown_minutes = int(self.cooldown_input.text().strip() or 10)
        except Exception:
            cooldown_minutes = 10

        refresh_interval_map = {
            "Instantly": 0,
            "3s": 3000,
            "5s": 5000,
            "10s": 10000,
        }
        refresh_interval_ms = refresh_interval_map.get(self.refresh_interval_combo.currentText(), 0)

        dedupe_map = {
            "Off": 0,
            "30s": 30,
            "1m": 60,
            "5m": 300,
        }
        log_dedupe_seconds = dedupe_map.get(self.log_dedupe_combo.currentText(), 60)

        # Basic email validation
        if from_address and not self._is_valid_email(from_address):
            QMessageBox.warning(self, "Invalid From Address", "Please enter a valid From email address.")
            return
        if not to_addresses:
            QMessageBox.warning(self, "No Recipients", "Please specify at least one recipient in To Addresses.")
            return
        for a in to_addresses:
            if not self._is_valid_email(a):
                QMessageBox.warning(self, "Invalid Recipient", f"The recipient address '{a}' does not look valid.")
                return

        email_alerts = {
            "enabled": True,
            "smtp_server": smtp_server,
            "port": port,
            "use_tls": use_tls,
            "username": username,
            "password": password,
            "from_address": from_address,
            "to_addresses": to_addresses,
            "threshold_percent": threshold_percent,
            "cooldown_minutes": cooldown_minutes,
            "refresh_interval_ms": refresh_interval_ms,
            "log_dedupe_seconds": log_dedupe_seconds,
        }

        # Attempt to save — save_all_configs will securely persist password if possible
        if not save_all_configs(new_configs, new_subsystems, new_ports, email_alerts=email_alerts):
            QMessageBox.critical(self, "Save Failed", "The configuration could not be saved.")
            return

        # Update in-memory globals
        self.configs = new_configs
        SERVER_CONFIGS.clear()
        SERVER_CONFIGS.update(new_configs)

        EXPECTED_SUBSYSTEMS.clear()
        EXPECTED_SUBSYSTEMS.update(new_subsystems)

        EXPECTED_PORTS.clear()
        EXPECTED_PORTS.update(new_ports)

        # Update config module's EMAIL_ALERTS so runtime code picks up changes
        try:
            config.EMAIL_ALERTS = email_alerts
        except Exception:
            pass

        self.accept()


class SSHRunnerThread(QThread):
    output_signal = pyqtSignal(str)

    def __init__(self, host, username, password, command):
        super().__init__()
        self.host = host
        self.username = username
        self.password = password
        self.command = command

    def run(self):
        try:
            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            ssh.connect(self.host, port=22, username=self.username, password=self.password, timeout=5)
            
            full_cmd = f"system \"{self.command}\""
            stdin, stdout, stderr = ssh.exec_command(full_cmd, get_pty=False)
            
            out = stdout.read().decode('utf-8')
            err = stderr.read().decode('utf-8')
            
            result = out if out else err if err else "Command executed with no output."
            self.output_signal.emit(f"=== Host: {self.host} ===\n{result}")
            ssh.close()
        except Exception as e:
            self.output_signal.emit(f"SSH Error on {self.host}: {str(e)}")


class CommandQuickActionDialog(QDialog):
    def __init__(self, default_server="", default_cmd="", username="", password="", server_configs=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Command Quick-Action Panel")
        self.resize(550, 400)
        app = QApplication.instance()
        is_dark_theme = bool(app is not None and app.property("is_dark_theme"))
        dialog_bg = "#161b22" if is_dark_theme else "#ffffff"
        input_bg = "#0d1117" if is_dark_theme else "#f6f8fa"
        text = "#c9d1d9" if is_dark_theme else "#1f2328"
        muted = "#8b949e" if is_dark_theme else "#57606a"
        border = "#30363d" if is_dark_theme else "#d0d7de"
        self.setStyleSheet(f"""
            QDialog {{ background-color: {dialog_bg}; color: {text}; }}
            QLabel {{ color: {text}; font-weight: bold; }}
            QLineEdit, QComboBox {{ background-color: {input_bg}; border: 1px solid {border}; color: {text}; padding: 6px; border-radius: 4px; }}
            QTextEdit {{ background-color: {input_bg}; border: 1px solid {border}; color: #3fb950; font-family: Consolas; border-radius: 4px; }}
            QPushButton {{ background-color: #238636; color: #ffffff; border-radius: 4px; padding: 6px 12px; font-weight: bold; }}
            QPushButton:hover {{ background-color: #2ea043; }}
        """)

        self.username = username
        self.password = password
        self.server_configs = server_configs or SERVER_CONFIGS

        layout = QVBoxLayout(self)

        h_layout1 = QHBoxLayout()
        h_layout1.addWidget(QLabel("Target LPAR:"))
        self.server_combo = QComboBox()
        self.server_combo.addItems(list(self.server_configs.keys()))
        if default_server in self.server_configs:
            self.server_combo.setCurrentText(default_server)
        h_layout1.addWidget(self.server_combo, stretch=1)
        layout.addLayout(h_layout1)

        h_layout2 = QHBoxLayout()
        h_layout2.addWidget(QLabel("CL Command:"))
        self.cmd_input = QLineEdit(default_cmd)
        h_layout2.addWidget(self.cmd_input, stretch=1)
        
        self.exec_btn = QPushButton("Execute via SSH")
        self.exec_btn.clicked.connect(self.execute_command)
        h_layout2.addWidget(self.exec_btn)
        layout.addLayout(h_layout2)

        layout.addWidget(QLabel("Execution Output Log:"))
        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        layout.addWidget(self.output_text)

    def execute_command(self):
        server = self.server_combo.currentText()
        cfg = self.server_configs.get(server, {})
        host = cfg.get("host", "") if isinstance(cfg, dict) else str(cfg)
        cmd = self.cmd_input.text().strip()

        if not cmd:
            self.output_text.append("Error: Command field cannot be empty.")
            return

        self.output_text.append(f"Connecting to {server} ({host}) to execute: {cmd}...")
        self.exec_btn.setEnabled(False)

        self.ssh_thread = SSHRunnerThread(host, self.username, self.password, cmd)
        self.ssh_thread.output_signal.connect(self.handle_output)
        self.ssh_thread.start()

    def handle_output(self, text):
        self.output_text.append(text)
        self.exec_btn.setEnabled(True)