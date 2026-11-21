# Bresun Microscope — User Guide

Desktop app for the **Bresun HH-4K MAX** USB microscope (Linux).
Live 4K/1080p60 view, software contrast & IC-marking enhancement, multi-mode
measurement (line, pin pitch, circle, rectangle) with calibration, magnifier
loupe, snapshots and GPU-accelerated recording — tuned for electronics/SMD
and PCB inspection.

---

## 1. Quick start

1. Power the microscope and plug in USB.
2. Launch **Microscope** from your application menu (or:
   `PYTHONPATH=.deps:. /usr/bin/python3.14 -m microscope_app` inside
   `~/Documents/microscope`).
3. Live video appears within ~2 seconds. Press **H** for the in-app shortcut
   overlay, or **Right-click** anywhere on the video canvas for the quick menu.

Capture starts at **1920×1080 @60** by default — fluid 60 fps on a 2K/4K screen.
Switch to **3840×2160 @60** in *View → Resolution* when you need maximum zoom
headroom or full-res 4K snapshots; the app remembers your settings between launches
and automatically rescales active calibrations and on-screen measurements when
you switch resolutions.

---

## 2. The interface

### Top Menu Bar & Right-Click Canvas Menu

* **File** — Take Snapshot (`S`), Start/Stop Recording (`R`), Open Captures Folder (`Ctrl+O`), Quit (`Ctrl+Q`).
* **Measure** — Toggle Measure Mode (`M`), switch tools (`Line / Pitch`, `Circle 2-pt ⌀`, `Circle 3-pt Rim`, `Rectangle W×H`), Calibrate (`C`), Toggle Magnifier Loupe (`G`), Undo Last (`Backspace`), Clear All (`Esc`).
* **View** — Freeze (`Space`), Enhance IC Markings / CLAHE (`E`), Monochrome mode, Crosshair, Grid, Low-Latency (`L`), Fullscreen (`F`/`F11`), Reset View, Reset Image.
* **Help** — Usage Guide, Keyboard Shortcuts (`H`).
* **Right-click on the video canvas** — Opens a quick context menu for measurement modes, freeze, snapshot, CLAHE, and view reset (especially handy in fullscreen).

### Video canvas (left)

| Element | Meaning |
|---|---|
| HUD (top-left) | fps · resolution · zoom · active calibration (`µm/px`) · active image filters · measurement prompt |
| Crosshair & Grid | Center crosshair and optional physical scale grid |
| Scale bar (bottom-right) | Physical length bar (`mm` / `µm` / `mil`); appears once a calibration is active |
| Magnifier Loupe | 4× pixel-exact inset around the cursor when placing or dragging measurement points (`G` toggles) |
| `❄ FROZEN` badge | Freeze-frame is on (`Space`) |
| `● REC` badge | Video recording in progress (`R`) |

### Control panel (right)

* **Measure & Calibrate** — Measure toggle (`M`), Calibrate (`C`), Clear, Tool selector (`Line / Pitch`, `Circle 2-pt`, `Circle 3-pt`, `Rectangle`), `Pin Pitch` divider (`1..64 pins`), `Units` (`Auto mm/µm`, `mm + mil`, `mm`, `µm`, `mil`), `ΔX/ΔY/∠/Area` details, `Loupe (G)`, multiple-measurement toggle, and saved calibration profiles.
* **Image & Contrast** — Real-time software enhancements (**Enhance IC Markings (CLAHE)**, **Monochrome**, **Brightness**, **Contrast**, **Gamma**, **Sharpness**) plus hardware sensor **Auto-Exposure**, **HW Exp**, and **Flicker** (`Off / 50 Hz / 60 Hz`).
* **Capture** — Snapshot (`S`), Record (`R`), Folder button, Codec & GPU Encoder selection, burn-measurements-into-snapshots option.
* **View** — Crosshair, Grid, Mirror H, Flip V, Rotate (`0°/90°/180°/270°`), Low-latency 60 Hz mode (`L`), Resolution selector, Reset View.

---

## 3. Keyboard & mouse reference

| Key / mouse | Action |
|---|---|
| `Space` | Freeze / unfreeze the live image |
| `S` | Snapshot — full-resolution PNG to `~/Pictures/Microscope/Photos` |
| `R` | Start / stop recording to `~/Pictures/Microscope/Videos` |
| `M` | Toggle Measure mode (Line / Pin Pitch) |
| `D` | Measure Circle / Diameter (2-point) |
| `B` | Measure Rectangle / Footprint (`W × H`) |
| `C` | Calibrate scale: click both ends of a known length |
| `Shift` (while measuring) | Snap line or calibration to `0° / 45° / 90°` |
| Drag endpoint dot | Fine-tune any existing measurement endpoint |
| `G` | Toggle 4× cursor magnifier loupe |
| `E` | Toggle **Enhance IC Markings (CLAHE)** local contrast |
| `Backspace` | Undo / remove the most recent measurement |
| `Esc` | Cancel pending click / clear all measurements / close help |
| `+` / `−` | Hardware exposure longer / shorter (switches to manual exposure) |
| `L` | Low-latency 60 Hz mode on/off |
| `F` / `F11` | Fullscreen (`Tab` slides the control-panel drawer in/out) |
| `H` | Toggle keyboard shortcut overlay |
| Mouse wheel | Zoom (`1.0×` to `12.0×`), anchored under the cursor |
| Left-drag | Pan view (or drag a measurement endpoint handle) |
| Right-click | Cancel pending measurement click, or open quick context menu |
| Double-click / middle-click | Reset zoom & pan |

