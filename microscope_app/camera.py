"""Background V4L2 capture thread.

Grabs full-resolution MJPEG frames and stores the latest one in a slot the
UI polls on a display timer (no per-frame signal copies of 24 MB frames).
If a recorder is attached, frames are also handed to its bounded queue.
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np
from PySide6.QtCore import QThread, QMutex, QMutexLocker


class CameraThread(QThread):
    # The Venus firmware misbehaves on instant close→reopen cycles (first
    # grabs can raise raw C++ exceptions inside cv2 → process abort). Give
    # it a moment to settle between users within this process.
    _last_release = 0.0
    REOPEN_SETTLE = 0.3

    def __init__(
        self,
        device: str = "/dev/video0",
        width: int = 3840,
        height: int = 2160,
        fps: int = 60,
        parent=None,
    ):
        super().__init__(parent)
        # OpenCV's V4L2 backend rejects /dev/v4l/by-id symlinks — resolve to
        # the real /dev/videoN node (which can change across replugs).
        self.device = os.path.realpath(device)
        self.width = width
        self.height = height
        self.fps = fps
        self._cap: cv2.VideoCapture | None = None
        self._mutex = QMutex()
        self._frame: np.ndarray | None = None
        self._running = False
        self.recorder = None  # set by main window; must have try_put(frame)
        self.measured_fps = 0.0
        self.error: str | None = None
        # Minimum seconds between decodes for the live view: 0.0 decodes
        # every grabbed frame (low-latency mode), 1/30 s halves the CPU.
        self.decode_interval = 1.0 / 30.0

    # ---- frame access from UI thread ---------------------------------------
    def latest_frame(self) -> np.ndarray | None:
        with QMutexLocker(self._mutex):
            return None if self._frame is None else self._frame

    def frame_size(self) -> tuple[int, int]:
        f = self.latest_frame()
        if f is None:
            return self.height, self.width  # rows, cols
        return f.shape[0], f.shape[1]

    # ---- lifecycle -----------------------------------------------------------
    def stop(self) -> None:
        self._running = False
        if not self.wait(2500):
            # A stalled USB stream can block grab() indefinitely; force the
            # thread down so the app never aborts on a live QThread at exit.
            self.terminate()
            self.wait(1500)

    def run(self) -> None:  # noqa: D102 (thread body)
        settle = self.REOPEN_SETTLE - (time.monotonic() - CameraThread._last_release)
        if settle > 0:
            time.sleep(settle)
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.error = (
                f"Cannot open {self.device}.\n"
                "Is another app (mpv?) holding the camera?"
            )
            return
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        self._running = True

        # Low-latency strategy: grab() is cheap (dequeues compressed data and
        # keeps the driver queue drained, so the newest frame is always on
        # top), while retrieve() does the expensive MJPEG decode. Decoding on
        # a cadence instead of every frame halves the CPU and removes the
        # chronic 1-3 frame staleness of a decode-bound loop.
        grabbed_frames, t0, last_log = 0, time.monotonic(), time.monotonic()
        last_decode = 0.0
        while self._running:
            if not cap.grab():
                if time.monotonic() - last_log > 2.0:
                    self.error = "Frame read failed — check USB connection."
                    last_log = time.monotonic()
                self.msleep(20)
                continue
            self.error = None
            grabbed_frames += 1
            now = time.monotonic()
            if now - t0 >= 0.5:
                self.measured_fps = grabbed_frames / (now - t0)
                grabbed_frames, t0 = 0, now
            recording = self.recorder is not None and self.recorder.active
            if recording or (now - last_decode) >= self.decode_interval:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    with QMutexLocker(self._mutex):
                        self._frame = frame
                    if recording:
                        self.recorder.try_put(frame)
                    last_decode = time.monotonic()

        cap.release()
        self._cap = None
        CameraThread._last_release = time.monotonic()
