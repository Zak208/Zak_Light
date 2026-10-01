"""
DX Light USB HID Protocol Implementation.
Reverse engineered from DX Light hardware communication.
"""

import struct
from typing import List, Tuple

HEADER_RB = b'RB'
HEADER_SC = b'SC'

# Protocol Actions
ACTION_SET_SYNC_SCREEN = 128    # 0x80 - High speed screen sync streaming
ACTION_WRITE_DEVICE_INFO = 129  # 0x81
ACTION_READ_DEVICE_INFO = 130   # 0x82
ACTION_READ_DEVICE_UUID = 131   # 0x83
ACTION_SET_LED_EFFECT = 133     # 0x85 - Built-in dynamic hardware effects
ACTION_SET_SECTION_LED = 134    # 0x86 - Set static ranges of LEDs
ACTION_SET_BRIGHTNESS = 135     # 0x87 - Brightness control
ACTION_SET_AUTO_OFF = 137       # 0x89
ACTION_SET_DYNAMIC_SPEED = 138  # 0x8A
ACTION_SET_SOUND_SENSITIVITY = 139 # 0x8B
ACTION_SET_EXTERNAL_AUDIO = 145 # 0x91
ACTION_SET_LAMPS_AMOUNT = 149   # 0x95
ACTION_SET_WHITE_BRIGHT = 150   # 0x96
ACTION_TURN_OFF_LIGHT = 151     # 0x97
ACTION_SET_COMPUTER_RHYTHM = 152 # 0x98


def checksum(data: bytes | bytearray) -> int:
    """Calculate 1-byte sum modulo 256 checksum."""
    return sum(data) % 256


def chunk_for_hid(packet: bytearray, chunk_size: int = 64) -> List[bytearray]:
    """
    Splits packet into chunk_size (64 bytes) chunks.
    Prefixes each with Report ID 0x00 and pads to 65 bytes total for Windows HID.
    """
    chunks = []
    for i in range(0, len(packet), chunk_size):
        chunk = packet[i:i + chunk_size]
        padded = bytearray([0]) + chunk
        if len(padded) < chunk_size + 1:
            padded += bytearray((chunk_size + 1) - len(padded))
        chunks.append(padded)
    return chunks


def merge_runs(colors) -> List[Tuple[int, int, int, int, int]]:
    """
    Turns per-LED colors into (start_led, r, g, b, end_led) segments (1-indexed),
    merging consecutive LEDs that share the same color into one range. Shorter packets
    mean fewer 64-byte HID reports per frame (e.g. a black or flat-color screen is 1 segment).
    """
    segments: List[Tuple[int, int, int, int, int]] = []
    for i, c in enumerate(colors, start=1):
        r, g, b = int(c[0]), int(c[1]), int(c[2])
        if segments and segments[-1][1:4] == (r, g, b):
            segments[-1] = (segments[-1][0], r, g, b, i)
        else:
            segments.append((i, r, g, b, i))
    return segments


def build_sync_screen_packet(segments: List[Tuple[int, int, int, int, int]], seq_id: int) -> List[bytearray]:
    """
    Builds a high-speed setSyncScreen packet.
    segments: list of (start_led, r, g, b, end_led) tuples (1-indexed).
    Packet format:
      [0..1] = 'SC' (0x53, 0x43)
      [2..3] = total_length (UInt16BE) = 7 + len(payload)
      [4]    = seq_id (1-254)
      [5]    = 128 (0x80)
      [6..]  = payload: [start_led, r, g, b, end_led] * N
      [last] = checksum
    """
    payload = bytearray()
    for start_led, r, g, b, end_led in segments:
        payload.extend([
            max(1, min(255, start_led)),
            max(0, min(255, r)),
            max(0, min(255, g)),
            max(0, min(255, b)),
            max(1, min(255, end_led))
        ])

    total_len = 7 + len(payload)
    pkt = bytearray([83, 67])  # 'SC'
    pkt.extend(struct.pack('>H', total_len))
    pkt.append(seq_id & 0xFF)
    pkt.append(ACTION_SET_SYNC_SCREEN)
    pkt.extend(payload)
    pkt.append(checksum(pkt))

    return chunk_for_hid(pkt)


def build_section_led_packet(segments: List[Tuple[int, int, int, int, int]], seq_id: int) -> List[bytearray]:
    """
    Builds a setSectionLED packet.
    Packet format:
      [0..1] = 'RB' (0x52, 0x42)
      [2]    = total_length = 6 + len(payload)
      [3]    = seq_id
      [4]    = 134 (0x86)
      [5..]  = payload: [start_led, r, g, b, end_led] * N
      [last] = checksum
    """
    payload = bytearray()
    for start_led, r, g, b, end_led in segments:
        payload.extend([
            max(1, min(255, start_led)),
            max(0, min(255, r)),
            max(0, min(255, g)),
            max(0, min(255, b)),
            max(1, min(255, end_led))
        ])

    total_len = 6 + len(payload)
    pkt = bytearray([0x52, 0x42, total_len, seq_id & 0xFF, ACTION_SET_SECTION_LED])
    pkt.extend(payload)
    pkt.append(checksum(pkt))

    return chunk_for_hid(pkt)


def build_brightness_packet(level: int, seq_id: int) -> List[bytearray]:
    """Builds a setBrightness packet (level: 0 to 255)."""
    lvl = max(0, min(255, level))
    pkt = bytearray([0x52, 0x42, 7, seq_id & 0xFF, ACTION_SET_BRIGHTNESS, lvl])
    pkt.append(checksum(pkt))
    return chunk_for_hid(pkt)


def build_effect_packet(effect_id: int, speed: int, seq_id: int) -> List[bytearray]:
    """Builds a setLedEffect packet."""
    pkt = bytearray([
        0x52, 0x42, 8, seq_id & 0xFF, ACTION_SET_LED_EFFECT,
        effect_id & 0xFF, speed & 0xFF
    ])
    pkt.append(checksum(pkt))
    return chunk_for_hid(pkt)


def build_turn_off_packet(seq_id: int) -> List[bytearray]:
    """Builds a turnOffLight packet."""
    pkt = bytearray([0x52, 0x42, 6, seq_id & 0xFF, ACTION_TURN_OFF_LIGHT])
    pkt.append(checksum(pkt))
    return chunk_for_hid(pkt)


def build_lamps_amount_packet(amount: int, seq_id: int) -> List[bytearray]:
    """Builds setLampsAmount packet."""
    pkt = bytearray([0x52, 0x42, 7, seq_id & 0xFF, ACTION_SET_LAMPS_AMOUNT, amount & 0xFF])
    pkt.append(checksum(pkt))
    return chunk_for_hid(pkt)


def build_computer_rhythm_packet(effect_idx: int, volume: int, seq_id: int) -> List[bytearray]:
    """Builds setComputerRhythm packet (action 152 / 0x98) with volume (0-100)."""
    vol = max(0, min(100, volume))
    eff = max(0, min(6, effect_idx))
    pkt = bytearray([0x52, 0x42, 8, seq_id & 0xFF, ACTION_SET_COMPUTER_RHYTHM, eff, vol])
    pkt.append(checksum(pkt))
    return chunk_for_hid(pkt)

