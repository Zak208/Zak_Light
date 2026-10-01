"""
LED animations. Every effect is a pure function of (phase, LED positions, palette): vectorised numpy, no
per-LED Python loops, so even a 254-LED strip costs a fraction of a millisecond.

`pos` is each LED's place along the loop around the monitor, in [0, 1). Travelling effects therefore circle
the screen instead of just running along a strip. Direction and mirroring are applied by the engine on top
of any effect (see EffectEngine.render).

Two groups, so the UI can offer a selector:
  ambient  slow, soft, meant to stay on in the background without tiring the eye
  show     fast and eye-catching
Effects flagged `uses_colors` take the three colors the user picked; the others have their own look.
Nothing flashes faster than 3 times a second (photosensitivity guideline).
"""

import math
from dataclasses import dataclass
from typing import Callable, Dict

import numpy as np

TAU = 2.0 * math.pi


@dataclass(frozen=True)
class Effect:
    key: str
    label: str
    group: str            # "ambient" | "show"
    description: str
    swatch: str           # CSS background for the UI tile
    uses_colors: bool
    render: Callable | None  # fn(engine, t, dt, pos, colors) -> (n, 3) float32, or None for the legacy ones
    colors: tuple = ((255, 60, 140), (60, 120, 255), (255, 200, 60))   # the palette it starts with (each effect has its own)
    roles: tuple = ("Color 1", "Color 2", "Color 3")                   # what each colour is used for; "" = not used


