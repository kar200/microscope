# Bresun HH-4K MAX Microscope App

> **Note:** This project was **vibe-coded** with an AI coding assistant to build a fast, low-latency Linux desktop workbench for the **Bresun HH-4K MAX** (and compatible UVC 4K60 MJPEG USB microscopes).

A PySide6 + OpenCV + V4L2 + FFmpeg desktop application tailored for PCB inspection, SMD rework, calibrated geometric measurement, live board stitching, and hardware-accelerated video recording.

---

## Features

- **Low-Latency Live View (up to 4K @ 60 fps):**
  - Threaded V4L2 MJPEG capture with digital zoom (`0.25×`–`20×`), pan, freeze-frame (`Space`), crosshair/grid overlays, rotation (`0°/90°/180°/270°`), and mirror/flip.
  - Automatic screen-blanking inhibition while in fullscreen mode.
- **PCB & SMD Measurement Suite:**
  - **Line & Multi-Pin Pitch (`line`, `M`):** Point-to-point distance, `ΔX`/`ΔY`, angle, automatic SMD passive/package hints (`01005`–`2512`, `SOT-23`, `SOIC-8`), and `N`-pin pitch division.
  - **Multi-Point Ruler / Polygon (`polyline`, `N`):** Click continuously along multiple segments with live per-segment lengths and total length; hover back over the starting dot to auto-snap and close the loop for **Perimeter & Area (`mm²`)**.
  - **2-Point & 3-Point Circle (`circle`, `circle3`, `D`):** Pad/via/hole diameter, radius, and area (`mm²`).
  - **Tilted 3-Point Rectangle (`rect`, `B`):** Trace one edge to set width + tilt angle, then pull to the opposite edge for height — displays `W × H`, diagonal, area, tilt angle, and automatic 2D SMD footprint matching.
  - **Precision Magnifier Loupe (`G`)** & `Shift` 45° angle snapping.
  - Named calibration profiles persisted in `~/.config/Bresun/calibrations.json`.
- **Image Enhancement & Hardware V4L2 Controls:**
  - **Enhance IC Markings (`E`):** CLAHE adaptive local contrast to make faint laser-etched IC markings pop.
  - Monochrome high-contrast inspection mode, plus software Brightness, Contrast, Gamma, and Unsharp-Mask Sharpness.
  - Direct V4L2 hardware sensor focus, auto-focus, exposure, and power-line flicker (`50/60 Hz`) control via `v4l2-ctl`.
- **Board Scan / Live PCB Mosaic Stitcher (`P`):**
  - Pan across a PCB while the app tracks and blends sharp frames into a high-resolution full-board mosaic image.
- **Hardware-Accelerated Video Recording (`R`):**
  - Configurable codec (**H.265/HEVC**, **H.264/AVC**, **MJPEG**), backend (**VA-API**, **Vulkan Video**, **CPU**), GPU device selection (iGPU vs dGPU), resolution (`720p`, `1080p`, `4K`), framerate (`30`/`60 fps`), and rate control (**CQP**, **VBR**, **CBR**).
  - Includes [`tovideo.fish`](tovideo.fish), an interactive Fish shell helper to transcode recordings for archiving (HEVC/AV1 `.mkv`) or instant sharing on WhatsApp/Telegram/Discord (H.264 `.mp4` with `+faststart`).
- **Persistent Configuration:**
  - All UI, camera, and recording settings are saved automatically in `~/.config/Bresun/Microscope.conf` with a one-click **Reset All Defaults** option.

---

## System Requirements (Linux)

- **Python** 3.10+
- **System tools:** `ffmpeg` (for hardware-accelerated recording) and `v4l-utils` (`v4l2-ctl` for hardware sensor focus/exposure controls)
- **Python packages:** `PySide6`, `opencv-python`, `numpy`

On Arch Linux:
```bash
sudo pacman -S python pyside6 python-numpy ffmpeg v4l-utils
```

---

## Installation & Usage

### Option 1: Install as a User Launcher & Desktop Entry (Recommended)
Run the included installer script to set up local dependencies in `.deps/`, create `~/.local/bin/bresun-microscope`, and install the desktop entry at `~/.local/share/applications/microscope.desktop`:

```bash
chmod +x install.sh
./install.sh
```

You can then launch **Microscope** from your application menu or run `bresun-microscope` from any terminal.

### Option 2: Install as a Native Arch Linux System Package (`pacman`)
A [`PKGBUILD`](PKGBUILD) is included so you can build and install a native system package managed by `pacman`:

```bash
sudo pacman -S python-opencv python-build python-installer python-wheel python-setuptools
makepkg -si
```

### Option 3: Install as an Isolated User Package via `uv` or `pipx`
```bash
uv tool install .
# or
pipx install .
```

### Option 4: Run Directly from Source
```bash
uv pip install --target .deps opencv-python
PYTHONPATH=.deps:. python3 -m microscope_app
```

### Command-Line Options
```bash
python3 -m microscope_app --device /dev/video0 --width 3840 --height 2160 --fps 60
```

See [`docs/USAGE.md`](docs/USAGE.md) for the full keyboard shortcut reference and measurement workflow guide.
