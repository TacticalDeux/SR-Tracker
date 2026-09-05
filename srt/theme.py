"""Visual identity for SR Tracker.

The product is a session tracker for *Soul's Remnant* — a game whose
collectible economy centers on Soul Crystals and whose atmosphere is
dark-fantasy / ruined-arena. The UI borrows from that world:

  Ink      — the deep near-black backdrop of the app
  Parch    — the only light source; carries labels, table rules
  Crystal  — the soul-crystal palette; the single saturated accent
  Ash      — secondary text, a cool desaturated gray
  Rune     — hover/active, a quiet warm gold (the only warm accent
             now that the soul crystal is cool blue)
  Crimson  — warnings, locked-state, destructive actions

The overlay gets the warm parchment treatment so it reads as part of
the game world; the main window stays in Ink so the user's eyes have
a calm landing place between sessions. The signature element is the
pixel-art soul crystal (see `srt.crystal`) used as a counter chip
in the overlay and as a hero element on the main window's summary.

Type uses three voices, chosen for what they DO not for novelty:

  Display  — Georgia. Tracked uppercase labels ("SOUL CRYSTALS",
             "KILLS", "LEVEL") and section headings. Reads carved-
             in-stone rather than screen-saver.
  Body     — Segoe UI. Chrome, control chips, prose. Familiar
             because it has to disappear.
  Data     — Consolas. Every number, every timecode, every ID.
             Monospace gives a ledger feel and makes digits tabular
             so the value never shifts width as it changes.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


# --- palette tokens ---
# Ink
INK_0 = "#0c0a14"   # main window background — near-black with a warm cast
INK_1 = "#15121f"   # raised surface, the field log pages
INK_2 = "#1f1a2c"   # hover/selected surface
INK_BORDER = "#2a2438"
INK_BORDER_2 = "#3a3349"

# Parchment — the only light source
PARCH_BG = "#e8dfc8"     # overlay card background
PARCH_INK = "#1a1424"    # primary text on parchment
PARCH_FAINT = "#7a6f5a"  # faint ink for tertiary text
PARCH_RULE = "#b8a87a"   # hairline rules on the parchment

# Crystal — the soul crystal accent family.
# These are the canonical crystal palette tokens, sampled from the source
# asset. The same family is exported by `srt.crystal` for the pixmap
# renderer; keep these in sync if you tweak the source colors.
from .crystal import (  # noqa: E402  - kept here so the theme is the one-stop
    CRYSTAL_DEEP, CRYSTAL_MID, CRYSTAL_LIGHT, CRYSTAL_HIGH,
)
CRYSTAL = CRYSTAL_MID
# CRYSTAL_DEEP / CRYSTAL_LIGHT / CRYSTAL_HIGH are already in this module's
# namespace via the import above and re-export from here as-is.

# Ash
ASH = "#3a3a48"          # secondary text on dark
ASH_BRIGHT = "#7d7d8a"   # tertiary

# Rune — quiet warm gold; the only warm accent now that the crystal is cool
RUNE = "#e5b85a"         # hover/active
RUNE_FAINT = "#8a7438"   # subtle separator

# Crimson — rare
CRIMSON = "#c4493a"      # warnings, locked overlay
CRIMSON_FAINT = "#7a2a22"


def _palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.Window, QColor(INK_0))
    p.setColor(QPalette.WindowText, QColor(PARCH_BG))
    p.setColor(QPalette.Base, QColor(INK_1))
    p.setColor(QPalette.AlternateBase, QColor(INK_2))
    p.setColor(QPalette.Text, QColor(PARCH_BG))
    p.setColor(QPalette.Button, QColor(INK_1))
    p.setColor(QPalette.ButtonText, QColor(PARCH_BG))
    p.setColor(QPalette.Highlight, QColor(CRYSTAL))
    p.setColor(QPalette.HighlightedText, QColor(INK_0))
    p.setColor(QPalette.PlaceholderText, QColor(ASH))
    p.setColor(QPalette.ToolTipBase, QColor(INK_2))
    p.setColor(QPalette.ToolTipText, QColor(PARCH_BG))
    return p


# --- global stylesheet ---
_GLOBAL_QSS = f"""
    QMainWindow, QWidget {{
        background-color: {INK_0};
        color: {PARCH_BG};
    }}

    QTabWidget::pane {{
        background: {INK_1};
        border: 1px solid {INK_BORDER};
        margin-top: 0;
    }}
    QTabBar {{
        background: transparent;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {ASH_BRIGHT};
        padding: 10px 18px 8px;
        border: 0;
        font-family: "Georgia", serif;
        font-size: 11px;
        letter-spacing: 2px;
        text-transform: uppercase;
    }}
    QTabBar::tab:selected {{
        color: {CRYSTAL_LIGHT};
        background: {INK_1};
        border-bottom: 2px solid {CRYSTAL};
    }}
    QTabBar::tab:hover:!selected {{
        color: {RUNE};
    }}

    /* Command chips for the top bar */
    QPushButton {{
        background: transparent;
        border: 1px solid {INK_BORDER_2};
        border-radius: 2px;
        padding: 7px 14px;
        color: {PARCH_BG};
        font-family: "Segoe UI";
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1px;
        text-transform: uppercase;
    }}
    QPushButton:hover {{
        color: {CRYSTAL_LIGHT};
        border-color: {RUNE};
        background: {INK_2};
    }}
    QPushButton:pressed {{
        background: {CRYSTAL_DEEP};
        color: {INK_0};
    }}
    QPushButton:checked {{
        background: {CRYSTAL};
        color: {INK_0};
        border-color: {CRYSTAL};
    }}
    QPushButton[role="primary"] {{
        color: {CRYSTAL_LIGHT};
        border-color: {CRYSTAL};
    }}
    QPushButton[role="primary"]:hover {{
        background: {CRYSTAL};
        color: {INK_0};
    }}
    QPushButton[role="destructive"]:hover {{
        color: {CRIMSON};
        border-color: {CRIMSON};
    }}

    QTableWidget {{
        background: {INK_1};
        alternate-background-color: {INK_0};
        gridline-color: {INK_BORDER};
        selection-background-color: {CRYSTAL};
        selection-color: {INK_0};
        font-family: "Consolas", "Courier New", monospace;
        font-size: 10px;
    }}
    QHeaderView::section {{
        background: transparent;
        color: {ASH_BRIGHT};
        padding: 10px 8px;
        border: 0;
        border-bottom: 1px solid {RUNE_FAINT};
        font-family: "Georgia", serif;
        font-size: 10px;
        font-weight: normal;
        letter-spacing: 2px;
        text-transform: uppercase;
    }}
    QTableWidget::item {{
        padding: 6px 8px;
        border-bottom: 1px solid {INK_BORDER};
    }}

    QStatusBar {{
        background: {INK_0};
        color: {ASH_BRIGHT};
        border-top: 1px solid {INK_BORDER};
        font-family: "Georgia", serif;
        font-size: 10px;
        letter-spacing: 1px;
    }}

    QSlider::groove:horizontal {{
        height: 2px; background: {INK_BORDER_2}; border-radius: 0;
    }}
    QSlider::handle:horizontal {{
        background: {CRYSTAL}; border: 0;
        width: 10px; height: 14px; margin: -6px 0; border-radius: 1px;
    }}
    QSlider::handle:horizontal:hover {{ background: {CRYSTAL_LIGHT}; }}
    QSlider::sub-page:horizontal {{ background: {CRYSTAL}; }}

    QCheckBox {{ color: {PARCH_BG}; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 12px; height: 12px;
        border: 1px solid {INK_BORDER_2};
        border-radius: 0;
        background: {INK_1};
    }}
    QCheckBox::indicator:hover {{ border-color: {RUNE}; }}
    QCheckBox::indicator:checked {{
        background: {CRYSTAL}; border-color: {CRYSTAL};
    }}
