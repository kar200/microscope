"""Video canvas: zoom/pan, freeze frame, software image processing,
multi-mode measurement (line/pitch, circle, rectangle), magnifier loupe,
and HUD overlays.

Coordinate spaces:
  * widget px      — what you see on screen
  * transformed px — the frame after flip/rotate (t-space)
  * frame px       — the raw sensor image (o-space)
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QCursor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF,
)
from PySide6.QtWidgets import QMenu, QWidget

from .measure import (
    Measurement, format_measurement_lines, format_scalar, snap_point,
)

ZOOM_MIN, ZOOM_MAX = 1.0, 12.0
ENDPOINT_HIT_RADIUS_PX = 10.0

NICE_BAR_MM = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50]

HELP_TEXT = [
    "Space        freeze / unfreeze live view",
    "S            snapshot (full-res PNG)",
    "R            start / stop video recording",
    "M            measure mode (Line / Pitch)",
    "D / B        measure Circle (D) / Rectangle (B)",
    "C            calibrate (click 2 points on known length)",
    "Shift+Drag   snap measurement line to 0° / 45° / 90°",
    "Drag dot     fine-tune any existing measurement endpoint",
    "G            toggle cursor magnifier loupe in measure mode",
    "E            toggle Enhance IC Markings (CLAHE local contrast)",
    "Backspace    remove last measurement  ·  Esc  cancel / clear all",
    "L            low-latency mode on/off",
    "F / F11      fullscreen  ·  Tab  toggle control drawer",
    "Wheel / Drag zoom under cursor / pan  ·  Double-click  reset view",
    "Right-click  quick context menu (or cancel pending point)",
    "H            toggle this help  ·  full guide: docs/USAGE.md",
]


def _inv_rot(k: int, tx: float, ty: float, W: int, H: int) -> tuple[float, float]:
    """Transformed point → original frame point (rotation inverse)."""
    if k == 1:  # display rotated 90° CW
        return ty, H - 1.0 - tx
    if k == 2:
        return W - 1.0 - tx, H - 1.0 - ty
    if k == 3:
        return W - 1.0 - ty, tx
    return tx, ty


def _fwd_rot(k: int, x: float, y: float, W: int, H: int) -> tuple[float, float]:
    """Original frame point → transformed point."""
    if k == 1:
        return H - 1.0 - y, x
    if k == 2:
        return W - 1.0 - x, H - 1.0 - y
    if k == 3:
        return y, W - 1.0 - x
    return x, y


class Viewer(QWidget):
    calibration_captured = Signal(float)  # px length of the drawn calib line
    context_action = Signal(str)          # emitted for main_window actions

    def __init__(self, camera, controls, calib_store, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(640, 400)
        self.setStyleSheet("background-color: #101010;")

        self.cam = camera
        self.ctl = controls
        self.calib = calib_store

        # view state
        self.zoom = 1.0
        self.center_t: tuple[float, float] | None = None  # view center, t-space
        self.flip_h = False
        self.flip_v = False
        self.rot = 0  # quarter turns CW
        self.frozen: np.ndarray | None = None
        self.is_frozen = False
        self._low_latency = False

        # software image processing (works reliably regardless of UVC firmware stubs)
        self.sw_brightness = 0     # -100 .. +100
        self.sw_contrast = 0       # -100 .. +100
        self.sw_gamma = 1.0        # 0.30 .. 3.00
        self.sw_sharpness = 0      # 0 .. 100
        self.sw_clahe = False      # local contrast enhancement for IC markings
        self.sw_grayscale = False  # monochrome inspection mode
        self._lut_key: tuple[int, int, float] | None = None
        self._lut: np.ndarray | None = None
        self._clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))

        # overlays
        self.show_crosshair = True
        self.show_grid = False
        self.show_help = False
        self.extra_hint = ""

        # measurement state (frame coordinates)
        self.measure_mode = False
        self.measure_kind = "line"      # "line" | "circle" | "circle3" | "rect"
        self.pin_count = 1              # >1 calculates pin pitch along line
        self.unit_mode = "auto"         # "auto" | "mm" | "um" | "mil" | "both"
        self.show_details = True        # show ΔX, ΔY, Angle / Area
        self.keep_measurements = True   # accumulate multiple measurements
        self.show_loupe = True          # precision magnifier loupe when clicking

        self.calibrating = False
        self.pending_pts: list[tuple[float, float]] = []
        self.cursor_frame: tuple[float, float] | None = None
        self.cursor_widget: QPoint | None = None
        self.measurements: list[Measurement] = []
        self.calib_line: Measurement | None = None
        self._last_frame_size: tuple[int, int] | None = None

        # endpoint dragging state: (measurement_obj, endpoint_index)
        self._dragging_ep: tuple[Measurement, int] | None = None
        self._hover_ep: tuple[Measurement, int] | None = None

        self._pan_start = None
        self._display: QImage | None = None
        # _view = (dx, dy, dw, dh, x0t, y0t, scale_x, scale_y, frame_shape)
        self._view = None

        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self._refresh)
        self._tick.start()

    # ---- backwards-compatible properties ------------------------------------
    @property
    def pending_pt(self) -> tuple[float, float] | None:
        return self.pending_pts[0] if self.pending_pts else None

    @pending_pt.setter
    def pending_pt(self, val: tuple[float, float] | None) -> None:
        if val is None:
            self.pending_pts.clear()
        else:
            self.pending_pts = [val]

    @property
    def measurement(self) -> Measurement | None:
        return self.measurements[-1] if self.measurements else None

    @measurement.setter
    def measurement(self, val: Measurement | None) -> None:
        if val is None:
            self.measurements.clear()
        else:
            frame = self.current_frame()
            if frame is not None:
                val.frame_size = (frame.shape[1], frame.shape[0])
            self.measurements = [val]

    # ----------------------------------------------------------------- frames
    def current_frame(self) -> np.ndarray | None:
        frame = self.frozen if self.is_frozen else self.cam.latest_frame()
        if frame is not None:
            H, W = frame.shape[:2]
            if self._last_frame_size is not None and self._last_frame_size != (W, H):
                for m in self.measurements:
                    m.rescale_to(W, H)
                if self.calib_line is not None:
                    self.calib_line.rescale_to(W, H)
                sx = W / float(self._last_frame_size[0])
                sy = H / float(self._last_frame_size[1])
                self.pending_pts = [(p[0] * sx, p[1] * sy) for p in self.pending_pts]
            self._last_frame_size = (W, H)
        return frame

    @property
    def low_latency(self) -> bool:
        return self._low_latency

    @low_latency.setter
    def low_latency(self, on: bool) -> None:
        self._low_latency = bool(on)
        self._tick.setInterval(16 if on else 33)
        self.cam.decode_interval = 0.0 if on else 1.0 / 30.0

    def toggle_freeze(self) -> None:
        if not self.is_frozen:
            self.frozen = self.cam.latest_frame()
            self.is_frozen = self.frozen is not None
        else:
            self.is_frozen = False
            self.frozen = None
        self._refresh()

    # ------------------------------------------ software image processing
    def has_sw_adjustments(self) -> bool:
        return (
            self.sw_brightness != 0
            or self.sw_contrast != 0
            or abs(self.sw_gamma - 1.0) > 1e-3
            or self.sw_sharpness > 0
            or self.sw_clahe
            or self.sw_grayscale
        )

    def _get_lut(self) -> np.ndarray | None:
        if (
            self.sw_brightness == 0
            and self.sw_contrast == 0
            and abs(self.sw_gamma - 1.0) <= 1e-3
        ):
            return None
        key = (self.sw_brightness, self.sw_contrast, round(self.sw_gamma, 3))
        if self._lut_key != key or self._lut is None:
            x = np.arange(256, dtype=np.float32) / 255.0
            # 1. Gamma correction (gamma > 1 lifts dark PCB/IC regions)
            if abs(self.sw_gamma - 1.0) > 1e-3:
                x = np.power(np.clip(x, 0.0, 1.0), 1.0 / max(0.1, self.sw_gamma))
            # 2. Contrast around midpoint 0.5 + Brightness offset
            c_factor = math.pow(2.0, self.sw_contrast / 50.0)
            b_offset = self.sw_brightness / 150.0
            x = (x - 0.5) * c_factor + 0.5 + b_offset
            self._lut = np.clip(x * 255.0, 0, 255).astype(np.uint8)
            self._lut_key = key
        return self._lut

    def process_frame(self, img: np.ndarray) -> np.ndarray:
        """Apply active software image adjustments to a BGR frame or crop."""
        if not self.has_sw_adjustments():
            return img
        out = img
        lut = self._get_lut()
        if lut is not None:
            out = cv2.LUT(out, lut)
        if self.sw_clahe:
            if self.sw_grayscale:
                gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
                eq = self._clahe.apply(gray)
                out = cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)
            else:
                lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
                l_ch, a_ch, b_ch = cv2.split(lab)
                l_ch = self._clahe.apply(l_ch)
                out = cv2.cvtColor(cv2.merge((l_ch, a_ch, b_ch)), cv2.COLOR_LAB2BGR)
        elif self.sw_grayscale:
            gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
            out = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        if self.sw_sharpness > 0:
            blurred = cv2.GaussianBlur(out, (0, 0), 1.5)
            amount = (self.sw_sharpness / 100.0) * 1.8
            out = cv2.addWeighted(out, 1.0 + amount, blurred, -amount, 0)
        return out

    # --------------------------------------------------------- transform math
    def _t_to_o(self, tx: float, ty: float, frame: np.ndarray) -> tuple[float, float]:
        H, W = frame.shape[:2]
        x, y = _inv_rot(self.rot, tx, ty, W, H)
        if self.flip_h:
            x = W - 1.0 - x
        if self.flip_v:
            y = H - 1.0 - y
        return x, y

    def _o_to_t(self, x: float, y: float, frame: np.ndarray) -> tuple[float, float]:
        H, W = frame.shape[:2]
        if self.flip_h:
            x = W - 1.0 - x
        if self.flip_v:
            y = H - 1.0 - y
        return _fwd_rot(self.rot, x, y, W, H)

    def widget_to_frame(self, pos: QPoint | QPointF) -> tuple[float, float] | None:
        if self._view is None:
            return None
        dx, dy, dw, dh, x0t, y0t, scale_x, scale_y, shape = self._view
        frame = self.current_frame()
        if frame is None or frame.shape != shape:
            return None
        px, py = float(pos.x()), float(pos.y())
        if not (0.0 <= px - dx <= dw and 0.0 <= py - dy <= dh):
            return None
        tx = x0t + (px - dx) / scale_x
        ty = y0t + (py - dy) / scale_y
        ox, oy = self._t_to_o(tx, ty, frame)
        H, W = frame.shape[:2]
        return (min(max(ox, 0.0), W - 1.0), min(max(oy, 0.0), H - 1.0))

    def frame_to_widget_f(self, pt: tuple[float, float]) -> QPointF | None:
        if self._view is None:
            return None
        dx, dy, _, _, x0t, y0t, scale_x, scale_y, shape = self._view
        frame = self.current_frame()
        if frame is None or frame.shape != shape:
            return None
        tx, ty = self._o_to_t(pt[0], pt[1], frame)
        return QPointF(dx + (tx - x0t) * scale_x, dy + (ty - y0t) * scale_y)

    def frame_to_widget(self, pt: tuple[float, float]) -> QPoint | None:
        pf = self.frame_to_widget_f(pt)
        if pf is None:
            return None
        return QPoint(round(pf.x()), round(pf.y()))

    # ---------------------------------------------------------- display build
    def _refresh(self) -> None:
        frame = self.current_frame()
        if frame is None:
            self._display = None
            self._view = None
            self.update()
            return
        cw, ch = max(1, self.width()), max(1, self.height())
        H, W = frame.shape[:2]
        tw, th = (H, W) if self.rot % 2 else (W, H)

        fit = min(cw / tw, ch / th)
        scale = fit * self.zoom
        sw = min(cw / scale, float(tw))  # source rect size, t-space
        sh = min(ch / scale, float(th))
        if self.center_t is None:
            self.center_t = (tw / 2.0, th / 2.0)
        x0t = min(max(self.center_t[0] - sw / 2.0, 0.0), tw - sw)
        y0t = min(max(self.center_t[1] - sh / 2.0, 0.0), th - sh)

        # letterboxed content rect
        dw, dh = max(2, round(sw * scale)), max(2, round(sh * scale))
        dx, dy = (cw - dw) // 2, (ch - dh) // 2

        # inverse-transform source rect corners → frame-space bbox
        corners = [
            (x0t, y0t),
            (x0t + sw, y0t),
            (x0t + sw, y0t + sh),
            (x0t, y0t + sh),
        ]
        pts = [self._t_to_o(*c, frame) for c in corners]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        fx0, fy0 = max(0, int(np.floor(min(xs)))), max(0, int(np.floor(min(ys))))
        fx1 = min(W, int(np.ceil(max(xs))))
        fy1 = min(H, int(np.ceil(max(ys))))
        if fx1 - fx0 < 2 or fy1 - fy0 < 2:
            self._display = None
            self.update()
            return

        # Compute exact t-space origin & scale of the integer crop slice so
        # measurement overlays match the rendered pixels with zero drift at 12× zoom
        slice_o_corners = [
            (float(fx0), float(fy0)),
            (float(fx1 - 1), float(fy0)),
            (float(fx1 - 1), float(fy1 - 1)),
            (float(fx0), float(fy1 - 1)),
        ]
        slice_t_corners = [self._o_to_t(ox, oy, frame) for ox, oy in slice_o_corners]
        actual_x0t = min(c[0] for c in slice_t_corners)
        actual_y0t = min(c[1] for c in slice_t_corners)

        crop = frame[fy0:fy1, fx0:fx1]
        if self.flip_h:
            crop = cv2.flip(crop, 1)
        if self.flip_v:
            crop = cv2.flip(crop, 0)
        if self.rot:
            crop = cv2.rotate(
                crop,
                (
                    cv2.ROTATE_90_CLOCKWISE,
                    cv2.ROTATE_180,
                    cv2.ROTATE_90_COUNTERCLOCKWISE,
                )[self.rot - 1],
            )
        crop_th, crop_tw = crop.shape[:2]
        scale_x = dw / max(1.0, float(crop_tw - 1 if crop_tw > 1 else 1))
        scale_y = dh / max(1.0, float(crop_th - 1 if crop_th > 1 else 1))

        ratio = dw / crop_tw
        if ratio < 0.5 and not self._low_latency:
            interp = cv2.INTER_AREA
        elif ratio > 1.2:
            interp = cv2.INTER_CUBIC
        else:
            interp = cv2.INTER_LINEAR
        img = cv2.resize(crop, (dw, dh), interpolation=interp)
        img = self.process_frame(img)
        h, w = img.shape[:2]
        self._display = QImage(
            img.data, w, h, w * 3, QImage.Format.Format_BGR888
        ).copy()
        self._view = (
            dx,
            dy,
            dw,
            dh,
            actual_x0t,
            actual_y0t,
            scale_x,
            scale_y,
            frame.shape,
        )
        self.update()

    # --------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#101010"))
        if self._display is None:
            err = self.cam.error
            txt = err if err else "Waiting for frames…"
            p.setPen(QColor("#c0c0c0"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, txt)
            p.end()
            return
        dx, dy, dw, dh, *_ = self._view
        p.drawImage(dx, dy, self._display)

        if self.show_grid:
            self._draw_grid(p, dx, dy, dw, dh)
        self._draw_crosshair(p, dx, dy, dw, dh)
        self._draw_scale_bar(p, dx, dy, dw, dh)
        self._draw_measure(p)
        if self.show_loupe and (
            self.measure_mode or self.calibrating or self._dragging_ep is not None
        ):
            self._draw_loupe(p, dx, dy, dw, dh)
        self._draw_hud(p)
        if self.show_help:
            self._draw_help(p)
        if self.is_frozen:
            self._badge(p, dx + dw - 92, dy + 10, "❄ FROZEN", QColor("#3aa0ff"))
        if getattr(self.cam, "recorder", None) and self.cam.recorder.active:
            self._badge(p, dx + dw - 92, dy + 44, "● REC", QColor("#e04040"))
        p.end()

    def _badge(self, p: QPainter, x: int, y: int, text: str, color: QColor) -> None:
        rect = QRect(x, y, 84, 26)
        p.fillRect(rect, QColor(0, 0, 0, 140))
        p.setPen(color)
        p.drawRect(rect)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_grid(self, p: QPainter, dx: int, dy: int, dw: int, dh: int) -> None:
        frame = self.current_frame()
        fw = frame.shape[1] if frame is not None else self.cam.width
        um_pp = self.calib.um_per_px(fw)
        p.setPen(QPen(QColor(255, 255, 255, 55), 1, Qt.PenStyle.DotLine))
        if um_pp and self._view is not None:
            scale_x, scale_y = self._view[6], self._view[7]
            target_mm = 0.14 * dw / scale_x * um_pp / 1000.0
            nice = min(NICE_BAR_MM, key=lambda v: abs(v - target_mm))
            step_x = max(24.0, nice * 1000.0 / um_pp * scale_x)
            step_y = max(24.0, nice * 1000.0 / um_pp * scale_y)
        else:
            step_x, step_y = dw / 8.0, dh / 8.0
        cx, cy = dx + dw / 2.0, dy + dh / 2.0
        k = 1
        while cx + k * step_x < dx + dw or cx - k * step_x > dx:
            if cx + k * step_x < dx + dw:
                x = round(cx + k * step_x)
                p.drawLine(x, dy, x, dy + dh)
            if cx - k * step_x > dx:
                x = round(cx - k * step_x)
                p.drawLine(x, dy, x, dy + dh)
            k += 1
        k = 1
        while cy + k * step_y < dy + dh or cy - k * step_y > dy:
            if cy + k * step_y < dy + dh:
                y = round(cy + k * step_y)
                p.drawLine(dx, y, dx + dw, y)
            if cy - k * step_y > dy:
                y = round(cy - k * step_y)
                p.drawLine(dx, y, dx + dw, y)
            k += 1

    def _draw_crosshair(self, p: QPainter, dx: int, dy: int, dw: int, dh: int) -> None:
        if not self.show_crosshair:
            return
        cx, cy = dx + dw // 2, dy + dh // 2
        gap, arm = 18, 70
        p.setPen(QPen(QColor(255, 255, 255, 200), 1))
        for x1, y1, x2, y2 in (
            (cx - gap - arm, cy, cx - gap, cy),
            (cx + gap, cy, cx + gap + arm, cy),
            (cx, cy - gap - arm, cx, cy - gap),
            (cx, cy + gap, cx, cy + gap + arm),
        ):
            p.drawLine(x1, y1, x2, y2)

    def _draw_scale_bar(self, p: QPainter, dx: int, dy: int, dw: int, dh: int) -> None:
        frame = self.current_frame()
        fw = frame.shape[1] if frame is not None else self.cam.width
        um_pp = self.calib.um_per_px(fw)
        if not um_pp or self._view is None:
            return
        scale = self._view[6]
        target_mm = 0.14 * dw / scale * um_pp / 1000.0
        if target_mm <= 0:
            return
        nice = min(NICE_BAR_MM, key=lambda v: abs(v - target_mm))
        bar_px = int(round(nice * 1000.0 / um_pp * scale))
        x1, x2 = dx + dw - 24 - bar_px, dx + dw - 24
        y = dy + dh - 30
        p.setPen(QPen(QColor(0, 0, 0, 180), 5))
        p.drawLine(x1, y, x2, y)
        p.setPen(QPen(QColor(255, 255, 255, 235), 3))
        p.drawLine(x1, y, x2, y)
        p.drawLine(x1, y - 6, x1, y + 6)
        p.drawLine(x2, y - 6, x2, y + 6)
        label = f"{nice:g} mm" if nice >= 1 else f"{nice * 1000:g} µm"
        if self.unit_mode in ("mil", "both"):
            mils = nice * 1000.0 / 25.4
            label = f"{mils:.1f} mil" if self.unit_mode == "mil" else f"{label} ({mils:.1f} mil)"
        lrect = QRect(x1 - 50, y - 34, bar_px + 100, 22)
        p.fillRect(lrect.adjusted(10, 2, -10, -2), QColor(0, 0, 0, 130))
        p.drawText(lrect, Qt.AlignmentFlag.AlignCenter, label)

    def _build_preview_measurement(self) -> tuple[Measurement, bool] | None:
        if not self.pending_pts or not self.cursor_frame:
            return None
        frame = self.current_frame()
        fsize = (frame.shape[1], frame.shape[0]) if frame is not None else (self.cam.width, self.cam.height)
        if self.calibrating:
            return Measurement(self.pending_pts[0], self.cursor_frame, kind="line", frame_size=fsize), True
        if self.measure_kind == "circle3":
            if len(self.pending_pts) == 1:
                return Measurement(self.pending_pts[0], self.cursor_frame, kind="circle", frame_size=fsize), False
            return Measurement(
                self.pending_pts[0], self.pending_pts[1], p3=self.cursor_frame, kind="circle", frame_size=fsize
            ), False
        if self.measure_kind == "rect":
            if len(self.pending_pts) == 1:
                return Measurement(self.pending_pts[0], self.cursor_frame, kind="line", frame_size=fsize), False
            return Measurement(
                self.pending_pts[0], self.pending_pts[1], p3=self.cursor_frame, kind="rect", frame_size=fsize
            ), False
        if self.measure_kind == "polyline":
            if self._is_near_polyline_start():
                pts = list(self.pending_pts)
                return Measurement(pts[0], pts[-1], kind="polyline", points=pts, closed=True, frame_size=fsize), False
            pts = [*self.pending_pts, self.cursor_frame]
            return Measurement(pts[0], pts[-1], kind="polyline", points=pts, frame_size=fsize), False
        kind = "circle" if self.measure_kind == "circle" else self.measure_kind
        return Measurement(
            self.pending_pts[0],
            self.cursor_frame,
            kind=kind,
            pin_count=self.pin_count,
            frame_size=fsize,
        ), False

    def _draw_measure(self, p: QPainter) -> None:
        frame = self.current_frame()
        fw = frame.shape[1] if frame is not None else self.cam.width
        um_pp = self.calib.um_per_px(fw)

        items: list[tuple[Measurement, QColor, bool]] = []
        for idx, m in enumerate(self.measurements):
            is_last = idx == len(self.measurements) - 1
            col = QColor("#ffd24d") if is_last else QColor("#b8d868")
            items.append((m, col, False))
        if self.calib_line:
            items.append((self.calib_line, QColor("#4dd2ff"), True))
        prev = self._build_preview_measurement()
        if prev is not None:
            pm, is_cal = prev
            col = QColor("#4dd2ff") if is_cal else QColor("#ffd24d")
            items.append((pm, col, is_cal))

        for m, color, is_calib in items:
            w1 = self.frame_to_widget_f(m.p1)
            w2 = self.frame_to_widget_f(m.p2)
            if w1 is None or w2 is None:
                continue

            if m.kind == "circle":
                geom = m.circle_geometry()
                if geom is None:
                    continue
                (cx, cy), r_px = geom
                wc = self.frame_to_widget_f((cx, cy))
                edge_w = self.frame_to_widget_f((cx + r_px, cy))
                if wc is None or edge_w is None:
                    continue
                rw = math.hypot(edge_w.x() - wc.x(), edge_w.y() - wc.y())
                p.setPen(QPen(QColor(0, 0, 0, 160), 4))
                p.drawEllipse(wc, rw, rw)
                p.setPen(QPen(color, 2))
                p.drawEllipse(wc, rw, rw)
                # diameter dashed line & center cross
                p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(wc.x() - rw, wc.y()), QPointF(wc.x() + rw, wc.y()))
                p.drawLine(QPointF(wc.x(), wc.y() - 5), QPointF(wc.x(), wc.y() + 5))
                anchor = QPoint(round(wc.x()), round(wc.y() - rw - 6))
            elif m.kind == "rect":
                corners_f, w_px, h_px, _ = m.rect_geometry()
                w_corners = [self.frame_to_widget_f(c) for c in corners_f]
                if any(c is None for c in w_corners):
                    continue
                poly = QPolygonF(w_corners)
                p.setPen(QPen(QColor(0, 0, 0, 160), 4))
                p.drawPolygon(poly)
                p.setPen(QPen(color, 2))
                p.drawPolygon(poly)
                p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
                p.drawLine(w_corners[0], w_corners[2])
                # Draw individual edge labels on W (c0->c1) and H (c1->c2)
                fm_s = p.fontMetrics()
                for a_w, b_w, edge_px in (
                    (w_corners[0], w_corners[1], w_px),
                    (w_corners[1], w_corners[2], h_px),
                ):
                    if math.hypot(b_w.x() - a_w.x(), b_w.y() - a_w.y()) > 42:
                        etxt = format_scalar(edge_px, um_pp, unit=self.unit_mode)
                        tw = fm_s.horizontalAdvance(etxt) + 8
                        th = fm_s.height() + 2
                        mx = round((a_w.x() + b_w.x()) * 0.5)
                        my = round((a_w.y() + b_w.y()) * 0.5)
                        eb = QRect(mx - tw // 2, my - th // 2, tw, th)
                        p.fillRect(eb, QColor(0, 0, 0, 175))
                        p.setPen(color)
                        p.drawText(eb, Qt.AlignmentFlag.AlignCenter, etxt)
                top_y = min(c.y() for c in w_corners)
                mid_x = sum(c.x() for c in w_corners) / 4.0
                anchor = QPoint(round(mid_x), round(top_y - 6))
            elif m.kind == "polyline":
                segs = m.polyline_segments()
                w_pts: list[QPointF] = []
                fm_s = p.fontMetrics()
                for a_f, b_f, seg_px in segs:
                    wa = self.frame_to_widget_f(a_f)
                    wb = self.frame_to_widget_f(b_f)
                    if wa is None or wb is None:
                        continue
                    w_pts.append(wb)
                    p.setPen(QPen(QColor(0, 0, 0, 170), 4))
                    p.drawLine(wa, wb)
                    p.setPen(QPen(color, 2))
                    p.drawLine(wa, wb)
                    # Draw per-segment length pill at segment midpoint
                    if math.hypot(wb.x() - wa.x(), wb.y() - wa.y()) > 26:
                        stxt = format_scalar(seg_px, um_pp, unit=self.unit_mode)
                        tw = fm_s.horizontalAdvance(stxt) + 8
                        th = fm_s.height() + 2
                        mx = round((wa.x() + wb.x()) * 0.5)
                        my = round((wa.y() + wb.y()) * 0.5)
                        sb = QRect(mx - tw // 2, my - th // 2 - 8, tw, th)
                        p.fillRect(sb, QColor(0, 0, 0, 180))
                        p.setPen(QColor(255, 255, 255))
                        p.drawText(sb, Qt.AlignmentFlag.AlignCenter, stxt)
                if not w_pts:
                    continue
                if m.closed:
                    top_y = min(pt.y() for pt in w_pts)
                    mid_x = sum(pt.x() for pt in w_pts) / len(w_pts)
                    anchor = QPoint(round(mid_x), round(top_y - 10))
                else:
                    last_w = w_pts[-1]
                    anchor = QPoint(round(last_w.x()), round(last_w.y() - 12))
                # Highlight the start point when snapping to close a polyline
                if (
                    self.measure_kind == "polyline"
                    and len(self.pending_pts) >= 3
                    and m.points == self.pending_pts
                    and m.closed
                ):
                    w0 = self.frame_to_widget_f(self.pending_pts[0])
                    if w0 is not None:
                        p.setPen(QPen(QColor("#ffd24d"), 2.5))
                        p.setBrush(QColor(255, 210, 77, 70))
                        p.drawEllipse(w0, 11.0, 11.0)
                        p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                # Line (+ optional ΔX/ΔY right-triangle guide & pin-pitch ticks)
                if self.show_details and not is_calib and m.dx > 8 and m.dy > 8:
                    corner = QPointF(w2.x(), w1.y())
                    p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 110), 1, Qt.PenStyle.DotLine))
                    p.drawLine(w1, corner)
                    p.drawLine(corner, w2)
                p.setPen(QPen(QColor(0, 0, 0, 170), 4))
                p.drawLine(w1, w2)
                p.setPen(QPen(color, 2))
                p.drawLine(w1, w2)
                # Draw perpendicular end ticks and intermediate pin-pitch ticks
                dx_w = w2.x() - w1.x()
                dy_w = w2.y() - w1.y()
                len_w = math.hypot(dx_w, dy_w)
                if len_w > 4.0:
                    nx, ny = -dy_w / len_w, dx_w / len_w
                    gaps = max(1, m.pin_count - 1) if not is_calib else 1
                    for step_i in range(gaps + 1):
                        t = step_i / float(gaps)
                        px = w1.x() + dx_w * t
                        py = w1.y() + dy_w * t
                        tick_h = 7.0 if step_i in (0, gaps) else 5.0
                        p.drawLine(
                            QPointF(px - nx * tick_h, py - ny * tick_h),
                            QPointF(px + nx * tick_h, py + ny * tick_h),
                        )
                anchor = QPoint(round((w1.x() + w2.x()) / 2.0), round((w1.y() + w2.y()) / 2.0 - 8))

            # Draw draggable endpoint handles
            p.setPen(QPen(color, 2))
            for ep_idx, ep_pt in enumerate(m.endpoints()):
                w_ep = self.frame_to_widget_f(ep_pt)
                if w_ep is None:
                    continue
                is_hovered = (
                    self._hover_ep is not None
                    and self._hover_ep[0] is m
                    and self._hover_ep[1] == ep_idx
                ) or (
                    self._dragging_ep is not None
                    and self._dragging_ep[0] is m
                    and self._dragging_ep[1] == ep_idx
                )
                rad = 6.5 if is_hovered else 4.5
                if is_hovered:
                    p.setBrush(color)
                else:
                    p.setBrush(QColor(0, 0, 0, 180))
                p.drawEllipse(w_ep, rad, rad)
            p.setBrush(Qt.BrushStyle.NoBrush)

            lines = format_measurement_lines(
                m,
                um_pp,
                unit=self.unit_mode,
                show_details=self.show_details,
                is_calib=is_calib,
            )
            fm = p.fontMetrics()
            txt_w = max(160, max(fm.horizontalAdvance(line) for line in lines) + 22)
            box_h = 22 * len(lines) + 4
            bx = max(8, min(self.width() - txt_w - 8, anchor.x() - txt_w // 2))
            by = max(8, min(self.height() - box_h - 8, anchor.y() - box_h))
            rect = QRect(bx, by, txt_w, box_h)
            p.fillRect(rect, QColor(12, 12, 16, 195))
            p.setPen(QPen(color, 1))
            p.drawRect(rect)
            for li, line_txt in enumerate(lines):
                l_rect = QRect(bx + 6, by + 2 + li * 22, txt_w - 12, 22)
                p.setPen(color if li == 0 else QColor(225, 225, 230))
                p.drawText(l_rect, Qt.AlignmentFlag.AlignCenter, line_txt)

    def _draw_loupe(self, p: QPainter, dx: int, dy: int, dw: int, dh: int) -> None:
        """Draw a 4× precision magnifier inset around the cursor in frame space."""
        if self.cursor_frame is None or self.cursor_widget is None:
            return
        frame = self.current_frame()
        if frame is None:
            return
        H, W = frame.shape[:2]
        cx_f, cy_f = self.cursor_frame
        # Extract a small patch around (cx_f, cy_f) in t-space
        tx, ty = self._o_to_t(cx_f, cy_f, frame)
        # Half-radius in sensor pixels so the loupe shows ~32×32 sensor pixels
        r_px = max(10, min(36, int(round(28.0 / max(1.0, self.zoom * 0.5)))))
        ox, oy = int(round(cx_f)), int(round(cy_f))
        x0 = max(0, ox - r_px)
        y0 = max(0, oy - r_px)
        x1 = min(W, ox + r_px + 1)
        y1 = min(H, oy + r_px + 1)
        if x1 - x0 < 3 or y1 - y0 < 3:
            return
        patch = frame[y0:y1, x0:x1]
        if self.flip_h:
            patch = cv2.flip(patch, 1)
        if self.flip_v:
            patch = cv2.flip(patch, 0)
        if self.rot:
            patch = cv2.rotate(
                patch,
                (
                    cv2.ROTATE_90_CLOCKWISE,
                    cv2.ROTATE_180,
                    cv2.ROTATE_90_COUNTERCLOCKWISE,
                )[self.rot - 1],
            )
        patch = self.process_frame(patch)
        loupe_sz = 132
        zoomed = cv2.resize(patch, (loupe_sz, loupe_sz), interpolation=cv2.INTER_NEAREST)
        qimg = QImage(
            zoomed.data, loupe_sz, loupe_sz, loupe_sz * 3, QImage.Format.Format_BGR888
        ).copy()

        # Position offset from cursor, flipping if close to right/bottom edge
        cw_x, cw_y = self.cursor_widget.x(), self.cursor_widget.y()
        lx = cw_x + 26 if cw_x + 26 + loupe_sz < dx + dw - 8 else cw_x - 26 - loupe_sz
        ly = cw_y - 26 - loupe_sz if cw_y - 26 - loupe_sz > dy + 8 else cw_y + 26
        lrect = QRectF(lx, ly, loupe_sz, loupe_sz)

        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(lrect, 10, 10)
        p.setClipPath(clip)
        p.drawImage(QPoint(round(lx), round(ly)), qimg)
        # Inner precision crosshair
        mid_x, mid_y = lx + loupe_sz / 2.0, ly + loupe_sz / 2.0
        p.setPen(QPen(QColor(0, 0, 0, 180), 3))
        p.drawLine(QPointF(lx, mid_y), QPointF(lx + loupe_sz, mid_y))
        p.drawLine(QPointF(mid_x, ly), QPointF(mid_x, ly + loupe_sz))
        col = QColor("#4dd2ff") if self.calibrating else QColor("#ffd24d")
        p.setPen(QPen(col, 1.2))
        p.drawLine(QPointF(lx, mid_y), QPointF(lx + loupe_sz, mid_y))
        p.drawLine(QPointF(mid_x, ly), QPointF(mid_x, ly + loupe_sz))
        p.restore()

        p.setPen(QPen(col, 2))
        p.drawRoundedRect(lrect, 10, 10)

    def _draw_hud(self, p: QPainter) -> None:
        lines = [
            f"{self.cam.measured_fps:5.1f} fps   {self.cam.width}×{self.cam.height}   zoom {self.zoom:.2f}×",
        ]
        cal = self.calib.active
        um_pp = self.calib.um_per_px(self.cam.width)
        if cal and um_pp:
            lines.append(f"cal: {cal} ({um_pp:.2f} µm/px)")
        else:
            lines.append("cal: none — press C to calibrate")
        if self.has_sw_adjustments():
            tags = []
            if self.sw_clahe:
                tags.append("CLAHE")
            if self.sw_grayscale:
                tags.append("Mono")
            if abs(self.sw_gamma - 1.0) > 1e-3:
                tags.append(f"γ={self.sw_gamma:.2f}")
            if self.sw_brightness or self.sw_contrast:
                tags.append(f"B/C={self.sw_brightness:+d}/{self.sw_contrast:+d}")
            if self.sw_sharpness:
                tags.append(f"Sharp={self.sw_sharpness}")
            if tags:
                lines.append("img: " + " · ".join(tags))
        if self.measure_mode:
            mode_names = {
                "line": f"LINE (pins={self.pin_count})" if self.pin_count > 1 else "LINE",
                "polyline": f"MULTI-POINT RULER ({len(self.pending_pts)} pts · Right-click/Enter to finish)",
                "circle": "CIRCLE (2-pt diameter)",
                "circle3": f"CIRCLE (3-pt rim: {len(self.pending_pts)}/3)",
                "rect": f"TILTED RECTANGLE ({len(self.pending_pts)}/3: edge → opposite side)",
            }
            mname = mode_names.get(self.measure_kind, "LINE")
            lines.append(
                f"MEASURE [{mname}] — Shift snaps 0°/45°/90° · Drag dots to adjust · Esc clears"
            )
        if self.calibrating:
            lines.append("CALIBRATE — click both ends of a known length (Shift snaps axis)")
        if self.extra_hint:
            lines.append(self.extra_hint)
        p.setPen(QColor(0, 0, 0, 175))
        for i, txt in enumerate(lines):
            p.drawText(13, 25 + i * 20 + 1, txt)
        p.setPen(QColor(235, 235, 235))
        for i, txt in enumerate(lines):
            p.drawText(12, 24 + i * 20, txt)

    def _draw_help(self, p: QPainter) -> None:
        p.fillRect(self.rect(), QColor(0, 0, 0, 205))
        p.setPen(QColor("#ffd24d"))
        p.drawText(60, 52, "Keyboard & Mouse Shortcuts (press H or Esc to close)")
        p.setPen(QColor("#ffffff"))
        for i, line in enumerate(HELP_TEXT):
            p.drawText(60, 84 + i * 24, line)

    # ------------------------------------------------------------------ input
    def _find_endpoint_near(self, wpos: QPointF) -> tuple[Measurement, int] | None:
        """Check if widget point wpos is within ENDPOINT_HIT_RADIUS_PX of any measurement endpoint."""
        best: tuple[Measurement, int] | None = None
        best_d = ENDPOINT_HIT_RADIUS_PX
        for m in reversed(self.measurements):
            for idx, pt in enumerate(m.endpoints()):
                w_ep = self.frame_to_widget_f(pt)
                if w_ep is None:
                    continue
                d = math.hypot(wpos.x() - w_ep.x(), wpos.y() - w_ep.y())
                if d <= best_d:
                    best_d = d
                    best = (m, idx)
        return best

    def _effective_frame_pt(self, raw_pt: tuple[float, float], ev: QMouseEvent) -> tuple[float, float]:
        """Apply Shift axis-snapping when placing the second point of a line or calibration."""
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if not shift:
            return raw_pt
        if self._dragging_ep is not None:
            m, idx = self._dragging_ep
            if m.kind == "line":
                anchor = m.p2 if idx == 0 else m.p1
                return snap_point(anchor, raw_pt)
            if m.kind == "polyline" and m.points and idx > 0:
                return snap_point(m.points[idx - 1], raw_pt)
        if self.pending_pts:
            if self.calibrating or self.measure_kind == "line":
                return snap_point(self.pending_pts[0], raw_pt)
            if self.measure_kind == "rect" and len(self.pending_pts) == 1:
                return snap_point(self.pending_pts[0], raw_pt)
            if self.measure_kind == "polyline":
                return snap_point(self.pending_pts[-1], raw_pt)
        return raw_pt

    def _is_near_polyline_start(self, pos: QPointF | QPoint | None = None) -> bool:
        """True if cursor is close enough to pending_pts[0] to auto-close a polyline."""
        if self.measure_kind != "polyline" or len(self.pending_pts) < 3:
            return False
        wpos = pos if pos is not None else self.cursor_widget
        if wpos is None:
            return False
        w0 = self.frame_to_widget_f(self.pending_pts[0])
        if w0 is None:
            return False
        return math.hypot(w0.x() - wpos.x(), w0.y() - wpos.y()) <= 14.0

    def finish_polyline(self, closed: bool = False) -> None:
        """Commit an in-progress multi-point ruler if at least 2 points are placed."""
        if self.measure_kind != "polyline" or len(self.pending_pts) < 2:
            return
        frame = self.current_frame()
        fsize = (
            (frame.shape[1], frame.shape[0])
            if frame is not None
            else (self.cam.width, self.cam.height)
        )
        pts = list(self.pending_pts)
        m = Measurement(
            pts[0],
            pts[-1],
            kind="polyline",
            points=pts,
            closed=closed and len(pts) >= 3,
            frame_size=fsize,
        )
        if not self.keep_measurements:
            self.measurements.clear()
        self.measurements.append(m)
        self.pending_pts.clear()
        self.update()

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:
        if (
            ev.button() == Qt.MouseButton.LeftButton
            and self.measure_mode
            and self.measure_kind == "polyline"
            and len(self.pending_pts) >= 2
        ):
            # Remove duplicate vertex added by the second click of double-click if very close
            if (
                len(self.pending_pts) >= 3
                and math.hypot(
                    self.pending_pts[-1][0] - self.pending_pts[-2][0],
                    self.pending_pts[-1][1] - self.pending_pts[-2][1],
                )
                < 4.0
            ):
                self.pending_pts.pop()
            self.finish_polyline()
            return
        super().mouseDoubleClickEvent(ev)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        left = ev.button() == Qt.MouseButton.LeftButton
        right = ev.button() == Qt.MouseButton.RightButton

        if right:
            if self.pending_pts:
                if self.measure_kind == "polyline" and len(self.pending_pts) >= 2:
                    self.finish_polyline()
                else:
                    self.pending_pts.clear()
                    self.update()
                return
            self._show_context_menu(ev.globalPosition().toPoint())
            return

        if left:
            # Check if grabbing an existing measurement endpoint to fine-tune it
            if not self.pending_pts and not self.calibrating:
                hit = self._find_endpoint_near(ev.position())
                if hit is not None:
                    self._dragging_ep = hit
                    self.update()
                    return

        if left and (self.measure_mode or self.calibrating):
            raw_pt = self.widget_to_frame(ev.position())
            if raw_pt is None:
                return
            pt = self._effective_frame_pt(raw_pt, ev)
            frame = self.current_frame()
            fsize = (
                (frame.shape[1], frame.shape[0])
                if frame is not None
                else (self.cam.width, self.cam.height)
            )
            if self.calibrating:
                if not self.pending_pts:
                    self.pending_pts.append(pt)
                else:
                    self.calib_line = Measurement(
                        self.pending_pts[0], pt, kind="line", frame_size=fsize
                    )
                    self.calibrating = False
                    self.pending_pts.clear()
                    self.calibration_captured.emit(self.calib_line.px_len)
            elif self.measure_kind == "circle3":
                self.pending_pts.append(pt)
                if len(self.pending_pts) >= 3:
                    m = Measurement(
                        self.pending_pts[0],
                        self.pending_pts[1],
                        p3=self.pending_pts[2],
                        kind="circle",
                        frame_size=fsize,
                    )
                    if not self.keep_measurements:
                        self.measurements.clear()
                    self.measurements.append(m)
                    self.pending_pts.clear()
            elif self.measure_kind == "rect":
                self.pending_pts.append(pt)
                if len(self.pending_pts) >= 3:
                    m = Measurement(
                        self.pending_pts[0],
                        self.pending_pts[1],
                        p3=self.pending_pts[2],
                        kind="rect",
                        frame_size=fsize,
                    )
                    if not self.keep_measurements:
                        self.measurements.clear()
                    self.measurements.append(m)
                    self.pending_pts.clear()
            elif self.measure_kind == "polyline":
                if self._is_near_polyline_start(ev.position()):
                    self.finish_polyline(closed=True)
                    return
                self.pending_pts.append(pt)
            else:
                if not self.pending_pts:
                    self.pending_pts.append(pt)
                else:
                    kind = "circle" if self.measure_kind == "circle" else self.measure_kind
                    m = Measurement(
                        self.pending_pts[0],
                        pt,
                        kind=kind,
                        pin_count=self.pin_count,
                        frame_size=fsize,
                    )
                    if not self.keep_measurements:
                        self.measurements.clear()
                    self.measurements.append(m)
                    self.pending_pts.clear()
            self.update()
        elif left:
            if self.center_t is None:
                self._refresh()
            if self.center_t is not None:
                self._pan_start = (
                    ev.position().x(),
                    ev.position().y(),
                    *self.center_t,
                )
        elif ev.button() == Qt.MouseButton.MiddleButton:
            self.reset_view()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        self.cursor_widget = ev.position().toPoint()
        raw_pt = self.widget_to_frame(ev.position())
        self.cursor_frame = (
            self._effective_frame_pt(raw_pt, ev) if raw_pt is not None else None
        )

        if self._dragging_ep is not None:
            if self.cursor_frame is not None:
                m, idx = self._dragging_ep
                m.set_endpoint(idx, self.cursor_frame)
                self.update()
            return

        prev_hover = self._hover_ep
        if not self.pending_pts and not self.calibrating:
            self._hover_ep = self._find_endpoint_near(ev.position())
        else:
            self._hover_ep = None
        if self._hover_ep != prev_hover:
            self.set_measure_cursor()

        pan = self._pan_start
        if pan is not None and self._view is not None:
            px, py, cx, cy = pan
            scale_x, scale_y = self._view[6], self._view[7]
            self.center_t = (
                cx - (ev.position().x() - px) / scale_x,
                cy - (ev.position().y() - py) / scale_y,
            )
            self._refresh()
        elif self.pending_pts or self.show_loupe or self._hover_ep != prev_hover:
            self.update()

    def mouseReleaseEvent(self, _ev: QMouseEvent) -> None:
        self._pan_start = None
        if self._dragging_ep is not None:
            self._dragging_ep = None
            self.update()

    def mouseDoubleClickEvent(self, _ev: QMouseEvent) -> None:
        if not self.measure_mode and not self.calibrating:
            self.reset_view()

    def wheelEvent(self, ev) -> None:
        if self._view is None:
            return
        old = self.zoom
        self.zoom = max(
            ZOOM_MIN,
            min(
                ZOOM_MAX,
                self.zoom * (1.15 if ev.angleDelta().y() > 0 else 1 / 1.15),
            ),
        )
        pt = self.widget_to_frame(ev.position())
        if pt is not None and self.center_t is not None and self.zoom > ZOOM_MIN:
            frame = self.current_frame()
            if frame is not None:
                tx, ty = self._o_to_t(*pt, frame)
                cx, cy = self.center_t
                ratio = old / self.zoom
                self.center_t = (tx + (cx - tx) * ratio, ty + (cy - ty) * ratio)
        self._refresh()

    def leaveEvent(self, _ev) -> None:
        self.cursor_frame = None
        self.cursor_widget = None
        self._hover_ep = None
        self.update()

    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Measure: Line / Pitch (M)", lambda: self.context_action.emit("measure_line"))
        menu.addAction("Measure: Multi-Point Ruler (N)", lambda: self.context_action.emit("measure_polyline"))
        menu.addAction("Measure: Circle 2-pt Diameter (D)", lambda: self.context_action.emit("measure_circle"))
        menu.addAction("Measure: Circle 3-pt Rim", lambda: self.context_action.emit("measure_circle3"))
        menu.addAction("Measure: Tilted Rectangle W×H (B)", lambda: self.context_action.emit("measure_rect"))
        menu.addAction("Calibrate Scale (C)", lambda: self.context_action.emit("calibrate"))
        menu.addSeparator()
        if self.measurements:
            menu.addAction("Undo Last Measurement (Backspace)", self.pop_measurement)
            menu.addAction("Clear All Measurements (Esc)", self.clear_measurement)
            menu.addSeparator()
        menu.addAction(
            "Unfreeze Frame (Space)" if self.is_frozen else "Freeze Frame (Space)",
            self.toggle_freeze,
        )
        menu.addAction("Take Snapshot (S)", lambda: self.context_action.emit("snapshot"))
        menu.addAction("Toggle IC Marking Enhancer / CLAHE (E)", lambda: self.context_action.emit("toggle_clahe"))
        menu.addAction("Reset Zoom & View", lambda: self.context_action.emit("reset_view"))
        menu.exec(global_pos)

    # --------------------------------------------------------------- helpers
    def reset_view(self) -> None:
        self.zoom = 1.0
        self.center_t = None
        self._refresh()

    def pop_measurement(self) -> None:
        if self.pending_pts:
            self.pending_pts.clear()
        elif self.measurements:
            self.measurements.pop()
        self.update()

    def clear_measurement(self) -> None:
        self.measurements.clear()
        self.calib_line = None
        self.pending_pts.clear()
        self.update()

    def set_measure_cursor(self) -> None:
        if self._hover_ep is not None or self._dragging_ep is not None:
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        elif self.measure_mode or self.calibrating:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.unsetCursor()
