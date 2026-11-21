"""Guided Full-Board Scanner & Rigid SIFT Stitcher.

Workflow:
  1. Square up your PCB using the alignment guide square, then capture Tile 1.
  2. Pick the step direction (Right / Down / Left / Up — arrow keys or buttons).
     A translucent 35% overlap ghost strip from the anchor tile appears on the
     target edge of the live feed, while real-time SIFT+RANSAC tracks your exact
     (X, Y, Angle) offset and draws a docking target square.
  3. Slide the PCB until the live square sits inside the target square (turns
     GREEN: "ALIGNED — STOP!"). It auto-snaps when steady (or press Space).
  4. Click "Finish & Stitch" to run pairwise SIFT+RANSAC across all overlapping
     neighbors (including row-to-row loop closures), solve global least-squares
     positions, and composite with sharp seam blending (no ghosting, no warping).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import QProcess, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .recorder import SAVE_DIR

# Overlap fraction between adjacent guided tiles (0.40 = 40% overlap, 60% step)
OVERLAP_FRAC = 0.40
STEP_FRAC = 1.0 - OVERLAP_FRAC

DIRECTIONS = {
    "right": (+1, 0, "→ Right"),
    "down": (0, +1, "↓ Down"),
    "left": (-1, 0, "← Left"),
    "up": (0, -1, "↑ Up"),
}


@dataclass
class ScanTile:
    image: np.ndarray  # BGR uint8 at chosen scan scale
    x: float  # global top-left X in tile pixels
    y: float  # global top-left Y in tile pixels
    angle_deg: float  # rotation relative to tile 0 (degrees)
    kp_pts: np.ndarray  # (N, 2) float32 SIFT keypoint coords in match-image space
    des: np.ndarray | None  # (N, 128) float32 SIFT descriptors
    match_scale: float  # match_width / tile_width


@dataclass
class MatchResult:
    ok: bool
    dx: float = 0.0  # tile B top-left X minus tile A top-left X (in tile pixels)
    dy: float = 0.0  # tile B top-left Y minus tile A top-left Y (in tile pixels)
    angle_deg: float = 0.0  # rotation of B relative to A (degrees)
    inliers: int = 0


class SiftMatcher:
    """Fast, glare-resistant SIFT + rigid affine (RANSAC) pairwise aligner."""

    MATCH_WIDTH = 720

    def __init__(self, nfeatures: int = 2000):
        # Lower contrastThreshold picks up subtle PCB traces & silkscreen
        self._sift = cv2.SIFT_create(nfeatures=nfeatures, contrastThreshold=0.025)
        self._bf = cv2.BFMatcher(cv2.NORM_L2)

    def extract(
        self, tile_bgr: np.ndarray, roi: tuple[int, int, int, int] | None = None
    ) -> tuple[np.ndarray, np.ndarray | None, float]:
        """Extract SIFT features. Optional roi=(x1, y1, x2, y2) in match-image coords."""
        h, w = tile_bgr.shape[:2]
        mw = min(self.MATCH_WIDTH, w)
        scale = mw / float(w)
        mh = max(32, int(round(h * scale)))
        small = cv2.resize(tile_bgr, (mw, mh), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        # Local contrast normalization suppresses smooth ring-light vignetting
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        mask = None
        if roi is not None:
            x1, y1, x2, y2 = roi
            mask = np.zeros((mh, mw), dtype=np.uint8)
            mask[max(0, y1) : min(mh, y2), max(0, x1) : min(mw, x2)] = 255
        kps, des = self._sift.detectAndCompute(gray, mask)
        if not kps or des is None:
            return np.empty((0, 2), dtype=np.float32), None, scale
        pts = np.array([kp.pt for kp in kps], dtype=np.float32)
        return pts, des, scale

    def _filter_roi(
        self,
        pts: np.ndarray,
        des: np.ndarray | None,
        roi: tuple[int, int, int, int],
    ) -> tuple[np.ndarray, np.ndarray | None]:
        if des is None or len(pts) == 0:
            return pts, des
        x1, y1, x2, y2 = roi
        keep = (
            (pts[:, 0] >= x1)
            & (pts[:, 0] <= x2)
            & (pts[:, 1] >= y1)
            & (pts[:, 1] <= y2)
        )
        if not np.any(keep):
            return np.empty((0, 2), dtype=np.float32), None
        return pts[keep], des[keep]

    def match(
        self,
        pts_a: np.ndarray,
        des_a: np.ndarray | None,
        pts_b: np.ndarray,
        des_b: np.ndarray | None,
        match_scale: float,
        min_inliers: int = 8,
        max_angle_deg: float = 15.0,
    ) -> MatchResult:
        """Compute rigid transform mapping B -> A (returns camera shift dx, dy, angle)."""
        if des_a is None or des_b is None or len(des_a) < 6 or len(des_b) < 6:
            return MatchResult(ok=False)
        knn = self._bf.knnMatch(des_b, des_a, k=2)
        good = []
        for pair in knn:
            if len(pair) == 2 and pair[0].distance < 0.80 * pair[1].distance:
                good.append(pair[0])
        if len(good) < min_inliers:
            return MatchResult(ok=False, inliers=len(good))

        src_b = np.array([pts_b[m.queryIdx] for m in good], dtype=np.float32)
        dst_a = np.array([pts_a[m.trainIdx] for m in good], dtype=np.float32)

        M, mask = cv2.estimateAffinePartial2D(
            src_b,
            dst_a,
            method=cv2.RANSAC,
            ransacReprojThreshold=5.0,
            maxIters=2000,
            confidence=0.99,
        )
        if M is None or mask is None:
            return MatchResult(ok=False)

        inliers = int(mask.sum())
        if inliers < min_inliers:
            return MatchResult(ok=False, inliers=inliers)

        # Reject wild scale distortion (flat PCB at fixed focus has scale ~ 1.0)
        scale_est = math.hypot(M[0, 0], M[1, 0])
        if not (0.90 <= scale_est <= 1.10):
            return MatchResult(ok=False, inliers=inliers)

        angle_deg = math.degrees(math.atan2(M[1, 0], M[0, 0]))
        if abs(angle_deg) > max_angle_deg:
            return MatchResult(ok=False, inliers=inliers)

        dx = float(M[0, 2]) / match_scale
        dy = float(M[1, 2]) / match_scale
        return MatchResult(ok=True, dx=dx, dy=dy, angle_deg=angle_deg, inliers=inliers)


class BoardScanEngine:
    """Manages tiles, guided docking targets, live SIFT tracking, and coverage map."""

    def __init__(self, scale: float = 0.5):
        self.scale = scale
        self.tiles: list[ScanTile] = []
        self.direction: str = "right"
        self.matcher = SiftMatcher(nfeatures=2000)
        self._tile_w: int = 0
        self._tile_h: int = 0
        self.last_match: MatchResult = MatchResult(ok=False)
        self._aligned_since: float | None = None

    def _directional_rois(
        self, mscale: float
    ) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        """Return (anchor_roi, live_roi) in match-image pixels for current direction."""
        mw = int(round(self._tile_w * mscale))
        mh = int(round(self._tile_h * mscale))
        half = 0.60  # generous 60% strip covering the 40% target overlap + margin
        if self.direction == "right":
            return (int(mw * (1 - half)), 0, mw, mh), (0, 0, int(mw * half), mh)
        if self.direction == "left":
            return (0, 0, int(mw * half), mh), (int(mw * (1 - half)), 0, mw, mh)
        if self.direction == "down":
            return (0, int(mh * (1 - half)), mw, mh), (0, 0, mw, int(mh * half))
        return (0, 0, mw, int(mh * half)), (0, int(mh * (1 - half)), mw, mh)

    def _match_against_anchor(
        self,
        anchor: ScanTile,
        pts_b: np.ndarray,
        des_b: np.ndarray | None,
        mscale: float,
    ) -> MatchResult:
        """Try directional overlap-strip SIFT first (eliminates distractors), plus full-frame."""
        roi_a, roi_b = self._directional_rois(mscale)
        pa, da = self.matcher._filter_roi(anchor.kp_pts, anchor.des, roi_a)
        pb, db = self.matcher._filter_roi(pts_b, des_b, roi_b)
        res_roi = self.matcher.match(pa, da, pb, db, mscale, min_inliers=8)
        if res_roi.ok and res_roi.inliers >= 12:
            return res_roi
        res_full = self.matcher.match(
            anchor.kp_pts, anchor.des, pts_b, des_b, mscale, min_inliers=8
        )
        if res_roi.ok and (not res_full.ok or res_roi.inliers >= res_full.inliers):
            return res_roi
        return res_full

    def reset(self, scale: float | None = None) -> None:
        if scale is not None:
            self.scale = scale
        self.tiles.clear()
        self.direction = "right"
        self._tile_w = 0
        self._tile_h = 0
        self.last_match = MatchResult(ok=False)
        self._aligned_since = None

    def _scale_frame(self, frame_bgr: np.ndarray) -> np.ndarray:
        if self.scale >= 0.99:
            return frame_bgr.copy()
        h, w = frame_bgr.shape[:2]
        nw = max(64, int(round(w * self.scale)))
        nh = max(64, int(round(h * self.scale)))
        return cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_AREA)

    def target_step(self) -> tuple[float, float]:
        """Desired (dx, dy) from the last tile to the next stop point."""
        sx, sy, _ = DIRECTIONS[self.direction]
        return sx * STEP_FRAC * self._tile_w, sy * STEP_FRAC * self._tile_h

    def evaluate_live(self, frame_bgr: np.ndarray) -> tuple[MatchResult, bool, bool]:
        """Match live frame against anchor tile.

        Returns (match_result, in_target_zone, auto_snap_ready).
        """
        tile_img = self._scale_frame(frame_bgr)
        h, w = tile_img.shape[:2]
        if self._tile_w == 0:
            self._tile_w, self._tile_h = w, h

        if not self.tiles:
            self.last_match = MatchResult(ok=True)
            self._aligned_since = None
            return self.last_match, False, False

        anchor = self.tiles[-1]
        pts_b, des_b, mscale = self.matcher.extract(tile_img)
        res = self._match_against_anchor(anchor, pts_b, des_b, mscale)
        self.last_match = res

        if not res.ok:
            self._aligned_since = None
            return res, False, False

        tdx, tdy = self.target_step()
        err_x = abs(res.dx - tdx) / max(1, self._tile_w)
        err_y = abs(res.dy - tdy) / max(1, self._tile_h)
        in_zone = err_x <= 0.10 and err_y <= 0.10 and abs(res.angle_deg) <= 2.5

        now = time.monotonic()
        if in_zone:
            if self._aligned_since is None:
                self._aligned_since = now
            auto_ready = (now - self._aligned_since) >= 0.45
        else:
            self._aligned_since = None
            auto_ready = False
        return res, in_zone, auto_ready

    def capture_tile(self, frame_bgr: np.ndarray, force: bool = False) -> tuple[bool, str]:
        """Capture current frame as a tile. Rejects unlocked frames unless force=True."""
        tile_img = self._scale_frame(frame_bgr)
        h, w = tile_img.shape[:2]
        self._tile_w, self._tile_h = w, h
        pts, des, mscale = self.matcher.extract(tile_img)

        if not self.tiles:
            self.tiles.append(
                ScanTile(
                    image=tile_img,
                    x=0.0,
                    y=0.0,
                    angle_deg=0.0,
                    kp_pts=pts,
                    des=des,
                    match_scale=mscale,
                )
            )
            self._aligned_since = None
            return True, "Tile 1 captured (reference origin)"

        # Match against the last tile first (with directional ROI boost); fallback to recent tiles
        best_res = MatchResult(ok=False)
        best_anchor: ScanTile | None = None
        for idx_c, cand in enumerate(reversed(self.tiles[-6:])):
            r = (
                self._match_against_anchor(cand, pts, des, mscale)
                if idx_c == 0
                else self.matcher.match(cand.kp_pts, cand.des, pts, des, mscale, min_inliers=8)
            )
            if r.ok and r.inliers > best_res.inliers:
                best_res = r
                best_anchor = cand
                if idx_c == 0 and r.inliers >= 16:
                    break

        if not best_res.ok or best_anchor is None:
            if not force:
                return False, "No overlap lock — slide slightly closer to the previous tile"
            tdx, tdy = self.target_step()
            gx = self.tiles[-1].x + tdx
            gy = self.tiles[-1].y + tdy
            ang = 0.0
        else:
            gx = best_anchor.x + best_res.dx
            gy = best_anchor.y + best_res.dy
            ang = best_anchor.angle_deg + best_res.angle_deg

        self.tiles.append(
            ScanTile(
                image=tile_img,
                x=gx,
                y=gy,
                angle_deg=ang,
                kp_pts=pts,
                des=des,
                match_scale=mscale,
            )
        )
        self._aligned_since = None
        return True, f"Tile {len(self.tiles)} locked ({best_res.inliers} matches, {best_res.angle_deg:+.1f}°)"

    def undo_last(self) -> bool:
        if not self.tiles:
            return False
        self.tiles.pop()
        self._aligned_since = None
        return True

    # ---- live guided overlay ------------------------------------------------
    def render_live_guide(
        self,
        frame_bgr: np.ndarray,
        show_ghost: bool = True,
        in_zone: bool = False,
        display_w: int = 760,
    ) -> np.ndarray:
        """Draw alignment square, overlap ghost strip, and live docking reticle."""
        h, w = frame_bgr.shape[:2]
        scale = min(1.0, display_w / float(w))
        dw = max(160, int(round(w * scale)))
        dh = max(120, int(round(h * scale)))
        vis = cv2.resize(frame_bgr, (dw, dh), interpolation=cv2.INTER_AREA)

        if not self.tiles:
            # Square-up guide for Tile 1: crisp alignment box + rule-of-thirds + crosshair
            mx, my = int(dw * 0.12), int(dh * 0.12)
            cv2.rectangle(vis, (mx, my), (dw - mx, dh - my), (0, 220, 255), 2)
            # Inner grid lines to square up PCB edges / IC rows
            for frac in (0.333, 0.5, 0.667):
                gx = int(dw * frac)
                gy = int(dh * frac)
                col = (0, 190, 220) if abs(frac - 0.5) < 0.01 else (70, 120, 140)
                cv2.line(vis, (gx, my), (gx, dh - my), col, 1)
                cv2.line(vis, (mx, gy), (dw - mx, gy), col, 1)
            _draw_banner(
                vis,
                "Align PCB edge straight with the square -> press Space for Tile 1",
                (0, 210, 255),
            )
            return vis

        # 1. Translucent 35% overlap ghost strip from the anchor tile on the expected edge
        if show_ghost:
            anchor_img = cv2.resize(
                self.tiles[-1].image, (dw, dh), interpolation=cv2.INTER_AREA
            )
            ow = max(16, int(round(dw * OVERLAP_FRAC)))
            oh = max(16, int(round(dh * OVERLAP_FRAC)))
            alpha = 0.42
            if self.direction == "right":
                vis[:, :ow] = cv2.addWeighted(vis[:, :ow], 1 - alpha, anchor_img[:, dw - ow :], alpha, 0)
                cv2.line(vis, (ow, 0), (ow, dh), (0, 220, 255), 1, cv2.LINE_AA)
            elif self.direction == "left":
                vis[:, dw - ow :] = cv2.addWeighted(vis[:, dw - ow :], 1 - alpha, anchor_img[:, :ow], alpha, 0)
                cv2.line(vis, (dw - ow, 0), (dw - ow, dh), (0, 220, 255), 1, cv2.LINE_AA)
            elif self.direction == "down":
                vis[:oh, :] = cv2.addWeighted(vis[:oh, :], 1 - alpha, anchor_img[dh - oh :, :], alpha, 0)
                cv2.line(vis, (0, oh), (dw, oh), (0, 220, 255), 1, cv2.LINE_AA)
            elif self.direction == "up":
                vis[dh - oh :, :] = cv2.addWeighted(vis[dh - oh :, :], 1 - alpha, anchor_img[:oh, :], alpha, 0)
                cv2.line(vis, (0, dh - oh), (dw, dh - oh), (0, 220, 255), 1, cv2.LINE_AA)

        # 2. Docking target square in the center of the view
        cx, cy = dw // 2, dh // 2
        box_r = max(28, min(dw, dh) // 9)
        target_col = (50, 240, 100) if in_zone else (0, 210, 255)
        cv2.rectangle(
            vis, (cx - box_r, cy - box_r), (cx + box_r, cy + box_r), target_col, 2
        )
        cv2.drawMarker(vis, (cx, cy), target_col, cv2.MARKER_CROSS, 14, 1)

        m = self.last_match
        if m.ok:
            tdx, tdy = self.target_step()
            # Error in normalized tile units: how far we still need to move the camera
            rem_x = (tdx - m.dx) / max(1, self._tile_w)
            rem_y = (tdy - m.dy) / max(1, self._tile_h)
            # Moving square offset: lands directly inside center target box when rem == 0
            ox = int(np.clip(rem_x * dw * 0.45, -dw * 0.42, dw * 0.42))
            oy = int(np.clip(rem_y * dh * 0.45, -dh * 0.42, dh * 0.42))
            mx, my = cx + ox, cy + oy
            mr = box_r - 6
            live_col = (50, 240, 100) if in_zone else (40, 200, 255)
            cv2.rectangle(vis, (mx - mr, my - mr), (mx + mr, my + mr), live_col, 2)
            if not in_zone:
                cv2.arrowedLine(vis, (cx, cy), (mx, my), live_col, 2, tipLength=0.25)

            if in_zone:
                msg = f"ALIGNED — STOP! ({m.inliers} pts, {m.angle_deg:+.1f} deg)"
                _draw_banner(vis, msg, (40, 220, 90))
            else:
                msg = (
                    f"Locked ({m.inliers} pts) | Move box into target "
                    f"dX:{rem_x*100:+.0f}% dY:{rem_y*100:+.0f}% ang:{m.angle_deg:+.1f} deg"
                )
                _draw_banner(vis, msg, (0, 195, 240))
        else:
            _draw_banner(
                vis,
                f"NO LOCK ({m.inliers} pts) — slide slightly closer to ghost edge",
                (70, 90, 255),
            )

        return vis

    def render_coverage_map(self, max_dim: int = 380) -> np.ndarray | None:
        """Render a compact overview map of all captured tiles."""
        if not self.tiles or self._tile_w == 0:
            return None
        w, h = self._tile_w, self._tile_h
        xs = [t.x for t in self.tiles]
        ys = [t.y for t in self.tiles]
        min_x, min_y = min(xs), min(ys)
        max_x, max_y = max(xs) + w, max(ys) + h
        span_w, span_h = max(1.0, max_x - min_x), max(1.0, max_y - min_y)
        pad = 16
        s = min((max_dim - 2 * pad) / span_w, (max_dim - 2 * pad) / span_h, 1.0)
        cw = max(120, int(round(span_w * s)) + 2 * pad)
        ch = max(120, int(round(span_h * s)) + 2 * pad)
        canvas = np.full((ch, cw, 3), 24, dtype=np.uint8)
        tw = max(8, int(round(w * s)))
        th = max(8, int(round(h * s)))
        for idx, t in enumerate(self.tiles):
            px = pad + int(round((t.x - min_x) * s))
            py = pad + int(round((t.y - min_y) * s))
            thumb = cv2.resize(t.image, (tw, th), interpolation=cv2.INTER_AREA)
            x1, y1 = max(0, px), max(0, py)
            x2, y2 = min(cw, px + tw), min(ch, py + th)
            if x2 > x1 and y2 > y1:
                canvas[y1:y2, x1:x2] = thumb[y1 - py : y2 - py, x1 - px : x2 - px]
                col = (50, 230, 100) if idx == len(self.tiles) - 1 else (90, 120, 150)
                cv2.rectangle(canvas, (x1, y1), (x2 - 1, y2 - 1), col, 1)
                cv2.putText(
                    canvas,
                    str(idx + 1),
                    (x1 + 4, min(y2 - 4, y1 + 14)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.42,
                    (240, 245, 255),
                    1,
                    cv2.LINE_AA,
                )
        return canvas


def _draw_banner(img: np.ndarray, text: str, color: tuple[int, int, int]) -> None:
    h, w = img.shape[:2]
    cv2.rectangle(img, (0, 0), (w, 28), (18, 18, 22), -1)
    cv2.putText(
        img,
        text,
        (10, 19),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        color,
        1,
        cv2.LINE_AA,
    )


# ---- Global Rigid Pose Optimization + Sharp Seam Blending -------------------
def _rotate_image_keep_size(img: np.ndarray, angle_deg: float) -> tuple[np.ndarray, np.ndarray]:
    h, w = img.shape[:2]
    if abs(angle_deg) < 0.08:
        return img, np.full((h, w), 255, dtype=np.uint8)
    M = cv2.getRotationMatrix2D((w * 0.5, h * 0.5), -angle_deg, 1.0)
    rot = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    mask = cv2.warpAffine(
        np.full((h, w), 255, dtype=np.uint8),
        M,
        (w, h),
        flags=cv2.INTER_NEAREST,
        borderValue=0,
    )
    return rot, mask


def _crop_black_borders(img: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    coords = cv2.findNonZero(valid_mask)
    if coords is None:
        return img
    x, y, w, h = cv2.boundingRect(coords)
    return img[y : y + h, x : x + w]


def stitch_tiles_rigid(tiles: list[ScanTile]) -> tuple[np.ndarray, str]:
    """Globally align all overlapping tile pairs via SIFT+RANSAC and seam-blend."""
    n = len(tiles)
    if n == 1:
        return tiles[0].image.copy(), "single tile"

    # 1. De-rotate each tile by its tiny tracked angle so all tiles are 0 deg
    images: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    matcher = SiftMatcher(nfeatures=1400)
    feats: list[tuple[np.ndarray, np.ndarray | None, float]] = []

    for t in tiles:
        rot, msk = _rotate_image_keep_size(t.image, t.angle_deg)
        images.append(rot)
        masks.append(msk)
        feats.append(matcher.extract(rot))

    h, w = images[0].shape[:2]

    # 2. Match every pair of tiles whose initial positions overlap by >= 12%
    #    Restricting descriptors to the expected overlap ROI (+margin) eliminates
    #    repetitive SMD pad distractors outside the overlap zone.
    edges: list[tuple[int, int, float, float, float]] = []  # (i, j, dx, dy, weight)
    for i in range(n):
        for j in range(i + 1, n):
            init_dx = tiles[j].x - tiles[i].x
            init_dy = tiles[j].y - tiles[i].y
            ox = w - abs(init_dx)
            oy = h - abs(init_dy)
            if ox > 0.12 * w and oy > 0.12 * h:
                pts_i, des_i, sc_i = feats[i]
                pts_j, des_j, _ = feats[j]
                pad_x = 0.18 * w
                pad_y = 0.18 * h
                # Expected overlap rectangle in tile i and tile j coords
                ix1 = max(0.0, init_dx - pad_x) * sc_i
                iy1 = max(0.0, init_dy - pad_y) * sc_i
                ix2 = min(float(w), init_dx + w + pad_x) * sc_i
                iy2 = min(float(h), init_dy + h + pad_y) * sc_i
                jx1 = max(0.0, -init_dx - pad_x) * sc_i
                jy1 = max(0.0, -init_dy - pad_y) * sc_i
                jx2 = min(float(w), -init_dx + w + pad_x) * sc_i
                jy2 = min(float(h), -init_dy + h + pad_y) * sc_i
                pi_r, di_r = matcher._filter_roi(pts_i, des_i, (int(ix1), int(iy1), int(ix2), int(iy2)))
                pj_r, dj_r = matcher._filter_roi(pts_j, des_j, (int(jx1), int(jy1), int(jx2), int(jy2)))
                res = matcher.match(pi_r, di_r, pj_r, dj_r, sc_i, min_inliers=8)
                if not res.ok:
                    res = matcher.match(pts_i, des_i, pts_j, des_j, sc_i, min_inliers=8)
                if res.ok:
                    if math.hypot(res.dx - init_dx, res.dy - init_dy) < 0.45 * max(w, h):
                        edges.append((i, j, res.dx, res.dy, float(res.inliers)))

    # 3. Solve global least-squares positions (anchoring tile 0 at (0, 0))
    xs = np.array([t.x for t in tiles], dtype=np.float64)
    ys = np.array([t.y for t in tiles], dtype=np.float64)

    if edges:
        rows_A = []
        bx = []
        by = []
        # Strong anchor for tile 0
        anchor_row = np.zeros(n, dtype=np.float64)
        anchor_row[0] = 1000.0
        rows_A.append(anchor_row)
        bx.append(0.0)
        by.append(0.0)

        for i, j, dx, dy, wt in edges:
            r = np.zeros(n, dtype=np.float64)
            sw = math.sqrt(wt)
            r[j] = sw
            r[i] = -sw
            rows_A.append(r)
            bx.append(dx * sw)
            by.append(dy * sw)

        # Weak prior to keep any disconnected tile at its initial position
        for i in range(1, n):
            r = np.zeros(n, dtype=np.float64)
            r[i] = 0.05
            r[i - 1] = -0.05
            rows_A.append(r)
            bx.append((tiles[i].x - tiles[i - 1].x) * 0.05)
            by.append((tiles[i].y - tiles[i - 1].y) * 0.05)

        A = np.vstack(rows_A)
        xs, *_ = np.linalg.lstsq(A, np.array(bx), rcond=None)
        ys, *_ = np.linalg.lstsq(A, np.array(by), rcond=None)

    # 4. Compute output canvas size (cap extreme sizes safely)
    min_x = float(xs.min())
    min_y = float(ys.min())
    max_x = float((xs + w).max())
    max_y = float((ys + h).max())
    cw = int(math.ceil(max_x - min_x)) + 2
    ch = int(math.ceil(max_y - min_y)) + 2

    max_side = 12000
    cscale = 1.0
    if max(cw, ch) > max_side:
        cscale = max_side / float(max(cw, ch))
        cw = int(math.ceil(cw * cscale))
        ch = int(math.ceil(ch * cscale))

    # 5. Sharp Voronoi / nearest-center seam compositing with narrow 18 px feather
    # Each pixel strongly prefers the tile whose center is closest, preventing ghosting!
    acc = np.zeros((ch, cw, 3), dtype=np.float32)
    w_acc = np.zeros((ch, cw, 1), dtype=np.float32)
    valid_union = np.zeros((ch, cw), dtype=np.uint8)

    # Precompute sharp-peaked distance weight (steep power = razor-sharp seam, ~18px blend)
    tw_b = max(4, int(round(w * cscale)))
    th_b = max(4, int(round(h * cscale)))
    border = np.ones((th_b, tw_b), dtype=np.uint8)
    border[0, :] = 0
    border[-1, :] = 0
    border[:, 0] = 0
    border[:, -1] = 0
    dist_base = cv2.distanceTransform(border, cv2.DIST_L2, 3)
    dist_base = (dist_base / max(1e-6, float(dist_base.max()))) ** 8.0
    dist_base = np.clip(dist_base, 1e-6, 1.0)[..., None].astype(np.float32)

    for idx in range(n):
        img = images[idx]
        msk = masks[idx]
        if cscale < 0.999:
            img = cv2.resize(img, (tw_b, th_b), interpolation=cv2.INTER_AREA)
            msk = cv2.resize(msk, (tw_b, th_b), interpolation=cv2.INTER_NEAREST)
        th, tw = img.shape[:2]
        px = max(0, int(round((xs[idx] - min_x) * cscale)))
        py = max(0, int(round((ys[idx] - min_y) * cscale)))
        x2 = min(cw, px + tw)
        y2 = min(ch, py + th)
        if x2 <= px or y2 <= py:
            continue
        sub_img = img[: y2 - py, : x2 - px].astype(np.float32)
        sub_msk = (msk[: y2 - py, : x2 - px] > 127).astype(np.float32)[..., None]
        sub_w = dist_base[: y2 - py, : x2 - px] * sub_msk
        acc[py:y2, px:x2] += sub_img * sub_w
        w_acc[py:y2, px:x2] += sub_w
        valid_union[py:y2, px:x2] = np.maximum(
            valid_union[py:y2, px:x2], msk[: y2 - py, : x2 - px]
        )

    out = np.zeros((ch, cw, 3), dtype=np.uint8)
    valid = w_acc[..., 0] > 1e-7
    out[valid] = np.clip(acc[valid] / w_acc[valid], 0, 255).astype(np.uint8)
    out = _crop_black_borders(out, valid_union)
    return out, f"SIFT rigid global solve ({len(edges)} links)"


class BoardStitchWorker(QThread):
    finished_ok = Signal(object, str, str)
    failed = Signal(str)

    def __init__(self, tiles: list[ScanTile], parent=None):
        super().__init__(parent)
        self._tiles = list(tiles)

    def run(self) -> None:
        try:
            stitched, method = stitch_tiles_rigid(self._tiles)
            os.makedirs(SAVE_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SAVE_DIR, f"board_scan_{ts}.png")
            if not cv2.imwrite(path, stitched):
                raise RuntimeError(f"Failed to write {path}")
            self.finished_ok.emit(stitched, path, method)
        except Exception as exc:
            self.failed.emit(str(exc))


# ---- UI Dialog --------------------------------------------------------------
class BoardScannerDialog(QDialog):
    """Guided docking-square scanner window for clean full-PCB reconstruction."""

    def __init__(
        self,
        get_frame: Callable[[], np.ndarray | None],
        lock_exposure: Callable[[bool], None],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Board Scanner — Guided Square Stitcher")
        self.resize(1120, 680)
        self._get_frame = get_frame
        self._lock_exposure = lock_exposure
        self._exposure_locked = False
        self._scanning = False
        self._last_saved_path: str | None = None
        self._worker: BoardStitchWorker | None = None
        self.engine = BoardScanEngine(scale=0.5)
        self._dir_buttons: dict[str, QPushButton] = {}

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(120)  # ~8 Hz SIFT docking tracker
        self._timer.timeout.connect(self._tick)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._capture_manual)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self._set_direction("right"))
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self._set_direction("left"))
        QShortcut(QKeySequence(Qt.Key.Key_Down), self, activated=lambda: self._set_direction("down"))
        QShortcut(QKeySequence(Qt.Key.Key_Up), self, activated=lambda: self._set_direction("up"))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        tip = QLabel(
            "<b>1.</b> Click <b>Start Scan</b>, line up a straight PCB edge with the square, and press <b>Space</b> for Tile 1.  "
            "<b>2.</b> Pick the next direction (<b>Arrow keys</b> or buttons) and slide the board until the moving square sits inside the center target (turns <b>GREEN</b>).  "
            "<b>3.</b> Click <b>Finish &amp; Stitch</b>."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#bbb; font-size:12px;")
        root.addWidget(tip)

        # Split view: Live Docking Guide (left) + Coverage Map (right)
        split = QSplitter(Qt.Orientation.Horizontal)

        self.live_label = QLabel("Click 'Start Scan' to show the live alignment square")
        self.live_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_label.setMinimumSize(620, 400)
        self.live_label.setStyleSheet(
            "background:#18181c; border:1px solid #383842; color:#888;"
        )
        split.addWidget(self.live_label)

        right_panel = QWidget()
        rv = QVBoxLayout(right_panel)
        rv.setContentsMargins(6, 0, 0, 0)
        rv.addWidget(QLabel("<b>Board Coverage Map</b>"))
        self.map_label = QLabel("No tiles yet")
        self.map_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.map_label.setMinimumSize(300, 260)
        self.map_label.setStyleSheet(
            "background:#18181c; border:1px solid #383842; color:#777;"
        )
        rv.addWidget(self.map_label, 1)

        # Direction picker (Next Step)
        rv.addWidget(QLabel("<b>Next Step Direction (Arrow Keys)</b>"))
        drow1 = QHBoxLayout()
        drow2 = QHBoxLayout()
        for key in ("up",):
            b = self._make_dir_btn(key)
            drow1.addStretch(1)
            drow1.addWidget(b)
            drow1.addStretch(1)
        for key in ("left", "down", "right"):
            drow2.addWidget(self._make_dir_btn(key))
        rv.addLayout(drow1)
        rv.addLayout(drow2)

        split.addWidget(right_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        # Options row
        opt_row = QHBoxLayout()
        self.auto_snap_check = QCheckBox("Auto-Snap when aligned (green)")
        self.auto_snap_check.setChecked(True)
        opt_row.addWidget(self.auto_snap_check)

        self.ghost_check = QCheckBox("Show 40% overlap ghost edge")
        self.ghost_check.setChecked(True)
        opt_row.addWidget(self.ghost_check)

        opt_row.addStretch(1)
        opt_row.addWidget(QLabel("Tile Scale:"))
        self.scale_combo = QComboBox()
        self.scale_combo.addItem("50% (Fast / 1080p tiles)", 0.5)
        self.scale_combo.addItem("100% (Full 4K tiles)", 1.0)
        opt_row.addWidget(self.scale_combo)
        root.addLayout(opt_row)

        # Action buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶ Start Scan")
        self.start_btn.clicked.connect(self._toggle_scan)
        btn_row.addWidget(self.start_btn)

        self.capture_btn = QPushButton("Capture Tile (Space)")
        self.capture_btn.setEnabled(False)
        self.capture_btn.clicked.connect(self._capture_manual)
        btn_row.addWidget(self.capture_btn)

        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self._undo)
        btn_row.addWidget(self.undo_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setEnabled(False)
        self.clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(self.clear_btn)

        btn_row.addStretch(1)

        self.stitch_btn = QPushButton("Finish & Stitch")
        self.stitch_btn.setEnabled(False)
        self.stitch_btn.setStyleSheet("font-weight:bold;")
        self.stitch_btn.clicked.connect(self._finish_and_stitch)
        btn_row.addWidget(self.stitch_btn)
        root.addLayout(btn_row)

        # Status & result row
        res_row = QHBoxLayout()
        self.status_label = QLabel("Idle — 0 tiles")
        self.status_label.setStyleSheet("color:#aaa; font-size:12px;")
        res_row.addWidget(self.status_label, 1)

        self.open_img_btn = QPushButton("Open Stitched Image")
        self.open_img_btn.setVisible(False)
        self.open_img_btn.clicked.connect(self._open_last_image)
        res_row.addWidget(self.open_img_btn)

        self.open_dir_btn = QPushButton("Open Folder")
        self.open_dir_btn.setVisible(False)
        self.open_dir_btn.clicked.connect(
            lambda: QProcess.startDetached("xdg-open", [SAVE_DIR])
        )
        res_row.addWidget(self.open_dir_btn)
        root.addLayout(res_row)

        self._set_direction("right")

    def _make_dir_btn(self, key: str) -> QPushButton:
        _, _, label = DIRECTIONS[key]
        b = QPushButton(label)
        b.setCheckable(True)
        b.clicked.connect(lambda _=False, k=key: self._set_direction(k))
        self._dir_buttons[key] = b
        return b

    def _set_direction(self, key: str) -> None:
        self.engine.direction = key
        self.engine._aligned_since = None
        for k, b in self._dir_buttons.items():
            b.setChecked(k == key)

    # ---- scan lifecycle -----------------------------------------------------
    def _toggle_scan(self) -> None:
        if not self._scanning:
            scale = float(self.scale_combo.currentData())
            if not self.engine.tiles:
                self.engine.reset(scale=scale)
            self._scanning = True
            self.scale_combo.setEnabled(False)
            self.open_img_btn.setVisible(False)
            self.open_dir_btn.setVisible(False)
            if not self._exposure_locked:
                self._lock_exposure(True)
                self._exposure_locked = True
            self.start_btn.setText("⏸ Pause Scan")
            self.capture_btn.setEnabled(True)
            self._timer.start()
            self._update_status("Align PCB with square -> press Space for Tile 1")
        else:
            self._pause_scan()

    def _pause_scan(self) -> None:
        self._scanning = False
        self._timer.stop()
        self.start_btn.setText("▶ Resume Scan" if self.engine.tiles else "▶ Start Scan")
        self.capture_btn.setEnabled(False)
        self._update_status("Paused")

    def _tick(self) -> None:
        if not self._scanning:
            return
        frame = self._get_frame()
        if frame is None:
            return

        res, in_zone, auto_ready = self.engine.evaluate_live(frame)
        if auto_ready and self.auto_snap_check.isChecked():
            ok, msg = self.engine.capture_tile(frame)
            if ok:
                self._update_buttons()
                self._refresh_map()
                self._update_status(msg)
                in_zone = False

        vis = self.engine.render_live_guide(
            frame,
            show_ghost=self.ghost_check.isChecked(),
            in_zone=in_zone,
            display_w=max(480, self.live_label.width()),
        )
        self._show_bgr_on(self.live_label, vis)

    def _capture_manual(self) -> None:
        if not self._scanning:
            return
        frame = self._get_frame()
        if frame is None:
            return
        ok, msg = self.engine.capture_tile(frame, force=False)
        self._update_buttons()
        if ok:
            self._refresh_map()
        self._update_status(msg)

    def _undo(self) -> None:
        if self.engine.undo_last():
            self._update_buttons()
            self._refresh_map()
            self._update_status("Removed last tile")

    def _clear(self) -> None:
        self._pause_scan()
        self.engine.reset(scale=float(self.scale_combo.currentData()))
        self.scale_combo.setEnabled(True)
        self.start_btn.setText("▶ Start Scan")
        self.live_label.setPixmap(QPixmap())
        self.live_label.setText("Cleared — click 'Start Scan' to begin")
        self.map_label.setPixmap(QPixmap())
        self.map_label.setText("No tiles yet")
        self.open_img_btn.setVisible(False)
        self.open_dir_btn.setVisible(False)
        self._update_buttons()
        self._update_status("Idle")

    def _finish_and_stitch(self) -> None:
        if not self.engine.tiles:
            return
        self._pause_scan()
        self._set_busy(True)
        self._update_status(f"Stitching {len(self.engine.tiles)} tiles with SIFT…")
        self._worker = BoardStitchWorker(self.engine.tiles, self)
        self._worker.finished_ok.connect(self._on_stitch_ok)
        self._worker.failed.connect(self._on_stitch_failed)
        self._worker.start()

    def _on_stitch_ok(self, img: np.ndarray, path: str, method: str) -> None:
        self._set_busy(False)
        self._release_exposure_lock()
        self._last_saved_path = path
        self._show_bgr_on(self.live_label, img)
        h, w = img.shape[:2]
        self.status_label.setText(
            f"Saved {w}×{h} ({method}) → {os.path.basename(path)}"
        )
        self.open_img_btn.setVisible(True)
        self.open_dir_btn.setVisible(True)

    def _on_stitch_failed(self, err: str) -> None:
        self._set_busy(False)
        QMessageBox.warning(self, "Stitch failed", err)
        self._update_status("Stitch failed")

    def _open_last_image(self) -> None:
        if self._last_saved_path and os.path.exists(self._last_saved_path):
            QProcess.startDetached("xdg-open", [self._last_saved_path])

    # ---- UI helpers ---------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        for btn in (
            self.start_btn,
            self.capture_btn,
            self.undo_btn,
            self.clear_btn,
            self.stitch_btn,
        ):
            btn.setEnabled(not busy)
        if not busy:
            self._update_buttons()

    def _update_buttons(self) -> None:
        n = len(self.engine.tiles)
        self.undo_btn.setEnabled(n > 0)
        self.clear_btn.setEnabled(n > 0)
        self.stitch_btn.setEnabled(n >= 1)
        self.capture_btn.setEnabled(self._scanning)

    def _update_status(self, msg: str) -> None:
        self.status_label.setText(f"{msg} — {len(self.engine.tiles)} tiles")

    def _refresh_map(self) -> None:
        m = self.engine.render_coverage_map(max_dim=380)
        if m is not None:
            self._show_bgr_on(self.map_label, m)
        else:
            self.map_label.setPixmap(QPixmap())
            self.map_label.setText("No tiles yet")

    @staticmethod
    def _show_bgr_on(label: QLabel, img: np.ndarray) -> None:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
        pix = QPixmap.fromImage(qimg).scaled(
            label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(pix)

    def _release_exposure_lock(self) -> None:
        if self._exposure_locked:
            self._lock_exposure(False)
            self._exposure_locked = False

    def closeEvent(self, ev) -> None:
        self._timer.stop()
        self._scanning = False
        self._release_exposure_lock()
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)
        super().closeEvent(ev)
