"""
Geometry helpers for the LED layout and the screen-mode "styles" (pure numpy, no hardware).

Edge order everywhere is: 0 = left, 1 = top, 2 = right, 3 = bottom.
"""

import numpy as np

EDGE_LEFT, EDGE_TOP, EDGE_RIGHT, EDGE_BOTTOM = 0, 1, 2, 3
SIDE_BIAS = 0.012  # same preference for the side bars as the strip capture planner uses


def edge_groups(points: np.ndarray) -> np.ndarray:
    """Which screen edge each LED belongs to: (N,) ints in {left, top, right, bottom}."""
    x, y = points[:, 0], points[:, 1]
    is_side = np.minimum(x, 1.0 - x) <= np.minimum(y, 1.0 - y) + SIDE_BIAS
    return np.where(is_side, np.where(x < 0.5, EDGE_LEFT, EDGE_RIGHT), np.where(y < 0.5, EDGE_TOP, EDGE_BOTTOM))


def perimeter_positions(points: np.ndarray) -> np.ndarray:
    """
    Position of each LED along the loop around the monitor, in [0, 1), so animations can travel around
    the screen. Order follows the wiring (LED 0 first): positions increase with the LED index even if the
    strip does not close a full loop (3-sided layouts simply cover part of the circle).
    """
    n = len(points)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return (np.arange(n, dtype=np.float32) / n).astype(np.float32)


def apply_screen_style(raw: np.ndarray, points: np.ndarray, style: str, edges_enabled) -> np.ndarray:
    """
    Turns the per-zone colors sampled from the picture into what the strip should show.

    style "edge"    each LED shows its own zone (default)
    style "average" every LED shows the average color of the whole picture edge
    style "halves"  left-hand LEDs show the left half's average, right-hand LEDs the right half's
    edges_enabled   [left, top, right, bottom]: a disabled edge stays dark
    """
    out = raw
    if style == "average" and len(raw):
        out = np.broadcast_to(raw.mean(axis=0, keepdims=True), raw.shape).copy()
    elif style == "halves" and len(raw) and len(points) == len(raw):
        left = points[:, 0] < 0.5
        out = raw.copy()
        for mask in (left, ~left):
            if mask.any():
                out[mask] = raw[mask].mean(axis=0)

    if edges_enabled is not None and not all(edges_enabled) and len(points) == len(out):
        enabled = np.asarray(edges_enabled, dtype=bool)
        keep = enabled[edge_groups(points)]
        out = np.where(keep[:, None], out, 0.0).astype(raw.dtype)
    return out
