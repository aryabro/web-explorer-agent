from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from pigeonhole.contracts import BoundingBox, Checkpoint, Recovery, TargetBundle


class SurfaceResolutionError(RuntimeError):
    """Target strategies did not agree on one element, or none resolved."""

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


class ControlObservation(BaseModel):
    ref: str
    frame: str
    element_type: str
    input_type: str | None = None
    role: str | None = None
    accessible_name: str | None = None
    has_value: bool = False
    nearby_text: str = ""
    visible_text: str = ""
    geometry: BoundingBox


class Observation(BaseModel):
    url: str
    title: str
    visible_text: str
    controls: list[ControlObservation]
    digest: str


class SurfaceDriver(Protocol):
    """Perception and action seam. Playwright is one implementation."""

    async def observe(self) -> Observation: ...

    async def act(self, action: str, ref: str | None = None, value: Any = None) -> Any: ...

    async def harvest(self, ref: str) -> TargetBundle: ...

    async def resolve(self, target: TargetBundle): ...

    async def act_target(self, action: str, target: TargetBundle, value: Any = None) -> Any: ...

    async def recover(self, recovery: Recovery) -> bool: ...

    async def checkpoint_visible(self, checkpoint: Checkpoint) -> bool: ...

    async def checkpoint(self, checkpoint: Checkpoint) -> bool: ...

    async def screenshot(self, path: str, redact_values: list[str] | None = None) -> None: ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def storage_snapshot(self) -> dict[str, Any]: ...

    async def start_human_capture(self) -> None: ...

    async def stop_human_capture(self) -> list[dict[str, Any]]: ...
