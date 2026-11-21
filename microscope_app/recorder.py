"""Snapshot and video recording.

Recording runs on its own thread: the capture thread pushes frames into a
bounded deque; the writer pops and encodes. Two modes:

- mjpg4k — original 4K frames, MJPEG via cv2.VideoWriter at 30 fps.
- h2641080 — downscaled 1920x1080, HEVC (H.265) in .mkv via the system ffmpeg
  CLI at 60 fps. On AMD (VA-API) the encode runs on the Ryzen iGPU
  (/dev/dri/renderD129, hevc_vaapi CQP qp=28); otherwise it falls back to
  libx264, then to a cv2 writer.

4K MJPEG decode cannot sustain 60 fps in software, so the capture feed may
deliver fewer frames than the header rate. To keep playback speed honest,
the writer throttles: it never writes more frames than header_fps × elapsed
wall time (extras are dropped, so a 10 s recording always plays ~10 s).
"""
from __future__ import annotations

import glob
import os
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock, Thread

import cv2
import numpy as np

SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "Microscope")
PHOTO_DIR = os.path.join(SAVE_DIR, "Photos")   # snapshots
VIDEO_DIR = os.path.join(SAVE_DIR, "Videos")   # recordings

RECORD_MODES = {
    "mjpg4k": {"label": "MJPEG full-res @30 (large files)", "ext": ".mkv", "fps": 30},
    "h2641080": {"label": "H.265 1080p @60 (small files)", "ext": ".mkv", "fps": 60},
}


@dataclass
class RecordingConfig:
    codec: str = "hevc"          # "hevc", "h264", or "mjpeg"
    backend: str = "vaapi"       # "vaapi", "vulkan", or "cpu"
    resolution: str = "1080p"    # "720p", "1080p", or "source"
    fps: int = 60                # 30 or 60
    rc_mode: str = "CQP"         # "CQP", "VBR", or "CBR"
    qp: int = 28                 # 18..45 (higher = smaller file)
    bitrate_mbps: int = 4        # 1..40 Mbps (for VBR / CBR)

    def summary(self) -> str:
        res = "4K/Src" if self.resolution == "source" else self.resolution
        if self.codec == "mjpeg":
            return f"MJPEG · {res} @{self.fps}"
        cname = "H.265" if self.codec == "hevc" else "H.264"
        rc = f"CQP {self.qp}" if self.rc_mode == "CQP" else f"{self.rc_mode} {self.bitrate_mbps}M"
        return f"{cname} ({self.backend.upper()}) · {res} @{self.fps} · {rc}"

DEFAULT_VAAPI_DEVICE = "/dev/dri/renderD129"
BLOCKED_VAAPI_DEVICES: set[str] = set()


def _vaapi_candidates() -> list[str]:
    """Render nodes to try, in order. Prefers the Ryzen iGPU (/dev/dri/renderD129)
    by default, while still allowing explicit selection of any GPU."""
    nodes = []
    override = os.environ.get("MICROSCOPE_VAAPI_DEV")
    if override:
        nodes.append(override)
    if os.path.exists(DEFAULT_VAAPI_DEVICE):
        nodes.append(DEFAULT_VAAPI_DEVICE)
    nodes += [os.path.realpath(p)
              for p in sorted(glob.glob("/dev/dri/by-path/pci-*-render"))]
    nodes += sorted(glob.glob("/dev/dri/renderD*"))
    seen, ordered = set(), []
    for n in nodes:
        if n not in seen and n not in BLOCKED_VAAPI_DEVICES:
            seen.add(n)
            ordered.append(n)
    return ordered


def _vaapi_probe(node: str) -> bool:
    """Can ffmpeg encode hevc_vaapi on this render node?"""
    if node in BLOCKED_VAAPI_DEVICES:
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "bgr24",
             # keep >= hardware minimum (VCN HEVC requires width >= 130)
             "-s", "256x256", "-r", "30", "-i", "/dev/zero",
             "-frames:v", "3", "-vaapi_device", node,
             "-vf", "format=nv12,hwupload",
             "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", "28",
             "-f", "null", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


