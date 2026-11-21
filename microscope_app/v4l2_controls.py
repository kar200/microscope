"""Thin, throttled wrapper around v4l2-ctl for camera controls.

All control reads/writes go through one subprocess call at a time; slider
storms are coalesced by a pending-set dict flushed on a timer so the device
is never hammered faster than ~7 writes/second (focus moves need ~100 ms
to settle anyway).
"""
from __future__ import annotations

import re
import subprocess

from PySide6.QtCore import QObject, QTimer, Signal

_CTRL_RE = re.compile(
    r"^\s*(?P<name>\w+)\s+0x[0-9a-f]+\s+\((?P<type>int|bool|menu)\)\s*:"
    r"\s*(?P<rest>.*)$"
)
_RANGE_RE = re.compile(
    r"min=(?P<min>-?\d+)\s+max=(?P<max>-?\d+)(?:\s+step=(?P<step>\d+))?"
    r"\s+default=(?P<default>-?\d+)\s+value=(?P<value>-?\d+)"
)
_BOOL_RE = re.compile(r"default=(?P<default>-?\d+)\s+value=(?P<value>-?\d+)")


_MENU_ITEM_RE = re.compile(r"^\s+(?P<idx>-?\d+):\s*(?P<label>.+?)\s*$")


class V4L2Controls(QObject):
    """Read/parse/set V4L2 controls for one device."""

    controls_changed = Signal(dict)  # name -> value, after a flush

    def __init__(self, device: str, parent: QObject | None = None):
        super().__init__(parent)
        self.device = device
        self.pending: dict[str, int] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._flush)
        self.descriptors: dict[str, dict] = {}
        self.refresh_descriptors()

    # ---- subprocess helpers -------------------------------------------------
    def _run(self, *args: str) -> str:
        proc = subprocess.run(
            ["v4l2-ctl", "-d", self.device, *args],
            capture_output=True, text=True, timeout=2.0,
        )
        return proc.stdout

    # ---- discovery ----------------------------------------------------------
    def refresh_descriptors(self) -> None:
        """Parse `v4l2-ctl --list-ctrls-menus` into {name: {min,max,step,default,value,type,menu?}}."""
        self.descriptors = {}
        try:
            out = self._run("--list-ctrls-menus")
        except (subprocess.SubprocessError, FileNotFoundError):
            return
        last_name: str | None = None
        for line in out.splitlines():
            m = _CTRL_RE.match(line)
            if m:
                name, ctype, rest = m.group("name"), m.group("type"), m.group("rest")
                desc: dict = {"type": ctype}
                rm = _RANGE_RE.search(rest)
                if rm:
                    step_str = rm.group("step")
                    desc.update(
                        min=int(rm.group("min")), max=int(rm.group("max")),
                        step=int(step_str) if step_str is not None else 1,
                        default=int(rm.group("default")),
                        value=int(rm.group("value")),
                    )
                else:
                    bm = _BOOL_RE.search(rest)
                    if bm:
                        desc.update(
                            min=0, max=1, step=1,
                            default=int(bm.group("default")),
                            value=int(bm.group("value")),
                        )
                if "value" in desc:
                    self.descriptors[name] = desc
                    last_name = name
                else:
                    last_name = None
                continue
            if last_name and self.descriptors[last_name].get("type") == "menu":
                mm = _MENU_ITEM_RE.match(line)
                if mm:
                    menu = self.descriptors[last_name].setdefault("menu", {})
                    menu[int(mm.group("idx"))] = mm.group("label")

    def auto_exposure_values(self) -> tuple[int, int]:
        """Return (auto_value, manual_value) supported by this camera's auto_exposure menu."""
        d = self.descriptors.get("auto_exposure", {})
        menu: dict[int, str] = d.get("menu", {})
        if menu:
            manual_val = next(
                (idx for idx, lbl in menu.items() if "manual" in lbl.lower()),
                1 if 1 in menu else min(menu),
            )
            auto_candidates = [
                (idx, lbl.lower()) for idx, lbl in menu.items() if idx != manual_val
            ]
            if auto_candidates:
                # Prefer Aperture Priority / Auto Mode, or the control's default
                default_val = d.get("default")
                if default_val in menu and default_val != manual_val:
                    return default_val, manual_val
                for pref in ("aperture", "auto", "shutter"):
                    for idx, lbl in auto_candidates:
                        if pref in lbl:
                            return idx, manual_val
                return auto_candidates[0][0], manual_val
            return manual_val, manual_val
        # Fallback when menu labels aren't available
        manual_val = 1
        default_val = d.get("default")
        if default_val is not None and default_val != manual_val:
            return default_val, manual_val
        hi = d.get("max", 3)
        return (0 if hi == 1 else 3), manual_val

    def supports_auto_exposure(self) -> bool:
        """True only if the camera advertises a selectable non-manual exposure mode."""
        if "auto_exposure" not in self.descriptors:
            return False
        auto_val, manual_val = self.auto_exposure_values()
        return auto_val != manual_val

    # ---- reads ---------------------------------------------------------------
    def get(self, name: str) -> int | None:
        d = self.descriptors.get(name)
        return d["value"] if d else None

    def get_all(self) -> dict[str, int]:
        return {k: d["value"] for k, d in self.descriptors.items()}

    def read_back(self) -> None:
        """Re-read values from the device into descriptors."""
        self.refresh_descriptors()
        self.controls_changed.emit(self.get_all())

    # ---- writes (throttled) ---------------------------------------------------
    def set(self, name: str, value: int) -> None:
        d = self.descriptors.get(name)
        if d:
            value = max(d["min"], min(d["max"], int(value)))
        self.pending[name] = value
        if not self._timer.isActive():
            self._timer.start()

    def set_now(self, name: str, value: int) -> None:
        """Immediate write, bypassing the throttle (used for one-shot toggles)."""
        try:
            self._run(f"--set-ctrl={name}={int(value)}")
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    def _flush(self) -> None:
        if not self.pending:
            self._timer.stop()
            return
        pending, self.pending = self.pending, {}
        # One control per v4l2-ctl call: combining several controls in a
        # single comma-joined ext-ctrls transaction can wedge some UVC
        # firmwares (observed on the HH-4K MAX auto_exposure state machine).
        for name, value in pending.items():
            try:
                self._run(f"--set-ctrl={name}={value}")
            except (subprocess.SubprocessError, FileNotFoundError):
                self._timer.stop()
                return
        self.read_back()  # keep UI truthful
        if not self.pending:
            self._timer.stop()
