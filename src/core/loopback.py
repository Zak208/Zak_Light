"""
System-audio capture (WASAPI loopback) and spectrum analysis for the rhythm mode.

The old rhythm mode only read the master peak meter, which cannot tell a kick drum from a voice.
Here we capture the actual stream that goes to the chosen (or default) output device and split it into
bass / mid / high bands with an FFT, plus a beat detector on the bass band. Costs well under
1 % CPU. Uses only comtypes/pycaw (already dependencies): no extra package.
"""

import ctypes
import logging
import threading
import time
from ctypes import HRESULT, POINTER, c_uint64, c_void_p
from ctypes.wintypes import BYTE, DWORD
from ctypes import c_uint32 as UINT32
from typing import NamedTuple, Optional

import numpy as np

log = logging.getLogger("zak_light")

try:
    import comtypes
    from comtypes import CLSCTX_ALL, COMMETHOD, GUID, IUnknown
    from pycaw.api.audioclient import IAudioClient
    from pycaw.pycaw import AudioUtilities
    HAS_LOOPBACK = True
except ImportError:  # pragma: no cover
    HAS_LOOPBACK = False

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
REFTIMES_PER_SEC = 10_000_000


def _device_id(device) -> str:
    """Return a Core Audio endpoint ID across the small pycaw API variations."""
    value = getattr(device, "id", None)
    if value is None:
        getter = getattr(device, "GetId", None)
        value = getter() if callable(getter) else ""
    return str(value or "")


def _endpoint(device):
    """pycaw wraps IMMDevice in ``_dev``; enumerators return the raw object."""
    return getattr(device, "_dev", device)


def _device_name(device) -> str:
    return str(getattr(device, "FriendlyName", "") or _device_id(device) or "Salida de audio")


def _render_flow(device):
    """Pycaw exposes data_flow as an enum on most releases (0 means render/output)."""
    flow = getattr(device, "data_flow", getattr(device, "DataFlow", None))
    flow = getattr(flow, "value", flow)
    try:
        return int(flow)
    except (TypeError, ValueError):
        return None


def list_output_devices() -> list[dict]:
    """Return output devices once, for the Settings screen (never on the render loop)."""
    default = {"id": "", "name": "Salida predeterminada de Windows", "default": True}
    if not HAS_LOOPBACK:
        return [default]

    initialized = False
    try:
        comtypes.CoInitialize()
        initialized = True
        default_device = AudioUtilities.GetSpeakers()
        default_id = _device_id(default_device)
        devices = [default]
        seen = {""}
        for device in AudioUtilities.GetAllDevices():
            # eRender is 0. A missing flow is retained only when it is the default endpoint,
            # which keeps this compatible with older pycaw releases.
            device_id = _device_id(device)
            if not device_id or device_id in seen:
                continue
            flow = _render_flow(device)
            if flow not in (0, None) or (flow is None and device_id != default_id):
                continue
            devices.append({
                "id": device_id,
                "name": _device_name(device),
                "default": device_id == default_id,
            })
            seen.add(device_id)
        return devices
    except Exception:
        log.exception("Could not enumerate output audio devices")
        return [default]
    finally:
        if initialized:
            comtypes.CoUninitialize()


if HAS_LOOPBACK:
    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
        _methods_ = (
            COMMETHOD(
                [], HRESULT, "GetBuffer",
                (["out"], POINTER(POINTER(BYTE)), "ppData"),
                (["out"], POINTER(UINT32), "pNumFramesToRead"),
                (["out"], POINTER(DWORD), "pdwFlags"),
                (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                (["out"], POINTER(c_uint64), "pu64QPCPosition"),
            ),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], UINT32, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(UINT32), "pNumFramesInNextPacket")),
        )


