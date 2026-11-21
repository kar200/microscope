"""Measurement geometry, calibration storage, unit formatting, and PCB/SMD hints."""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field

# Standard imperial/metric SMD passive & package dimensions (mm)
SMD_SIZES_MM: list[tuple[str, float]] = [
    ("01005", 0.40),
    ("0201", 0.60),
    ("0402", 1.00),
    ("0603", 1.60),
    ("0805", 2.00),
    ("1206/1210", 3.20),
    ("1812", 4.50),
    ("2010", 5.00),
    ("2512", 6.30),
    ("SOT-23", 2.90),
    ("SOIC-8 body", 3.90),
    ("SOIC-8 span", 6.00),
]

# Standard PCB pin pitches (mm)
PIN_PITCHES_MM: list[tuple[str, float]] = [
    ("0.40mm pitch", 0.40),
    ("0.50mm pitch", 0.50),
    ("0.65mm pitch (SSOP/TSSOP)", 0.65),
    ("0.80mm pitch (QFP)", 0.80),
    ("1.00mm pitch", 1.00),
    ("1.27mm / 50mil pitch (SOIC)", 1.27),
    ("2.00mm pitch", 2.00),
    ("2.54mm / 100mil pitch", 2.54),
]

# 2D SMD footprint body sizes: (name, length_mm, width_mm)
SMD_FOOTPRINTS_MM: list[tuple[str, float, float]] = [
    ("0201", 0.60, 0.30),
    ("0402", 1.00, 0.50),
    ("0603", 1.60, 0.80),
    ("0805", 2.00, 1.25),
    ("1206", 3.20, 1.60),
    ("1210", 3.20, 2.50),
    ("1812", 4.50, 3.20),
    ("2010", 5.00, 2.50),
    ("2512", 6.30, 3.20),
    ("SOT-23 body", 2.90, 1.30),
    ("SOIC-8 body", 4.90, 3.90),
]

_HINT_TOL = 0.12       # ±12% for general component sizes
_PITCH_TOL = 0.05      # ±5% for pin pitch matching
UM_PER_MIL = 25.4      # 1 mil (0.001 inch) = 25.4 µm


def snap_point(
    p1: tuple[float, float],
    p2: tuple[float, float],
    step_deg: float = 45.0,
) -> tuple[float, float]:
    """Snap p2 relative to p1 to the nearest multiple of step_deg (0°, 45°, 90°, ...)."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return p2
    angle = math.atan2(dy, dx)
    step_rad = math.radians(step_deg)
    snapped = round(angle / step_rad) * step_rad
    # For clean exact horizontal/vertical coordinates:
    deg = round(math.degrees(snapped)) % 360
    if deg in (0, 180):
        return (p1[0] + math.copysign(dist, dx), p1[1])
    if deg in (90, 270):
        return (p1[0], p1[1] + math.copysign(dist, dy))
    return (p1[0] + dist * math.cos(snapped), p1[1] + dist * math.sin(snapped))


def circle_from_3_points(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> tuple[tuple[float, float], float] | None:
    """Return ((cx, cy), radius) through 3 points, or None if collinear."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-6:
        return None
    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / d
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / d
    r = math.hypot(x1 - ux, y1 - uy)
    return (ux, uy), r


