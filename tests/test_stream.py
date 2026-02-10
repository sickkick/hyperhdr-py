"""Tests for websocket LED stream parsing."""
from __future__ import annotations

import base64
import json

from hyperhdr import const
from hyperhdr import stream as stream_mod


def _make_stream() -> stream_mod._HyperHDRLedStreamBase:
    return stream_mod._HyperHDRLedStreamBase(
        "localhost",
        const.DEFAULT_PORT_UI,
        None,
        "test",
        const.KEY_LED_STREAM_START,
    )


def test_handle_binary_jpeg() -> None:
    stream = _make_stream()
    data = b"\xff\xd8\xff\xe0"
    frame = stream._handle_binary(data)
    assert frame is not None
    assert frame.image == data
    assert frame.raw is None
    assert frame.source == "binary-jpeg"


def test_handle_binary_led_bytes() -> None:
    stream = _make_stream()
    data = b"\x00\x01\x02\x03\x04\x05"
    frame = stream._handle_binary(data)
    assert frame is not None
    assert frame.raw == data
    assert frame.image is None
    assert frame.source == "binary-leds"


def test_ledstream_update_flat_list() -> None:
    stream = _make_stream()
    payload = {
        "command": "ledcolors-ledstream-update",
        "result": {"leds": [1, 2, 3, 4, 5, 6]},
    }
    frame = stream._handle_text(json.dumps(payload))
    assert frame is not None
    assert frame.raw == bytes([1, 2, 3, 4, 5, 6])
    assert frame.image is None
    assert frame.as_rgb_tuples() == [(1, 2, 3), (4, 5, 6)]


def test_ledstream_update_nested_list() -> None:
    stream = _make_stream()
    payload = {
        "command": "ledcolors-ledstream-update",
        "result": {"leds": [[7, 8, 9], [10, 11, 12]]},
    }
    frame = stream._handle_text(json.dumps(payload))
    assert frame is not None
    assert frame.raw == bytes([7, 8, 9, 10, 11, 12])
    assert frame.as_flat_list() == [7, 8, 9, 10, 11, 12]


def test_imagestream_update_base64() -> None:
    stream = _make_stream()
    image = b"jpegdata"
    payload = {
        "command": "ledcolors-imagestream-update",
        "result": {
            "image": "data:image/jpg;base64," + base64.b64encode(image).decode()
        },
    }
    frame = stream._handle_text(json.dumps(payload))
    assert frame is not None
    assert frame.image == image
    assert frame.raw is None
    assert frame.source == "imagestream-update"


def test_invalid_payload_returns_none() -> None:
    stream = _make_stream()
    assert stream._handle_text("not-json") is None


def test_invalid_leds_returns_none() -> None:
    stream = _make_stream()
    payload = {
        "command": "ledcolors-ledstream-update",
        "result": {"leds": ["x", "y", "z"]},
    }
    assert stream._handle_text(json.dumps(payload)) is None
