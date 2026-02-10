"""Debug helper for HyperHDR JSON API."""
import asyncio
import os

from hyperhdr import client, const
from hyperhdr.stream import (
    HyperHDRLedColorsStream,
    HyperHDRLedGradientStream,
    HyperHDRStreamError,
)


def cb(msg):
    print(msg.get("command"), msg)


async def _admin_login(
    hc: client.HyperHDRClient, admin_password: str | None
) -> str | None:
    if not admin_password:
        return None
    resp = await hc.async_login(password=admin_password)
    print("admin-login", resp)
    if resp and isinstance(resp, dict):
        token = resp.get("info", {}).get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    val = os.environ.get(name)
    if not val:
        return default
    items = [item.strip().lower() for item in val.split(",") if item.strip()]
    return items or default


async def _stream_leds(
    host: str,
    port: int,
    token: str | None,
    mode: str,
    max_frames: int | None,
    convert_to_jpeg: bool,
    auth_mode: str | None = None,
    first_frame_timeout: float | None = None,
    admin_password: str | None = None,
) -> tuple[bool, bool]:
    stream_cls = (
        HyperHDRLedGradientStream
        if mode == "gradient"
        else HyperHDRLedColorsStream
    )
    auth_env = "HYPERHDR_STREAM_AUTH"
    previous_auth = os.environ.get(auth_env)
    if auth_mode:
        os.environ[auth_env] = auth_mode
    try:
        stream = stream_cls(
            host,
            port=port,
            token=token,
            admin_password=admin_password,
            convert_to_jpeg=convert_to_jpeg,
        )
        count = 0
        saw_frame = False
        try:
            await stream.start()
            if first_frame_timeout is not None:
                try:
                    frame = await asyncio.wait_for(
                        stream.wait_for_frame(), timeout=first_frame_timeout
                    )
                except asyncio.TimeoutError:
                    return False, stream.auth_failed
                if frame is None:
                    return False, stream.auth_failed
                saw_frame = True
                count += 1
                if frame.raw:
                    print(
                        f"stream-frame {count}: raw_bytes={len(frame.raw)} "
                        f"source={frame.source}"
                    )
                elif frame.image:
                    print(
                        f"stream-frame {count}: image_bytes={len(frame.image)} "
                        f"source={frame.source}"
                    )
                else:
                    print(f"stream-frame {count}: empty source={frame.source}")
                if max_frames is not None and count >= max_frames:
                    return saw_frame, stream.auth_failed

            async for frame in stream.frames():
                saw_frame = True
                count += 1
                if frame.raw:
                    print(
                        f"stream-frame {count}: raw_bytes={len(frame.raw)} "
                        f"source={frame.source}"
                    )
                elif frame.image:
                    print(
                        f"stream-frame {count}: image_bytes={len(frame.image)} "
                        f"source={frame.source}"
                    )
                else:
                    print(f"stream-frame {count}: empty source={frame.source}")

                if max_frames is not None and count >= max_frames:
                    break
            return saw_frame, stream.auth_failed
        finally:
            await stream.stop()
    finally:
        if previous_auth is None:
            os.environ.pop(auth_env, None)
        else:
            os.environ[auth_env] = previous_auth


async def _stream_leds_with_auth_retry(
    host: str,
    port: int,
    token: str | None,
    mode: str,
    max_frames: int | None,
    convert_to_jpeg: bool,
    admin_password: str | None = None,
) -> None:
    explicit_auth = os.environ.get("HYPERHDR_STREAM_AUTH")
    auto_retry = _env_bool("HYPERHDR_STREAM_AUTH_RETRY", True)
    default_modes = ["json", "query", "header"]

    if explicit_auth and not auto_retry:
        auth_modes = [explicit_auth.strip().lower()]
    elif explicit_auth:
        auth_mode = explicit_auth.strip().lower()
        auth_modes = [auth_mode] + [m for m in default_modes if m != auth_mode]
    else:
        auth_modes = default_modes

    first_frame_timeout = _env_float("HYPERHDR_STREAM_FIRST_FRAME_TIMEOUT", 5.0)
    if first_frame_timeout <= 0:
        first_frame_timeout = None

    timeout_hint = (
        f" within {first_frame_timeout:.1f}s" if first_frame_timeout else ""
    )

    for auth_mode in auth_modes:
        print(f"stream-auth try: {auth_mode}")
        saw_frame, auth_failed = await _stream_leds(
            host,
            port,
            token,
            mode,
            max_frames,
            convert_to_jpeg,
            auth_mode=auth_mode,
            first_frame_timeout=first_frame_timeout,
            admin_password=admin_password,
        )
        if saw_frame:
            return
        if not auth_failed:
            # Auth succeeded (or wasn't required) but no frames arrived.
            # LEDs are likely idle -- don't fall through to other auth modes.
            print(
                f"stream-auth ok ({auth_mode}), but no LED frames received"
                f"{timeout_hint}."
            )
            print(
                "Hint: LEDs may be idle. Try starting an effect or setting "
                "a solid color in HyperHDR to force LED updates."
            )
            return

    modes = ", ".join(auth_modes)
    print(
        "stream-auth failed: authorization failed"
        f"{timeout_hint} after trying auth modes: {modes}."
    )
    print(
        "Hint: use the same token for the stream, set HYPERHDR_ADMIN_PASSWORD "
        "for admin password fallback, or configure HyperHDR to accept "
        "websocket auth for non-admin tokens."
    )