@dataclass
class Measurement:
    """A geometric measurement stored in frame pixel coordinates, with reference
    frame dimensions so it rescales cleanly when capture resolution changes.

    Supported kinds:
      * "line"     — point-to-point distance (with optional pin-count pitch division)
      * "circle"   — 2-point diameter or 3-point rim circle (diameter, radius, area)
      * "rect"     — 3-point tilted rectangle (or 2-point axis-aligned when p3 is None)
      * "polyline" — multi-point chained ruler (per-segment lengths + total length)
    """

    p1: tuple[float, float]
    p2: tuple[float, float]
    kind: str = "line"
    p3: tuple[float, float] | None = None
    points: list[tuple[float, float]] = field(default_factory=list)
    closed: bool = False
    pin_count: int = 1  # >1 divides line length by (pin_count - 1) gaps
    frame_size: tuple[int, int] = field(default=(1920, 1080))  # (W, H)

    @property
    def dx(self) -> float:
        return abs(self.p2[0] - self.p1[0])

    @property
    def dy(self) -> float:
        return abs(self.p2[1] - self.p1[1])

    def rect_geometry(self) -> tuple[list[tuple[float, float]], float, float, float]:
        """Return (4_corners, width_px, height_px, angle_deg) for rect mode.

        If p3 is None, falls back to an axis-aligned rectangle from p1 to p2.
        If p3 is provided, p1->p2 defines edge 1 (width & tilt angle) and p3's
        perpendicular distance from line p1->p2 defines edge 2 (height).
        """
        if self.p3 is None:
            x1, y1 = self.p1
            x2, y2 = self.p2
            corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            return corners, abs(x2 - x1), abs(y2 - y1), 0.0
        ux = self.p2[0] - self.p1[0]
        uy = self.p2[1] - self.p1[1]
        w = math.hypot(ux, uy)
        if w < 1e-6:
            return [self.p1, self.p2, self.p3, self.p1], 0.0, 0.0, 0.0
        ex, ey = ux / w, uy / w
        nx, ny = -ey, ex
        h_signed = (self.p3[0] - self.p1[0]) * nx + (self.p3[1] - self.p1[1]) * ny
        ox, oy = nx * h_signed, ny * h_signed
        c0 = self.p1
        c1 = self.p2
        c2 = (self.p2[0] + ox, self.p2[1] + oy)
        c3 = (self.p1[0] + ox, self.p1[1] + oy)
        angle = math.degrees(math.atan2(uy, ux))
        return [c0, c1, c2, c3], w, abs(h_signed), angle

    def polyline_segments(
        self,
    ) -> list[tuple[tuple[float, float], tuple[float, float], float]]:
        """Return [(pt_a, pt_b, length_px), ...] for polyline mode."""
        pts = self.points if len(self.points) >= 2 else [self.p1, self.p2]
        segs: list[tuple[tuple[float, float], tuple[float, float], float]] = []
        for a, b in zip(pts, pts[1:]):
            segs.append((a, b, math.hypot(b[0] - a[0], b[1] - a[1])))
        if self.closed and len(pts) >= 3:
            a, b = pts[-1], pts[0]
            segs.append((a, b, math.hypot(b[0] - a[0], b[1] - a[1])))
        return segs

    def polygon_area_px2(self) -> float:
        """Enclosed area in px² (shoelace formula) for a closed polyline."""
        if not self.closed or len(self.points) < 3:
            return 0.0
        pts = self.points
        acc = 0.0
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            acc += x1 * y2 - x2 * y1
        return abs(acc) * 0.5

    @property
    def px_len(self) -> float:
        if self.kind == "circle":
            c = self.circle_geometry()
            return c[1] * 2.0 if c else 0.0
        if self.kind == "rect":
            _, w, h, _ = self.rect_geometry()
            return math.hypot(w, h)
        if self.kind == "polyline":
            return sum(seg[2] for seg in self.polyline_segments())
        return math.hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1])

    @property
    def angle_deg(self) -> float:
        """Angle in degrees [-180, 180], where 0° is right and +90° is down."""
        return math.degrees(math.atan2(self.p2[1] - self.p1[1], self.p2[0] - self.p1[0]))

    def circle_geometry(self) -> tuple[tuple[float, float], float] | None:
        """Return ((cx, cy), radius_px) for circle mode."""
        if self.p3 is not None:
            res = circle_from_3_points(self.p1, self.p2, self.p3)
            if res is not None:
                return res
        # 2-point diameter circle: p1 and p2 are opposite ends of a diameter
        cx = (self.p1[0] + self.p2[0]) * 0.5
        cy = (self.p1[1] + self.p2[1]) * 0.5
        r = math.hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1]) * 0.5
        return (cx, cy), r

    def rescale_to(self, new_w: int, new_h: int) -> None:
        """Rescale coordinates in-place when camera capture resolution changes."""
        old_w, old_h = self.frame_size
        if (old_w, old_h) == (new_w, new_h) or old_w <= 0 or old_h <= 0:
            self.frame_size = (new_w, new_h)
            return
        sx, sy = new_w / float(old_w), new_h / float(old_h)
        self.p1 = (self.p1[0] * sx, self.p1[1] * sy)
        self.p2 = (self.p2[0] * sx, self.p2[1] * sy)
        if self.p3 is not None:
            self.p3 = (self.p3[0] * sx, self.p3[1] * sy)
        if self.points:
            self.points = [(x * sx, y * sy) for x, y in self.points]
        self.frame_size = (new_w, new_h)

    def endpoints(self) -> list[tuple[float, float]]:
        if self.kind == "polyline" and self.points:
            return list(self.points)
        pts = [self.p1, self.p2]
        if self.p3 is not None:
            pts.append(self.p3)
        return pts

    def set_endpoint(self, index: int, pt: tuple[float, float]) -> None:
        if self.kind == "polyline" and self.points:
            if 0 <= index < len(self.points):
                self.points[index] = pt
                self.p1 = self.points[0]
                self.p2 = self.points[-1]
            return
        if index == 0:
            self.p1 = pt
        elif index == 1:
            self.p2 = pt
        elif index == 2:
            self.p3 = pt


