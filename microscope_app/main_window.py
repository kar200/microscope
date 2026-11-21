"""Main window: viewer + control panel + menu bar + keyboard shortcuts."""
from __future__ import annotations

import math
import os
import time

import cv2
import numpy as np
from PySide6.QtCore import QProcess, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from .camera import CameraThread
from .inhibit import ScreenInhibitor
from .measure import CalibrationStore, UM_PER_MIL, format_measurement_lines, format_scalar
from .recorder import (
    BLOCKED_VAAPI_DEVICES,
    RECORD_MODES,
    SAVE_DIR,
    Recorder,
    RecordingConfig,
    save_snapshot,
)
from .scanner import BoardScannerDialog
from .v4l2_controls import V4L2Controls
from .viewer import Viewer

RESOLUTIONS = [
    ("3840×2160 @60", 3840, 2160, 60),
    ("1920×1080 @60", 1920, 1080, 60),
    ("1280×720 @60", 1280, 720, 60),
]

MEASURE_TOOLS = [
    ("Line / Pin Pitch (M)", "line"),
    ("Multi-Point Ruler (N)", "polyline"),
    ("Circle — 2-pt Diameter (D)", "circle"),
    ("Circle — 3-pt Rim", "circle3"),
    ("Rectangle — Tilted W × H (B)", "rect"),
]

UNIT_CHOICES = [
    ("Auto (mm / µm)", "auto"),
    ("mm + mil (PCB)", "both"),
    ("Millimeters (mm)", "mm"),
    ("Micrometers (µm)", "um"),
    ("Mils (1/1000 in)", "mil"),
]


class CalibrationDialog(QDialog):
    """Single-step dialog to enter known length, unit (mm/mil/µm), and profile name."""

    def __init__(self, px_len: float, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Calibrate Scale")
        self.setMinimumWidth(360)

        lay = QVBoxLayout(self)
        info = QLabel(f"Drawn reference line length: <b>{px_len:.1f} px</b>")
        lay.addWidget(info)

        form = QFormLayout()

        preset_combo = QComboBox()
        preset_combo.addItems([
            "Custom length",
            "1.00 mm (Steel ruler 1 mm)",
            "2.54 mm (100 mil pin header pitch)",
            "1.27 mm (50 mil SOIC pin pitch)",
            "3.90 mm (SOIC-8 body width)",
            "3.20 mm (1206 resistor length)",
            "2.00 mm (0805 resistor length)",
            "1.60 mm (0603 resistor length)",
        ])
        form.addRow("Preset:", preset_combo)

        len_row = QHBoxLayout()
        self.val_spin = QDoubleSpinBox()
        self.val_spin.setRange(0.0001, 100000.0)
        self.val_spin.setDecimals(4)
        self.val_spin.setValue(1.0)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["mm", "mil", "µm"])
        len_row.addWidget(self.val_spin, 1)
        len_row.addWidget(self.unit_combo)
        form.addRow("Real length:", len_row)

        self.name_edit = QLineEdit(time.strftime("cal %Y-%m-%d %H:%M"))
        form.addRow("Profile name:", self.name_edit)
        lay.addLayout(form)

        preset_values = {
            1: (1.00, "mm"),
            2: (2.54, "mm"),
            3: (1.27, "mm"),
            4: (3.90, "mm"),
            5: (3.20, "mm"),
            6: (2.00, "mm"),
            7: (1.60, "mm"),
        }

        def _on_preset(idx: int) -> None:
            if idx in preset_values:
                val, u = preset_values[idx]
                self.unit_combo.setCurrentText(u)
                self.val_spin.setValue(val)

        preset_combo.currentIndexChanged.connect(_on_preset)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def length_um(self) -> float:
        val = self.val_spin.value()
        unit = self.unit_combo.currentText()
        if unit == "mm":
            return val * 1000.0
        if unit == "mil":
            return val * UM_PER_MIL
        return val

    def profile_name(self) -> str:
        return self.name_edit.text().strip()


RECORDING_PRESETS: list[tuple[str, RecordingConfig | None]] = [
    ("Low / Compact (1080p @30, VBR 3M · ~22 MB/min)",
     RecordingConfig(codec="hevc", resolution="1080p", fps=30, rc_mode="VBR", bitrate_mbps=3)),
    ("Medium / Balanced (1080p @60, CQP 32 · ~45 MB/min)",
     RecordingConfig(codec="hevc", resolution="1080p", fps=60, rc_mode="CQP", qp=32)),
    ("High Quality (1080p @60, CQP 28 · ~90 MB/min)",
     RecordingConfig(codec="hevc", resolution="1080p", fps=60, rc_mode="CQP", qp=28)),
    ("Ultra / 4K (4K @60, CQP 26)",
     RecordingConfig(codec="hevc", resolution="source", fps=60, rc_mode="CQP", qp=26)),
    ("Custom", None),
]


