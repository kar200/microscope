"""Entry point: python -m microscope_app [--device /dev/videoX] [options]."""
from __future__ import annotations

import argparse
import glob
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def find_device() -> str:
    for pattern in ("/dev/v4l/by-id/*HH-4K*video-index0",):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return "/dev/video0"


def apply_dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    window, base, text, disabled = (QColor(32, 32, 36), QColor(24, 24, 27),
                                    QColor(225, 225, 228), QColor(110, 110, 116))
    pal.setColor(QPalette.ColorRole.Window, window)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, base)
    pal.setColor(QPalette.ColorRole.AlternateBase, window)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, window)
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.Highlight, QColor(58, 130, 210))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(240, 240, 244))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    app.setPalette(pal)


def main() -> int:
    ap = argparse.ArgumentParser(prog="microscope_app",
                                 description="Bresun HH-4K MAX microscope app")
    ap.add_argument("--device", default=find_device(), help="V4L2 device path")
    ap.add_argument("--width", type=int, default=None,
                    help="capture width (default: last used / 1920×1080@60)")
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--fps", type=int, default=None)
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Bresun Microscope")
    apply_dark_palette(app)
    win = MainWindow(args.device, args.width, args.height, args.fps)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
