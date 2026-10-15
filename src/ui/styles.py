# ui/styles.py

DARK_STYLESHEET = """
QMainWindow, QTabWidget::pane {
    background-color: #171819;
    border: none;
}
QTabBar::tab {
    background-color: #202225;
    color: #a4a8ae;
    padding: 8px 16px;
    border: 1px solid #34383d;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background-color: #171819;
    color: #79aaff;
    border-color: #34383d;
    border-bottom: 2px solid #79aaff;
}
QWidget {
    background-color: #171819;
    color: #e1e3e6;
    font-family: 'Segoe UI', 'SF Pro Display', -apple-system, sans-serif;
}
QDialog, QMessageBox, QFileDialog {
    background-color: #171819;
    color: #e1e3e6;
}
QMessageBox QLabel, QFileDialog QLabel {
    background-color: transparent;
    color: #e1e3e6;
}
QGroupBox {
    border: 1px solid #34383d;
    border-radius: 8px;
    margin-top: 10px;
    font-weight: bold;
    font-size: 11px;
    color: #a4a8ae;
    padding-top: 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QComboBox {
    background-color: #202225;
    border: 1px solid #34383d;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f3f4f6;
    font-size: 12px;
}
QComboBox QAbstractItemView {
    background-color: #202225;
    color: #f3f4f6;
    selection-background-color: #34445c;
    border: 1px solid #34383d;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #79aaff;
}
QPushButton {
    background-color: #2a2c2f;
    color: #e1e3e6;
    border: 1px solid #3a3d41;
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 600;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #36393d;
    border-color: #62676e;
}
QPushButton:disabled {
    background-color: #202225;
    color: #737980;
    border-color: #2a2c2f;
}
QTableWidget {
    background-color: #202225;
    gridline-color: #2a2c2f;
    border: 1px solid #34383d;
    border-radius: 8px;
    outline: none;
}
QTableWidget::item {
    border: none;
    padding: 4px;
}
QTableWidget::item:selected {
    background-color: #303943;
    color: #f3f4f6;
}
QHeaderView::section {
    background-color: #171819;
    color: #a4a8ae;
    padding: 6px 10px;
    font-weight: 600;
    font-size: 12px;
    border: none;
    border-bottom: 2px solid #34383d;
}
"""

LIGHT_STYLESHEET = """
QMainWindow, QTabWidget::pane {
    background-color: #f6f8fa;
    border: none;
}
QTabBar::tab {
    background-color: #eaeef2;
    color: #57606a;
    padding: 8px 16px;
    border: 1px solid #d0d7de;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background-color: #f6f8fa;
    color: #0969da;
    border-color: #d0d7de;
    border-bottom: 2px solid #0969da;
}
QWidget {
    background-color: #f6f8fa;
    color: #1f2328;
    font-family: 'Segoe UI', 'SF Pro Display', -apple-system, sans-serif;
}
QDialog, QMessageBox, QFileDialog {
    background-color: #f6f8fa;
    color: #1f2328;
}
QMessageBox QLabel, QFileDialog QLabel {
    background-color: transparent;
    color: #1f2328;
}
QGroupBox {
    border: 1px solid #d0d7de;
    border-radius: 8px;
    margin-top: 10px;
    font-weight: bold;
    font-size: 11px;
    color: #57606a;
    padding-top: 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QComboBox {
    background-color: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 6px 10px;
    color: #1f2328;
    font-size: 12px;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    color: #1f2328;
    selection-background-color: #ddf4ff;
    border: 1px solid #d0d7de;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #0969da;
}
QPushButton {
    background-color: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 600;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #f3f4f6;
    border-color: #8c959f;
}
QPushButton:disabled {
    background-color: #eaeef2;
    color: #8c959f;
    border-color: #d8dee4;
}
QTableWidget {
    background-color: #ffffff;
    gridline-color: #d8dee4;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    outline: none;
}
QTableWidget::item {
    border: none;
    padding: 4px;
}
QTableWidget::item:selected {
    background-color: #ddf4ff;
    color: #1f2328;
}
QHeaderView::section {
    background-color: #eaeef2;
    color: #57606a;
    padding: 6px 10px;
    font-weight: 600;
    font-size: 12px;
    border: none;
    border-bottom: 2px solid #d0d7de;
}
QScrollArea, QScrollBar:vertical, QScrollBar:horizontal {
    background-color: #f6f8fa;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background-color: #afb8c1;
    border-radius: 5px;
}
"""