class Recorder:
    """Bounded-queue recorder fed from the capture thread."""

    def __init__(self) -> None:
        self._queue: deque[np.ndarray] = deque(maxlen=4)
        self._lock = Lock()
        self._thread: Thread | None = None
        self._writer: cv2.VideoWriter | None = None
        self._proc: subprocess.Popen | None = None  # system ffmpeg pipe
        self.path: str | None = None
        self.frames_written = 0
        self.mode: str | None = None
        self.encoder = ""  # human-readable encoder description
        # "auto" (first working GPU), "cpu", or a render node path
        self.preferred_node = "auto"
        self._w = self._h = 0
        self._header_fps = 30.0
        self._t0 = 0.0

    # -- called from capture thread --
    def try_put(self, frame: np.ndarray) -> None:
        with self._lock:
            if self._writer is not None or self._proc is not None:
                self._queue.append(frame)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._writer is not None or self._proc is not None

    def start(self, mode: str, src_w: int, src_h: int, src_fps: int,
              config: RecordingConfig | None = None) -> str:
        os.makedirs(VIDEO_DIR, exist_ok=True)
        if config is not None:
            self.path = os.path.join(VIDEO_DIR, f"microscope_{_timestamp()}.mkv")
            if config.resolution == "720p":
                w, h = 1280, 720
            elif config.resolution == "1080p":
                w, h = 1920, 1080
            else:
                w, h = src_w, src_h
            fps = int(config.fps)
            if config.codec == "mjpeg":
                if not self._start_ffmpeg_pipe(w, h, fps, codec="mjpeg", config=config):
                    self._start_cv2_writer(w, h, fps)
            else:
                if not self._start_ffmpeg_pipe(w, h, fps, codec=config.codec, config=config):
                    self._start_cv2_writer_fallback(w, h, min(fps, 30))
        else:
            cfg = RECORD_MODES[mode]
            self.path = os.path.join(VIDEO_DIR, f"microscope_{_timestamp()}{cfg['ext']}")
            if mode == "mjpg4k":
                w, h, fps = src_w, src_h, cfg["fps"]
                # ffmpeg's threaded MJPEG ≈ 56 fps at 4K on hard content, vs
                # ~6 fps through OpenCV's writer — always prefer the pipe.
                if not self._start_ffmpeg_pipe(w, h, fps, codec="mjpeg"):
                    self._start_cv2_writer(w, h, fps)
            else:
                w, h, fps = 1920, 1080, cfg["fps"]
                if not self._start_ffmpeg_pipe(w, h, fps, codec="hevc"):
                    # fallback: mpeg4 in mp4 via cv2 — needs a smaller fps header
                    self._start_cv2_writer_fallback(w, h, min(fps, 30))
        with self._lock:
            self._queue.clear()
            self.mode = mode
            self._header_fps = float(fps)
            self._t0 = time.monotonic()
            self.frames_written = 0
            self._w, self._h = w, h
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.path

    def _start_cv2_writer(self, w: int, h: int, fps: int) -> None:
        writer = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"MJPG"),
                                 float(fps), (w, h))
        if not writer.isOpened():
            writer.release()
            raise RuntimeError("Could not open MJPEG video writer")
        with self._lock:
            self._writer = writer

    def _start_cv2_writer_fallback(self, w: int, h: int, fps: int) -> None:
        writer = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"),
                                 float(fps), (w, h))
        if not writer.isOpened():
            writer.release()
            raise RuntimeError("Could not open any video writer")
        with self._lock:
            self._writer = writer

    def _resolve_vaapi_node(self) -> str:
        """Render node to encode on, honouring preferred_node ('' = CPU)."""
        pref = self.preferred_node or "auto"
        if pref == "cpu":
            return ""
        if pref != "auto" and _vaapi_probe(pref):
            return pref
        # auto, or the preferred node failed its probe → first working GPU
        return next((n for n in _vaapi_candidates() if _vaapi_probe(n)), "")

    def _start_ffmpeg_pipe(self, w: int, h: int, fps: int, codec: str,
                           config: RecordingConfig | None = None) -> bool:
        """Encode with the system ffmpeg via a rawvideo pipe."""
        cfg = config or RecordingConfig()
        if codec == "mjpeg":
            encode = ["-c:v", "mjpeg", "-q:v", "2",
                      "-threads", str(max(4, (os.cpu_count() or 4)))]
            self.encoder = "mjpeg (CPU, ffmpeg)"
        else:
            is_h264 = codec == "h264"
            backend = cfg.backend
            if self.preferred_node == "cpu":
                backend = "cpu"
            b = max(1, int(cfg.bitrate_mbps))
            qp = max(18, min(45, int(cfg.qp)))
            if backend == "vulkan":
                enc_name = "h264_vulkan" if is_h264 else "hevc_vulkan"
                if cfg.rc_mode in ("VBR", "CBR"):
                    rc_args = ["-rc_mode", cfg.rc_mode.lower(), "-b:v", f"{b}M"]
                    rc_desc = f"{cfg.rc_mode} {b}M"
                else:
                    rc_args = ["-qp", str(qp)]
                    rc_desc = f"CQP {qp}"
                encode = ["-init_hw_device", "vulkan",
                          "-vf", "format=nv12,hwupload",
                          "-c:v", enc_name, *rc_args]
                self.encoder = f"{enc_name} ({rc_desc})"
            elif backend == "vaapi":
                node = self._resolve_vaapi_node()
                if node:
                    enc_name = "h264_vaapi" if is_h264 else "hevc_vaapi"
                    if cfg.rc_mode == "VBR":
                        rc_args = ["-rc_mode", "VBR", "-b:v", f"{b}M",
                                   "-maxrate", f"{max(b + 1, int(round(b * 1.5)))}M"]
                        rc_desc = f"VBR {b}M"
                    elif cfg.rc_mode == "CBR":
                        rc_args = ["-rc_mode", "CBR", "-b:v", f"{b}M"]
                        rc_desc = f"CBR {b}M"
                    else:
                        rc_args = ["-rc_mode", "CQP", "-qp", str(qp)]
                        rc_desc = f"CQP {qp}"
                    encode = ["-vaapi_device", node,
                              "-vf", "format=nv12,hwupload",
                              "-c:v", enc_name, *rc_args]
                    self.encoder = f"{enc_name} ({rc_desc}, {node.rsplit('/', 1)[-1]})"
                else:
                    backend = "cpu"
            if backend == "cpu":
                enc_name = "libx264" if is_h264 else "libx265"
                if cfg.rc_mode in ("VBR", "CBR"):
                    rc_args = ["-b:v", f"{b}M"]
                    rc_desc = f"{cfg.rc_mode} {b}M"
                else:
                    rc_args = ["-crf", str(qp)]
                    rc_desc = f"CRF {qp}"
                encode = ["-c:v", enc_name, "-preset", "veryfast", *rc_args,
                          "-pix_fmt", "yuv420p"]
                self.encoder = f"{enc_name} (CPU, {rc_desc})"
        try:
            self._proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", *encode,
                 self.path],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._proc = None
            self.encoder = ""
            return False
        return True

    def _loop(self) -> None:
        try:
            while True:
                with self._lock:
                    writer = self._writer
                    proc = self._proc
                    if writer is None and proc is None:
                        break
                    queue_len = len(self._queue)
                    # If we are ahead of elapsed time and queue is not backing up,
                    # pause briefly rather than discarding a frame due to micro-jitter.
                    if (self.frames_written >= self._header_fps * (time.monotonic() - self._t0)
                            and queue_len <= 1):
                        wait_ahead = True
                        frame = None
                    else:
                        wait_ahead = False
                        frame = self._queue.popleft() if self._queue else None
                if wait_ahead:
                    time.sleep(0.002)
                    continue
                if frame is None:
                    time.sleep(0.002)
                    continue
                if self.frames_written >= self._header_fps * (time.monotonic() - self._t0):
                    continue
                if frame.shape[1] != self._w or frame.shape[0] != self._h:
                    frame = cv2.resize(frame, (self._w, self._h),
                                       interpolation=cv2.INTER_AREA)
                if writer is not None:
                    writer.write(frame)
                elif proc is not None and proc.stdin:
                    proc.stdin.write(frame.tobytes())
                else:
                    break
                self.frames_written += 1

                with self._lock:
                    queue_empty = not self._queue
                owed = self._header_fps * (time.monotonic() - self._t0)
                dups = 0
                while queue_empty and self.frames_written < owed - 1.0 and dups < 60:
                    if writer is not None:
                        writer.write(frame)
                    elif proc is not None and proc.stdin:
                        proc.stdin.write(frame.tobytes())
                    self.frames_written += 1
                    dups += 1
        except (BrokenPipeError, OSError):
            pass
        finally:
            with self._lock:
                self._writer = None
                self._proc = None

    def stop(self) -> str | None:
        with self._lock:
            if self._writer is None and self._proc is None:
                return self.path
            self._writer = None
            proc, self._proc = self._proc, None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            proc.wait(timeout=5.0)
        return self.path


def save_snapshot(frame: np.ndarray, fmt: str = "png", quality: int = 95) -> str:
    os.makedirs(PHOTO_DIR, exist_ok=True)
    path = os.path.join(PHOTO_DIR, f"microscope_{_timestamp()}.{fmt}")
    if fmt == "jpg":
        cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    else:
        cv2.imwrite(path, frame)
    return path