def format_scalar(px_val: float, um_per_px: float | None, unit: str = "auto") -> str:
    """Format a single scalar length in the chosen unit system:
    'auto' (mm/µm), 'mm', 'um', 'mil', or 'both' (mm + mil).
    """
    if not um_per_px:
        return f"{px_val:.1f} px"
    um = px_val * um_per_px
    mm = um / 1000.0
    mils = um / UM_PER_MIL
    if unit == "mm":
        return f"{mm:.3f} mm"
    if unit == "um":
        return f"{um:.1f} µm"
    if unit == "mil":
        return f"{mils:.1f} mil"
    if unit == "both":
        primary = f"{um:.0f} µm" if um < 1000.0 else f"{mm:.3f} mm"
        return f"{primary} ({mils:.1f} mil)"
    # "auto"
    if um < 1000.0:
        return f"{um:.1f} µm" if um < 100.0 else f"{um:.0f} µm"
    return f"{mm:.3f} mm"


def format_length(
    px_len: float,
    um_per_px: float | None,
    unit: str = "auto",
    include_px: bool = True,
) -> str:
    """Backwards-compatible length formatter."""
    if not um_per_px:
        return f"{px_len:.1f} px"
    s = format_scalar(px_len, um_per_px, unit=unit)
    if include_px:
        return f"{s}  ({px_len:.0f} px)"
    return s


def smd_hint(px_len: float, um_per_px: float | None, is_pitch: bool = False) -> str:
    """Return the closest matching SMD package size or pin pitch within tolerance."""
    if not um_per_px or px_len <= 0:
        return ""
    mm = px_len * um_per_px / 1000.0
    if is_pitch:
        best_name, best_ref = min(PIN_PITCHES_MM, key=lambda kv: abs(mm - kv[1]))
        if abs(mm - best_ref) / best_ref <= _PITCH_TOL:
            return f" ≈ {best_name}"
        return ""

    # Check closest SMD component dimension first
    best_name, best_ref = min(SMD_SIZES_MM, key=lambda kv: abs(mm - kv[1]))
    err_smd = abs(mm - best_ref) / best_ref

    # Also check pin pitches if very close (±3.5%)
    pitch_name, pitch_ref = min(PIN_PITCHES_MM, key=lambda kv: abs(mm - kv[1]))
    err_pitch = abs(mm - pitch_ref) / pitch_ref

    if err_smd <= _HINT_TOL and err_smd <= err_pitch:
        return f" ≈ {best_name}"
    if err_pitch <= 0.035:
        return f" ≈ {pitch_name}"
    return ""


def footprint_hint(dx_px: float, dy_px: float, um_per_px: float | None) -> str:
    """Match a 2D rectangle (W × H) against standard SMD component footprints."""
    if not um_per_px or dx_px <= 0 or dy_px <= 0:
        return ""
    L = max(dx_px, dy_px) * um_per_px / 1000.0
    W = min(dx_px, dy_px) * um_per_px / 1000.0
    best_name = ""
    best_err = 1.0
    for name, ref_l, ref_w in SMD_FOOTPRINTS_MM:
        err = max(abs(L - ref_l) / ref_l, abs(W - ref_w) / ref_w)
        if err < best_err:
            best_err = err
            best_name = name
    if best_err <= 0.12:
        return f" ≈ {best_name}"
    return ""


