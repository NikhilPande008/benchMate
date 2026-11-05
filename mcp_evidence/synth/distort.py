"""Camera-side distortions for synthetic display frames.

Each takes the rendered image (and its label points) and returns a new image; geometric ones also
move the points. Severity is 0..1. The per-frame ones (flicker, mid-update) are built in
dataset.py from the renderer's segment specs, since they change what is lit, not how it is seen.
"""
from __future__ import annotations

import cv2
import numpy as np


def perspective(img: np.ndarray, points: dict[str, np.ndarray], rng: np.random.Generator, severity: float) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]:
    """Tilt: move the image corners by up to 15% of the size, as an off-axis camera would."""
    h, w = img.shape[:2]
    src = np.array([(0, 0), (w, 0), (w, h), (0, h)], np.float32)
    j = 0.15 * severity
    dst = src + rng.uniform(-j, j, (4, 2)).astype(np.float32) * np.array([w, h], np.float32)
    dst -= dst.min(axis=0)
    W, H = int(np.ceil(dst[:, 0].max())), int(np.ceil(dst[:, 1].max()))
    M = cv2.getPerspectiveTransform(src, dst)
    border = tuple(float(x) for x in img[2, 2])
    out = cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=border)
    moved = {k: cv2.perspectiveTransform(v.reshape(-1, 1, 2), M).reshape(-1, 2) for k, v in points.items()}
    return out, moved, M


def blur(img: np.ndarray, rng: np.random.Generator, severity: float) -> np.ndarray:
    """Focus blur or a little motion (a shaky mount)."""
    if rng.random() < 0.6:
        return cv2.GaussianBlur(img, (0, 0), 0.6 + 3.2 * severity)
    n = int(3 + 12 * severity)
    k = np.zeros((n, n), np.float32)
    k[n // 2, :] = 1.0 / n
    M = cv2.getRotationMatrix2D((n / 2 - 0.5, n / 2 - 0.5), float(rng.uniform(0, 180)), 1.0)
    k = cv2.warpAffine(k, M, (n, n))
    return cv2.filter2D(img, -1, k / max(k.sum(), 1e-6))


def glare(img: np.ndarray, glass: np.ndarray, rng: np.random.Generator, severity: float) -> np.ndarray:
    """A specular highlight from the ring light: a soft ellipse that saturates to white."""
    h, w = img.shape[:2]
    x0, y0 = glass.min(axis=0)
    x1, y1 = glass.max(axis=0)
    cx, cy = rng.uniform(x0, x1), rng.uniform(y0, y1)
    rx = (x1 - x0) * rng.uniform(0.08, 0.12 + 0.25 * severity)
    ry = rx * rng.uniform(0.4, 1.0)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ang = rng.uniform(0, np.pi)
    dx, dy = xx - cx, yy - cy
    u = (dx * np.cos(ang) + dy * np.sin(ang)) / rx
    v = (-dx * np.sin(ang) + dy * np.cos(ang)) / ry
    spot = np.exp(-(u * u + v * v) * 1.2) * (0.6 + 0.9 * severity)
    out = img.astype(np.float32) + spot[..., None] * 255 * np.array([1.0, 1.0, 0.98], np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def low_contrast(img: np.ndarray, rng: np.random.Generator, severity: float) -> np.ndarray:
    f = img.astype(np.float32)
    mean = f.mean()
    alpha = 1.0 - 0.8 * severity
    out = mean + (f - mean) * alpha + rng.uniform(-20, 20)
    return np.clip(out, 0, 255).astype(np.uint8)


def hand(img: np.ndarray, glass: np.ndarray, rng: np.random.Generator, severity: float) -> tuple[np.ndarray, np.ndarray]:
    """A hand or finger reaching in over the display from one side; returns the image and the
    occlusion mask (bool)."""
    h, w = img.shape[:2]
    x0, y0 = glass.min(axis=0)
    x1, y1 = glass.max(axis=0)
    cover = 0.15 + 0.6 * severity
    side = rng.choice(["left", "right", "bottom"])
    mask = np.zeros((h, w), np.uint8)
    skin = np.array([rng.uniform(60, 140), rng.uniform(90, 170), rng.uniform(140, 225)], np.float32)
    if side == "left":
        edge = x0 + (x1 - x0) * cover
        cv2.ellipse(mask, (int(x0 - (x1 - x0) * 0.3), int(rng.uniform(y0, y1))), (int(edge - x0 + (x1 - x0) * 0.3), int((y1 - y0) * rng.uniform(0.5, 0.9))), 0, 0, 360, 255, -1)
    elif side == "right":
        edge = x1 - (x1 - x0) * cover
        cv2.ellipse(mask, (int(x1 + (x1 - x0) * 0.3), int(rng.uniform(y0, y1))), (int(x1 - edge + (x1 - x0) * 0.3), int((y1 - y0) * rng.uniform(0.5, 0.9))), 0, 0, 360, 255, -1)
    else:
        top = y1 - (y1 - y0) * cover
        cx = rng.uniform(x0, x1)
        fw = (x1 - x0) * rng.uniform(0.12, 0.3)
        cv2.rectangle(mask, (int(cx - fw / 2), int(top + fw / 2)), (int(cx + fw / 2), h), 255, -1)
        cv2.circle(mask, (int(cx), int(top + fw / 2)), int(fw / 2), 255, -1)
    m = cv2.GaussianBlur(mask, (0, 0), 2).astype(np.float32) / 255.0
    yy = np.linspace(0.75, 1.1, h, dtype=np.float32)[:, None, None]
    shaded = skin * yy + rng.normal(0, 4, (h, w, 1)).astype(np.float32)
    out = img.astype(np.float32) * (1 - m[..., None]) + shaded * m[..., None]
    return np.clip(out, 0, 255).astype(np.uint8), mask > 127


def noise(img: np.ndarray, rng: np.random.Generator, severity: float) -> np.ndarray:
    f = img.astype(np.float32) + rng.normal(0, 3 + 17 * severity, img.shape).astype(np.float32)
    return np.clip(f, 0, 255).astype(np.uint8)


def realism(img: np.ndarray, scene: np.random.Generator, frame: np.random.Generator) -> np.ndarray:
    """Applied to every frame: a light gradient and slight softness (the same in every frame of a
    sample: the light doesn't move in 300 ms) and sensor noise (new in every frame)."""
    h, w = img.shape[:2]
    gx = np.linspace(scene.uniform(0.85, 1.0), scene.uniform(0.95, 1.12), w, dtype=np.float32)[None, :, None]
    gy = np.linspace(scene.uniform(0.9, 1.05), scene.uniform(0.9, 1.08), h, dtype=np.float32)[:, None, None]
    f = img.astype(np.float32) * gx * gy
    f = cv2.GaussianBlur(f, (0, 0), scene.uniform(0.3, 0.8))
    f += frame.normal(0, scene.uniform(1.0, 3.0), f.shape).astype(np.float32)
    return np.clip(f, 0, 255).astype(np.uint8)


def jpeg(img: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    assert ok
    return buf.tobytes()
