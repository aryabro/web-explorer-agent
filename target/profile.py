"""Test Bank launch options. App-specific frame names stay out of web_explorer."""

from __future__ import annotations

from web_explorer.surface.playwright import PlaywrightSurface

SKIP_FRAMES: tuple[str, ...] = ()
COVER_FRAMES: tuple[str, ...] = ()
PREFERRED_FRAME = "night-work"


async def launch_browser(*, headless: bool = True) -> PlaywrightSurface:
    return await PlaywrightSurface.launch(
        headless=headless,
        skip_frames=SKIP_FRAMES,
        cover_frames=COVER_FRAMES,
        preferred_frame=PREFERRED_FRAME,
    )