---

## 4. Image Enhancement & Reading Faint IC Markings

The HH-4K MAX uses a mechanical C-mount lens (focused manually via the lens ring)
and a Z-Star Venus USB 2.0 UVC bridge whose internal UVC processing registers
have minimal effect over USB. To provide immediate, reliable image control,
the **Image & Contrast** panel runs real-time software processing on the video stream
(and automatically applies your settings to saved snapshots):

* **Enhance IC Markings (`E`)** — Applies CLAHE (Contrast Limited Adaptive Histogram
  Equalization) to reveal faint laser-etched part numbers on black IC packages, flux
  residues, and fine copper trace edges even under uneven ring-light glare.
* **Monochrome / High-Contrast** — Removes solder-mask color distraction for crisp
  grayscale inspection.
* **Gamma** — Increase above `1.00` to brighten dark PCB substrates and black IC bodies
  without blowing out shiny solder joints.
* **Brightness / Contrast / Sharpness** — Real-time LUT & unsharp-mask adjustments.
* **Sensor Auto-Exp & HW Exp** — Controls the camera sensor's integration time. In dim
  light, auto-exposure can lengthen exposure and drop FPS to ~25–30 fps; uncheck
  *Sensor Auto-Exp* and lower *HW Exp* to lock 60 fps, then use the LED ring light or
  software *Gamma / Brightness* to brighten the image.

---

## 5. Measurement & Calibration

Measurements are stored in resolution-independent coordinates and stay pinned to
your subject across zoom, pan, flip, rotation, and resolution changes (`1080p ↔ 4K`).

### Calibrating (1 minute)

1. Place a known reference under the lens: a steel ruler (`1.00 mm`), a standard
   `2.54 mm` (`100 mil`) pin header, an SOIC pin pitch (`1.27 mm` / `50 mil`), or a known
   SMD package (e.g. `SOIC-8 body = 3.90 mm`).
2. Press `C`, then click both ends of the reference length (hold `Shift` to snap horizontal/vertical).
3. In the **Calibrate Scale** dialog, pick a preset or enter the length in **`mm`**, **`mil`**, or **`µm`**, and name the profile.

### Measurement Tools

1. **Line / Pin Pitch (`M`)**:
   * Click point A and point B (hold `Shift` to lock `0° / 45° / 90°`).
   * Shows total distance, closest SMD package hint (`≈ 0402`, `≈ 0603`, `≈ 0805`, etc.),
     and `ΔX`, `ΔY`, and angle `∠`.
   * **Measuring IC Pin Pitch accurately**: Set **Pin Pitch** to the number of pins spanned
     (e.g. `8 pins`), then click from the center of Pin 1 to the center of Pin 8. The app
     draws tick marks at each pin position, divides the total span by `7 gaps`, and displays
     the exact per-pin pitch with standard pitch matching (`0.50mm`, `0.65mm`, `0.80mm`, `1.27mm / 50mil`, `2.54mm / 100mil`).
2. **Circle — 2-pt Diameter (`D`) & 3-pt Rim**:
   * **2-pt Diameter (`D`)**: Click opposite edges of a via, drill hole, or BGA pad to measure
     diameter `⌀`, radius `r`, and area (`mm²`).
   * **3-pt Rim**: Click any 3 points along a circle or arc perimeter to fit the exact circle.
3. **Rectangle — W × H (`B`)**:
   * Click two opposite corners of an SMD component body or pad to measure `Width × Height`,
     2D footprint match (`0201`..`2512`, `1206` vs `1210`, `SOT-23`, `SOIC-8`), diagonal, and area (`mm²`).

### Fine-Tuning & Multiple Measurements

* **Draggable endpoints**: Hover over any endpoint dot until the cursor turns into a hand,
  then left-drag to reposition that endpoint with the 4× magnifier loupe.
* **Multiple measurements**: With *Keep multiple measurements on screen* checked, each new
  measurement is added to the canvas. Press `Backspace` to remove the last measurement, or
  `Esc` / *Clear* to remove all.
* **Units**: Switch anytime between `Auto (mm / µm)`, `mm + mil (PCB)`, `mm`, `µm`, and `mil`.

---

## 6. Snapshots & Recording

* **Snapshots (`S`)** → `~/Pictures/Microscope/Photos/` (full-res PNG, including active
  software image enhancements and burned-in measurements if checked).
* **Recordings (`R`)** → `~/Pictures/Microscope/Videos/` (`H.264 1080p @60 .mp4` on AMD VA-API GPU or `MJPEG 4K @30 .mkv` on CPU).
* Click **Folder** in the *Capture* panel (or `Ctrl+O`) to open `~/Pictures/Microscope` in your file manager.
