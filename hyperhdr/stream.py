"""Websocket streaming helpers for HyperHDR LED colors and gradients."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlencode

from hyperhdr import client, const

try:
    from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType
except ImportError as exc:  # pragma: no cover - optional dependency
    ClientSession = None  # type: ignore[assignment]
    ClientWebSocketResponse = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    _AIOHTTP_IMPORT_ERROR: Exception | None = exc
else:  # pragma: no cover - exercised when aiohttp is installed
    _AIOHTTP_IMPORT_ERROR = None

_LOGGER = logging.getLogger(__name__)

_IMAGE_STREAM_PREFIXES = (
    "data:image/jpg;base64,",
    "data:image/jpeg;base64,",
)
_CMD_LEDSTREAM_UPDATE = f"{const.KEY_LEDCOLORS}-ledstream-update"
_CMD_IMAGESTREAM_UPDATE = f"{const.KEY_LEDCOLORS}-{const.KEY_IMAGE_STREAM}-update"

_WS_AUTH_ENV = "HYPERHDR_STREAM_AUTH"
_WS_AUTH_HEADER_ENV = "HYPERHDR_STREAM_AUTH_HEADER"
_WS_AUTH_HEADER_PREFIX_ENV = "HYPERHDR_STREAM_AUTH_HEADER_PREFIX"
_WS_AUTH_QUERY_ENV = "HYPERHDR_STREAM_AUTH_QUERY"
_WS_AUTH_MODES = {"json", "header", "query"}


def _normalize_ws_auth_mode(value: str | None) -> str:
    if not value:
        return "json"
    mode = value.strip().lower()
    if mode in _WS_AUTH_MODES:
        return mode
    _LOGGER.warning(
        "Unknown %s=%s; defaulting to json", _WS_AUTH_ENV, value
    )
    return "json"


@dataclass(slots=True)
class LedStreamFrame:
    """A single LED stream frame."""

    raw: bytes | None
    image: bytes | None
    source: str

    def as_flat_list(self) -> list[int] | None:
        """Return LED values as a flat RGB list."""
        if self.raw is None:
            return None
        return list(self.raw)

    def as_rgb_tuples(self) -> list[tuple[int, int, int]] | None:
        """Return LED values as list of (r, g, b) tuples."""
        if self.raw is None:
            return None
        if len(self.raw) % 3 != 0:
            return None
        return [
            (self.raw[i], self.raw[i + 1], self.raw[i + 2])
            for i in range(0, len(self.raw), 3)
        ]


class HyperHDRStreamError(Exception):
    """Base error for HyperHDR websocket streaming."""


class _HyperHDRLedStreamBase:
    """Shared helpers for HyperHDR LED websocket streams."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str | None,
        label: str,
        start_subcommand: str,
        *,
        admin_password: str | None = None,
        heartbeat: float = 30.0,
        reconnect_delay: float = 5.0,
        convert_to_jpeg: bool = False,
        jpeg_height: int = 20,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._admin_password = admin_password
        self._label = label
        self._start_subcommand = start_subcommand
        self._heartbeat = heartbeat
        self._reconnect_delay = reconnect_delay
        self._convert_to_jpeg = convert_to_jpeg
        self._jpeg_height = jpeg_height

        self._ws_auth_mode = _normalize_ws_auth_mode(os.getenv(_WS_AUTH_ENV))
        self._ws_auth_header = os.getenv(_WS_AUTH_HEADER_ENV) or "Authorization"
        ws_auth_prefix = os.getenv(_WS_AUTH_HEADER_PREFIX_ENV)
        if ws_auth_prefix is None:
            ws_auth_prefix = "Bearer"
        self._ws_auth_header_prefix = ws_auth_prefix.strip()
        self._ws_auth_query_param = os.getenv(_WS_AUTH_QUERY_ENV) or "token"

        self._auth_warning_logged = False
        self._auth_failed = False
        self._stream_task: asyncio.Task[None] | None = None
        self._frame_cond = asyncio.Condition()
        self._last_frame: LedStreamFrame | None = None
        self._stopped = asyncio.Event()

    @property
    def last_frame(self) -> LedStreamFrame | None:
        """Return the most recent frame."""
        return self._last_frame

    @property
    def auth_failed(self) -> bool:
        """Return whether the most recent authentication attempt failed."""
        return self._auth_failed

    async def start(self) -> None:
        """Start the background websocket stream."""
        self._require_aiohttp()
        if self._stream_task and not self._stream_task.done():
            return
        self._stopped.clear()
        self._stream_task = asyncio.create_task(self._stream_worker())

    async def stop(self) -> None:
        """Stop the background websocket stream."""
        self._stopped.set()
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        async with self._frame_cond:
            self._last_frame = None
            self._frame_cond.notify_all()

    async def wait_for_frame(self) -> LedStreamFrame | None:
        """Wait for the next frame."""
        async with self._frame_cond:
            if self._stopped.is_set():
                return None
            await self._frame_cond.wait()
            if self._stopped.is_set():
                return None
            return self._last_frame

    async def frames(self) -> AsyncIterator[LedStreamFrame]:
        """Async iterator over incoming frames."""
        await self.start()
        while True:
            frame = await self.wait_for_frame()
            if frame is None:
                return
            yield frame

    @staticmethod
    def _require_aiohttp() -> None:
        if ClientSession is None or WSMsgType is None:
            raise HyperHDRStreamError(
                "aiohttp is required for websocket streaming. "
                "Install it with `pip install aiohttp`."
            ) from _AIOHTTP_IMPORT_ERROR

    def _log_ws_error(
        self, ws: ClientWebSocketResponse, msg: Any, label: str
    ) -> None:
        """Log websocket errors with reduced noise for known protocol glitches."""
        err = ws.exception() or getattr(msg, "data", None)
        err_text = str(err) if err else "unknown websocket error"
        if "fragmented control frame" in err_text.lower():
            _LOGGER.debug("HyperHDR %s stream websocket error: %s", label, err_text)
            return
        _LOGGER.warning("HyperHDR %s stream websocket error: %s", label, err_text)

    def _log_stream_text(self, msg: str) -> None:
        """Log stream text with context."""
        _LOGGER.debug("HyperHDR %s stream text: %s", self._label, msg)

    def _log_auth_error(self, error_text: str) -> None:
        """Log a stream authorization error once."""
        if self._auth_warning_logged:
            return
        if self._token:
            _LOGGER.warning(
                "HyperHDR %s stream authorization failed for %s; "
                "check the configured token. Error: %s",
                self._label,
                self._host,
                error_text,
            )
        else:
            _LOGGER.warning(
                "HyperHDR %s stream requires authorization but no token is "
                "configured for %s. Error: %s",
                self._label,
                self._host,
                error_text,
            )
        self._auth_warning_logged = True
        self._auth_failed = True

    def _maybe_log_auth_error(self, payload: dict[str, Any]) -> None:
        """Log authorization errors if present in the payload."""
        error = payload.get("error")
        if not error:
            return
        error_text = error if isinstance(error, str) else str(error)
        if "authorization" in error_text.lower():
            self._log_auth_error(error_text)

    def _ws_connect_details(
        self,
    ) -> tuple[str, dict[str, str] | None, str]:
        """Return websocket URL, headers, and a redacted URL for logging."""
        base_url = f"ws://{self._host}:{self._port}/json-rpc"
        url = base_url
        log_url = base_url
        headers: dict[str, str] | None = None

        if self._ws_auth_mode == "query" and self._token:
            query = urlencode({self._ws_auth_query_param: self._token})
            url = f"{base_url}?{query}"
            redacted_query = urlencode(
                {self._ws_auth_query_param: "<redacted>"}
            )
            log_url = f"{base_url}?{redacted_query}"

        if self._ws_auth_mode == "header" and self._token:
            header_value = self._token
            if self._ws_auth_header_prefix:
                header_value = (
                    f"{self._ws_auth_header_prefix} {self._token}"
                )
            headers = {self._ws_auth_header: header_value}

        return url, headers, log_url

    async def _await_ws_command(
        self,
        ws: ClientWebSocketResponse,
        command: str,
        timeout: float = 5.0,
    ) -> dict[str, Any] | None:
        """Wait for a specific command response on the websocket."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    _LOGGER.debug(
                        "HyperHDR stream returned non-JSON text: %s", msg.data
                    )
                    continue
                if payload.get(const.KEY_COMMAND) == command:
                    return payload
            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                return None
        return None

    async def _try_password_login(self, ws: ClientWebSocketResponse) -> bool:
        """Attempt WebSocket login using admin password.

        Returns True if login succeeded or auth is not required.
        """
        if not self._admin_password:
            return False
        _LOGGER.debug(
            "HyperHDR %s stream: attempting admin password login for %s",
            self._label,
            self._host,
        )
        await ws.send_json(
            {
                const.KEY_COMMAND: const.KEY_AUTHORIZE,
                const.KEY_SUBCOMMAND: const.KEY_LOGIN,
                const.KEY_PASSWORD: self._admin_password,
                const.KEY_TAN: 97,
            }
        )
        login_resp = await self._await_ws_command(ws, const.KEY_AUTHORIZE_LOGIN)
        if login_resp is not None and client.LoginResponseOK(login_resp):
            _LOGGER.debug(
                "HyperHDR %s stream: admin password login succeeded for %s",
                self._label,
                self._host,
            )
            return True
        _LOGGER.debug(
            "HyperHDR %s stream: admin password login failed for %s",
            self._label,
            self._host,
        )
        return False

    async def _authorize_ws(self, ws: ClientWebSocketResponse) -> bool:
        """Authorize the websocket connection if required."""
        # --- Attempt 1: token login ---
        if self._token:
            await ws.send_json(
                {
                    const.KEY_COMMAND: const.KEY_AUTHORIZE,
                    const.KEY_SUBCOMMAND: const.KEY_LOGIN,
                    const.KEY_TOKEN: self._token,
                    const.KEY_TAN: 99,
                }
            )
            login_resp = await self._await_ws_command(ws, const.KEY_AUTHORIZE_LOGIN)
            if login_resp is None:
                # No response — server likely does not require auth.
                return True
            if client.LoginResponseOK(login_resp):
                return True

            # Token login failed — try admin password before giving up.
            if await self._try_password_login(ws):
                return True

            # Check whether auth is actually required.
            await ws.send_json(
                {
                    const.KEY_COMMAND: const.KEY_AUTHORIZE,
                    const.KEY_SUBCOMMAND: const.KEY_TOKEN_REQUIRED,
                    const.KEY_TAN: 98,
                }
            )
            required_resp = await self._await_ws_command(
                ws, f"{const.KEY_AUTHORIZE}-{const.KEY_TOKEN_REQUIRED}"
            )
            if (
                required_resp
                and required_resp.get(const.KEY_SUCCESS, True)
                and not required_resp.get(const.KEY_INFO, {}).get(
                    const.KEY_REQUIRED, False
                )
            ):
                return True

            if not self._auth_warning_logged:
                _LOGGER.warning(
                    "HyperHDR %s stream authorization failed for %s; "
                    "check the configured token or admin password",
                    self._label,
                    self._host,
                )
                self._auth_warning_logged = True
            self._auth_failed = True
            return False

        # --- Attempt 2: no token — try admin password directly ---
        if self._admin_password:
            if await self._try_password_login(ws):
                return True
            # Password login failed.
            self._log_auth_error(
                "Admin password login failed; check HYPERHDR_ADMIN_PASSWORD"
            )
            return False

        # --- Attempt 3: no credentials — check if auth is even required ---
        await ws.send_json(
            {
                const.KEY_COMMAND: const.KEY_AUTHORIZE,
                const.KEY_SUBCOMMAND: const.KEY_TOKEN_REQUIRED,
                const.KEY_TAN: 98,
            }
        )
        required_resp = await self._await_ws_command(
            ws, f"{const.KEY_AUTHORIZE}-{const.KEY_TOKEN_REQUIRED}"
        )
        if not required_resp:
            return True
        if not required_resp.get(const.KEY_SUCCESS, True):
            return True
        if required_resp.get(const.KEY_INFO, {}).get(const.KEY_REQUIRED, False):
            if not self._auth_warning_logged:
                _LOGGER.warning(
                    "HyperHDR %s stream requires authorization but no token or "
                    "admin password is configured for %s; stream disabled",
                    self._label,
                    self._host,
                )
                self._auth_warning_logged = True
            self._auth_failed = True
            return False
        return True

    async def _publish_frame(self, frame: LedStreamFrame) -> None:
        async with self._frame_cond:
            self._last_frame = frame
            self._frame_cond.notify_all()

    @staticmethod
    def _flatten_leds(leds: list[Any]) -> list[int] | None:
        if not leds:
            return None
        if isinstance(leds[0], list):
            flattened: list[int] = []
            for row in leds:
                if not isinstance(row, list):
                    return None
                try:
                    flattened.extend(int(v) for v in row)
                except (TypeError, ValueError):
                    return None
            return flattened
        try:
            return [int(v) for v in leds]
        except (TypeError, ValueError):
            return None

    @classmethod
    def _leds_to_bytes(cls, leds: list[Any]) -> bytes | None:
        flattened = cls._flatten_leds(leds)
        if not flattened:
            return None
        try:
            return bytes(flattened)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode_image_stream(image_data: str) -> bytes | None:
        for prefix in _IMAGE_STREAM_PREFIXES:
            if image_data.startswith(prefix):
                encoded = image_data.removeprefix(prefix)
                try:
                    return base64.b64decode(encoded)
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.warning("Failed to decode LED imagestream: %s", err)
                    return None
        return None

    def _led_bytes_to_jpeg(self, data: bytes) -> bytes | None:
        if not self._convert_to_jpeg:
            return None
        if len(data) % 3 != 0:
            _LOGGER.debug("Received incomplete LED data: %d bytes", len(data))
            return None
        total_leds = len(data) // 3
        if total_leds <= 0:
            return None

        try:
            from PIL import Image  # type: ignore[import-not-found]
        except ImportError as err:  # pragma: no cover - optional dependency
            _LOGGER.debug("Pillow not installed; cannot build JPEG: %s", err)
            return None

        resample = (
            Image.Resampling.NEAREST
            if hasattr(Image, "Resampling")
            else Image.NEAREST
        )
        img = Image.frombytes("RGB", (total_leds, 1), data)
        img = img.resize((total_leds, self._jpeg_height), resample=resample)
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format="JPEG")
        return img_byte_arr.getvalue()

    def _handle_binary(self, data: bytes) -> LedStreamFrame | None:
        if data.startswith(b"\xff\xd8"):
            return LedStreamFrame(raw=None, image=data, source="binary-jpeg")
        image = self._led_bytes_to_jpeg(data)
        return LedStreamFrame(raw=data, image=image, source="binary-leds")

    def _handle_text(self, msg: str) -> LedStreamFrame | None:
        try:
            payload = json.loads(msg)
        except json.JSONDecodeError:
            _LOGGER.debug("HyperHDR stream returned non-JSON text: %s", msg)
            return None

        self._maybe_log_auth_error(payload)
        command = payload.get(const.KEY_COMMAND)

        if command == _CMD_LEDSTREAM_UPDATE:
            result = payload.get(const.KEY_RESULT)
            if isinstance(result, dict):
                leds = result.get(const.KEY_LEDS)
                if isinstance(leds, list):
                    raw = self._leds_to_bytes(leds)
                    if raw:
                        image = self._led_bytes_to_jpeg(raw)
                        return LedStreamFrame(
                            raw=raw,
                            image=image,
                            source="ledstream-update",
                        )
            return None

        if command == _CMD_IMAGESTREAM_UPDATE:
            result = payload.get(const.KEY_RESULT)
            if isinstance(result, dict):
                image_data = result.get(const.KEY_IMAGE)
                if isinstance(image_data, str):
                    image = self._decode_image_stream(image_data)
                    if image:
                        return LedStreamFrame(
                            raw=None,
                            image=image,
                            source="imagestream-update",
                        )
            return None

        self._log_stream_text(msg)
        return None

    async def _stream_worker(self) -> None:
        """Background task to maintain WebSocket connection and update frames."""
        while True:
            url, headers, log_url = self._ws_connect_details()
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(
                        url, heartbeat=self._heartbeat, headers=headers
                    ) as ws:
                        _LOGGER.debug(
                            "Connected to HyperHDR %s stream at %s (auth=%s)",
                            self._label,
                            log_url,
                            self._ws_auth_mode,
                        )
                        if self._ws_auth_mode == "json":
                            if not await self._authorize_ws(ws):
                                await ws.close()
                                continue

                        await ws.send_json(
                            {
                                const.KEY_COMMAND: const.KEY_LEDCOLORS,
                                const.KEY_SUBCOMMAND: self._start_subcommand,
                                const.KEY_TAN: 1,
                            }
                        )
                        _LOGGER.debug(
                            "Sent %s start command", self._start_subcommand
                        )

                        async for msg in ws:
                            if msg.type == WSMsgType.BINARY:
                                frame = self._handle_binary(msg.data)
                                if frame:
                                    await self._publish_frame(frame)
                            elif msg.type == WSMsgType.TEXT:
                                frame = self._handle_text(msg.data)
                                if frame:
                                    await self._publish_frame(frame)
                            elif msg.type == WSMsgType.CLOSED:
                                _LOGGER.warning(
                                    "HyperHDR %s stream connection closed", self._label
                                )
                                break
                            elif msg.type == WSMsgType.ERROR:
                                self._log_ws_error(ws, msg, self._label)
                                break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error(
                    "HyperHDR %s stream connection failed: %s", self._label, err
                )

            if self._stopped.is_set():
                return

            _LOGGER.debug(
                "HyperHDR %s stream worker waiting %.1fs before reconnect",
                self._label,
                self._reconnect_delay,
            )
            await asyncio.sleep(self._reconnect_delay)


class HyperHDRLedColorsStream(_HyperHDRLedStreamBase):
    """Websocket stream for LED color imagestreams."""

    def __init__(
        self,
        host: str,
        port: int = const.DEFAULT_PORT_UI,
        token: str | None = None,
        *,
        admin_password: str | None = None,
        heartbeat: float = 30.0,
        reconnect_delay: float = 5.0,
        convert_to_jpeg: bool = False,
        jpeg_height: int = 20,
    ) -> None:
        super().__init__(
            host,
            port,
            token,
            "LED colors",
            const.KEY_IMAGE_STREAM_START,
            admin_password=admin_password,
            heartbeat=heartbeat,
            reconnect_delay=reconnect_delay,
            convert_to_jpeg=convert_to_jpeg,
            jpeg_height=jpeg_height,
        )


class HyperHDRLedGradientStream(_HyperHDRLedStreamBase):
    """Websocket stream for LED color gradients."""

    def __init__(
        self,
        host: str,
        port: int = const.DEFAULT_PORT_UI,
        token: str | None = None,
        *,
        admin_password: str | None = None,
        heartbeat: float = 30.0,
        reconnect_delay: float = 5.0,
        convert_to_jpeg: bool = False,
        jpeg_height: int = 20,
    ) -> None:
        super().__init__(
            host,
            port,
            token,
            "LED gradient",
            const.KEY_LED_STREAM_START,
            admin_password=admin_password,
            heartbeat=heartbeat,
            reconnect_delay=reconnect_delay,
            convert_to_jpeg=convert_to_jpeg,
            jpeg_height=jpeg_height,
        )
