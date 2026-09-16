import paramiko
import re
import sys
import smtplib
import webbrowser
from typing import Optional, cast
from email.message import EmailMessage
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView,
    QApplication, QMessageBox, QGroupBox, QCheckBox,
    QWidget, QTabWidget
)
import config
from config import (
    SERVER_CONFIGS,
    EXPECTED_SUBSYSTEMS,
    EXPECTED_PORTS,
    save_all_configs,
    load_email_alerts,
)


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