class LoopbackCapture:
    """Background thread that keeps recent samples of the selected Windows output device."""

    RING_SECONDS = 0.5

    def __init__(self, device_id: str = ""):
        self.sample_rate = 48000
        size = int(self.sample_rate * self.RING_SECONDS)
        self._ring = np.zeros(size, dtype=np.float32)      # mono mix
        self._ring_l = np.zeros(size, dtype=np.float32)    # left channel
        self._ring_r = np.zeros(size, dtype=np.float32)    # right channel
        self._lock = threading.Lock()
        self._device_lock = threading.Lock()
        self._last_data = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.active = False  # True while a loopback stream is open
        self.device_id = self._clean_device_id(device_id)
        self.active_device_id = ""
        self.active_device_name = ""
        self.using_fallback = False
        self.last_error = ""

    # ---- public ----------------------------------------------------------------------------

    def start(self):
        if not HAS_LOOPBACK:
            return
        if self._thread and self._thread.is_alive():
            if not self._stop.is_set():
                return  # already running
            self._thread.join(timeout=1.0)  # a previous stop() is still winding down
            if self._thread.is_alive():
                return
        # One stop flag per thread: an old thread stuck in a COM call can never be revived by
        # clearing a shared flag, and two threads can never feed the same ring.
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="loopback", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            if not self._thread.is_alive():
                self._thread = None
        self.active = False

    @staticmethod
    def _clean_device_id(value) -> str:
        if not isinstance(value, str) or any(ord(ch) < 32 for ch in value):
            return ""
        return value.strip()[:512]

    def selected_device_id(self) -> str:
        with self._device_lock:
            return self.device_id

    def set_device(self, device_id: str):
        """Switch endpoint without ever allowing two capture threads to share the audio ring."""
        device_id = self._clean_device_id(device_id)
        with self._device_lock:
            if device_id == self.device_id:
                return
            self.device_id = device_id
            self.active_device_id = ""
            self.active_device_name = ""
            self.using_fallback = False
            self.last_error = ""
        was_running = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
        self.stop()
        if was_running:
            self.start()

    def status(self) -> dict:
        """Small, read-only health snapshot suitable for the local API."""
        with self._device_lock:
            return {
                "available": bool(HAS_LOOPBACK),
                "active": self.active,
                "selected": bool(self.device_id),
                "active_device": self.active_device_name,
                "using_fallback": self.using_fallback,
                "last_error": self.last_error[:180],
            }

    def read_latest(self, n: int) -> np.ndarray:
        with self._lock:
            return self._ring[-n:].copy()

    def read_latest_lr(self, n: int):
        """(left, right) samples of the latest n frames, for the stereo patterns."""
        with self._lock:
            return self._ring_l[-n:].copy(), self._ring_r[-n:].copy()

    @property
    def silent_for(self) -> float:
        """Seconds since the last packet. Loopback delivers nothing while nothing is playing."""
        return time.monotonic() - self._last_data if self._last_data else float("inf")

    # ---- capture thread --------------------------------------------------------------------

    @staticmethod
    def _roll(ring: np.ndarray, data: np.ndarray):
        n = data.size
        if n >= ring.size:
            ring[:] = data[-ring.size:]
        else:
            ring[:-n] = ring[n:]
            ring[-n:] = data

    def _push(self, mono: np.ndarray, left=None, right=None):
        with self._lock:
            self._roll(self._ring, mono)
            self._roll(self._ring_l, mono if left is None else left)
            self._roll(self._ring_r, mono if right is None else right)
        self._last_data = time.monotonic()

    def _run(self, stop: threading.Event):
        comtypes.CoInitialize()
        failures, last_error = 0, None
        try:
            while not stop.is_set():
                try:
                    self._capture_until_device_changes(stop)
                    failures = 0
                    with self._device_lock:
                        self.last_error = ""
                except Exception as e:
                    failures += 1
                    if str(e) != last_error:  # log each distinct problem once, not every retry
                        last_error = str(e)
                        log.exception("Loopback capture error (retrying with back-off)")
                    self.active = False
                    with self._device_lock:
                        self.last_error = str(e)[:180]
                    stop.wait(min(30.0, 2.0 * failures))
        finally:
            comtypes.CoUninitialize()

    def _capture_until_device_changes(self, stop: threading.Event):
        selected_id = self.selected_device_id()
        using_fallback = False
        try:
            speakers = AudioUtilities.GetDeviceEnumerator().GetDevice(selected_id) if selected_id else AudioUtilities.GetSpeakers()
            client = _endpoint(speakers).Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient)
        except Exception:
            if not selected_id:
                raise
            # A Bluetooth/USB endpoint can vanish between settings and activation. Keep the saved
            # choice, but let rhythm continue on the Windows default and expose that state to the UI.
            log.warning("Selected audio output disappeared; using the Windows default until it returns")
            speakers = AudioUtilities.GetSpeakers()
            client = _endpoint(speakers).Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient)
            using_fallback = True
        device_id = _device_id(speakers)
        if not device_id:
            raise RuntimeError("no output audio device is available")
        with self._device_lock:
            self.active_device_id = device_id
            self.active_device_name = _device_name(speakers)
            self.using_fallback = using_fallback
            self.last_error = ""

        fmt = client.GetMixFormat()
        try:
            channels = int(fmt.contents.nChannels)
            self.sample_rate = int(fmt.contents.nSamplesPerSec)
            bits = int(fmt.contents.wBitsPerSample)
            block = int(fmt.contents.nBlockAlign)
            tag = int(fmt.contents.wFormatTag)
            if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: first 2 bytes of the SubFormat GUID say PCM(1)/float(3)
                tag = int.from_bytes(ctypes.string_at(ctypes.addressof(fmt.contents) + 24, 2), "little")
            is_float = tag == 3 and bits == 32
            if not is_float and not (tag == 1 and bits == 16):
                raise RuntimeError(f"unsupported mix format tag={tag} bits={bits}")
            if self._ring.size != int(self.sample_rate * self.RING_SECONDS):
                size = int(self.sample_rate * self.RING_SECONDS)
                self._ring, self._ring_l, self._ring_r = (np.zeros(size, dtype=np.float32) for _ in range(3))

            client.Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, REFTIMES_PER_SEC, 0, fmt, None)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(fmt, c_void_p))  # also when the format is rejected
        capture = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
        client.Start()
        self.active = True
        log.info("Loopback capture started (%d Hz, %d ch, %s)", self.sample_rate, channels, "float32" if is_float else "int16")

        last_device_check = time.monotonic()
        try:
            while not stop.is_set():
                while capture.GetNextPacketSize() > 0:
                    data, frames, flags, _, _ = capture.GetBuffer()
                    try:
                        if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                            mono = left = right = np.zeros(frames, dtype=np.float32)
                        else:
                            raw = ctypes.string_at(ctypes.addressof(data.contents), frames * block)
                            if is_float:
                                samples = np.frombuffer(raw, dtype=np.float32)
                            else:
                                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                            lr = samples.reshape(-1, channels)
                            mono = lr.mean(axis=1)
                            left, right = lr[:, 0], lr[:, 1 if channels > 1 else 0]
                        self._push(mono, left, right)
                    finally:
                        capture.ReleaseBuffer(frames)

                # 20 ms polling (the ring holds 500 ms); relax to 100 ms while nothing is playing
                stop.wait(0.02 if self.silent_for < 2.0 else 0.1)

                if not selected_id and time.monotonic() - last_device_check > 2.0:  # follow the default output device
                    last_device_check = time.monotonic()
                    if _device_id(AudioUtilities.GetSpeakers()) != device_id:
                        log.info("Default output device changed; reopening loopback")
                        return
        finally:
            self.active = False
            try:
                client.Stop()
            except Exception:
                pass


