from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from web_explorer.contracts import BoundingBox, Checkpoint, Recovery, TargetBundle


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
    interactive: bool = True
    disabled: bool = False
    checked: bool | None = None
    required: bool = False
    read_only: bool = False
    expanded: bool | None = None
    nearby_text: str = ""
    visible_text: str = ""
    geometry: BoundingBox


class FrameObservation(BaseModel):
    """A bounded, frame-aware view of the current page.

    Controls stay in the flat list for action lookup, while this summary gives
    the decision model the document/frame structure it needs for orientation.
    """

    key: str
    url: str = ""
    visible_text: str = ""
    control_refs: list[str] = Field(default_factory=list)


class Observation(BaseModel):
    observation_id: str = "legacy"
    captured_at: str | None = None
    url: str
    title: str
    visible_text: str
    controls: list[ControlObservation]
    frames: list[FrameObservation] = Field(default_factory=list)
    dialogs: list[str] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)
    busy: bool = False
    truncated: bool = False
    digest: str


class SurfaceDriver(Protocol):
    """Perception and action seam. Playwright is one implementation."""

    async def observe(self) -> Observation: ...

    async def act(
        self, action: str, ref: str | None = None, value: Any = None
    ) -> Any: ...

    async def harvest(self, ref: str) -> TargetBundle: ...

    async def resolve(self, target: TargetBundle): ...

    async def act_target(
        self, action: str, target: TargetBundle, value: Any = None
    ) -> Any: ...

    async def recover(self, recovery: Recovery) -> bool: ...

    async def checkpoint_visible(self, checkpoint: Checkpoint) -> bool: ...

    async def checkpoint(self, checkpoint: Checkpoint) -> bool: ...

    async def screenshot(
        self, path: str, redact_values: list[str] | None = None
    ) -> None: ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def storage_snapshot(self) -> dict[str, Any]: ...

    async def start_human_capture(self) -> None: ...

    async def stop_human_capture(self) -> list[dict[str, Any]]: ...
