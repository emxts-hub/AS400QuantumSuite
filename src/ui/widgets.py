from collections import deque
from PyQt6.QtCore import Qt, QRectF, QTimer, QDateTime, QPointF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QCursor, QPolygonF, QBrush, QMouseEvent
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QDialog, QVBoxLayout, 
    QHBoxLayout, QGridLayout, QApplication, QProgressBar
)
from config import EXPECTED_SUBSYSTEMS, SERVICE_COMMANDS, SUBSYSTEM_COMMANDS


class SparklineWidget(QWidget):
    """Dual-trend sparkline chart supporting independent primary and secondary metric paths."""
    def __init__(self, max_points=45, parent=None):
        super().__init__(parent)
        self.history_primary = deque(maxlen=max_points)
        self.history_secondary = deque(maxlen=max_points)
        self.setFixedHeight(28)
        self.is_dark_theme = True

    def add_values(self, primary_val, secondary_val=None):
        """Add a primary value and an optional secondary metric value."""
        self.history_primary.append(float(primary_val))
        if secondary_val is not None:
            self.history_secondary.append(float(secondary_val))
        self.update()

    def add_value(self, val):
        """Backward compatibility for single-value updates."""
        self.add_values(val)

    def set_theme(self, is_dark):
        self.is_dark_theme = is_dark
        self.update()

    def paintEvent(self, a0):
        if len(self.history_primary) < 2:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        margin = 2.0

        # --- Draw Secondary Metric Line & Fill (e.g., Memory) ---
        if len(self.history_secondary) >= 2:
            max_sec = max(100.0, max(self.history_secondary))
            step_x_sec = (w - 2 * margin) / max(1, len(self.history_secondary) - 1)

            points_sec = []
            for i, val in enumerate(self.history_secondary):
                x = margin + i * step_x_sec
                y = (h - margin) - ((val / max_sec) * (h - 2 * margin))
                points_sec.append(QPointF(x, y))

            sec_color = QColor("#a371f7" if self.is_dark_theme else "#8250df")  # Purple Accent
            painter.setPen(QPen(sec_color, 1.2, Qt.PenStyle.DashLine))

            for i in range(len(points_sec) - 1):
                painter.drawLine(points_sec[i], points_sec[i+1])

            fill_sec = QColor(sec_color)
            fill_sec.setAlpha(20)
            poly_sec = [QPointF(points_sec[0].x(), h)] + points_sec + [QPointF(points_sec[-1].x(), h)]
            painter.setBrush(QBrush(fill_sec))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygonF(poly_sec))

        # --- Draw Primary Metric Line & Fill (e.g., CPU) ---
        max_pri = max(100.0, max(self.history_primary))
        step_x_pri = (w - 2 * margin) / max(1, len(self.history_primary) - 1)

        points_pri = []
        for i, val in enumerate(self.history_primary):
            x = margin + i * step_x_pri
            y = (h - margin) - ((val / max_pri) * (h - 2 * margin))
            points_pri.append(QPointF(x, y))

        pri_color = QColor("#f85149") if self.history_primary[-1] >= 90 else QColor("#58a6ff" if self.is_dark_theme else "#0969da")
        painter.setPen(QPen(pri_color, 1.5))

        for i in range(len(points_pri) - 1):
            painter.drawLine(points_pri[i], points_pri[i+1])

        fill_pri = QColor(pri_color)
        fill_pri.setAlpha(35)
        poly_pri = [QPointF(points_pri[0].x(), h)] + points_pri + [QPointF(points_pri[-1].x(), h)]
        painter.setBrush(QBrush(fill_pri))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF(poly_pri))


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
            QDialog {{
                background-color: {bg_clr};
                border: 1px solid {border_clr};
                border-radius: 8px;
            }}
            QLabel {{
                color: {text_clr};
                font-family: "Segoe UI", sans-serif;
                font-size: 12px;
                font-weight: bold;
            }}
            QProgressBar {{
                border: none;
                background-color: {"#21262d" if is_dark else "#e1e4e8"};
                height: 4px;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background-color: #238636;
                border-radius: 2px;
            }}
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


class RefreshStatusWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_dark_theme = True
        self.setFixedWidth(180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(1)
        layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.header_label = QLabel("○ Auto-Refresh Paused")
        self.header_label.setStyleSheet("color: #8b949e; font-size: 11px; font-weight: bold;")
        layout.addWidget(self.header_label)

        self.time_label = QLabel()
        self.time_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        layout.addWidget(self.time_label)

        self.date_label = QLabel()
        self.date_label.setFont(QFont("Segoe UI", 9))
        layout.addWidget(self.date_label)

        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self.update_timestamp)
        self.update_timestamp()
        self.clock_timer.start()

    def set_theme(self, is_dark_theme):
        self.is_dark_theme = is_dark_theme
        time_color = "#ffffff" if is_dark_theme else "#1f2328"
        muted_color = "#8b949e" if is_dark_theme else "#57606a"
        self.time_label.setStyleSheet(f"color: {time_color};")
        self.date_label.setStyleSheet(f"color: {muted_color};")
        self.set_active_state(self.header_label.text().startswith("●"))

    def set_active_state(self, active: bool):
        if active:
            self.header_label.setText("● Auto-Refresh Active")
            self.header_label.setStyleSheet(
                "color: #3fb950; font-size: 11px; font-weight: bold;"
            )
        else:
            self.header_label.setText("○ Auto-Refresh Paused")
            muted_color = "#8b949e" if self.is_dark_theme else "#57606a"
            self.header_label.setStyleSheet(
                f"color: {muted_color}; font-size: 11px; font-weight: bold;"
            )


    def update_timestamp(self):
        now = QDateTime.currentDateTime()
        self.time_label.setText(now.toString("hh:mm:ss AP"))
        self.date_label.setText(now.toString("MMM d, yyyy"))


