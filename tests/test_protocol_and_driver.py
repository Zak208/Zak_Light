import numpy as np

from core import protocol
from core.driver import DxLightDriver, KEEPALIVE_SECONDS
from conftest import FakeHid


def test_checksum_is_sum_modulo_256():
    assert protocol.checksum(bytes([200, 100, 1])) == 45


def test_sync_screen_packet_layout():
    (chunk,) = protocol.build_sync_screen_packet([(1, 10, 20, 30, 5)], seq_id=7)
    assert len(chunk) == 65                      # report id + 64 bytes, padded for Windows HID
    pkt = bytes(chunk[1:])
    assert pkt[:2] == b"SC"
    assert int.from_bytes(pkt[2:4], "big") == 12  # 7 + 5 payload bytes
    assert pkt[4] == 7 and pkt[5] == protocol.ACTION_SET_SYNC_SCREEN
    assert list(pkt[6:11]) == [1, 10, 20, 30, 5]
    assert pkt[11] == sum(pkt[:11]) % 256


def test_led_range_is_clamped_to_one_byte():
    (chunk,) = protocol.build_sync_screen_packet([(0, 300, -4, 20, 999)], seq_id=1)
    assert list(bytes(chunk[1:])[6:11]) == [1, 255, 0, 20, 255]


def test_large_frames_are_split_in_64_byte_reports():
    segments = [(i, i, i, i, i) for i in range(1, 64)]
    chunks = protocol.build_sync_screen_packet(segments, seq_id=1)
    assert len(chunks) > 1 and all(len(c) == 65 for c in chunks)


def test_merge_runs_merges_equal_neighbours_only():
    colors = [(0, 0, 0)] * 3 + [(1, 2, 3), (1, 2, 3), (9, 9, 9), (0, 0, 0)]
    assert protocol.merge_runs(colors) == [(1, 0, 0, 0, 3), (4, 1, 2, 3, 5), (6, 9, 9, 9, 6), (7, 0, 0, 0, 7)]
    assert protocol.merge_runs([]) == []


# ---- driver ------------------------------------------------------------------------------------

def make_driver(**kwargs):
    hid = FakeHid(**kwargs)
    return DxLightDriver(hid_module=hid), hid


def test_connect_initialises_hardware_brightness_and_flags_reinit():
    driver, hid = make_driver()
    assert driver.connect() and driver.is_connected
    assert len(hid.devices[0].writes) == 1           # brightness packet
    assert driver.consume_needs_init() is True
    assert driver.consume_needs_init() is False


def test_failed_write_drops_the_device_then_reconnects():
    """hidapi reports a dead device with -1 (no exception): the driver must notice and reconnect."""
    driver, hid = make_driver()
    driver.connect()
    hid.devices[0].fail = True
    assert driver.send_sync_screen([(1, 1, 2, 3, 1)]) is False
    assert driver.is_connected is False
    # next call reconnects to a healthy device
    assert driver.send_sync_screen([(1, 1, 2, 3, 1)]) is True
    assert driver.is_connected is True and len(hid.devices) == 2


def test_falls_back_to_interface_minus_one_and_rejects_foreign_ones():
    driver, _ = make_driver(interfaces=(-1,))
    assert driver.connect() is True
    driver, _ = make_driver(interfaces=(3,))
    assert driver.connect() is False


def test_no_device_means_not_connected():
    driver, _ = make_driver(interfaces=())
    assert driver.connect() is False and driver.send_sync_screen([(1, 1, 1, 1, 1)]) is False


def test_identical_frames_are_not_rewritten_until_keepalive(monkeypatch):
    driver, hid = make_driver()
    driver.connect()
    frame = np.full((4, 3), 120, dtype=np.uint8)
    driver.send_colors(frame)
    writes = len(hid.devices[0].writes)
    driver.send_colors(frame.copy())
    assert len(hid.devices[0].writes) == writes      # exact same bytes: skipped

    driver.send_colors(frame + 1)                    # +1 on bright colors: inside the deadband
    assert len(hid.devices[0].writes) == writes
    driver.send_colors(frame + 5)                    # a visible change is sent
    assert len(hid.devices[0].writes) > writes

    # after the keep-alive interval an unchanged frame is sent again (recovers a reset controller)
    now = driver._last_send + KEEPALIVE_SECONDS + 0.1
    monkeypatch.setattr("core.driver.time.monotonic", lambda: now)
    writes = len(hid.devices[0].writes)
    driver.send_colors(frame + 5)
    assert len(hid.devices[0].writes) > writes


def test_dark_colors_have_no_deadband():
    driver, hid = make_driver()
    driver.connect()
    driver.send_colors(np.zeros((4, 3), dtype=np.uint8))
    writes = len(hid.devices[0].writes)
    driver.send_colors(np.ones((4, 3), dtype=np.uint8))   # 0 -> 1 is visible at the dark end
    assert len(hid.devices[0].writes) > writes


def test_other_commands_force_the_next_frame():
    driver, hid = make_driver()
    driver.connect()
    frame = np.full((4, 3), 120, dtype=np.uint8)
    driver.send_colors(frame)
    driver.turn_off()
    writes = len(hid.devices[0].writes)
    driver.send_colors(frame)                          # the strip is dark now: must be sent again
    assert len(hid.devices[0].writes) > writes


def test_empty_frames_send_nothing():
    driver, hid = make_driver()
    driver.connect()
    writes = len(hid.devices[0].writes)
    assert driver.send_sync_screen([]) and driver.send_colors(np.zeros((0, 3), dtype=np.uint8))
    assert len(hid.devices[0].writes) == writes