class RecordingSettingsDialog(QDialog):
    """Dedicated dialog for configuring video recording & snapshot options."""

    def __init__(
        self,
        parent: QWidget,
        config: RecordingConfig,
        preferred_node: str,
        encoder_choices: list[tuple[str, str]],
        burn_snapshots: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recording & Capture Settings")
        self.setMinimumWidth(400)
        self._encoder_values = [v for _, v in encoder_choices]
        self._syncing = False

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Quick preset
        self.preset_combo = QComboBox()
        for label, _ in RECORDING_PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("Preset:", self.preset_combo)

        # Codec
        self.codec_combo = QComboBox()
        self.codec_combo.addItem("H.265 / HEVC (.mkv)", "hevc")
        self.codec_combo.addItem("H.264 / AVC (.mkv)", "h264")
        self.codec_combo.addItem("MJPEG (.mkv — large files)", "mjpeg")
        self._set_data(self.codec_combo, config.codec)
        self.codec_combo.currentIndexChanged.connect(self._on_manual_change)
        form.addRow("Codec:", self.codec_combo)

        # FFmpeg backend
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("VA-API (GPU)", "vaapi")
        self.backend_combo.addItem("Vulkan Video (GPU)", "vulkan")
        self.backend_combo.addItem("CPU (Software libx265/libx264)", "cpu")
        self._set_data(self.backend_combo, config.backend)
        self.backend_combo.currentIndexChanged.connect(self._on_manual_change)
        form.addRow("Backend:", self.backend_combo)

        # Resolution
        self.res_combo = QComboBox()
        self.res_combo.addItem("1280×720 (720p)", "720p")
        self.res_combo.addItem("1920×1080 (1080p)", "1080p")
        self.res_combo.addItem("Source / 4K (full-res)", "source")
        self._set_data(self.res_combo, config.resolution)
        self.res_combo.currentIndexChanged.connect(self._on_manual_change)
        form.addRow("Resolution:", self.res_combo)

        # FPS
        self.fps_combo = QComboBox()
        self.fps_combo.addItem("30 fps", 30)
        self.fps_combo.addItem("60 fps", 60)
        self._set_data(self.fps_combo, config.fps)
        self.fps_combo.currentIndexChanged.connect(self._on_manual_change)
        form.addRow("Framerate:", self.fps_combo)

        # Rate control
        self.rc_combo = QComboBox()
        self.rc_combo.addItem("CQP (Constant Quality)", "CQP")
        self.rc_combo.addItem("VBR (Target Bitrate)", "VBR")
        self.rc_combo.addItem("CBR (Constant Bitrate)", "CBR")
        self._set_data(self.rc_combo, config.rc_mode)
        self.rc_combo.currentIndexChanged.connect(self._on_manual_change)
        form.addRow("Rate control:", self.rc_combo)

        # QP
        self.qp_spin = QSpinBox()
        self.qp_spin.setRange(18, 45)
        self.qp_spin.setValue(config.qp)
        self.qp_spin.setToolTip("Lower = sharper & larger; higher = smaller files (28–36 typical)")
        self.qp_spin.valueChanged.connect(self._on_manual_change)
        form.addRow("CQP value (18–45):", self.qp_spin)

        # Bitrate
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(1, 40)
        self.bitrate_spin.setSuffix(" Mbps")
        self.bitrate_spin.setValue(config.bitrate_mbps)
        self.bitrate_spin.setToolTip("Target bitrate for VBR/CBR (3 Mbps ≈ 22 MB/min)")
        self.bitrate_spin.valueChanged.connect(self._on_manual_change)
        form.addRow("Target bitrate:", self.bitrate_spin)

        # Encoder device
        self.enc_combo = QComboBox()
        for label, val in encoder_choices:
            self.enc_combo.addItem(label, val)
        self._set_data(self.enc_combo, preferred_node)
        form.addRow("Encoder GPU:", self.enc_combo)

        # Snapshots
        self.burn_check = QCheckBox("Burn measurements into snapshots")
        self.burn_check.setChecked(burn_snapshots)
        form.addRow("", self.burn_check)

        layout.addLayout(form)

        note = QLabel("Settings are saved automatically in ~/.config/Bresun/Microscope.conf")
        note.setStyleSheet("color:#888; font-size:11px;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_enabled()
        self._sync_preset_combo()

    @staticmethod
    def _set_data(combo: QComboBox, value: object) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _update_enabled(self) -> None:
        is_mjpeg = self.codec_combo.currentData() == "mjpeg"
        backend = self.backend_combo.currentData()
        rc = self.rc_combo.currentData()
        self.backend_combo.setEnabled(not is_mjpeg)
        self.rc_combo.setEnabled(not is_mjpeg)
        self.qp_spin.setEnabled(not is_mjpeg and rc == "CQP")
        self.bitrate_spin.setEnabled(not is_mjpeg and rc in ("VBR", "CBR"))
        self.enc_combo.setEnabled(not is_mjpeg and backend == "vaapi")

    def _sync_preset_combo(self) -> None:
        self._syncing = True
        cur = self.recording_config()
        matched = len(RECORDING_PRESETS) - 1  # Custom
        for i, (_, p) in enumerate(RECORDING_PRESETS):
            if p is None:
                continue
            if (p.codec == cur.codec and p.backend == cur.backend
                    and p.resolution == cur.resolution
                    and p.fps == cur.fps and p.rc_mode == cur.rc_mode
                    and (p.rc_mode != "CQP" or p.qp == cur.qp)
                    and (p.rc_mode == "CQP" or p.bitrate_mbps == cur.bitrate_mbps)):
                matched = i
                break
        self.preset_combo.setCurrentIndex(matched)
        self._syncing = False

    def _on_preset_changed(self, idx: int) -> None:
        if self._syncing or idx < 0 or idx >= len(RECORDING_PRESETS):
            return
        _, preset = RECORDING_PRESETS[idx]
        if preset is None:
            return
        self._syncing = True
        self._set_data(self.codec_combo, preset.codec)
        self._set_data(self.backend_combo, preset.backend)
        self._set_data(self.res_combo, preset.resolution)
        self._set_data(self.fps_combo, preset.fps)
        self._set_data(self.rc_combo, preset.rc_mode)
        self.qp_spin.setValue(preset.qp)
        self.bitrate_spin.setValue(preset.bitrate_mbps)
        self._syncing = False
        self._update_enabled()

    def _on_manual_change(self) -> None:
        if self._syncing:
            return
        self._update_enabled()
        self._sync_preset_combo()

    def recording_config(self) -> RecordingConfig:
        return RecordingConfig(
            codec=str(self.codec_combo.currentData()),
            backend=str(self.backend_combo.currentData()),
            resolution=str(self.res_combo.currentData()),
            fps=int(self.fps_combo.currentData()),
            rc_mode=str(self.rc_combo.currentData()),
            qp=int(self.qp_spin.value()),
            bitrate_mbps=int(self.bitrate_spin.value()),
        )

    def preferred_node(self) -> str:
        return str(self.enc_combo.currentData() or "auto")

    def burn_snapshots(self) -> bool:
        return self.burn_check.isChecked()


class MainWindow(QMainWindow):
    def __init__(
        self,
        device: str,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
    ):
        super().__init__()
        self.setWindowTitle("Bresun Microscope — HH-4K MAX")
        self.resize(1520, 960)

        self.settings = QSettings("Bresun", "Microscope", self)
        self._cli_override = width is not None
        if width is None or height is None or fps is None:
            res_idx = max(
                0,
                min(int(self.settings.value("res_index", 1)), len(RESOLUTIONS) - 1),
            )
            _, width, height, fps = RESOLUTIONS[res_idx]
        else:
            res_idx = next(
                (
                    i
                    for i, r in enumerate(RESOLUTIONS)
                    if r[1:] == (width, height, fps)
                ),
                0,
            )
        self._res_idx = res_idx

        self.ctl = V4L2Controls(device, self)
        cfg_dir = os.path.dirname(self.settings.fileName())
        os.makedirs(cfg_dir, exist_ok=True)
        user_cal_path = os.path.join(cfg_dir, "calibrations.json")
        legacy_cal_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "calibrations.json",
        )
        if not os.path.exists(user_cal_path) and os.path.exists(legacy_cal_path):
            try:
                import shutil
                shutil.copy2(legacy_cal_path, user_cal_path)
            except OSError:
                pass
        self.calib = CalibrationStore(user_cal_path)
        self.recorder = Recorder()
        self.rec_config = RecordingConfig()
        self._burn_snapshots = True
        self._inhibitor = ScreenInhibitor()
        self.cam = CameraThread(device, width, height, fps, self)
        self.cam.recorder = self.recorder
        self.viewer = Viewer(self.cam, self.ctl, self.calib, self)
        self.viewer.context_action.connect(self._handle_context_action)

        self._syncing = False
        self._pending_res: tuple[int, int, int] | None = None
        self._enc_values = [v for _, v in self._encoder_choices()]
        self._build_ui()
        self._build_menus()
        self._shortcuts()
        self._refresh_cal_combo()
        self._apply_settings()
        self.ctl.controls_changed.connect(self._sync_from_device)
        self._sync_from_device(self.ctl.get_all())
        self.cam.start()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        split = QSplitter(Qt.Orientation.Horizontal)
        self.split = split
        split.addWidget(self.viewer)

        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setSpacing(6)

        # -- 1. Measure & Calibrate -----------------------------------------
        g = QGroupBox("Measure && Calibrate")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.measure_btn = QPushButton("Measure (M)")
        self.measure_btn.setCheckable(True)
        self.measure_btn.toggled.connect(self._toggle_measure)
        self.cal_btn = QPushButton("Calibrate (C)")
        self.cal_btn.clicked.connect(self._start_calibrate)
        self.clear_meas_btn = QPushButton("Clear")
        self.clear_meas_btn.setToolTip("Clear all measurements (Esc) · Backspace removes last")
        self.clear_meas_btn.clicked.connect(self.viewer.clear_measurement)
        row.addWidget(self.measure_btn, 1)
        row.addWidget(self.cal_btn, 1)
        row.addWidget(self.clear_meas_btn)
        v.addLayout(row)

        tool_row = QHBoxLayout()
        tool_row.addWidget(QLabel("Tool:"))
        self.tool_combo = QComboBox()
        for label, _kind in MEASURE_TOOLS:
            self.tool_combo.addItem(label)
        self._narrow_combo(self.tool_combo)
        self.tool_combo.currentIndexChanged.connect(self._tool_changed)
        tool_row.addWidget(self.tool_combo, 1)
        v.addLayout(tool_row)

        pitch_row = QHBoxLayout()
        pitch_row.addWidget(QLabel("Pin Pitch:"))
        self.pin_spin = QSpinBox()
        self.pin_spin.setRange(1, 64)
        self.pin_spin.setValue(1)
        self.pin_spin.setSpecialValueText("1 (direct length)")
        self.pin_spin.setSuffix(" pins")
        self.pin_spin.setToolTip(
            "For IC pin pitch: set number of pins spanned (e.g. 8 pins),\n"
            "then measure from pin 1 center to pin N center.\n"
            "The app divides total length by (N − 1) gaps."
        )
        self.pin_spin.valueChanged.connect(self._pin_count_changed)
        pitch_row.addWidget(self.pin_spin, 1)
        v.addLayout(pitch_row)

        unit_row = QHBoxLayout()
        unit_row.addWidget(QLabel("Units:"))
        self.unit_combo = QComboBox()
        for label, _u in UNIT_CHOICES:
            self.unit_combo.addItem(label)
        self._narrow_combo(self.unit_combo)
        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        unit_row.addWidget(self.unit_combo, 1)
        v.addLayout(unit_row)

        opt_row1 = QHBoxLayout()
        self.details_check = QCheckBox("ΔX / ΔY / ∠ / Area")
        self.details_check.setChecked(True)
        self.details_check.toggled.connect(self._set_measure_details)
        self.loupe_check = QCheckBox("Loupe (G)")
        self.loupe_check.setChecked(True)
        self.loupe_check.setToolTip("Show 4× precision magnifier inset around cursor while measuring")
        self.loupe_check.toggled.connect(self._set_loupe)
        opt_row1.addWidget(self.details_check)
        opt_row1.addWidget(self.loupe_check)
        v.addLayout(opt_row1)

        self.keep_meas_check = QCheckBox("Keep multiple measurements on screen")
        self.keep_meas_check.setChecked(True)
        self.keep_meas_check.toggled.connect(self._set_keep_measurements)
        v.addWidget(self.keep_meas_check)

        cal_row = QHBoxLayout()
        self.cal_combo = QComboBox()
        self.cal_combo.currentTextChanged.connect(self._cal_selected)
        self._narrow_combo(self.cal_combo)
        self.cal_del = QPushButton("Delete")
        self.cal_del.clicked.connect(self._cal_delete)
        cal_row.addWidget(self.cal_combo, 1)
        cal_row.addWidget(self.cal_del)
        v.addLayout(cal_row)

        self.cal_info = QLabel("no calibration")
        self.cal_info.setStyleSheet("color:#888; font-size:11px;")
        v.addWidget(self.cal_info)
        self.viewer.calibration_captured.connect(self._calibration_captured)
        lay.addWidget(g)

        # -- 2. Image & Contrast (moved to its own non-modal Settings dialog) -
        self.image_dialog = QDialog(self)
        self.image_dialog.setWindowTitle("Image & Exposure Settings")
        self.image_dialog.setMinimumWidth(360)
        v = QVBoxLayout(self.image_dialog)
        top_row = QHBoxLayout()
        self.clahe_check = QCheckBox("Enhance IC Markings (E)")
        self.clahe_check.setToolTip(
            "Adaptive local contrast (CLAHE) — makes faint laser-etched\n"
            "SMD IC package markings and trace edges stand out clearly."
        )
        self.clahe_check.toggled.connect(self._set_clahe)
        self.reset_img_btn = QPushButton("Reset")
        self.reset_img_btn.setToolTip("Reset all image adjustments and exposure to defaults")
        self.reset_img_btn.clicked.connect(self.reset_image_controls)
        top_row.addWidget(self.clahe_check, 1)
        top_row.addWidget(self.reset_img_btn)
        v.addLayout(top_row)

        self.mono_check = QCheckBox("Monochrome / High-Contrast Inspection")
        self.mono_check.toggled.connect(self._set_grayscale)
        v.addWidget(self.mono_check)

        self.sw_sliders: dict[str, dict] = {}
        sw_specs = [
            ("brightness", "Brightness", -100, 100, 0, 10, lambda val: f"{val:+d}"),
            ("contrast", "Contrast", -100, 100, 0, 10, lambda val: f"{val:+d}"),
            ("gamma", "Gamma", 30, 250, 100, 10, lambda val: f"{val / 100.0:.2f}"),
            ("sharpness", "Sharpness", 0, 100, 0, 10, lambda val: f"{val}%"),
        ]
        for key, label, lo, hi, default, step, fmt_fn in sw_specs:
            s = self._make_sw_slider(key, label, lo, hi, default, step, fmt_fn)
            self.sw_sliders[key] = s
            v.addLayout(s["lay"])

        # Hardware sensor exposure & flicker
        hw_row = QHBoxLayout()
        self.auto_exp_check = QCheckBox("Sensor Auto-Exp")
        self.auto_exp_check.toggled.connect(self._toggle_auto_exposure)
        if not self.ctl.supports_auto_exposure():
            self.auto_exp_check.hide()
        hw_row.addWidget(self.auto_exp_check, 1)
        hw_row.addWidget(QLabel("Flicker:"))
        self.plf_combo = QComboBox()
        self.plf_combo.addItems(["Off", "50 Hz", "60 Hz"])
        self.plf_combo.currentIndexChanged.connect(
            lambda i: self.ctl.set("power_line_frequency", i)
        )
        hw_row.addWidget(self.plf_combo)
        v.addLayout(hw_row)

        self.exp_slider = self._make_hw_slider("exposure_time_absolute", "HW Exp", 1, 500, 50)
        self.exp_slider["slider"].setMaximum(500)
        self.exp_slider["slider"].setToolTip(
            "Hardware sensor exposure time (+ / − keys).\n"
            "Values beyond ~166 @60 fps are clamped by the camera firmware."
        )
        v.addLayout(self.exp_slider["lay"])
        img_btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        img_btns.rejected.connect(self.image_dialog.close)
        v.addWidget(img_btns)

        # -- 3. Capture ------------------------------------------------------
        g = QGroupBox("Capture")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        b = QPushButton("Snapshot (S)")
        b.clicked.connect(self.snapshot)
        row.addWidget(b, 1)
        self.rec_btn = QPushButton("Record (R)")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._record_toggled)
        row.addWidget(self.rec_btn, 1)
        open_dir_btn = QPushButton("Folder")
        open_dir_btn.setToolTip(f"Open captures folder ({SAVE_DIR})")
        open_dir_btn.clicked.connect(self._open_captures_folder)
        row.addWidget(open_dir_btn)
        v.addLayout(row)

        self.scan_btn = QPushButton("Board Scan / Stitch (P)")
        self.scan_btn.setToolTip(
            "Scan a full PCB by panning and reconstruct a stitched mosaic image"
        )
        self.scan_btn.clicked.connect(self.open_board_scanner)
        v.addWidget(self.scan_btn)

        row = QHBoxLayout()
        self.rec_summary_label = QLabel(self.rec_config.summary())
        self.rec_summary_label.setStyleSheet("color:#aaa; font-size:11px;")
        row.addWidget(self.rec_summary_label, 1)
        self.rec_settings_btn = QPushButton("Settings…")
        self.rec_settings_btn.setToolTip("Configure recording quality, bitrate, resolution & encoder")
        self.rec_settings_btn.clicked.connect(self.open_recording_settings)
        row.addWidget(self.rec_settings_btn)
        v.addLayout(row)

        self.rec_label = QLabel("")
        self.rec_label.setStyleSheet("color:#888;")
        v.addWidget(self.rec_label)
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(500)
        self._rec_timer.timeout.connect(self._rec_tick)
        lay.addWidget(g)

        # -- 4. View & Camera (moved to its own non-modal Settings dialog) ---
        self.view_dialog = QDialog(self)
        self.view_dialog.setWindowTitle("View & Camera Settings")
        self.view_dialog.setMinimumWidth(340)
        v = QVBoxLayout(self.view_dialog)
        ov_row = QHBoxLayout()
        self.cross_check = QCheckBox("Crosshair")
        self.cross_check.setChecked(True)
        self.cross_check.toggled.connect(lambda on: self._set_view("show_crosshair", on))
        self.grid_check = QCheckBox("Grid")
        self.grid_check.toggled.connect(lambda on: self._set_view("show_grid", on))
        ov_row.addWidget(self.cross_check)
        ov_row.addWidget(self.grid_check)
        v.addLayout(ov_row)

        row = QHBoxLayout()
        self.flip_h = QCheckBox("Mirror H")
        self.flip_h.toggled.connect(lambda on: self._set_view("flip_h", on))
        self.flip_v = QCheckBox("Flip V")
        self.flip_v.toggled.connect(lambda on: self._set_view("flip_v", on))
        row.addWidget(self.flip_h)
        row.addWidget(self.flip_v)
        v.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Rotate:"))
        self.rot_combo = QComboBox()
        self.rot_combo.addItems(["0°", "90°", "180°", "270°"])
        self.rot_combo.currentIndexChanged.connect(lambda i: self._set_view("rot", i))
        row.addWidget(self.rot_combo, 1)
        v.addLayout(row)

        self.ll_check = QCheckBox("Low-latency 60 Hz mode (L)")
        self.ll_check.setToolTip("60 Hz refresh + newest-frame decode.")
        self.ll_check.toggled.connect(self._set_low_latency)
        v.addWidget(self.ll_check)

        self.awake_check = QCheckBox("Keep screen awake in fullscreen")
        self.awake_check.setToolTip("Prevent screen blanking / screensaver while in fullscreen mode")
        self.awake_check.toggled.connect(self._set_keep_awake)
        v.addWidget(self.awake_check)

        row = QHBoxLayout()
        row.addWidget(QLabel("Resolution:"))
        self.res_combo = QComboBox()
        for label, *_ in RESOLUTIONS:
            self.res_combo.addItem(label)
        self._narrow_combo(self.res_combo)
        self.res_combo.currentIndexChanged.connect(self._res_changed)
        row.addWidget(self.res_combo, 1)
        v.addLayout(row)

        self.reset_view_btn = QPushButton("Reset View (Zoom / Pan / Flip)")
        self.reset_view_btn.setToolTip(
            "Reset digital zoom to 1.0×, center view, and reset mirror/flip/rotation"
        )
        self.reset_view_btn.clicked.connect(self.reset_view)
        v.addWidget(self.reset_view_btn)
        view_btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        view_btns.rejected.connect(self.view_dialog.close)
        v.addWidget(view_btns)

        # -- Compact Settings launcher on main sidebar -----------------------
        g = QGroupBox("Settings")
        v = QVBoxLayout(g)
        row1 = QHBoxLayout()
        img_btn = QPushButton("Image & Exposure…")
        img_btn.clicked.connect(self.open_image_settings)
        view_btn = QPushButton("View & Camera…")
        view_btn.clicked.connect(self.open_view_settings)
        row1.addWidget(img_btn, 1)
        row1.addWidget(view_btn, 1)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        rst_view_btn = QPushButton("Reset View")
        rst_view_btn.clicked.connect(self.reset_view)
        rst_all_btn = QPushButton("Reset All Defaults…")
        rst_all_btn.clicked.connect(self.reset_all_settings)
        row2.addWidget(rst_view_btn, 1)
        row2.addWidget(rst_all_btn, 1)
        v.addLayout(row2)
        lay.addWidget(g)
        lay.addStretch(1)

        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(310)
        scroll.setMaximumWidth(350)
        panel.setMinimumWidth(290)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)

        self.setCentralWidget(split)

        # fullscreen overlay drawer
        self._fs_overlay = QWidget(self)
        self._fs_overlay.setObjectName("fsOverlay")
        self._fs_overlay.setStyleSheet(
            "#fsOverlay { background: rgba(16,16,20,235);"
            " border-left: 1px solid #3a3a44; border-radius: 6px; }"
        )
        self._fs_overlay_layout = QVBoxLayout(self._fs_overlay)
        self._fs_overlay_layout.setContentsMargins(6, 6, 6, 6)
        self._fs_overlay.hide()

        self.statusBar().showMessage(
            f"{self.cam.device} — Right-click canvas for quick menu · H toggles shortcuts"
        )

    def _make_sw_slider(
        self,
        key: str,
        label: str,
        lo: int,
        hi: int,
        default: int,
        step: int,
        fmt_fn,
    ) -> dict:
        lab = QLabel(label)
        lab.setMinimumWidth(68)
        lab.setStyleSheet("font-size:11px;")
        sld = QSlider(Qt.Orientation.Horizontal)
        sld.setRange(lo, hi)
        sld.setSingleStep(1)
        sld.setPageStep(step)
        sld.setValue(default)
        val_lab = QLabel(fmt_fn(default))
        val_lab.setMinimumWidth(44)
        val_lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val_lab.setStyleSheet("font-size:11px;color:#888;")

        def _on_val(v: int) -> None:
            val_lab.setText(fmt_fn(v))
            self._on_sw_slider_changed(key, v)

        sld.valueChanged.connect(_on_val)
        lay = QHBoxLayout()
        lay.addWidget(lab)
        lay.addWidget(sld, 1)
        lay.addWidget(val_lab)
        return {
            "slider": sld,
            "label": lab,
            "lay": lay,
            "key": key,
            "default": default,
            "value_label": val_lab,
        }

    def _make_hw_slider(self, name: str, label: str, lo: int, hi: int, step: int) -> dict:
        desc = self.ctl.descriptors.get(name, {})
        lo = desc.get("min", lo)
        hi = desc.get("max", hi)
        val = desc.get("value", desc.get("default", lo))
        lab = QLabel(label)
        lab.setMinimumWidth(68)
        lab.setStyleSheet("font-size:11px;")
        sld = QSlider(Qt.Orientation.Horizontal)
        sld.setRange(lo, hi)
        sld.setSingleStep(max(1, step // 10))
        sld.setPageStep(step)
        sld.setValue(val)
        val_lab = QLabel(str(val))
        val_lab.setMinimumWidth(44)
        val_lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val_lab.setStyleSheet("font-size:11px;color:#888;")

        def _on_val(v: int) -> None:
            val_lab.setText(str(v))
            if not self._syncing:
                if name == "exposure_time_absolute" and self.auto_exp_check.isChecked():
                    self.auto_exp_check.setChecked(False)
                self.ctl.set(name, v)

        sld.valueChanged.connect(_on_val)
        lay = QHBoxLayout()
        lay.addWidget(lab)
        lay.addWidget(sld, 1)
        lay.addWidget(val_lab)
        return {
            "slider": sld,
            "label": lab,
            "lay": lay,
            "name": name,
            "value_label": val_lab,
        }

    def _build_menus(self) -> None:
        mb = self.menuBar()
        mb.clear()

        # -- File menu --
        file_menu = mb.addMenu("&File")
        file_menu.addAction("Take Snapshot (S)", self.snapshot)
        file_menu.addAction("Start / Stop Recording (R)", lambda: self.rec_btn.toggle())
        file_menu.addAction("Board Scan / Stitch… (P)", self.open_board_scanner)
        file_menu.addSeparator()
        file_menu.addAction(
            "Open Captures Folder",
            QKeySequence("Ctrl+O"),
            self._open_captures_folder,
        )
        file_menu.addSeparator()
        file_menu.addAction("Quit", QKeySequence("Ctrl+Q"), self.close)

        # -- Measure menu --
        meas_menu = mb.addMenu("&Measure")
        meas_menu.addAction("Toggle Measure Mode (M)", lambda: self.measure_btn.toggle())
        meas_menu.addAction("Tool: Line / Pin Pitch", lambda: self._select_tool("line"))
        meas_menu.addAction("Tool: Multi-Point Ruler (N)", lambda: self._select_tool("polyline"))
        meas_menu.addAction("Tool: Circle 2-pt Diameter (D)", lambda: self._select_tool("circle"))
        meas_menu.addAction("Tool: Circle 3-pt Rim", lambda: self._select_tool("circle3"))
        meas_menu.addAction("Tool: Tilted Rectangle W × H (B)", lambda: self._select_tool("rect"))
        meas_menu.addSeparator()
        meas_menu.addAction("Calibrate Scale (C)", self._start_calibrate)
        meas_menu.addSeparator()
        meas_menu.addAction("Toggle Magnifier Loupe (G)", lambda: self.loupe_check.toggle())
        meas_menu.addAction("Toggle ΔX / ΔY / Angle Details", lambda: self.details_check.toggle())
        meas_menu.addSeparator()
        meas_menu.addAction("Undo Last Measurement (Backspace)", self.viewer.pop_measurement)
        meas_menu.addAction("Clear All Measurements (Esc)", self._escape)

        # -- View menu --
        view_menu = mb.addMenu("&View")
        view_menu.addAction("Freeze / Unfreeze Frame (Space)", self.viewer.toggle_freeze)
        view_menu.addAction("Enhance IC Markings / CLAHE (E)", lambda: self.clahe_check.toggle())
        view_menu.addAction("Monochrome Mode", lambda: self.mono_check.toggle())
        view_menu.addSeparator()
        view_menu.addAction("Toggle Crosshair", lambda: self.cross_check.toggle())
        view_menu.addAction("Toggle Grid Overlay", lambda: self.grid_check.toggle())
        view_menu.addAction("Low-Latency 60 Hz Mode (L)", lambda: self.ll_check.toggle())
        view_menu.addAction("Keep Screen Awake in Fullscreen", lambda: self.awake_check.toggle())
        view_menu.addAction("Fullscreen (F / F11)", self.toggle_fullscreen)
        view_menu.addSeparator()
        view_menu.addAction("Reset Zoom & View", self.reset_view)
        view_menu.addAction("Reset Image Adjustments", self.reset_image_controls)

        # -- Settings menu --
        settings_menu = mb.addMenu("&Settings")
        settings_menu.addAction(
            "Recording & Capture Settings…",
            QKeySequence("Ctrl+,"),
            self.open_recording_settings,
        )
        settings_menu.addAction(
            "Image & Exposure Settings…",
            self.open_image_settings,
        )
        settings_menu.addAction(
            "View & Camera Settings…",
            self.open_view_settings,
        )
        settings_menu.addSeparator()
        settings_menu.addAction(
            "Reset All Settings to Defaults…",
            self.reset_all_settings,
        )
        settings_menu.addAction(
            "Open Config Folder (~/.config)",
            self._open_config_folder,
        )

        # -- Help menu --
        help_menu = mb.addMenu("&Help")
        help_menu.addAction("Usage Guide", self._open_guide)
        help_menu.addAction("Keyboard Shortcuts (H)", self._toggle_help)

    def _shortcuts(self) -> None:
        def add(key: str, fn) -> None:
            QShortcut(QKeySequence(key), self, activated=fn)

        add("Space", self.viewer.toggle_freeze)
        add("S", self.snapshot)
        add("R", lambda: self.rec_btn.toggle())
        add("M", lambda: self.measure_btn.toggle())
        add("N", lambda: self._select_tool("polyline"))
        add("D", lambda: self._select_tool("circle"))
        add("B", lambda: self._select_tool("rect"))
        add("Return", self.viewer.finish_polyline)
        add("Enter", self.viewer.finish_polyline)
        add("C", self._start_calibrate)
        add("G", lambda: self.loupe_check.toggle())
        add("E", lambda: self.clahe_check.toggle())
        add("H", self._toggle_help)
        add("Backspace", self.viewer.pop_measurement)
        add("Esc", self._escape)
        add("+", lambda: self._nudge_exposure(+25))
        add("=", lambda: self._nudge_exposure(+25))
        add("-", lambda: self._nudge_exposure(-25))
        add("L", lambda: self.ll_check.toggle())
        add("P", self.open_board_scanner)
        add("F", self.toggle_fullscreen)
        add("F11", self.toggle_fullscreen)
        add("Tab", self._toggle_overlay)

    def _handle_context_action(self, action: str) -> None:
        if action == "measure_line":
            self._select_tool("line")
        elif action == "measure_polyline":
            self._select_tool("polyline")
        elif action == "measure_circle":
            self._select_tool("circle")
        elif action == "measure_circle3":
            self._select_tool("circle3")
        elif action == "measure_rect":
            self._select_tool("rect")
        elif action == "calibrate":
            self._start_calibrate()
        elif action == "snapshot":
            self.snapshot()
        elif action == "toggle_clahe":
            self.clahe_check.toggle()
        elif action == "reset_view":
            self.reset_view()

    def _set_low_latency(self, on: bool) -> None:
        self.viewer.low_latency = on

    def _set_keep_awake(self, _on: bool) -> None:
        self._update_inhibit()

    def _update_inhibit(self) -> None:
        self._inhibitor.set_enabled(self.awake_check.isChecked() and self.isFullScreen())

    # -- software image callbacks --------------------------------------------
    def _on_sw_slider_changed(self, key: str, val: int) -> None:
        if key == "brightness":
            self.viewer.sw_brightness = val
        elif key == "contrast":
            self.viewer.sw_contrast = val
        elif key == "gamma":
            self.viewer.sw_gamma = val / 100.0
        elif key == "sharpness":
            self.viewer.sw_sharpness = val
        self.viewer._refresh()

    def _set_clahe(self, on: bool) -> None:
        self.viewer.sw_clahe = on
        self.viewer._refresh()

    def _set_grayscale(self, on: bool) -> None:
        self.viewer.sw_grayscale = on
        self.viewer._refresh()

    # -- fullscreen -----------------------------------------------------------
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._leave_fullscreen()
        else:
            self.showFullScreen()
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        self.menuBar().hide()
        self.statusBar().hide()
        self._fs_overlay_layout.addWidget(self.scroll)
        self._fs_overlay.hide()
        self._fs_overlay.raise_()
        self.viewer.extra_hint = "Tab — controls · Right-click — quick menu · F — exit fullscreen"
        self._update_inhibit()
        self.viewer._refresh()

    def _leave_fullscreen(self) -> None:
        self._fs_overlay.hide()
        self._fs_overlay_layout.removeWidget(self.scroll)
        self.scroll.setParent(self)
        self.split.addWidget(self.scroll)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.menuBar().show()
        self.statusBar().show()
        self.viewer.extra_hint = ""
        self._update_inhibit()
        self.viewer._refresh()

    def _toggle_overlay(self) -> None:
        if self.isFullScreen():
            self._fs_overlay.setVisible(not self._fs_overlay.isVisible())
            if self._fs_overlay.isVisible():
                self._fs_overlay.raise_()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._fs_overlay.setGeometry(
            self.width() - 358, 8, 350, max(200, self.height() - 16)
        )

    # -- encoder selection ---------------------------------------------------
    @staticmethod
    def _encoder_choices() -> list[tuple[str, str]]:
        """(label, value) pairs; value 'auto' | 'cpu' | render node path."""
        import glob as _glob
        import re as _re
        import subprocess as _sp

        choices = [("Auto (first working GPU)", "auto")]
        nodes = sorted(
            n.rsplit("/", 1)[-1]
            for n in _glob.glob("/dev/dri/renderD*")
            if os.path.realpath(n) not in BLOCKED_VAAPI_DEVICES
        )
        for node in nodes:
            desc = ""
            try:
                link = os.path.realpath(f"/dev/dri/{node}")
                for bp in _glob.glob("/dev/dri/by-path/*-render"):
                    if os.path.realpath(bp) == link:
                        slot = os.path.basename(bp)[4:-7].split(":")[-2:]
                        out = _sp.run(
                            ["lspci", "-s", ":".join(slot)],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        ).stdout
                        m = _re.search(r"controller:\s*(.+?)\s*\(rev", out)
                        if m:
                            d = m.group(1)
                            if "] " in d:
                                d = d.split("] ", 1)[1]
                            d = d.split(" [")[0].strip()
                            desc = f" — {d}" if d else ""
                        break
            except Exception:
                desc = ""
            choices.append((f"{node}{desc}", link))
        choices.append(("CPU (libx264)", "cpu"))
        return choices

    @staticmethod
    def _narrow_combo(combo: QComboBox) -> None:
        """Never let item text dictate the combo (and panel) width."""
        combo.setMinimumContentsLength(14)
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )

    def open_recording_settings(self) -> None:
        if self.recorder.active:
            QMessageBox.information(
                self, "Recording active",
                "Stop the current recording before changing recording settings.",
            )
            return
        dlg = RecordingSettingsDialog(
            self,
            self.rec_config,
            self.recorder.preferred_node,
            self._encoder_choices(),
            self._burn_snapshots,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.rec_config = dlg.recording_config()
            self.recorder.preferred_node = dlg.preferred_node()
            self._burn_snapshots = dlg.burn_snapshots()
            self.rec_summary_label.setText(self.rec_config.summary())
            self._save_settings()

    def open_image_settings(self) -> None:
        self.image_dialog.show()
        self.image_dialog.raise_()
        self.image_dialog.activateWindow()

    def open_view_settings(self) -> None:
        self.view_dialog.show()
        self.view_dialog.raise_()
        self.view_dialog.activateWindow()

    def reset_all_settings(self) -> None:
        if self.recorder.active:
            QMessageBox.information(
                self, "Recording active",
                "Stop the current recording before resetting settings.",
            )
            return
        ans = QMessageBox.question(
            self,
            "Reset All Settings",
            "Reset Recording, Image & Exposure, View, and Measurement settings to defaults?\n\n"
            "Default recording: H.265 (VA-API · renderD129) · 1080p @60 · CQP 28",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.settings.clear()
        self.rec_config = RecordingConfig()
        self.recorder.preferred_node = "auto"
        self._burn_snapshots = True
        self.rec_summary_label.setText(self.rec_config.summary())
        self.reset_image_controls()
        self.reset_view()
        self.cross_check.setChecked(True)
        self.grid_check.setChecked(False)
        self.ll_check.setChecked(True)
        self.awake_check.setChecked(True)
        self.res_combo.setCurrentIndex(0)
        self.unit_combo.setCurrentIndex(0)
        self.details_check.setChecked(True)
        self.loupe_check.setChecked(True)
        self.keep_meas_check.setChecked(True)
        self._save_settings()
        self.statusBar().showMessage("All settings reset to defaults", 4000)

    def _open_config_folder(self) -> None:
        cfg_dir = os.path.dirname(self.settings.fileName())
        os.makedirs(cfg_dir, exist_ok=True)
        QProcess.startDetached("xdg-open", [cfg_dir])

    # -- persisted preferences ------------------------------------------------
    def _bool_setting(self, key: str, default: bool) -> bool:
        v = self.settings.value(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("true", "1")
        return default

    def _apply_settings(self) -> None:
        s = self.settings
        self.res_combo.setCurrentIndex(self._res_idx)
        self.ll_check.setChecked(self._bool_setting("low_latency", True))
        keep_awake = self._bool_setting("keep_awake", True)
        self.awake_check.setChecked(keep_awake)
        self._set_keep_awake(keep_awake)
        self.cross_check.setChecked(self._bool_setting("crosshair", True))
        self.grid_check.setChecked(self._bool_setting("grid", False))
        self.flip_h.setChecked(self._bool_setting("flip_h", False))
        self.flip_v.setChecked(self._bool_setting("flip_v", False))
        self.rot_combo.setCurrentIndex(int(s.value("rot", 0)))
        self._burn_snapshots = self._bool_setting("burn", True)
        self.rec_config = RecordingConfig(
            codec=str(s.value("rec_codec", "hevc")),
            backend=str(s.value("rec_backend", "vaapi")),
            resolution=str(s.value("rec_resolution", "1080p")),
            fps=int(s.value("rec_fps", 60)),
            rc_mode=str(s.value("rec_rc_mode", "CQP")),
            qp=int(s.value("rec_qp", 28)),
            bitrate_mbps=int(s.value("rec_bitrate_mbps", 4)),
        )
        self.rec_summary_label.setText(self.rec_config.summary())
        self.unit_combo.setCurrentIndex(int(s.value("unit_idx", 0)))
        self.details_check.setChecked(self._bool_setting("measure_details", True))
        self.loupe_check.setChecked(self._bool_setting("measure_loupe", True))
        self.keep_meas_check.setChecked(self._bool_setting("keep_measurements", True))
        self.clahe_check.setChecked(self._bool_setting("sw_clahe", False))
        self.mono_check.setChecked(self._bool_setting("sw_grayscale", False))
        for key, info in self.sw_sliders.items():
            saved_val = int(s.value(f"sw_{key}", info["default"]))
            info["slider"].setValue(saved_val)

        enc = s.value("encoder_gpu", "auto")
        if enc not in self._enc_values:
            enc = "auto"
        self.recorder.preferred_node = enc
        active = s.value("cal_profile", "")
        if active and active in self.calib.profiles:
            self.calib.active = active
            self._refresh_cal_combo()

    def _save_settings(self) -> None:
        s = self.settings
        if not self._cli_override:
            s.setValue("res_index", self.res_combo.currentIndex())
        s.setValue("low_latency", self.ll_check.isChecked())
        s.setValue("keep_awake", self.awake_check.isChecked())
        s.setValue("crosshair", self.cross_check.isChecked())
        s.setValue("grid", self.grid_check.isChecked())
        s.setValue("flip_h", self.flip_h.isChecked())
        s.setValue("flip_v", self.flip_v.isChecked())
        s.setValue("rot", self.rot_combo.currentIndex())
        s.setValue("burn", self._burn_snapshots)
        s.setValue("rec_codec", self.rec_config.codec)
        s.setValue("rec_backend", self.rec_config.backend)
        s.setValue("rec_resolution", self.rec_config.resolution)
        s.setValue("rec_fps", self.rec_config.fps)
        s.setValue("rec_rc_mode", self.rec_config.rc_mode)
        s.setValue("rec_qp", self.rec_config.qp)
        s.setValue("rec_bitrate_mbps", self.rec_config.bitrate_mbps)
        s.setValue("unit_idx", self.unit_combo.currentIndex())
        s.setValue("measure_details", self.details_check.isChecked())
        s.setValue("measure_loupe", self.loupe_check.isChecked())
        s.setValue("keep_measurements", self.keep_meas_check.isChecked())
        s.setValue("sw_clahe", self.clahe_check.isChecked())
        s.setValue("sw_grayscale", self.mono_check.isChecked())
        for key, info in self.sw_sliders.items():
            s.setValue(f"sw_{key}", info["slider"].value())
        s.setValue("encoder_gpu", self.recorder.preferred_node)
        if self.calib.active:
            s.setValue("cal_profile", self.calib.active)

    def _open_guide(self) -> None:
        guide = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docs",
            "USAGE.md",
        )
        if os.path.exists(guide):
            QProcess.startDetached("xdg-open", [guide])
        else:
            self.statusBar().showMessage(f"Guide not found: {guide}", 4000)

    def _open_captures_folder(self) -> None:
        os.makedirs(SAVE_DIR, exist_ok=True)
        QProcess.startDetached("xdg-open", [SAVE_DIR])

    # ------------------------------------------------------------- callbacks
    def _sync_from_device(self, values: dict) -> None:
        self._syncing = True
        _auto_val, manual_val = self.ctl.auto_exposure_values()
        is_auto = False
        try:
            if "exposure_time_absolute" in values:
                self.exp_slider["slider"].setValue(
                    min(
                        values["exposure_time_absolute"],
                        self.exp_slider["slider"].maximum(),
                    )
                )
            if "power_line_frequency" in values:
                self.plf_combo.blockSignals(True)
                self.plf_combo.setCurrentIndex(values["power_line_frequency"])
                self.plf_combo.blockSignals(False)
            if "auto_exposure" in values:
                is_auto = values["auto_exposure"] != manual_val
                self.auto_exp_check.blockSignals(True)
                self.auto_exp_check.setChecked(is_auto)
                self.auto_exp_check.blockSignals(False)
                self.exp_slider["slider"].setEnabled(not is_auto)
        finally:
            self._syncing = False
        # Only clamp hardware exposure back to the device in manual mode;
        # writing exposure_time_absolute during auto-exposure kicks UVC
        # firmware back into manual mode.
        if not is_auto:
            exp = values.get("exposure_time_absolute")
            if exp is not None and exp > self.exp_slider["slider"].maximum():
                self.ctl.set(
                    "exposure_time_absolute", self.exp_slider["slider"].maximum()
                )

    def _toggle_auto_exposure(self, on: bool) -> None:
        if self._syncing:
            return
        auto_val, manual_val = self.ctl.auto_exposure_values()
        self.ctl.set("auto_exposure", auto_val if on else manual_val)
        self.exp_slider["slider"].setEnabled(not on)
        if not on:
            self.ctl.set("exposure_time_absolute", self.exp_slider["slider"].value())

    def reset_image_controls(self) -> None:
        for info in self.sw_sliders.values():
            info["slider"].setValue(info["default"])
        self.clahe_check.setChecked(False)
        self.mono_check.setChecked(False)
        for name in ("brightness", "contrast", "saturation", "sharpness", "gain"):
            d = self.ctl.descriptors.get(name, {})
            if "default" in d:
                self.ctl.set(name, d["default"])
        if self.ctl.supports_auto_exposure():
            auto_val, _manual_val = self.ctl.auto_exposure_values()
            self.auto_exp_check.setChecked(True)
            self.ctl.set("auto_exposure", auto_val)
            self.exp_slider["slider"].setEnabled(False)
        else:
            d_exp = self.ctl.descriptors.get("exposure_time_absolute", {})
            exp_def = min(self.exp_slider["slider"].maximum(), d_exp.get("default", 150))
            self.ctl.set("exposure_time_absolute", exp_def)
            self.exp_slider["slider"].setValue(exp_def)
        d_plf = self.ctl.descriptors.get("power_line_frequency", {})
        plf_def = min(d_plf.get("default", 1), self.plf_combo.count() - 1)
        self.plf_combo.setCurrentIndex(plf_def)
        self.ctl.set("power_line_frequency", plf_def)
        self.viewer._refresh()
        self.statusBar().showMessage("Image & exposure controls reset to defaults", 3000)

    def reset_view(self) -> None:
        self.viewer.reset_view()
        self.flip_h.setChecked(False)
        self.flip_v.setChecked(False)
        self.rot_combo.setCurrentIndex(0)
        self.statusBar().showMessage("View reset (1.0× unrotated)", 3000)

    def _nudge_exposure(self, delta: int) -> None:
        if self.auto_exp_check.isChecked():
            self.auto_exp_check.setChecked(False)
        cur = self.exp_slider["slider"].value()
        new_val = max(1, min(self.exp_slider["slider"].maximum(), cur + delta))
        self.exp_slider["slider"].setValue(new_val)

    # -- measurement ------------------------------------------------------
    def _toggle_measure(self, on: bool) -> None:
        self.viewer.measure_mode = on
        if on:
            self.viewer.calibrating = False
        self.viewer.pending_pts.clear()
        self.viewer.set_measure_cursor()
        self.viewer.update()

    def _select_tool(self, kind: str) -> None:
        for idx, (_label, k) in enumerate(MEASURE_TOOLS):
            if k == kind:
                self.tool_combo.setCurrentIndex(idx)
                break
        if not self.measure_btn.isChecked():
            self.measure_btn.setChecked(True)

    def _tool_changed(self, index: int) -> None:
        if 0 <= index < len(MEASURE_TOOLS):
            kind = MEASURE_TOOLS[index][1]
            self.viewer.measure_kind = kind
            self.viewer.pending_pts.clear()
            self.pin_spin.setEnabled(kind == "line")
            self.viewer.update()

    def _pin_count_changed(self, val: int) -> None:
        self.viewer.pin_count = val
        # Also update the most recent line measurement if one is selected
        if self.viewer.measurements and self.viewer.measurements[-1].kind == "line":
            self.viewer.measurements[-1].pin_count = val
        self.viewer.update()

    def _unit_changed(self, index: int) -> None:
        if 0 <= index < len(UNIT_CHOICES):
            self.viewer.unit_mode = UNIT_CHOICES[index][1]
            self._update_cal_info()
            self.viewer.update()

    def _set_measure_details(self, on: bool) -> None:
        self.viewer.show_details = on
        self.viewer.update()

    def _set_loupe(self, on: bool) -> None:
        self.viewer.show_loupe = on
        self.viewer.update()

    def _set_keep_measurements(self, on: bool) -> None:
        self.viewer.keep_measurements = on

    def _start_calibrate(self) -> None:
        self.viewer.calibrating = True
        self.viewer.measure_mode = False
        self.measure_btn.setChecked(False)
        self.viewer.pending_pts.clear()
        self.viewer.set_measure_cursor()
        self.viewer.update()
        self.statusBar().showMessage(
            "Calibrate: click both ends of a known length (hold Shift to snap horizontal/vertical)",
            5000,
        )

    def _calibration_captured(self, px_len: float) -> None:
        if px_len <= 0:
            self.viewer.calib_line = None
            return
        dlg = CalibrationDialog(px_len, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.viewer.calib_line = None
            self.viewer.update()
            return
        name = dlg.profile_name()
        length_um = dlg.length_um()
        if not name or length_um <= 0:
            self.viewer.calib_line = None
            self.viewer.update()
            return
        um_per_px = length_um / px_len
        self.calib.add(name, um_per_px, ref_width=self.cam.width)
        self.viewer.calib_line = None
        self._refresh_cal_combo()
        self.viewer.update()
        self.statusBar().showMessage(
            f"Calibrated '{name}': {um_per_px:.3f} µm/px (@ {self.cam.width}×{self.cam.height})",
            5000,
        )

    def _refresh_cal_combo(self) -> None:
        self.cal_combo.blockSignals(True)
        self.cal_combo.clear()
        self.cal_combo.addItems(sorted(self.calib.profiles))
        if self.calib.active:
            self.cal_combo.setCurrentText(self.calib.active)
        self.cal_combo.blockSignals(False)
        self._update_cal_info()

    def _cal_selected(self, name: str) -> None:
        if name:
            self.calib.active = name
        self._update_cal_info()
        self.viewer.update()

    def _cal_delete(self) -> None:
        name = self.cal_combo.currentText()
        if name:
            self.calib.remove(name)
            self._refresh_cal_combo()
            self.viewer.update()

    def _update_cal_info(self) -> None:
        um = self.calib.um_per_px(self.cam.width)
        if um:
            w_mm = self.cam.width * um / 1000.0
            h_mm = self.cam.height * um / 1000.0
            if self.viewer.unit_mode in ("mil", "both"):
                w_mil = (self.cam.width * um) / UM_PER_MIL
                h_mil = (self.cam.height * um) / UM_PER_MIL
                self.cal_info.setText(
                    f"{um:.2f} µm/px — FOV ≈ {w_mm:.1f}×{h_mm:.1f} mm ({w_mil:.0f}×{h_mil:.0f} mil)"
                )
            else:
                self.cal_info.setText(
                    f"{um:.3f} µm/px — field ≈ {w_mm:.1f} × {h_mm:.1f} mm"
                )
        else:
            self.cal_info.setText("no calibration")

    # -- capture -----------------------------------------------------------
    def snapshot(self) -> None:
        frame = self.viewer.current_frame()
        if frame is None:
            self.statusBar().showMessage("No frame to save", 2000)
            return
        out = self.viewer.process_frame(frame.copy())
        if self._burn_snapshots and (
            self.viewer.measurements or self.viewer.calib_line
        ):
            out = self._burn_overlays(out, frame)
        try:
            path = save_snapshot(out)
            self.statusBar().showMessage(f"Snapshot saved: {path}", 5000)
        except cv2.error as exc:
            QMessageBox.warning(self, "Snapshot failed", str(exc))

    def _burn_overlays(self, out: np.ndarray, frame: np.ndarray) -> np.ndarray:
        """Draw all active measurements (line/circle/rect) + labels into the full-res frame."""
        scale_factor = frame.shape[1] / 1920.0
        font_scale = max(0.55, 0.65 * scale_factor)
        thick_bg = max(3, int(6 * scale_factor))
        thick_fg = max(1, int(2 * scale_factor))
        line_w = max(2, int(3 * scale_factor))
        dot_r = max(4, int(7 * scale_factor))
        um_pp = self.calib.um_per_px(frame.shape[1])

        items = [(m, (0, 210, 255), False) for m in self.viewer.measurements]
        if self.viewer.calib_line:
            items.append((self.viewer.calib_line, (255, 210, 80), True))

        for m, color, is_calib in items:
            p1 = tuple(int(round(c)) for c in m.p1)
            p2 = tuple(int(round(c)) for c in m.p2)
            if m.kind == "circle":
                geom = m.circle_geometry()
                if not geom:
                    continue
                (cx, cy), r_px = geom
                center = (int(round(cx)), int(round(cy)))
                rad = max(1, int(round(r_px)))
                cv2.circle(out, center, rad, color, line_w, cv2.LINE_AA)
                mid = (center[0], max(20, center[1] - rad - 12))
            elif m.kind == "rect":
                corners, _, _, _ = m.rect_geometry()
                pts_arr = np.array(
                    [[int(round(c[0])), int(round(c[1]))] for c in corners],
                    dtype=np.int32,
                )
                cv2.polylines(out, [pts_arr], True, color, line_w, cv2.LINE_AA)
                mid = (
                    int(round(sum(c[0] for c in corners) / 4.0)),
                    max(20, int(round(min(c[1] for c in corners))) - 12),
                )
            elif m.kind == "polyline":
                segs = m.polyline_segments()
                for a, b, seg_px in segs:
                    pa = (int(round(a[0])), int(round(a[1])))
                    pb = (int(round(b[0])), int(round(b[1])))
                    cv2.line(out, pa, pb, color, line_w, cv2.LINE_AA)
                    cv2.circle(out, pa, dot_r, color, max(1, line_w // 2), cv2.LINE_AA)
                    cv2.circle(out, pb, dot_r, color, max(1, line_w // 2), cv2.LINE_AA)
                    stxt = format_scalar(seg_px, um_pp, unit=self.viewer.unit_mode).replace("µ", "u")
                    smid = ((pa[0] + pb[0]) // 2, (pa[1] + pb[1]) // 2 - 8)
                    cv2.putText(out, stxt, smid, cv2.FONT_HERSHEY_SIMPLEX, scale * 0.8, (0, 0, 0), thick_bg, cv2.LINE_AA)
                    cv2.putText(out, stxt, smid, cv2.FONT_HERSHEY_SIMPLEX, scale * 0.8, (255, 255, 255), thick_fg, cv2.LINE_AA)
                last_pt = segs[-1][1] if segs else m.p2
                mid = (int(round(last_pt[0])), max(20, int(round(last_pt[1])) - 18))
            else:
                cv2.line(out, p1, p2, color, line_w, cv2.LINE_AA)
                for q in (p1, p2):
                    cv2.circle(out, q, dot_r, color, max(1, line_w // 2), cv2.LINE_AA)
                mid = ((p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2 - 14)

            lines = format_measurement_lines(
                m,
                um_pp,
                unit=self.viewer.unit_mode,
                show_details=self.viewer.show_details,
                is_calib=is_calib,
            )
            # ASCII-safe fallback for Hershey font (µ -> u, ⌀ -> D=, ∠ -> ang, · -> |)
            for li, raw_txt in enumerate(lines):
                txt = (
                    raw_txt.replace("µ", "u")
                    .replace("⌀", "D=")
                    .replace("≈", "~")
                    .replace("×", "x")
                    .replace("²", "^2")
                    .replace("°", " deg")
                    .replace("∠", "ang")
                    .replace("·", "|")
                )
                (tw, th), _ = cv2.getTextSize(
                    txt, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thick_fg
                )
                tx = max(10, min(frame.shape[1] - tw - 10, mid[0] - tw // 2))
                ty = max(
                    th + 10,
                    min(frame.shape[0] - 10, mid[1] + li * int((th + 12))),
                )
                cv2.putText(
                    out,
                    txt,
                    (tx, ty),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    font_scale,
                    (0, 0, 0),
                    thick_bg,
                    cv2.LINE_AA,
                )
                cv2.putText(
                    out,
                    txt,
                    (tx, ty),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    font_scale,
                    color,
                    thick_fg,
                    cv2.LINE_AA,
                )
        return out

    def _record_toggled(self, on: bool) -> None:
        self.res_combo.setEnabled(not on)
        self.rec_settings_btn.setEnabled(not on)
        if on:
            try:
                path = self.recorder.start(
                    "h2641080",
                    self.cam.width,
                    self.cam.height,
                    self.cam.fps,
                    config=self.rec_config,
                )
            except RuntimeError as exc:
                QMessageBox.warning(self, "Recording failed", str(exc))
                self.rec_btn.setChecked(False)
                return
            self.rec_btn.setText("■ Stop")
            self._rec_started = time.monotonic()
            self._rec_timer.start()
            enc = f" · {self.recorder.encoder}" if self.recorder.encoder else ""
            self.statusBar().showMessage(f"Recording{enc} → {path}", 4000)
        else:
            self._rec_timer.stop()
            path = self.recorder.stop()
            self.rec_btn.setText("Record (R)")
            if path:
                n = self.recorder.frames_written
                self.statusBar().showMessage(f"Saved {n} frames → {path}", 8000)
            self.rec_label.setText("")

    def _rec_tick(self) -> None:
        if not self.recorder.active:
            self._rec_timer.stop()
            self.rec_btn.setChecked(False)
            self.rec_btn.setText("Record (R)")
            self.rec_label.setText("")
            self.statusBar().showMessage(
                "Recording stopped unexpectedly (encoder pipe closed)", 5000
            )
            return
        secs = int(time.monotonic() - self._rec_started)
        self.rec_label.setText(
            f"{secs // 60:02d}:{secs % 60:02d} · {self.recorder.frames_written} frames"
        )

    # -- view -----------------------------------------------------------------
    def _set_view(self, attr: str, value) -> None:
        setattr(self.viewer, attr, value)
        self.viewer._refresh()

    def _res_changed(self, index: int) -> None:
        if index < 0:
            return
        _, w, h, fps = RESOLUTIONS[index]
        if (w, h, fps) == (self.cam.width, self.cam.height, self.cam.fps):
            return
        self._pending_res = (w, h, fps)
        old = self.cam
        old.recorder = None
        old._running = False
        self._swap_when_released(tries=0)

    def _swap_when_released(self, tries: int) -> None:
        if self._pending_res is None:
            return
        old = self.cam
        if old.isRunning() and tries < 150:
            QTimer.singleShot(20, lambda: self._swap_when_released(tries + 1))
            return
        old.stop()
        old.deleteLater()
        w, h, fps = self._pending_res
        self._pending_res = None
        self.cam = CameraThread(old.device, w, h, fps, self)
        self.cam.decode_interval = 0.0 if self.viewer.low_latency else 1.0 / 30.0
        self.cam.recorder = self.recorder
        self.viewer.cam = self.cam
        self.cam.start()
        self._update_cal_info()

    def _toggle_help(self) -> None:
        self.viewer.show_help = not self.viewer.show_help
        self.viewer.update()

    def _escape(self) -> None:
        if self.viewer.show_help:
            self.viewer.show_help = False
            self.viewer.update()
            return
        if self.viewer.pending_pts:
            self.viewer.pending_pts.clear()
            self.viewer.update()
            return
        self.viewer.clear_measurement()
        self.viewer.calibrating = False
        self.viewer.measure_mode = False
        self.measure_btn.setChecked(False)
        self.viewer.set_measure_cursor()

    def open_board_scanner(self) -> None:
        dlg = getattr(self, "_scanner_dlg", None)
        if dlg is None:
            def _grab() -> np.ndarray | None:
                f = self.viewer.current_frame()
                if f is None:
                    return None
                return self.viewer.process_frame(f.copy())

            dlg = BoardScannerDialog(
                get_frame=_grab,
                lock_exposure=self._lock_exposure_for_scan,
                parent=self,
            )
            self._scanner_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _lock_exposure_for_scan(self, lock: bool) -> None:
        if lock:
            self._scan_prev_auto_exp = self.auto_exp_check.isChecked()
            if self._scan_prev_auto_exp:
                self.auto_exp_check.setChecked(False)
        else:
            if getattr(self, "_scan_prev_auto_exp", False):
                self.auto_exp_check.setChecked(True)
            self._scan_prev_auto_exp = False

    # -- lifecycle ---------------------------------------------------------
    def closeEvent(self, ev) -> None:
        self._pending_res = None
        dlg = getattr(self, "_scanner_dlg", None)
        if dlg is not None:
            dlg.close()
        self._save_settings()
        self._inhibitor.release()
        self._rec_timer.stop()
        if self.recorder.active:
            self.recorder.stop()
        self.cam.stop()
        super().closeEvent(ev)
