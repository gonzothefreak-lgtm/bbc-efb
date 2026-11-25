STYLE = """
* { font-family: "Segoe UI"; font-size: 13px; color: #e7eef7; }
QMainWindow, QWidget#surface, QWidget#page { background: #0b1420; }
QFrame#top { background: #080f19; border-bottom: 1px solid #283b50; }
QFrame#footer { background: #0a121b; border-top: 1px solid #2a4055; }
QFrame#sidebar { background: #101b28; border: 1px solid #243548; border-radius: 9px; }
QFrame#card { background: #142131; border: 1px solid #283b50; border-radius: 9px; }
QLabel#brand { font-size: 19px; font-weight: 700; color: #f1f7ff; }
QLabel#brandMark { background: #10558a; color: #e9f6ff; border-radius: 6px;
                    font-size: 11px; font-weight: 700; }
QLabel#heading { font-size: 27px; font-weight: 650; color: #f1f7ff; }
QLabel#cardTitle { font-size: 15px; font-weight: 650; }
QLabel#metric { font-size: 25px; font-weight: 650; color: #64b1ff; }
QLabel#muted { color: #a7bbd0; }
QLabel#warning { color: #ffd693; }
QPushButton { background: #192a3c; border: 1px solid #344b61; border-radius: 6px;
              padding: 7px 11px; font-weight: 600; }
QPushButton:hover { background: #253d53; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus,
QDateEdit:focus, QSpinBox:focus, QTableWidget:focus { border: 1px solid #86caff; }
QPushButton:disabled { color: #a1b0bf; background: #14202b; border-color: #223247; }
QPushButton#primary, QPushButton:checked { background: #175286; border-color: #3285bf; }
QPushButton#primary:hover, QPushButton:checked:hover { background: #206da6; }
QPushButton#navigation:!checked { background: transparent; border-color: transparent; }
QPushButton#navigation:!checked:hover { background: #253d53; }
QPushButton#window, QPushButton#close { background: transparent; border-color: transparent;
                                       font-size: 15px; padding: 3px; }
QPushButton#window:hover { background: #293d50; }
QPushButton#close:hover { background: #993542; }
QPushButton#status { background: transparent; border-color: transparent; color: #b6c8da;
                      padding: 4px 8px; }
QPushButton#status:hover { background: #1d2b38; }
QLineEdit, QDateEdit, QSpinBox, QPlainTextEdit, QComboBox, QListWidget {
    background: #0c1723; border: 1px solid #344b61; border-radius: 5px; padding: 6px;
    selection-background-color: #175b8f; }
QTableWidget { background: #101a26; alternate-background-color: #162333;
               border: 1px solid #2a4055; border-radius: 5px; gridline-color: #24374a;
               selection-background-color: #1a5682; }
QTableWidget::item { padding: 4px; }
QHeaderView::section { background: #1d3043; color: #deebf6; border: none;
                       padding: 7px; font-weight: 650; }
QTableWidget#flightHistory, QTableWidget#flightHistory QHeaderView::section { font-size: 11px; }
QTableWidget#flightHistory::item { padding: 2px; }
QTabWidget::pane { background: #0b1420; border: 1px solid #283b50; }
QTabBar::tab { background: #142131; padding: 7px 9px; border: 1px solid #283b50; }
QTabBar::tab:selected { background: #175286; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical, QScrollBar:horizontal { background: #101b28; border: none; }
QScrollBar:vertical { width: 10px; }
QScrollBar:horizontal { height: 10px; }
QScrollBar::handle { background: #344b61; border-radius: 4px; }
QScrollBar::handle:vertical { min-height: 24px; }
QScrollBar::handle:horizontal { min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QProgressBar { min-height: 8px; max-height: 8px; border: none;
               border-radius: 4px; background: #22364a; }
QProgressBar::chunk { background: #f69955; border-radius: 4px; }
QMessageBox, QFileDialog, QCalendarWidget, QMenu { background: #142130; }
QToolTip { background: #15293c; color: #edf6ff; border: 1px solid #48718f; }
"""
