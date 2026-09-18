from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field

from pigeonhole.contracts import (
    Capability,
    SemanticTarget,
    StrictModel,
    TargetOverride,
    TenantOverrides,
)


class TenantProfile(StrictModel):
    tenant: str
    entry_point: str
    vendor: str | None = None
    product_version: str | None = None
    version_range: str | None = None
    overrides: TenantOverrides = Field(default_factory=TenantOverrides)

    @classmethod
    def load(cls, path: str | Path) -> "TenantProfile":
        return cls.model_validate(
            yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        )


def find_profile(tenant: str, directory: str | Path = "tenants") -> TenantProfile:
    path = Path(directory) / f"{tenant}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no tenant profile at {path}")
    return TenantProfile.load(path)


def compatibility_fingerprint(
    *,
    vendor: str | None,
    product: str,
    product_version: str,
    entry_point: str,
) -> str:
    return "|".join(
        part for part in (vendor, product, product_version, entry_point) if part
    )


def apply_tenant(capability: Capability, profile: TenantProfile) -> Capability:
    """Return a copy specialized to a tenant via sparse label/checkpoint overlays."""
    specialized = capability.model_copy(deep=True)
    specialized.compatibility.tenant = profile.tenant
    specialized.compatibility.surface.entry_point = profile.entry_point
    if profile.vendor:
        specialized.compatibility.surface.vendor = profile.vendor
    if profile.product_version:
        specialized.compatibility.surface.product_version = profile.product_version
    if profile.version_range:
        specialized.compatibility.surface.version_range = profile.version_range
    specialized.compatibility.tenant_overrides = profile.overrides.model_copy(deep=True)
    specialized.compatibility.fingerprint = compatibility_fingerprint(
        vendor=specialized.compatibility.surface.vendor,
        product=specialized.compatibility.surface.product,
        product_version=profile.product_version
        or specialized.compatibility.surface.product_version,
        entry_point=profile.entry_point,
    )
    for step in specialized.execution.steps:
        target_patch = profile.overrides.targets.get(step.id)
        if target_patch is not None and step.target is not None:
            _apply_target_override(step.target.strategies, target_patch)
            if target_patch.description:
                step.target.description = target_patch.description
        if step.checkpoint is not None:
            checkpoint_patch = profile.overrides.checkpoints.get(step.checkpoint.id)
            if checkpoint_patch is not None and checkpoint_patch.expected is not None:
                step.checkpoint.expected = checkpoint_patch.expected
    return specialized


def _apply_target_override(strategies: list, patch: TargetOverride) -> None:
    for strategy in strategies:
        if not isinstance(strategy, SemanticTarget):
            continue
        if patch.adjacent_text is not None:
            strategy.adjacent_text = patch.adjacent_text
        if patch.name is not None:
            strategy.name = patch.name
        if patch.test_id is not None:
            strategy.test_id = patch.test_id