class CircularGauge(QWidget):
    def __init__(self, value, parent=None):
        super().__init__(parent)
        self.value = float(value)
        self.setFixedSize(80, 80)

    def set_value(self, value):
        self.value = float(value)
        self.update()

    def paintEvent(self, a0):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        gauge_size = 64
        x = (self.width() - gauge_size) / 2
        y = (self.height() - gauge_size) / 2
        rect = QRectF(x, y, gauge_size, gauge_size)

        pen_width = 5.5

        bg_pen = QPen(QColor("#21262d"), pen_width)
        bg_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(bg_pen)
        painter.drawEllipse(rect)

        color_hex = "#f85149" if self.value >= 90.0 else "#e3b341" if self.value >= 80.0 else "#388bfd"
        progress_pen = QPen(QColor(color_hex), pen_width)
        progress_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(progress_pen)

        capped_val = min(100.0, max(0.0, self.value))
        span_angle = int(-capped_val * 3.6 * 16)
        start_angle = 90 * 16
        painter.drawArc(rect, start_angle, span_angle)

        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        text_rect = QRectF(x, y, gauge_size, gauge_size)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, f"{self.value:.2f}%")


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
            QDialog {{
                background-color: {dialog_bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
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
            QPushButton {{
                background-color: {surface};
                color: {text_clr};
                border: 1px solid {border};
                border-radius: 6px;
                font-weight: bold;
            }}
            QPushButton:hover {{ 
                background-color: {border}; 
                color: {'#ffffff' if is_dark_theme else '#1f2328'}; 
            }}
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


class SubsystemBadge(QLabel):
    def __init__(self, name, is_up, parent=None):
        status_str = "UP" if is_up else "DOWN"
        super().__init__(f"● {name} ({status_str})", parent)
        self.name = name
        self.is_up = is_up
        self.command = SUBSYSTEM_COMMANDS.get(name, f"STRSBS SBSD({name})")

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(22)
        self.setMinimumWidth(110)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        if is_up:
            self.setStyleSheet("""
                QLabel {
                    background-color: #0d281e;
                    color: #3fb950;
                    border: 1px solid #1e4b33;
                    border-radius: 4px;
                    font-weight: 600;
                    font-size: 9px;
                    padding: 2px 4px;
                }
                QLabel:hover {
                    background-color: #123b2c;
                    border-color: #2ea043;
                }
            """)
        else:
            self.setStyleSheet("""
                QLabel {
                    background-color: #3c1618;
                    color: #f85149;
                    border: 1px solid #6e2024;
                    border-radius: 4px;
                    font-weight: 600;
                    font-size: 9px;
                    padding: 2px 4px;
                }
                QLabel:hover {
                    background-color: #4e1c20;
                    border-color: #f85149;
                }
            """)

    def mousePressEvent(self, ev: QMouseEvent | None):
        if ev is not None and ev.button() == Qt.MouseButton.LeftButton:
            dialog = ItemDetailDialog(f"Subsystem: {self.name}", self.is_up, self.command, self)
            pos = QCursor.pos()
            dialog.move(pos.x() - 150, pos.y() - 100)
            dialog.exec()


class SubsystemGridWidget(QWidget):
    def __init__(self, server_name, active_subsystems, expected_key=None, on_expand_callback=None, parent=None):
        super().__init__(parent)
        self.server_name = server_name
        self.on_expand_callback = on_expand_callback

        def normalize_name(name):
            return str(name).strip().upper() if name is not None else ""

        expected_key = expected_key or server_name
        self.expected_subs = [
            normalize_name(item)
            for item in EXPECTED_SUBSYSTEMS.get(expected_key, [])
        ]

        active_names = []
        for sub in active_subsystems:
            if isinstance(sub, dict):
                name = normalize_name(sub.get("name"))
                status = str(sub.get("status", "ACTIVE")).upper()
                if name and status == "ACTIVE":
                    active_names.append(name)
            else:
                name = normalize_name(sub)
                if name:
                    active_names.append(name)

        self.active_set = set(active_names)

        if self.expected_subs:
            expected_running_count = sum(1 for s in self.expected_subs if s in self.active_set)
            expected_total_count = len(self.expected_subs)
            actual_running_count = len(self.active_set)
        else:
            expected_running_count = len(self.active_set)
            expected_total_count = len(self.active_set)
            actual_running_count = len(self.active_set)

        self.all_healthy = expected_running_count == expected_total_count
        self.expected_running_count = expected_running_count
        self.actual_running_count = actual_running_count

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(4, 4, 4, 4)
        self.main_layout.setSpacing(4)
        self.main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        header_container = QWidget()
        h_layout = QHBoxLayout(header_container)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(8)

        header_color = "#3fb950" if self.all_healthy else "#f85149"
        self.header_label = QLabel(f"● {self.expected_running_count} / {expected_total_count} Active")
        self.header_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.header_label.setStyleSheet(f"color: {header_color}; background: transparent;")
        h_layout.addWidget(self.header_label)

        h_layout.addStretch()

        self.toggle_btn = QPushButton("Expand ▾")
        self.toggle_btn.setFixedSize(70, 22)
        self.toggle_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #21262d;
                color: #c9d1d9;
                border: 1px solid #30363d;
                border-radius: 4px;
                font-size: 10px;
                padding: 0px;
            }
            QPushButton:hover {
                color: #ffffff;
                border-color: #58a6ff;
            }
        """)
        self.toggle_btn.clicked.connect(self.trigger_expand)
        h_layout.addWidget(self.toggle_btn)

        self.main_layout.addWidget(header_container)

    def trigger_expand(self):
        if self.on_expand_callback:
            self.on_expand_callback(self.server_name)


class ServiceBadge(QLabel):
    def __init__(self, port_info, parent=None):
        if isinstance(port_info, dict):
            self.name = (
                port_info.get("name") or 
                port_info.get("service") or 
                port_info.get("port") or 
                port_info.get("label") or 
                "UNK"
            )
            self.is_up = port_info.get("is_up", port_info.get("status") == "UP")
            self.port_num = port_info.get("port", port_info.get("port_num", ""))
            self.desc = port_info.get("description", port_info.get("desc", f"{self.name} Service"))
            self.command = port_info.get(
                "command", 
                SERVICE_COMMANDS.get(
                    int(self.port_num) if str(self.port_num).isdigit() else -1,
                    f"STRTCPSVR SERVER(*{self.name})",
                )
            )
        else:
            self.name = str(port_info)
            self.is_up = True
            self.port_num = ""
            self.desc = f"{self.name} Service"
            command_key = int(self.name) if self.name.isdigit() else -1
            self.command = SERVICE_COMMANDS.get(command_key, f"STRTCPSVR SERVER(*{self.name})")

        status_symbol = "●" if self.is_up else "✖"
        super().__init__(f"{self.name} {status_symbol}", parent)

        self.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(22)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        port_str = f" (Port {self.port_num})" if self.port_num else ""
        status_text = "UP" if self.is_up else "DOWN"
        status_clr = "#3fb950" if self.is_up else "#f85149"

        tooltip_html = f"""
        <div style="background-color: #161b22; color: #f0f6fc; padding: 6px; border-radius: 6px; border: 1px solid #30363d; font-family: 'Segoe UI', sans-serif;">
            <b>Service:</b> {self.name}{port_str}<br>
            <b>Status:</b> <span style="color: {status_clr}; font-weight: bold;">{status_text}</span><br>
            <b>Description:</b> {self.desc}<br>
            <b>Command:</b> <code style="color: #79c0ff; background-color: #21262d; padding: 2px 4px; border-radius: 3px;">{self.command}</code>
        </div>
        """
        self.setToolTip(tooltip_html)

        if self.is_up:
            self.setStyleSheet("""
                QLabel {
                    background-color: #0d2818;
                    color: #3fb950;
                    border: 1px solid #2ea043;
                    border-radius: 4px;
                    padding: 1px 2px;
                }
                QLabel:hover {
                    background-color: #123d24;
                    border-color: #3fb950;
                }
            """)
        else:
            self.setStyleSheet("""
                QLabel {
                    background-color: #361718;
                    color: #f85149;
                    border: 1px solid #da3633;
                    border-radius: 4px;
                    padding: 1px 2px;
                }
                QLabel:hover {
                    background-color: #4d1f21;
                    border-color: #f85149;
                }
            """)

    def mousePressEvent(self, ev: QMouseEvent | None) -> None:
        if ev is not None and ev.button() == Qt.MouseButton.LeftButton:
            title = f"{self.name} Service"
            if self.port_num:
                title += f" (Port {self.port_num})"
            dialog = ItemDetailDialog(title, self.is_up, self.command, self)
            pos = QCursor.pos()
            dialog.move(pos.x() - 150, pos.y() - 100)
            dialog.exec()


class StatusBadgesWidget(QWidget):
    def __init__(self, ports_data, parent=None):
        super().__init__(parent)
        
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setHorizontalSpacing(4)
        layout.setVerticalSpacing(4)

        cols = 5
        for idx, port in enumerate(ports_data):
            row = idx // cols
            col = idx % cols
            
            badge = ServiceBadge(port, parent=None)
            layout.addWidget(badge, row, col)
            
        for c in range(cols):
            layout.setColumnStretch(c, 1)


class CenteredCellWidget(QWidget):
    def __init__(self, child_widget, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(child_widget)