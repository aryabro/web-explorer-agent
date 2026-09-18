"""Surface-agnostic observation assembly and target harvest.

Adapters supply raw control snapshots; this module never imports Playwright.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pigeonhole.contracts import (
    BoundingBox,
    GeometryTarget,
    SemanticTarget,
    StructuralTarget,
    TargetBundle,
)
from pigeonhole.surface.base import ControlObservation, Observation


def control_from_snapshot(
    ref: str, frame: str, item: dict[str, Any]
) -> ControlObservation:
    return ControlObservation(
        ref=ref,
        frame=frame,
        element_type=item["tag"],
        input_type=item.get("inputType"),
        role=item.get("role"),
        accessible_name=item.get("name"),
        has_value=bool(item.get("hasValue")),
        nearby_text=item.get("nearbyText") or "",
        visible_text=item.get("visibleText") or "",
        geometry=BoundingBox(**item["box"]),
    )


def observation_digest(visible_text: str, controls: list[ControlObservation]) -> str:
    payload = {
        "text": visible_text,
        "controls": [
            (
                control.frame,
                control.element_type,
                control.role,
                control.has_value,
                control.nearby_text,
                control.visible_text,
            )
            for control in controls
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]


def assemble_observation(
    *,
    url: str,
    title: str,
    visible_text: str,
    controls: list[ControlObservation],
) -> Observation:
    return Observation(
        url=url,
        title=title,
        visible_text=visible_text,
        controls=controls,
        digest=observation_digest(visible_text, controls),
    )


def harvest_target(frame: str, item: dict[str, Any]) -> TargetBundle:
    nearby = " ".join((item.get("nearbyText") or "").split())
    adjacent = nearby.split("\n", 1)[0][:100] if nearby else None
    semantic_name = item.get("name") or item.get("visibleText") or None
    strategies = [
        SemanticTarget(
            frame=frame,
            test_id=item.get("testId"),
            role=item.get("role"),
            name=semantic_name,
            adjacent_text=adjacent,
            element_type=item["tag"],
        ),
        StructuralTarget(
            frame=frame,
            table_index=item.get("tableIndex"),
            row_index=item.get("rowIndex"),
            cell_index=item.get("cellIndex"),
            element_type=item["tag"],
            type_index=item.get("typeIndex") or 0,
        ),
        GeometryTarget(
            frame=frame,
            anchor_text=adjacent or semantic_name or item["tag"],
            element_type=item["tag"],
            expected_box=BoundingBox(**item["box"]),
        ),
    ]
    return TargetBundle(
        description=nearby or semantic_name or item["tag"],
        strategies=strategies,
    )
