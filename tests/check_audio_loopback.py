"""
Manual check of the rhythm-mode audio analysis (plays a few seconds of QUIET test tones through
the default output device, from a separate process, and listens to them with the loopback capture).

Run:  python tests/check_audio_loopback.py
Expected: hats-only -> 0 beats; kicks-only -> ~6 beats; kicks+hats -> most of ~10; nothing -> level 0.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np

from core.loopback import LoopbackCapture, SpectrumAnalyzer

SR = 48000
PLAYER = (
    "import sys, numpy as np, sounddevice as sd\n"
    "x = np.load(sys.argv[1]); sd.play(np.stack([x, x], axis=1) * 0.35, 48000); sd.wait()"
)


def make_signal(seconds, kicks, hats):
    x = np.zeros(int(SR * seconds), dtype=np.float32)
    rng = np.random.default_rng(0)
    if kicks:  # 120 BPM: a 60 Hz thump every 0.5 s
        for k in np.arange(0.5, seconds - 0.2, 0.5):
            i, n = int(k * SR), int(0.12 * SR)
            tt = np.arange(n) / SR
            x[i:i + n] += 0.5 * np.sin(2 * np.pi * 60 * tt) * np.exp(-tt / 0.04)
    if hats:  # 4 Hz hi-hat ticks
        for k in np.arange(0.25, seconds - 0.1, 0.25):
            i, n = int(k * SR), int(0.03 * SR)
            x[i:i + n] += 0.12 * rng.standard_normal(n) * np.exp(-np.arange(n) / SR / 0.01)
    return x


def run(analyzer, label, signal, seconds, expect):
    path = os.path.join(tempfile.gettempdir(), "zak_signal.npy")
    np.save(path, signal)
    player = subprocess.Popen([sys.executable, "-c", PLAYER, path])
    time.sleep(0.4)
    start = last = time.perf_counter()
    beats, previous, rows = 0, 0.0, []
    while time.perf_counter() - start < seconds:
        now = time.perf_counter()
        f = analyzer.update(now - last)
        last = now
        if f.beat > 0.95 >= previous:
            beats += 1
        previous = f.beat
        rows.append(f)
        time.sleep(0.025)
    player.wait()
    a = np.array(rows)
    print(f"{label:24s} beats={beats:2d} (expected {expect})  level={a[:, 0].mean():.2f} "
          f"bass={a[:, 1].mean():.2f} mid={a[:, 2].mean():.2f} high={a[:, 3].mean():.2f}")
    return beats


capture = LoopbackCapture()
capture.start()
analyzer = SpectrumAnalyzer(capture)
time.sleep(0.6)
print("loopback active:", capture.active, "| sample rate:", capture.sample_rate)

# The loopback hears EVERYTHING the PC plays: the check is only meaningful on a quiet system
ambient = 0.0
for _ in range(40):
    ambient = max(ambient, float(np.abs(capture.read_latest(2048)).max()))
    time.sleep(0.05)
if ambient > 0.01:
    print(f"SKIPPED: other audio is playing (peak {ambient:.2f}). Pause it and run the check again.")
    capture.stop()
    sys.exit(2)

hats = run(analyzer, "hats only", make_signal(4, False, True), 3.5, "0")
time.sleep(1.0)
kicks = run(analyzer, "kicks only", make_signal(4, True, False), 3.5, "~6")
time.sleep(1.0)
both = run(analyzer, "kicks + hats", make_signal(6, True, True), 5.5, "~8-10")
capture.stop()

ok = capture.sample_rate > 0 and hats == 0 and 4 <= kicks <= 8 and both >= 6
print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