async def _request_token(host: str, port: int, request_id: str, comment: str) -> None:
    async with client.HyperHDRClient(
        host,
        port=port,
        default_callback=cb,
    ) as hc:
        if not hc:
            print("Failed to connect for token request")
            return
        resp = await hc.async_request_token(id=request_id, comment=comment)
        print("request-token", resp)


async def _answer_request(
    host: str,
    port: int,
    auth_token: str | None,
    admin_password: str | None,
    request_id: str,
    accept: bool,
) -> None:
    async with client.HyperHDRClient(
        host,
        port=port,
        token=auth_token,
        default_callback=cb,
    ) as hc:
        if not hc:
            print("Failed to connect for admin approval")
            return
        admin_login_token = await _admin_login(hc, admin_password)
        resp = await hc.async_answer_request(id=request_id, accept=accept)
        print("answer-request", resp)


async def main() -> None:
    host = os.environ.get("HYPERHDR_HOST", "10.12.0.12") #ip of hyperhdr
    port = int(os.environ.get("HYPERHDR_PORT", "19444"))
    stream_port = int(
        os.environ.get("HYPERHDR_STREAM_PORT", str(const.DEFAULT_PORT_UI))
    )
    token = os.environ.get("HYPERHDR_TOKEN", "66e9cced-23e9-471b-afe2-bf23fae41db4") #token of hyperhdr
    admin_token = os.environ.get("HYPERHDR_ADMIN_TOKEN")
    admin_password = os.environ.get("HYPERHDR_ADMIN_PASSWORD")
    auth_token = admin_token or token or None

    request_id = os.environ.get("HYPERHDR_REQUEST_ID")
    request_comment = os.environ.get("HYPERHDR_REQUEST_COMMENT", "hyperhdr-py debug")
    auto_approve = _env_bool("HYPERHDR_AUTO_APPROVE")
    accept = _env_bool("HYPERHDR_ACCEPT", True)
    admin_login_token: str | None = None

    if request_id:
        await _request_token(host, port, request_id, request_comment)
        if auto_approve:
            if not admin_token and not admin_password:
                print(
                    "HYPERHDR_AUTO_APPROVE set but HYPERHDR_ADMIN_PASSWORD or "
                    "HYPERHDR_ADMIN_TOKEN is not set."
                )
            else:
                await _answer_request(
                    host, port, auth_token, admin_password, request_id, accept
                )

    async with client.HyperHDRClient(
        host,
        port=port,
        token=auth_token,
        default_callback=cb,
    ) as hc:
        if not hc:
            print("Failed to connect")
            return
        if admin_password and not os.environ.get("HYPERHDR_TOKEN") and not admin_token:
            print(
                "admin password set but no token provided; if Local API "
                "Authentication is enabled, set HYPERHDR_TOKEN too."
            )
        token_required = await hc.async_is_auth_required()
        admin_required = await hc.async_is_admin_required()
        print("token-required", token_required)
        print("admin-required", admin_required)
        admin_login_token = await _admin_login(hc, admin_password)
        if (
            admin_required
            and admin_required.get("info", {}).get("adminRequired")
        ):
            if admin_password:
                print(
                    "admin-required is true; will use HYPERHDR_ADMIN_PASSWORD "
                    "for admin endpoints and stream fallback."
                )
            else:
                print(
                    "admin-required is true; set HYPERHDR_ADMIN_PASSWORD to access "
                    "admin endpoints and as stream auth fallback."
                )
        await hc.async_get_serverinfo()

        get_config = _env_bool(
            "HYPERHDR_GET_CONFIG",
            admin_token is not None or admin_password is not None,
        )
        if get_config:
            config = await hc.async_get_config()
            print("config", config)
        else:
            print("config skipped (admin token not set)")

        if (admin_token or admin_password) and _env_bool("HYPERHDR_LIST_PENDING"):
            pending = await hc.async_get_pending_token_requests()
            print("pending-requests", pending)

        if (admin_token or admin_password) and _env_bool("HYPERHDR_LIST_TOKENS"):
            tokens = await hc.async_get_token_list()
            print("token-list", tokens)

        if _env_bool("HYPERHDR_STREAM"):
            mode = os.environ.get("HYPERHDR_STREAM_MODE", "colors").strip().lower()
            if mode not in {"colors", "gradient"}:
                print(
                    "HYPERHDR_STREAM_MODE must be 'colors' or 'gradient'; "
                    f"got {mode!r}, defaulting to 'colors'."
                )
                mode = "colors"
            frames = _env_int("HYPERHDR_STREAM_FRAMES", 10)
            max_frames = frames if frames > 0 else None
            convert_to_jpeg = _env_bool("HYPERHDR_STREAM_JPEG")
            stream_token = (
                os.environ.get("HYPERHDR_STREAM_TOKEN")
                or admin_token
                or admin_login_token
                or auth_token
            )
            if stream_token is None and not admin_password:
                print(
                    "stream token not set; if HyperHDR requires authorization, "
                    "set HYPERHDR_STREAM_TOKEN or HYPERHDR_ADMIN_PASSWORD."
                )
            try:
                await _stream_leds_with_auth_retry(
                    host,
                    stream_port,
                    stream_token,
                    mode,
                    max_frames,
                    convert_to_jpeg,
                    admin_password=admin_password,
                )
            except HyperHDRStreamError as exc:
                print("stream-error", exc)

        await asyncio.sleep(2)


asyncio.run(main())