"""


def apply(app: QApplication) -> None:
    """Apply the dark journal theme to the main window + chrome."""
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setPalette(_palette())
    app.setStyleSheet(_GLOBAL_QSS)


# --- overlay-specific tokens (exported for srt.overlay) ---
# The overlay used to be parchment-on-ink, like a journal page pinned to
# the side of the screen. The user wanted it to feel like part of the
# same app as the main window instead, so it's now Ink + crystal — the
# same dark palette, same Georgia + Consolas pairing, same crystal-blue
# accent. The only places that differ are the layout (tall narrow card
# vs wide window) and the lock interaction.
OVERLAY_QSS = f"""
    #OverlayCard {{
        background: {INK_1};
        border: 1px solid {INK_BORDER_2};
        border-radius: 0;
    }}
    #OverlayHandle {{
        color: {CRYSTAL_LIGHT};
        background: transparent;
        font-family: "Georgia", serif;
        font-size: 9px;
        font-weight: bold;
        letter-spacing: 3px;
    }}
    #OverlayField {{
        color: {PARCH_BG};
        background: transparent;
        font-family: "Consolas", "Courier New", monospace;
    }}
    #OverlayLabel {{
        color: {ASH_BRIGHT};
        background: transparent;
        font-family: "Georgia", serif;
        font-size: 9px;
        font-weight: bold;
        letter-spacing: 3px;
    }}
    #OverlayNumber {{
        color: {PARCH_BG};
        background: transparent;
        font-family: "Consolas", "Courier New", monospace;
        font-weight: bold;
    }}
    #OverlayPill {{
        background: transparent;
        border: 1px solid {INK_BORDER_2};
        border-radius: 0;
        color: {PARCH_BG};
        font-family: "Georgia", serif;
        font-size: 9px;
        font-weight: bold;
        letter-spacing: 2px;
        padding: 0 10px;
    }}
    #OverlayPill:hover {{
        background: {CRYSTAL};
        color: {INK_0};
        border-color: {CRYSTAL};
    }}
    #OverlayRule {{
        background: {RUNE_FAINT};
        max-height: 1px;
        min-height: 1px;
    }}
"""