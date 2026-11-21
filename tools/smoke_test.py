"""Smoke test: run the app offscreen, exercise core paths, render UI to PNG.

Usage: PYTHONPATH=.deps:. /usr/bin/python3.14 tools/smoke_test.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from microscope_app.main_window import MainWindow  # noqa: E402
from microscope_app.measure import Measurement  # noqa: E402
from microscope_app.__main__ import apply_dark_palette, find_device  # noqa: E402

OUT = "/tmp/microscope_smoke.png"


def main() -> int:
    app = QApplication(sys.argv)
    apply_dark_palette(app)
    win = MainWindow(find_device(), 1920, 1080, 60)
    win.resize(1520, 960)
    win.show()

    results: list[str] = []

    def stage2() -> None:
        frame = win.cam.latest_frame()
        if frame is None:
            # Synthesize a test PCB pattern frame if running in a sandbox without /dev/videoN
            synth = np.zeros((1080, 1920, 3), dtype=np.uint8)
            synth[:, :] = (35, 65, 30)  # dark green solder mask
            win.viewer.frozen = synth
            win.viewer.is_frozen = True
            frame = synth
            results.append("INFO: using synthetic 1920x1080 frame (no V4L2 device node in sandbox)")
        else:
            results.append(f"OK: live frame {frame.shape[1]}x{frame.shape[0]} @ {win.cam.measured_fps:.1f} fps")

        H, W = frame.shape[:2]
        # Exercise zoom, software image processing, and multi-mode measurements
        win.viewer.zoom = 2.0
        win.clahe_check.setChecked(True)
        win.sw_sliders["gamma"]["slider"].setValue(120)
        win.viewer.measurements = [
            Measurement((W * 0.35, H * 0.40), (W * 0.55, H * 0.40), kind="line", pin_count=5, frame_size=(W, H)),
            Measurement((W * 0.45, H * 0.55), (W * 0.52, H * 0.62), kind="circle", frame_size=(W, H)),
            Measurement((W * 0.25, H * 0.55), (W * 0.35, H * 0.63), kind="rect", frame_size=(W, H)),
        ]
        # Verify resolution rescaling of measurements
        m_test = Measurement((960.0, 540.0), (1920.0, 1080.0), frame_size=(1920, 1080))
        m_test.rescale_to(3840, 2160)
        assert m_test.p1 == (1920.0, 1080.0) and m_test.p2 == (3840.0, 2160.0)
        results.append("OK: measurement math & resolution rescaling verified")
        win.viewer._refresh()
        QTimer.singleShot(300, finish)

    def finish() -> None:
        win.grab().save(OUT)
        results.append(f"OK: UI rendered -> {OUT}")
        win.cam.stop()
        for line in results:
            print(line, file=sys.stderr)
        app.quit()

    QTimer.singleShot(1000, stage2)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
