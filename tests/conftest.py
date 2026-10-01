"""Shared fixtures: everything here runs without the LED controller, a GPU capture or audio device."""
import os
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT, "src"))

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import create_app
from core.config import AppConfig
from core.profiles import ProfileStore
from engine import EngineState


REAL_CONFIG = os.path.join(PROJECT, "data", "config.json")


def _digest(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


@pytest.fixture(scope="session", autouse=True)
def real_config_is_never_touched():
    """Safety net: no test may modify the user's real config.json (this happened once, through a timer)."""
    before = _digest(REAL_CONFIG)
    yield
    import time
    time.sleep(0.8)  # let any stray debounce timer fire before judging
    assert _digest(REAL_CONFIG) == before, "a test modified the real config.json"


class FakeHidDevice:
    """Stands in for hid.device: records writes; can be told to fail like hidapi does (-1)."""

    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail
        self.closed = False

    def open_path(self, path):
        pass

    def write(self, data):
        if self.fail:
            return -1
        self.writes.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


class FakeHid:
    """Stands in for the `hid` module."""

    def __init__(self, interfaces=(0,)):
        self.interfaces = interfaces
        self.devices = []
        self.fail_next = False

    def enumerate(self, vid, pid):
        return [{"interface_number": i, "path": b"fake"} for i in self.interfaces]

    def device(self):
        dev = FakeHidDevice(fail=self.fail_next)
        self.devices.append(dev)
        return dev


class FakeDriver:
    """Driver double for the engine/API tests."""

    def __init__(self):
        self.is_connected = True
        self.sent = []
        self.off_count = 0
        self.last_frame = None

    def connect(self):
        self.is_connected = True
        return True

    def disconnect(self):
        self.is_connected = False

    def consume_needs_init(self):
        return False

    def invalidate(self):
        pass

    def turn_off(self):
        self.off_count += 1
        self.last_frame = None
        return True

    def send_sync_screen(self, segments, dedupe=False):
        self.sent.append(list(segments))
        return True

    def send_colors(self, colors):
        self.sent.append(np.array(colors))
        self.last_frame = np.array(colors)
        return True

    def set_brightness(self, level):
        return True


class FakeSampler:
    monitor_idx = 0
    backend = "fake"
    detect_black_bars = True
    frame_id = 0

    def switch_monitor(self, idx):
        self.monitor_idx = idx

    def list_monitors(self):
        return [{"index": 0, "width": 2560, "height": 1440, "primary": True},
                {"index": 1, "width": 1920, "height": 1080, "primary": False}]

    def capture_and_sample(self, points, box_size, detect_black_bars):
        self.frame_id += 1
        return np.full((len(points), 3), 100.0, dtype=np.float32)


class FakeAudio:
    pattern_idx = 0
    led_count = 10
    sensitivity = 1.5

    def get_current_volume(self):
        return 0.0

    def set_palette(self, name):
        self.palette_name = name

    def spectrum(self):
        return {"active": False, "bars": [0.0] * 7, "beat": 0.0, "level": 0.0}

    def get_frame_colors(self):
        return np.zeros((self.led_count, 3), dtype=np.float32)

    def pulse(self, dt):
        return 0.5

    def activate(self):
        pass

    def suspend(self):
        pass


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A default config whose file lives in a temp folder, so tests never touch the user's settings."""
    import core.config as cfgmod
    monkeypatch.setattr(cfgmod, "CONFIG_FILE_PATH", str(tmp_path / "config.json"))
    return AppConfig()


@pytest.fixture(autouse=True)
def no_real_process_inspection(monkeypatch):
    """The API looks for competing programs; tests must neither see nor touch the real ones."""
    import core.conflicts as conflicts
    monkeypatch.setattr(conflicts, "active_conflicts", lambda: [])
    monkeypatch.setattr(conflicts, "close_conflicts", lambda: (_ for _ in ()).throw(AssertionError("real close attempted")))
    monkeypatch.setattr(conflicts, "disable_conflict_autostart", lambda: (_ for _ in ()).throw(AssertionError("real registry edit attempted")))


@pytest.fixture
def saved_configs(monkeypatch):
    """Replaces the config writer for every engine/API test.

    The engine saves through a 0.5 s debounce timer, which can fire AFTER a test has finished and the
    temporary config path has been restored: that once overwrote the real config.json. Tests now
    record what would have been saved instead of touching any file.
    """
    import engine as engine_module
    saved = []
    monkeypatch.setattr(engine_module, "save_config", lambda cfg: saved.append(cfg))
    return saved


@pytest.fixture
def state(config, tmp_path, saved_configs):
    st = EngineState(config=config, driver=FakeDriver(), screen_sampler=FakeSampler(), audio_engine=FakeAudio(),
                     profiles=ProfileStore(str(tmp_path / "profiles.json")))
    yield st
    st.is_running = False
    if st._save_timer:
        st._save_timer.cancel()  # never leave a timer behind


@pytest.fixture
def client(state, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    return TestClient(create_app(state, str(web)), base_url="http://127.0.0.1:8765")
