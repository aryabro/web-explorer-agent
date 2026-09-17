from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pigeonhole.contracts import (
    Approval,
    BusinessOutcome,
    Capability,
    Compatibility,
    Contract,
    Execution,
    FatalState,
    Governance,
    Parameter,
    Provenance,
    Recovery,
    Step,
    SuccessCondition,
    SurfaceCompatibility,
    StructuralTarget,
    TargetBundle,
    TenantOverrides,
)
from pigeonhole.discovery import Recording


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability_id: str
    version: str = "1.0.0"
    title: str
    description: str
    goal: str
    target: str
    inputs: dict[str, Parameter]
    outputs: dict[str, Parameter]
    business_outcomes: list[BusinessOutcome]
    fatal_states: list[FatalState] = Field(default_factory=list)
    click_recoveries: list[Recovery] = Field(default_factory=list)
    discovery_hints: list[str] = Field(default_factory=list)
    preferred_frame: str | None = None
    product: str
    product_version: str
    surface_kind: str = "legacy_web"
    tenant: str | None = None
    max_steps: int = 20

    @classmethod
    def load(cls, path: str | Path) -> "Job":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def compile_recording(
    recording: Recording,
    job: Job,
    *,
    trace_ref: str,
    compiler_version: str = "pigeonhole-0.1.0",
) -> Capability:
    if not recording.success:
        raise ValueError(
            f"cannot compile unsuccessful recording: {recording.stop_reason}"
        )
    checkpoint_ids = [
        step.checkpoint.id for step in recording.steps if step.checkpoint is not None
    ]
    extracted = {
        step.action.output for step in recording.steps if step.action.output is not None
    }
    missing = set(job.outputs) - extracted
    if missing:
        raise ValueError(f"recording did not extract outputs: {sorted(missing)}")
    steps: list[Step] = []
    for recorded in recording.steps:
        data = recorded.model_dump(exclude={"before_digest", "after_digest"})
        if recorded.action.type not in {"click", "navigate", "dismiss"}:
            data["checkpoint"] = None
        output_name = recorded.action.output
        if (
            output_name
            and job.outputs[output_name].sensitivity != "none"
            and recorded.target is not None
        ):
            structural = [
                strategy
                for strategy in recorded.target.strategies
                if isinstance(strategy, StructuralTarget)
            ]
            data["target"] = TargetBundle(
                description=recorded.target.description,
                strategies=structural,
            ).model_dump()
        steps.append(Step.model_validate(data))

    return Capability(
        contract=Contract(
            id=job.capability_id,
            version=job.version,
            title=job.title,
            description=job.description,
            inputs=job.inputs,
            outputs=job.outputs,
            business_outcomes=job.business_outcomes,
        ),
        compatibility=Compatibility(
            surface=SurfaceCompatibility(
                kind=job.surface_kind,
                product=job.product,
                product_version=job.product_version,
                entry_point=job.target,
            ),
            tenant=job.tenant,
            tenant_overrides=TenantOverrides(),
        ),
        execution=Execution(
            steps=steps,
            success=SuccessCondition(
                checkpoint_ids=checkpoint_ids[-1:],
                required_outputs=list(job.outputs),
            ),
            fatal_states=job.fatal_states,
        ),
        governance=Governance(
            approval=Approval(status="draft"),
            provenance=Provenance(
                discovered_by=recording.model,
                discovered_at=recording.completed_at,
                trace_ref=trace_ref,
                compiler_version=compiler_version,
            ),
        ),
    )


def save_capability(capability: Capability, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        capability.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )

