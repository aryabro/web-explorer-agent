from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sensitivity(StrEnum):
    NONE = "none"
    IDENTIFIER = "identifier"
    PII = "pii"
    FINANCIAL = "financial"
    SECRET = "secret"


class Risk(StrEnum):
    SAFE = "safe"
    MUTATING = "mutating"
    IRREVERSIBLE = "irreversible"


class FailureCode(StrEnum):
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    LOCATOR_UNRESOLVED = "LOCATOR_UNRESOLVED"
    LOCATOR_CONFLICT = "LOCATOR_CONFLICT"
    POLICY_DENIED = "POLICY_DENIED"
    POLICY_REQUIRES_CONFIRMATION = "POLICY_REQUIRES_CONFIRMATION"
    INPUT_INVALID = "INPUT_INVALID"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    OUTPUT_MISSING = "OUTPUT_MISSING"
    SUCCESS_CONDITION_FAILED = "SUCCESS_CONDITION_FAILED"
    ACTION_FAILED = "ACTION_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class Parameter(StrictModel):
    type: Literal["string", "number", "boolean"]
    description: str
    sensitivity: Sensitivity = Sensitivity.NONE
    required: bool = True


class Checkpoint(StrictModel):
    id: str
    kind: Literal["visible_text", "element_visible", "url_contains"]
    expected: str
    frame: str | None = None
    timeout_ms: int = Field(default=5000, ge=100, le=60000)


def _checkpoint_from_visible(code: str, visible: str) -> dict[str, Any]:
    return {
        "id": f"state-{code.lower().replace('_', '-')}",
        "kind": "visible_text",
        "expected": visible,
    }


