dark_stylesheet = """
QWidget {
    background-color: #121212;
    color: #FFFFFF;
    font-family: 'Segoe UI', 'Arial';
}

QLabel {
    padding: 4px;
}

QLineEdit, QComboBox, QSpinBox, QTextEdit {
    background-color: #1e1e1e;
    border: 1px solid #333;
    padding: 4px;
    border-radius: 4px;
}

QGroupBox {
    border: 1px solid #333;
    border-radius: 6px;
    margin-top: 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px 0 4px;
    color: #b5d26b;
}

QPushButton {
    background-color: #b5d26b;
    border: none;
    color: white;
    padding: 6px 12px;
    border-radius: 4px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #cbd7ad;
}
QPushButton:pressed {
    background-color: #a1d12a;
}

QStatusBar {
    background-color: #1e1e1e;
    color: #aaa;
}

QTextEdit {
    background-color: #1e1e1e;
    color: #adb795;
}
"""
