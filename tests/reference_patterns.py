"""
The original per-LED loop implementations of the rhythm patterns and effects, kept verbatim as the
reference the vectorised versions in core/ are compared against (see test_vectorised.py).
"""
import math

import numpy as np


def reference_pattern(pat, n, vol, phase, palette):
    half = n // 2
    colors = np.zeros((n, 3), dtype=np.float32)
    c_base = np.array(palette[0], dtype=np.float32)
    c_mid = np.array(palette[1], dtype=np.float32)
    c_peak = np.array(palette[2], dtype=np.float32)
    ambient_scale = 0.22 + 0.15 * vol
    punch_scale = 0.40 + 0.60 * vol

    if pat == 0:
        active_extent = max(2.0, float(half) * (0.2 + 0.9 * vol))
        for i in range(n):
            dist = abs(i - half)
            if dist <= active_extent:
                ratio = 1.0 - (dist / active_extent)
                ratio = math.pow(ratio, 0.7)
                blend = ratio * c_peak + (1.0 - ratio) * c_mid
                colors[i] = blend * punch_scale
            else:
                colors[i] = c_base * ambient_scale
    elif pat == 1:
        active_extent = max(3.0, float(n) * (0.2 + 0.85 * vol))
        for i in range(n):
            if i <= active_extent:
                ratio = 1.0 - (i / active_extent)
                blend = ratio * c_peak + (1.0 - ratio) * c_mid
                colors[i] = blend * punch_scale
            else:
                colors[i] = c_base * ambient_scale
    elif pat == 2:
        pulse = 0.25 + 0.75 * math.pow(vol, 0.85)
        t_blend = 0.5 + 0.5 * math.sin(phase)
        blend = t_blend * c_mid + (1.0 - t_blend) * c_peak
        colors[:] = blend * pulse
    elif pat == 3:
        for i in range(n):
            heat = math.sin(i * 0.14 + phase * 2.2) * 0.3 + 0.7
            heat_val = max(0.0, min(1.0, heat * (0.35 + 0.65 * vol)))
            if heat_val > 0.6:
                r = (heat_val - 0.6) / 0.4
                colors[i] = r * c_peak + (1.0 - r) * c_mid
            else:
                r = heat_val / 0.6
                colors[i] = r * c_mid + (1.0 - r) * c_base
            colors[i] *= punch_scale
    elif pat == 4:
        for i in range(n):
            pos = (float(i) / n + phase * 0.2) % 1.0
            if pos < 0.33:
                t = pos / 0.33
                blend = (1.0 - t) * c_base + t * c_mid
            elif pos < 0.66:
                t = (pos - 0.33) / 0.33
                blend = (1.0 - t) * c_mid + t * c_peak
            else:
                t = (pos - 0.66) / 0.34
                blend = (1.0 - t) * c_peak + t * c_base
            colors[i] = blend * (0.35 + 0.65 * vol)
    elif pat == 5:
        for i in range(n):
            wave = math.sin(float(i) / n * 4.0 * math.pi + phase) * 0.5 + 0.5
            wave_pow = math.pow(wave, 1.4)
            blend = wave_pow * c_peak + (1.0 - wave_pow) * c_base
            colors[i] = blend * (0.28 + 0.72 * vol)
    else:
        ambient = 0.30 + 0.70 * vol
        for i in range(n):
            subtle = math.sin(float(i) / n * 2.0 * math.pi + phase * 0.5) * 0.2 + 0.8
            colors[i] = c_mid * ambient * subtle
    return np.clip(colors, 0.0, 255.0)


def hsv_to_rgb(h, s, v):
    i = int(h * 6.0)
    f = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def reference_rainbow(n, t, spatial_freq=1.0):
    colors = np.empty((n, 3), dtype=np.float32)
    for i in range(n):
        h = (t + (i / float(n)) * spatial_freq) % 1.0
        colors[i] = hsv_to_rgb(h, 1.0, 1.0)
    return colors


def reference_fire(n, t):
    colors = np.empty((n, 3), dtype=np.float32)
    for i in range(n):
        noise = (math.sin(t + i * 0.35) + math.sin(t * 1.7 + i * 0.15)) * 0.25 + 0.5
        colors[i] = [int(220 + 35 * noise), int(50 + 70 * noise), int(10 * noise)]
    return colors
