"""Micophone's look: two palettes built on the Farzonline green, and the Qt stylesheet.

Shapes follow Spinin's "Voltage" theme (near-black body, big soft corners), so the two
apps read as a family. Every colour the window draws comes from the active palette.
"""

GREEN = "#04fa73"  # the eye in the Farzonline logo

PALETTES = {
    "dark": {
        "chassis": "#0b0c0d", "chassis2": "#121416",
        "panel": "#15171a", "panel2": "#101214",
        "raised": "#1e2125", "line": "#272b30", "select": "#2c3137",
        "text": "#f2f5f3", "muted": "#878d92",
        "accent": GREEN, "accent2": "#02d863", "on_accent": "#02140a", "accent_text": GREEN,
        "warn": "#ffbe5c", "danger": "#ff6b5e", "dark": True,
    },
    "light": {
        "chassis": "#f4f6f5", "chassis2": "#eceff0",
        "panel": "#ffffff", "panel2": "#fbfcfb",
        "raised": "#eef1f0", "line": "#dde2df", "select": "#dfe5e2",
        "text": "#111413", "muted": "#68706c",
        # The logo green is too pale for text on white, so text uses a deeper shade.
        "accent": GREEN, "accent2": "#03e068", "on_accent": "#02140a", "accent_text": "#00873f",
        "warn": "#b86e00", "danger": "#d0382b", "dark": False,
    },
}

R_CARD, R_BTN, R_INPUT = 18, 12, 10
DISPLAY = "Segoe UI Variable Display"


def stylesheet(t):
    return f"""
* {{ font-family: 'Segoe UI Variable Text', 'Segoe UI', sans-serif; font-size: 13px; color: {t['text']}; }}
QWidget#root {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {t['chassis']}, stop:1 {t['chassis2']}); }}
QFrame#titlebar {{ background: transparent; }}
QFrame#card {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {t['panel']}, stop:1 {t['panel2']});
    border: 1px solid {t['line']}; border-radius: {R_CARD}px; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {t['muted']}; }}
QLabel[role="wordmark"] {{ font-family: '{DISPLAY}'; font-size: 15px; font-weight: 600; letter-spacing: 0.4px; }}
QLabel[role="hero"] {{ font-family: '{DISPLAY}'; font-size: 24px; font-weight: 600; }}
QLabel[role="section"] {{ font-family: '{DISPLAY}'; font-size: 12px; font-weight: 600; color: {t['muted']};
    letter-spacing: 1.2px; }}
QLabel[role="key"] {{ color: {t['muted']}; }}
QLabel[tone="ok"] {{ color: {t['accent_text']}; }}
QLabel[tone="warn"] {{ color: {t['warn']}; }}
QLabel[role="stats"] {{ color: {t['muted']}; font-size: 12px; }}

QPushButton {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: {R_BTN}px;
    padding: 7px 16px; }}
QPushButton:hover {{ border-color: {t['muted']}; }}
QPushButton:pressed {{ background: {t['select']}; }}
QPushButton:disabled {{ color: {t['muted']}; }}
QPushButton[kind="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {t['accent']}, stop:1 {t['accent2']});
    color: {t['on_accent']}; border: 1px solid {t['accent']}; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {t['accent']}; }}
QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; color: {t['muted']}; }}
QPushButton[kind="ghost"]:hover {{ color: {t['text']}; background: {t['raised']}; }}
QPushButton[kind="chrome"] {{ background: transparent; border: none; border-radius: 8px; padding: 0;
    min-width: 40px; max-width: 40px; min-height: 30px; max-height: 30px; color: {t['muted']}; }}
QPushButton[kind="chrome"]:hover {{ background: {t['raised']}; }}
QPushButton[kind="chrome"][danger="true"]:hover {{ background: {t['danger']}; }}

QFrame#segment {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: {R_INPUT + 1}px; }}
QPushButton[kind="segment"] {{ background: transparent; border: none; border-radius: {R_INPUT - 2}px;
    padding: 5px 12px; color: {t['muted']}; }}
QPushButton[kind="segment"]:hover {{ color: {t['text']}; }}
QPushButton[kind="segment"]:checked {{ background: {t['select']}; color: {t['text']}; }}

QComboBox {{ background: {t['raised']}; border: 1px solid {t['line']}; border-radius: {R_INPUT}px; padding: 7px 10px; }}
QComboBox:hover {{ border-color: {t['muted']}; }}
QComboBox:focus {{ border-color: {t['accent']}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {t['panel']}; border: 1px solid {t['line']}; outline: none; padding: 4px;
    selection-background-color: {t['select']}; selection-color: {t['text']}; }}

QCheckBox {{ spacing: 9px; }}

QPlainTextEdit {{ background: {t['chassis']}; border: 1px solid {t['line']}; border-radius: {R_INPUT}px; padding: 6px;
    font-family: 'Cascadia Mono', Consolas, monospace; font-size: 11px; color: {t['muted']}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {t['line']}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: {t['raised']}; color: {t['text']}; border: 1px solid {t['line']}; border-radius: 6px; padding: 4px; }}
QMenu {{ background: {t['panel']}; border: 1px solid {t['line']}; border-radius: {R_INPUT}px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 14px; border-radius: 7px; }}
QMenu::item:selected {{ background: {t['raised']}; }}
QMenu::item:disabled {{ color: {t['muted']}; }}
QMenu::separator {{ height: 1px; background: {t['line']}; margin: 5px 8px; }}
QMessageBox {{ background: {t['panel']}; }}
"""


if __name__ == "__main__":
    import re
    for name, p in PALETTES.items():
        css = stylesheet(p)
        for key, val in p.items():
            if key != "dark":
                assert re.fullmatch(r"#[0-9a-f]{6}", val), (name, key, val)
        assert p["accent"] in css and "None" not in css, name
        # Ticks are drawn by the app's style; a stylesheet indicator rule would turn them into squares.
        assert "::indicator" not in css, name
    print("theme ok")
