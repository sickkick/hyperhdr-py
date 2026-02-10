#!/usr/bin/env python
"""Stream LED colors or gradients over HyperHDR websocket."""
from __future__ import annotations

import argparse
import asyncio
import os

from hyperhdr import const
from hyperhdr.stream import HyperHDRLedColorsStream, HyperHDRLedGradientStream


async def run(args: argparse.Namespace) -> None:
    """Run the stream example."""
    stream_cls = (
        HyperHDRLedGradientStream
        if args.mode == "gradient"
        else HyperHDRLedColorsStream
    )
    stream = stream_cls(
        args.host,
        port=args.port,
        token=args.token,
        admin_password=args.password,
        convert_to_jpeg=args.jpeg,
    )

    count = 0
    try:
        async for frame in stream.frames():
            count += 1
            if frame.raw:
                print(
                    f"frame {count}: raw_bytes={len(frame.raw)} source={frame.source}"
                )
            elif frame.image:
                print(
                    f"frame {count}: image_bytes={len(frame.image)} source={frame.source}"
                )
            else:
                print(f"frame {count}: empty source={frame.source}")

            if count >= args.frames:
                break
    finally:
        await stream.stop()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Stream LED colors or gradients from HyperHDR."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HYPERHDR_HOST", "hyperhdr.local"),
        help="HyperHDR host (env: HYPERHDR_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=const.DEFAULT_PORT_UI,
        help=f"HyperHDR UI port (default: {const.DEFAULT_PORT_UI})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HYPERHDR_TOKEN"),
        help="HyperHDR auth token (env: HYPERHDR_TOKEN)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("HYPERHDR_ADMIN_PASSWORD"),
        help="HyperHDR admin password for auth fallback (env: HYPERHDR_ADMIN_PASSWORD)",
    )
    parser.add_argument(
        "--mode",
        choices=["colors", "gradient"],
        default="colors",
        help="Stream mode",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=10,
        help="Number of frames to print",
    )
    parser.add_argument(
        "--jpeg",
        action="store_true",
        help="Convert LED bytes to JPEG (requires Pillow)",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point."""
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