class BusinessOutcome(StrictModel):
    code: str
    description: str
    checkpoint: Checkpoint

    @model_validator(mode="before")
    @classmethod
    def accept_visible_text(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        visible = payload.pop("visible_text", None)
        if "checkpoint" not in payload and visible:
            payload["checkpoint"] = _checkpoint_from_visible(
                str(payload.get("code", "outcome")), visible
            )
        return payload


class FatalState(StrictModel):
    """A named blocked state that stops replay instead of being retried."""

    code: str
    description: str
    checkpoint: Checkpoint

    @model_validator(mode="before")
    @classmethod
    def accept_visible_text(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        visible = payload.pop("visible_text", None)
        if "checkpoint" not in payload and visible:
            payload["checkpoint"] = _checkpoint_from_visible(
                str(payload.get("code", "fatal")), visible
            )
        return payload


class Contract(StrictModel):
    id: str
    version: str
    title: str
    description: str
    inputs: dict[str, Parameter]
    outputs: dict[str, Parameter]
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)


class SurfaceCompatibility(StrictModel):
    kind: Literal["web", "legacy_web", "desktop"]
    product: str
    product_version: str
    entry_point: str


class TargetOverride(StrictModel):
    adjacent_text: str | None = None
    name: str | None = None
    test_id: str | None = None
    description: str | None = None


class CheckpointOverride(StrictModel):
    expected: str | None = None


class TenantOverrides(StrictModel):
    targets: dict[str, TargetOverride] = Field(default_factory=dict)
    checkpoints: dict[str, CheckpointOverride] = Field(default_factory=dict)


class Compatibility(StrictModel):
    surface: SurfaceCompatibility
    tenant: str | None = None
    tenant_overrides: TenantOverrides = Field(default_factory=TenantOverrides)


class BoundingBox(StrictModel):
    x: float
    y: float
    width: float
    height: float


class SemanticTarget(StrictModel):
    kind: Literal["semantic"] = "semantic"
    frame: str
    test_id: str | None = None
    role: str | None = None
    name: str | None = None
    adjacent_text: str | None = None
    element_type: str


class StructuralTarget(StrictModel):
    kind: Literal["structural"] = "structural"
    frame: str
    table_index: int | None = None
    row_index: int | None = None
    cell_index: int | None = None
    element_type: str
    type_index: int = 0


class GeometryTarget(StrictModel):
    kind: Literal["geometry"] = "geometry"
    frame: str
    anchor_text: str
    element_type: str
    expected_box: BoundingBox


TargetStrategy = Annotated[
    SemanticTarget | StructuralTarget | GeometryTarget,
    Field(discriminator="kind"),
]


class TargetBundle(StrictModel):
    description: str
    strategies: list[TargetStrategy] = Field(min_length=1, max_length=3)


class InputValue(StrictModel):
    source: Literal["input"] = "input"
    name: str


class LiteralValue(StrictModel):
    source: Literal["literal"] = "literal"
    value: str | int | float | bool
    sensitivity: Literal[
        Sensitivity.NONE, Sensitivity.IDENTIFIER, Sensitivity.PII, Sensitivity.FINANCIAL
    ] = Sensitivity.NONE


ValueSource = Annotated[InputValue | LiteralValue, Field(discriminator="source")]


class Action(StrictModel):
    type: Literal[
        "navigate", "click", "type", "select", "extract", "wait", "dismiss"
    ]
    value: ValueSource | None = None
    output: str | None = None


class Recovery(StrictModel):
    """A bounded reaction to a recoverable runtime condition.

    Distinct from business outcomes (caller-visible results) and fatal states
    (hard stop / escalate). Trigger copy lives in the capability, not the engine.
    """

    trigger: Checkpoint
    strategy: Literal["dismiss", "wait"]
    action_text: str | None = None
    timeout_ms: int = Field(default=3000, ge=100, le=60000)
    max_attempts: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_kind(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        kind = payload.pop("kind", None)
        visible = payload.pop("visible_text", None)
        if "trigger" not in payload and visible:
            payload["trigger"] = {
                "id": f"recovery-{str(kind or 'trigger').replace('_', '-')}",
                "kind": "visible_text",
                "expected": visible,
            }
        if "strategy" not in payload:
            if kind == "dismiss_interstitial":
                payload["strategy"] = "dismiss"
            elif kind in {"retry_once", "wait"}:
                payload["strategy"] = "wait"
        return payload


class Step(StrictModel):
    id: str
    intent: str
    action: Action
    target: TargetBundle | None = None
    checkpoint: Checkpoint | None = None
    recover: list[Recovery] = Field(default_factory=list)
    risk: Risk = Risk.SAFE
    timeout_ms: int = Field(default=5000, ge=100, le=60000)


class SuccessCondition(StrictModel):
    checkpoint_ids: list[str]
    required_outputs: list[str]


class Execution(StrictModel):
    steps: list[Step]
    success: SuccessCondition
    fatal_states: list[FatalState] = Field(default_factory=list)


class Approval(StrictModel):
    status: Literal["draft", "approved", "deprecated"] = "draft"
    approved_by: str | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def approved_has_identity(self) -> "Approval":
        if self.status == "approved" and not self.approved_by:
            raise ValueError("approved capability requires approved_by")
        return self


class Provenance(StrictModel):
    discovered_by: str
    discovered_at: datetime
    trace_ref: str
    compiler_version: str


class Governance(StrictModel):
    approval: Approval
    provenance: Provenance


class Capability(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    contract: Contract
    compatibility: Compatibility
    execution: Execution
    governance: Governance

    @model_validator(mode="after")
    def validate_cross_references(self) -> "Capability":
        step_ids = [step.id for step in self.execution.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique")
        checkpoints = {
            step.checkpoint.id
            for step in self.execution.steps
            if step.checkpoint is not None
        }
        missing_checkpoints = set(self.execution.success.checkpoint_ids) - checkpoints
        if missing_checkpoints:
            raise ValueError(f"unknown success checkpoints: {sorted(missing_checkpoints)}")
        missing_outputs = set(self.execution.success.required_outputs) - set(
            self.contract.outputs
        )
        if missing_outputs:
            raise ValueError(f"unknown required outputs: {sorted(missing_outputs)}")
        for step in self.execution.steps:
            value = step.action.value
            if isinstance(value, InputValue) and value.name not in self.contract.inputs:
                raise ValueError(f"step {step.id} references unknown input {value.name}")
            if step.action.output and step.action.output not in self.contract.outputs:
                raise ValueError(f"step {step.id} writes unknown output {step.action.output}")
        return self


class StepDiagnostic(StrictModel):
    step_id: str | None = None
    code: FailureCode
    message: str
    expected: str | None = None
    observed: str | None = None


class LocatorVoteRecord(StrictModel):
    step_id: str
    winners: list[str]
    agreement: int
    weak: bool
    overridden: bool = False
    drifted: bool = False


class SuccessResult(StrictModel):
    status: Literal["success"] = "success"
    outputs: dict[str, Any]
    completed_steps: list[str]
    llm_calls: Literal[0] = 0
    locator_votes: list[LocatorVoteRecord] = Field(default_factory=list)
    override_score: float = 0.0
    drift_score: float = 0.0


class OutcomeResult(StrictModel):
    status: Literal["outcome"] = "outcome"
    code: str
    message: str
    completed_steps: list[str]
    llm_calls: Literal[0] = 0
    locator_votes: list[LocatorVoteRecord] = Field(default_factory=list)
    override_score: float = 0.0
    drift_score: float = 0.0


class FailureResult(StrictModel):
    status: Literal["failure"] = "failure"
    error: StepDiagnostic
    completed_steps: list[str]
    llm_calls: Literal[0] = 0
    locator_votes: list[LocatorVoteRecord] = Field(default_factory=list)
    override_score: float = 0.0
    drift_score: float = 0.0


class EscalatedResult(StrictModel):
    status: Literal["escalated"] = "escalated"
    intervention_id: str
    reason: StepDiagnostic
    completed_steps: list[str]
    llm_calls: Literal[0] = 0
    locator_votes: list[LocatorVoteRecord] = Field(default_factory=list)
    override_score: float = 0.0
    drift_score: float = 0.0


ReplayResult = Annotated[
    SuccessResult | OutcomeResult | FailureResult | EscalatedResult,
    Field(discriminator="status"),
]

