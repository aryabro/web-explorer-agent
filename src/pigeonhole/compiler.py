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
    StructuralTarget,
    SuccessCondition,
    SurfaceCompatibility,
    TargetBundle,
    TenantOverrides,
)
from pigeonhole.discovery import Recording
from pigeonhole.tenants import compatibility_fingerprint


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
    vendor: str | None = None
    version_range: str | None = None
    tenant: str | None = None
    max_steps: int = 20
    discovery_timeout_seconds: int = 300
    max_no_progress_steps: int = 3

    @classmethod
    def load(cls, path: str | Path) -> "Job":
        return cls.model_validate(
            yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        )


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
    failed_steps = [
        step.id for step in recording.steps if step.execution_status != "succeeded"
    ]
    if failed_steps:
        raise ValueError(f"recording contains failed actions: {failed_steps}")
    unverified_transitions = [
        step.id
        for step in recording.steps
        if step.action.type in {"click", "navigate", "dismiss"}
        and (step.checkpoint is None or step.checkpoint_verified is not True)
    ]
    if unverified_transitions:
        raise ValueError(
            "recording contains unverified screen transitions: "
            f"{unverified_transitions}"
        )
    checkpoint_ids = [
        step.checkpoint.id for step in recording.steps if step.checkpoint is not None
    ]
    postcondition_id = checkpoint_ids[-1] if checkpoint_ids else None
    extracted = {
        step.action.output for step in recording.steps if step.action.output is not None
    }
    missing = set(job.outputs) - extracted
    if missing:
        raise ValueError(f"recording did not extract outputs: {sorted(missing)}")
    steps: list[Step] = []
    for recorded in recording.steps:
        data = recorded.model_dump(
            exclude={
                "before_digest",
                "after_digest",
                "execution_status",
                "result_detail",
                "checkpoint_verified",
                "checkpoint_source",
            }
        )
        if recorded.action.type not in {"click", "navigate", "dismiss"}:
            data["checkpoint"] = None
        elif recorded.checkpoint is not None:
            data["checkpoint"] = recorded.checkpoint.model_dump()
            data["checkpoint"]["role"] = (
                "postcondition"
                if recorded.checkpoint.id == postcondition_id
                else "intermediate"
            )
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
                vendor=job.vendor,
                version_range=job.version_range,
            ),
            tenant=job.tenant,
            tenant_overrides=TenantOverrides(),
            fingerprint=compatibility_fingerprint(
                vendor=job.vendor,
                product=job.product,
                product_version=job.product_version,
                entry_point=job.target,
            ),
        ),
        execution=Execution(
            steps=steps,
            success=SuccessCondition(
                checkpoint_ids=[postcondition_id] if postcondition_id else [],
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
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(capability.model_dump_json(indent=2, exclude_none=True))
        handle.write("\n")