def format_measurement_lines(
    m: Measurement,
    um_per_px: float | None,
    unit: str = "auto",
    show_details: bool = False,
    is_calib: bool = False,
) -> list[str]:
    """Build 1 or 2 concise label lines for a Measurement overlay."""
    if is_calib:
        return [f"Calibrate: {m.px_len:.1f} px  (ΔX {m.dx:.0f}, ΔY {m.dy:.0f})"]

    if m.kind == "circle":
        geom = m.circle_geometry()
        if not geom:
            return ["Invalid circle"]
        _, r_px = geom
        d_px = r_px * 2.0
        d_str = format_scalar(d_px, um_per_px, unit=unit)
        r_str = format_scalar(r_px, um_per_px, unit=unit)
        line1 = f"⌀ {d_str}  (r = {r_str})"
        if show_details and um_per_px:
            r_mm = r_px * um_per_px / 1000.0
            area_mm2 = math.pi * r_mm * r_mm
            return [line1, f"Area: {area_mm2:.3f} mm²  ({d_px:.0f} px)"]
        return [line1]

    if m.kind == "rect":
        _, w_px, h_px, angle = m.rect_geometry()
        w_str = format_scalar(w_px, um_per_px, unit=unit)
        h_str = format_scalar(h_px, um_per_px, unit=unit)
        hint = footprint_hint(w_px, h_px, um_per_px)
        line1 = f"{w_str} × {h_str}{hint}"
        if show_details:
            diag_str = format_scalar(math.hypot(w_px, h_px), um_per_px, unit=unit)
            ang_part = f" · ∠ {angle:+.1f}°" if abs(angle) > 0.1 else ""
            if um_per_px:
                area_mm2 = (w_px * um_per_px / 1000.0) * (h_px * um_per_px / 1000.0)
                return [line1, f"Area: {area_mm2:.3f} mm² · Diag: {diag_str}{ang_part}"]
            return [line1, f"Diag: {diag_str}{ang_part}"]
        return [line1]

    if m.kind == "polyline":
        segs = m.polyline_segments()
        total_px = sum(s[2] for s in segs)
        total_str = format_scalar(total_px, um_per_px, unit=unit)
        if m.closed:
            line1 = f"Perimeter: {total_str} ({len(segs)} sides)"
            if show_details and um_per_px:
                area_mm2 = m.polygon_area_px2() * (um_per_px / 1000.0) ** 2
                return [line1, f"Area: {area_mm2:.3f} mm²"]
            return [line1]
        line1 = f"Total: {total_str} ({len(segs)} segs)"
        if show_details and len(segs) >= 2:
            start = segs[0][0]
            end = segs[-1][1]
            span_px = math.hypot(end[0] - start[0], end[1] - start[1])
            span_str = format_scalar(span_px, um_per_px, unit=unit)
            return [line1, f"Direct span: {span_str}"]
        return [line1]

    # Default: "line"
    base_str = format_length(m.px_len, um_per_px, unit=unit, include_px=not show_details)
    if m.pin_count > 1:
        gaps = m.pin_count - 1
        pitch_px = m.px_len / gaps
        pitch_str = format_scalar(pitch_px, um_per_px, unit=unit)
        p_hint = smd_hint(pitch_px, um_per_px, is_pitch=True)
        line1 = f"Pitch ({m.pin_count} pins / {gaps} gaps): {pitch_str}{p_hint}"
        line2 = f"Total span: {format_scalar(m.px_len, um_per_px, unit=unit)} ({m.px_len:.0f} px)"
        return [line1, line2]

    line1 = base_str + smd_hint(m.px_len, um_per_px)
    if show_details:
        dx_s = format_scalar(m.dx, um_per_px, unit=unit)
        dy_s = format_scalar(m.dy, um_per_px, unit=unit)
        return [line1, f"ΔX: {dx_s} · ΔY: {dy_s} · ∠ {m.angle_deg:+.1f}° ({m.px_len:.1f} px)"]
    return [line1]


class CalibrationStore:
    """Named µm/px profiles persisted as JSON next to the app."""

    def __init__(self, path: str):
        self.path = path
        self.profiles: dict[str, dict] = {}
        self.active: str | None = None
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.profiles = json.load(fh)
            except (OSError, json.JSONDecodeError):
                self.profiles = {}
        if self.profiles and not (self.active in self.profiles):
            self.active = sorted(self.profiles)[-1]

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.profiles, fh, indent=2, sort_keys=True)

    def add(self, name: str, um_per_px: float, ref_width: int = 3840) -> None:
        self.profiles[name] = {
            "um_per_px": um_per_px,
            "ref_width": ref_width,
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.active = name
        self.save()

    def remove(self, name: str) -> None:
        self.profiles.pop(name, None)
        if self.active == name:
            self.active = sorted(self.profiles)[-1] if self.profiles else None
        self.save()

    def um_per_px(self, current_width: int | None = None) -> float | None:
        if self.active and self.active in self.profiles:
            prof = self.profiles[self.active]
            base = float(prof["um_per_px"])
            if current_width:
                ref = float(prof.get("ref_width", 3840))
                return base * (ref / float(current_width))
            return base
        return None
