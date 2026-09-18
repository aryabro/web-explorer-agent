"""Test Bank launch options. App-specific frame names stay out of pigeonhole."""

from __future__ import annotations

from pigeonhole.surface.playwright import PlaywrightSurface

SKIP_FRAMES: tuple[str, ...] = ()
COVER_FRAMES: tuple[str, ...] = ()


async def launch_browser(*, headless: bool = True) -> PlaywrightSurface:
    return await PlaywrightSurface.launch(
        headless=headless,
        skip_frames=SKIP_FRAMES,
        cover_frames=COVER_FRAMES,
    )