def grad(colors: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Cyclic blend through the three palette colors: u in [0, 1) -> (n, 3)."""
    x = (u % 1.0) * 3.0
    i = np.floor(x).astype(np.int64) % 3
    f = (x - np.floor(x))[:, None]
    return colors[i] * (1.0 - f) + colors[(i + 1) % 3] * f


def _bright(rgb: np.ndarray, level: np.ndarray) -> np.ndarray:
    return rgb * np.clip(level, 0.0, 1.0)[:, None]


# ------------------------------------------------------------------------------------------ ambient

def aurora(e, t, dt, pos, colors):
    warp = 1.7 * np.sin(TAU * 0.08 * t + pos * 3.0)
    u = 0.5 + 0.5 * np.sin(TAU * (pos * 1.3 - 0.05 * t) + warp)
    level = 0.35 + 0.65 * (0.5 + 0.5 * np.sin(TAU * (pos * 0.8 + 0.03 * t)))
    return _bright(grad(colors, u), level)


def lava(e, t, dt, pos, colors):
    v = (np.sin(TAU * (pos * 1.1 + 0.04 * t)) + np.sin(TAU * (pos * 2.3 - 0.07 * t) + 1.0)
         + np.sin(TAU * (pos * 0.7 + 0.05 * t) + 2.0)) / 3.0
    u = 0.5 + 0.5 * v
    return _bright(grad(colors, u * 0.66), 0.45 + 0.55 * u)


def ocean(e, t, dt, pos, colors):
    u = 0.5 + 0.5 * np.sin(TAU * (pos * 2.0 - 0.15 * t))
    deep, shallow = np.array([0, 40, 120], np.float32), np.array([0, 170, 200], np.float32)
    rgb = deep * (1 - u[:, None]) + shallow * u[:, None]
    foam = np.clip((u - 0.88) * 8.0, 0.0, 1.0)[:, None] * 200.0
    return np.clip(rgb + foam, 0, 255)


def candle(e, t, dt, pos, colors):
    n = len(pos)
    lvl = e.state.setdefault("lvl", np.full(n, 0.8, np.float32))
    if lvl.shape[0] != n:
        lvl = e.state["lvl"] = np.full(n, 0.8, np.float32)
    target = 0.55 + 0.45 * e.rng.random(n).astype(np.float32)
    lvl += (target - lvl) * min(1.0, dt * 9.0)           # smooth random flicker
    warm = np.array([255, 110, 18], np.float32)
    return _bright(np.broadcast_to(warm, (n, 3)), lvl * (0.85 + 0.15 * np.sin(TAU * 0.4 * t)))


def stars(e, t, dt, pos, colors):
    n = len(pos)
    glow = e.state.setdefault("glow", np.zeros(n, np.float32))
    if glow.shape[0] != n:
        glow = e.state["glow"] = np.zeros(n, np.float32)
    glow *= math.exp(-dt / 0.7)
    born = e.rng.random(n) < (dt * 0.9)                     # about 0.9 new twinkles per LED per second
    glow[born] = 1.0
    return np.clip(colors[0] * 0.16 + colors[1] * glow[:, None], 0, 255)


def sunrise(e, t, dt, pos, colors):
    seconds = max(5.0, e.sunrise_seconds)
    elapsed = (e.now - e.sunrise_start) if e.sunrise_start else 0.0
    p = float(np.clip(elapsed / seconds, 0.0, 1.0))
    stops = np.array([[8, 0, 0], [140, 20, 0], [255, 90, 8], [255, 170, 60], [255, 225, 160]], np.float32)
    # the bottom of the monitor "rises" slightly ahead of the top
    local = np.clip(p * 1.15 - 0.15 * np.abs(pos - 0.5), 0.0, 1.0) * (len(stops) - 1)
    i = np.minimum(local.astype(np.int64), len(stops) - 2)
    f = (local - i)[:, None]
    return stops[i] * (1 - f) + stops[i + 1] * f


def breathing(e, t, dt, pos, colors):
    intensity = (0.55 + 0.45 * math.sin(t * 2.0)) ** 2
    mix = 0.5 + 0.5 * math.sin(t * 0.45)
    rgb = colors[0] * (1 - mix) + colors[1] * mix
    return np.broadcast_to(rgb * intensity, (len(pos), 3)).copy()


def plasma(e, t, dt, pos, colors):
    v = (np.sin(TAU * (pos * 3 + 0.10 * t)) + np.sin(TAU * (pos * 5 - 0.13 * t) + 1.3)
         + np.sin(TAU * (pos * 7 + 0.07 * t))) / 3.0
    return _hue(0.5 + 0.5 * v, 0.92)


def _hue(h: np.ndarray, value: float = 1.0) -> np.ndarray:
    """Hue (0..1, cyclic) at full saturation -> RGB 0..255."""
    x = (h % 1.0) * 6.0
    sector = x.astype(np.int64) % 6
    f = x - np.floor(x)
    q = 1.0 - f
    z, o = np.zeros_like(f), np.ones_like(f)
    r = np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4], [o, q, z, z, f], o)
    g = np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4], [f, o, o, q, z], z)
    b = np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4], [z, z, f, o, o], q)
    return np.stack([r, g, b], axis=1) * 255.0 * value


# -------------------------------------------------------------------------------------------- show

def comet(e, t, dt, pos, colors):
    head = (0.22 * t) % 1.0
    behind = (head - pos) % 1.0
    level = np.exp(-behind * 7.0) * (behind < 0.55)
    rgb = _bright(colors[0] * np.ones((len(pos), 1), np.float32), level)
    rgb += _bright(colors[1] * np.ones((len(pos), 1), np.float32), np.clip(level - 0.6, 0, 1) * 1.6)   # hot head in the 2nd colour
    return np.clip(rgb + colors[0] * 0.03, 0, 255)


def scanner(e, t, dt, pos, colors):
    """A beam that sweeps back and forth: bright core in colour 1, a soft trail behind it in colour 2."""
    phase = TAU * 0.3 * t
    head = 0.5 + 0.5 * math.sin(phase)
    moving = 1.0 if math.cos(phase) >= 0 else -1.0
    ahead = (pos - head) * moving                                   # > 0 in front of the beam, < 0 behind it
    core = np.exp(-((ahead / 0.045) ** 2))
    trail = np.where(ahead < 0, np.exp(-((-ahead) / 0.16)), 0.0) * 0.55
    one = np.ones((len(pos), 1), np.float32)
    rgb = _bright(colors[0] * one, core) + _bright(colors[1] * one, trail * (1.0 - core))
    return np.clip(rgb + colors[0] * 0.03, 0, 255)                  # the faintest glow, in the beam's own colour


def fireworks(e, t, dt, pos, colors):
    bursts = e.state.setdefault("bursts", [])
    e.state["next"] = e.state.get("next", 0.0) - dt
    if e.state["next"] <= 0 and len(bursts) < 6:
        bursts.append({"c": float(e.rng.random()), "age": 0.0, "col": colors[int(e.rng.integers(0, 3))].copy()})
        e.state["next"] = 0.5 + float(e.rng.random()) * 0.9
    out = np.zeros((len(pos), 3), np.float32)
    for b in bursts:
        b["age"] += dt
        d = np.abs(((pos - b["c"] + 0.5) % 1.0) - 0.5)           # circular distance to the burst centre
        ring = np.exp(-(((d - b["age"] * 0.22) / 0.035) ** 2)) * math.exp(-b["age"] * 1.3)
        core = np.exp(-(d / 0.03) ** 2) * math.exp(-b["age"] * 5.0)
        out += b["col"] * ring[:, None] + 255.0 * core[:, None]
    e.state["bursts"] = [b for b in bursts if b["age"] < 2.6]
    return np.clip(out, 0, 255)


def police(e, t, dt, pos, colors):
    """Colour 1 flashes on one half of the strip, colour 2 on the other; each flash fades out quickly."""
    phase = (t * 1.1) % 1.0                                       # one cycle every ~0.9 s
    left = pos < 0.5

    def flash(start, length=0.22):
        x = (phase - start) % 1.0
        return max(0.0, 1.0 - x / length) ** 0.6 if x < length else 0.0

    out = np.zeros((len(pos), 3), np.float32)
    out[left] = colors[0] * flash(0.0)
    out[~left] = colors[1] * flash(0.5)
    return out


def storm(e, t, dt, pos, colors):
    e.state["wait"] = e.state.get("wait", 2.0) - dt
    flash = e.state.get("flash", 0.0)
    if e.state["wait"] <= 0:
        e.state["wait"] = 3.0 + float(e.rng.random()) * 6.0       # a bolt every 3-9 s, never a strobe
        e.state["centre"] = float(e.rng.random())
        flash = 1.0
    e.state["flash"] = flash * math.exp(-dt / 0.18)
    centre = e.state.get("centre", 0.5)
    d = np.abs(((pos - centre + 0.5) % 1.0) - 0.5)
    bolt = np.exp(-(d / 0.25) ** 2) * e.state["flash"]
    base = np.array([10, 16, 36], np.float32) * (0.7 + 0.3 * np.sin(TAU * 0.05 * t + pos * 4))[:, None]
    return np.clip(base + np.array([200, 215, 255], np.float32) * bolt[:, None], 0, 255)


def chase(e, t, dt, pos, colors):
    n = len(pos)
    dots = max(3, round(n / 6.0))                                   # a whole number of dots around the loop
    u = (np.arange(n) * dots / n - 0.6 * t) % 1.0
    on = np.where(u < 0.34, np.sin(np.pi * np.clip(u / 0.34, 0.0, 1.0)) ** 0.7, 0.0)      # each dot swells and fades
    return _bright(grad(colors, pos), 0.015 + 0.985 * on)           # the gaps stay (almost) dark


def waves(e, t, dt, pos, colors):
    d = np.abs(((pos - 0.25 + 0.5) % 1.0) - 0.5) * 2.0            # rings spreading from the top centre
    level = 0.5 + 0.5 * np.sin(TAU * (d * 2.2 - 0.55 * t))
    return _bright(grad(colors, d), 0.15 + 0.85 * level)


REGISTRY: Dict[str, Effect] = {e.key: e for e in [
    # ---- ambient
    Effect("aurora", "Aurora", "ambient", "Cortinas de luz que derivan despacio", "linear-gradient(90deg,#10b981,#3b82f6,#a855f7,#10b981)", True, aurora, ((16, 185, 129), (59, 130, 246), (168, 85, 247))),
    Effect("lava", "Lámpara de lava", "ambient", "Manchas de color que suben y bajan", "linear-gradient(90deg,#ff4d00,#ff2e93,#7a1cff,#ff4d00)", True, lava, ((255, 77, 0), (255, 46, 147), (122, 28, 255))),
    Effect("ocean", "Olas del mar", "ambient", "Oleaje azul con espuma", "linear-gradient(90deg,#002a70,#00aac8,#dffaff,#00aac8)", False, ocean),
    Effect("candle", "Vela", "ambient", "Luz cálida que parpadea como una llama", "radial-gradient(circle at 50% 70%,#ffb347,#c2410c 60%,#2a0d02)", False, candle),
    Effect("stars", "Estrellas", "ambient", "Destellos que aparecen y se apagan", "radial-gradient(circle at 30% 40%,#fff 0 2px,transparent 3px),radial-gradient(circle at 70% 60%,#fff 0 2px,transparent 3px),#0b1030", True, stars, ((20, 30, 70), (255, 255, 255), (170, 200, 255)), roles=("Fondo", "Estrellas", "")),
    Effect("sunrise", "Amanecer", "ambient", "Del rojo profundo a la luz del día", "linear-gradient(90deg,#3a0000,#ff5a08,#ffb060,#ffe9b0)", False, sunrise),
    Effect("rainbow", "Arcoíris", "ambient", "Espectro completo que fluye", "linear-gradient(90deg,#f33,#fc3,#3f6,#3cf,#93f,#f3c)", False, None),
    Effect("breathing", "Respiración", "ambient", "Late entre dos colores", "radial-gradient(circle at 50% 50%,#3ab0ff,#0b2a4a)", True, breathing, ((60, 170, 255), (160, 90, 255), (255, 255, 255)), roles=("Color 1", "Color 2", "")),
    Effect("fire", "Fuego", "ambient", "Chimenea cálida", "linear-gradient(90deg,#ff2d00,#ff8a00,#ffd21a,#ff5a00)", False, None),
    Effect("cycle", "Ciclo de color", "ambient", "Toda la tira cambia de tono", "linear-gradient(90deg,#ff5a8a,#c058ff,#5a8aff,#4cffc6)", False, None),
    Effect("plasma", "Plasma", "ambient", "Tonos que se mezclan sin parar", "linear-gradient(90deg,#ff3cac,#784ba0,#2b86c5,#3cffb5)", False, plasma),
    # ---- show
    Effect("comet", "Cometa", "show", "Una estela que da la vuelta al monitor", "linear-gradient(90deg,transparent,#ff3c8c,#fff)", True, comet, ((255, 40, 0), (255, 200, 140), (0, 0, 0)), roles=("Cuerpo", "Punta brillante", "")),
    Effect("scanner", "Scanner", "show", "Haz que va y viene", "linear-gradient(90deg,#111,#ff2020,#111)", True, scanner, ((255, 0, 0), (140, 0, 0), (0, 0, 0)), roles=("Haz", "Estela", "")),
    Effect("fireworks", "Fuegos artificiales", "show", "Explosiones de color al azar", "radial-gradient(circle at 50% 50%,#fff,#ff3c8c 35%,#3b82f6 60%,#0a0a1a)", True, fireworks, ((255, 60, 140), (60, 160, 255), (255, 210, 60)), roles=("Color 1", "Color 2", "Color 3")),
    Effect("police", "Policía", "show", "Destellos rojo y azul (2 por lado y ciclo)", "linear-gradient(90deg,#ff0000 0 50%,#0040ff 50%)", True, police, ((255, 0, 0), (0, 50, 255), (255, 255, 255)), roles=("Destello izquierdo", "Destello derecho", "")),
    Effect("storm", "Tormenta", "show", "Cielo oscuro con relámpagos ocasionales", "linear-gradient(180deg,#0a1024,#1c2a5a,#e8f0ff)", False, storm),
    Effect("chase", "Persecución", "show", "Puntos que corren por el borde", "repeating-linear-gradient(90deg,#ff3c8c 0 10px,#111 10px 24px)", True, chase, ((255, 120, 0), (255, 0, 90), (130, 0, 255))),
    Effect("waves", "Ondas", "show", "Anillos que se expanden desde arriba", "repeating-radial-gradient(circle at 50% 0%,#3b82f6 0 8px,#111 8px 18px)", True, waves, ((0, 110, 255), (0, 220, 200), (150, 80, 255))),
]}

EFFECT_KEYS = tuple(REGISTRY)
AMBIENT = tuple(k for k, e in REGISTRY.items() if e.group == "ambient")
SHOW = tuple(k for k, e in REGISTRY.items() if e.group == "show")


def catalogue() -> list:
    """What the UI needs to draw the effect tiles."""
    return [{"key": e.key, "label": e.label, "group": e.group, "description": e.description,
             "swatch": e.swatch, "uses_colors": e.uses_colors, "colors": [list(c) for c in e.colors], "roles": list(e.roles)}
            for e in REGISTRY.values()]


def default_palette(effect: str) -> list:
    """The three colours an effect starts with."""
    fx = REGISTRY.get(effect)
    return [list(c) for c in (fx.colors if fx else REGISTRY["aurora"].colors)]