class Features(NamedTuple):
    level: float   # overall loudness 0..1 (auto-gain normalized)
    bass: float    # 0..1
    mid: float     # 0..1
    high: float    # 0..1
    beat: float    # 1.0 on a detected beat, decaying to 0
    left: float = 0.0    # left channel loudness 0..1 (own auto-gain)
    right: float = 0.0   # right channel loudness 0..1


class SpectrumAnalyzer:
    """FFT band energies with per-band auto-gain and a bass beat detector."""

    N = 2048                               # ~43 ms at 48 kHz; 23 Hz bin width resolves kick drums
    N_BARS = 7
    BANDS = ((30, 160), (160, 2500), (2500, 12000))
    AGC_DECAY_TAU = 8.0                    # seconds for the gain reference to forget a loud passage
    AGC_FLOOR = 2e-4                       # below this RMS the band counts as silent
    BAND_FLOOR_SHARE = 0.25                # a band's gain reference is at least this share of the loudest band
    BEAT_BASS_DOMINANCE = 0.5              # bass must reach this fraction of the loudest other band for a beat
    BEAT_REFRACTORY = 0.13                 # seconds; caps detection at ~460 BPM
    BEAT_DECAY_TAU = 0.12

    def __init__(self, capture: LoopbackCapture):
        self.capture = capture
        self.window = np.hanning(self.N).astype(np.float32)
        self._rate = 0
        self._bins = []
        self._bar_bins = []
        self.bars = [0.0] * self.N_BARS    # 7 log-spaced bars 0..1 for the UI equalizer
        self._bar_peak = self.AGC_FLOOR
        self._peak = [self.AGC_FLOOR] * 3
        self._gpeak = self.AGC_FLOOR
        self._lr_peak = self.AGC_FLOOR
        self._prev_bass = 0.0
        self._flux_hist = np.zeros(40, dtype=np.float32)  # ~1 s of bass flux at 40 FPS
        self._flux_i = 0
        self._last_beat = 0.0
        self._beat = 0.0

    def _band_bins(self, rate: int):
        freqs = np.fft.rfftfreq(self.N, 1.0 / rate)
        self._bins = [np.flatnonzero((freqs >= lo) & (freqs < hi)) for lo, hi in self.BANDS]
        edges = np.geomspace(self.BANDS[0][0], self.BANDS[-1][1], self.N_BARS + 1)
        self._bar_bins = [np.flatnonzero((freqs >= lo) & (freqs < hi)) for lo, hi in zip(edges[:-1], edges[1:])]
        self._rate = rate

    def update(self, dt: float) -> Features:
        if self.capture.sample_rate != self._rate:
            self._band_bins(self.capture.sample_rate)

        if self.capture.silent_for > 0.15:
            x = np.zeros(self.N, dtype=np.float32)
            xl = xr = x
        else:
            x = self.capture.read_latest(self.N)
            xl, xr = self.capture.read_latest_lr(self.N)
        spec = np.abs(np.fft.rfft(x * self.window)) * (2.0 / self.N)

        # Band energy = RMS of the band-limited signal (sum of power, not mean per bin), so a
        # narrow bass band is not favoured over the wide high band.
        energy = [float(np.sqrt(np.sum(spec[b] ** 2) / 2.0)) if b.size else 0.0 for b in self._bins]

        decay = np.exp(-dt / self.AGC_DECAY_TAU)
        self._gpeak = max(self._gpeak * decay, max(energy), self.AGC_FLOOR)
        norm = []
        for i, e in enumerate(energy):
            # A band is judged against its own history, but never against less than a share of
            # the loudest band: otherwise an almost empty band (bass while only hi-hats play)
            # would be auto-gained up to "full".
            self._peak[i] = max(self._peak[i] * decay, e, self.AGC_FLOOR, self.BAND_FLOOR_SHARE * self._gpeak)
            norm.append(min(1.0, (e / self._peak[i]) ** 0.75) if e > self.AGC_FLOOR else 0.0)
        bass, mid, high = norm

        bar_e = [float(np.sqrt(np.sum(spec[b] ** 2) / 2.0)) if b.size else 0.0 for b in self._bar_bins]
        self._bar_peak = max(self._bar_peak * decay, max(bar_e), self.AGC_FLOOR)
        self.bars = [min(1.0, (e / self._bar_peak) ** 0.7) if e > self.AGC_FLOOR else 0.0 for e in bar_e]

        # Beat: sudden rise of bass energy compared with its recent behaviour, while the bass
        # actually dominates (a hi-hat leaks a little energy into the bass band too)
        now = time.monotonic()
        flux = max(0.0, energy[0] - self._prev_bass)
        self._prev_bass = energy[0]
        mean, std = float(self._flux_hist.mean()), float(self._flux_hist.std())
        self._flux_hist[self._flux_i % self._flux_hist.size] = flux
        self._flux_i += 1
        is_beat = (
            bass > 0.25
            and energy[0] >= self.BEAT_BASS_DOMINANCE * max(energy[1], energy[2])
            and flux > mean + 1.6 * std + 0.04 * self._peak[0]
            and now - self._last_beat > self.BEAT_REFRACTORY
        )
        if is_beat:
            self._last_beat = now
            self._beat = 1.0
        else:
            self._beat *= float(np.exp(-dt / self.BEAT_DECAY_TAU))

        level = min(1.0, 0.75 * bass + 0.5 * mid + 0.25 * high)
        # Stereo: loudness of each channel, normalised by the loudest recent value of either
        rms_l, rms_r = float(np.sqrt(np.mean(xl ** 2))), float(np.sqrt(np.mean(xr ** 2)))
        self._lr_peak = max(self._lr_peak * decay, rms_l, rms_r, self.AGC_FLOOR)
        left = min(1.0, (rms_l / self._lr_peak) ** 0.75) if rms_l > self.AGC_FLOOR else 0.0
        right = min(1.0, (rms_r / self._lr_peak) ** 0.75) if rms_r > self.AGC_FLOOR else 0.0
        return Features(level, bass, mid, high, self._beat, left, right)
