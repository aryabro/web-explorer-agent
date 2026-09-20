# Design Report

Web Explorer turns an unfamiliar browser workflow into a reusable capability. An LLM handles the ambiguity of the first successful run; trusted runtime code verifies and records what happened, a compiler converts that recording into a typed artifact, and later calls replay the artifact without a model.

The goal is reliable computer use against legacy applications that expose a UI but no practical API. Those applications often combine frames, weak semantics, changing field names, tenant differences, session expiry, and consequential actions. A useful system therefore needs more than a browser-driving loop: it needs an explicit contract, durable targeting, deterministic execution, policy enforcement, observable results, and a safe way to stop when automation no longer has enough evidence.

The implementation concentrates on one complete vertical slice rather than a broad collection of partial features. The local banking fixture is deliberately awkward enough to exercise the important boundaries, and each claim is backed by executable tests or recorded evidence. Production concerns that are not implemented are stated as cuts rather than hidden behind abstractions.

## 1. Architecture

```text
job + inputs → LLM discovery → verified recording → compiler → draft
draft → fresh-session qualification → published agent catalog
caller → agent catalog → deterministic replay
                            ├→ success | outcome | failure
                            └→ escalated (persisted; headless returns)
                                          ↓ headed execution
                                       operator
                                          ↓
                              reconcile live checkpoints
                                          ↓
                         resume deterministic replay in the same session
```

The implementation is one Python process with file-backed artifacts and evidence. Pydantic defines contracts, Playwright drives the browser, FastAPI serves the local target and operator console, and Typer exposes discovery and replay. This keeps the important boundaries visible without introducing queues, databases, or distributed workers.

The runtime path has six explicit stages. A job defines the goal and typed contract. Discovery observes the live surface and asks the model for one bounded action at a time; trusted code validates and executes it. The compiler accepts only successful, verified recordings. Qualification replays the candidate in a fresh browser without a model and publishes it only on success. Production-style invocation loads the capability by path or catalog ID and replays it deterministically. An unrecoverable condition becomes a typed failure or a same-session operator intervention, after which replay reconciles progress from the visible UI before resuming.

`SurfaceDriver` is the principal seam: it separates perception, action, target resolution, checkpoints, screenshots, pause/resume, and human-event capture from workflow semantics. Discovery and replay consume observations, target bundles, and actions—not Playwright `Page` objects or arbitrary DOM access. A `Capability` is both an agent-facing contract and an executable, reviewable plan. `ReplayResult` is a discriminated union of `success`, expected business `outcome`, automation `failure`, and human `escalated`.

Only discovery uses a model. Each turn receives the goal, typed input/output definitions, terminal states, recent actions, runtime feedback, remaining budget, and a bounded frame-aware observation. Sensitive input values, session storage, durable selectors, and browser objects are excluded. Controls have observation-scoped references; stale or invented references are rejected. The runtime—not the model—infers risk, applies policy, harvests durable targets, performs actions, and verifies checkpoints.

Compilation rejects failed actions, unverified transitions, and missing outputs. A candidate is then replayed without an LLM in a fresh browser before publication. This qualification gate detects accidental reliance on cookies, navigation, or other discovery-session residue. The main trade-off is conservative failure over replay-time improvisation: ambiguity becomes a typed failure or handoff instead of an unreviewed model decision.

This design pays an up-front discovery and qualification cost to make repeated execution simpler and more predictable. Keeping the working core in one process limits deployment scale, but it also makes the trust boundaries, state transitions, and evidence easy to inspect. The surface protocol and serialized capability boundary leave room for other adapters and storage systems without pretending those systems already exist.

## 2. Artifact schema

Artifacts carry information across each trust boundary instead of treating a successful browser session as an opaque transcript:

- A **job** states the human goal, entry point, typed inputs and outputs, expected business outcomes, fatal states, recovery rules, and discovery limits.
- A **recording** contains only runtime-validated actions and verified transitions from discovery. It is compiler input, not an executable script and not a raw model conversation.
- A **capability** is the versioned, agent-callable contract and deterministic execution plan produced by the compiler.
- An **evidence bundle** records the run manifest, redacted event trace, structured result, and any failure or handoff material needed to understand what the system did.
- An **intervention** preserves the diagnostic, live-session ownership state, and audit metadata when automation cedes control to an operator.

Capability schema version `1.0.0` uses strict Pydantic models with unknown fields forbidden. Its four sections are:

- **Contract:** capability ID/version, descriptions, typed inputs and outputs, sensitivity, and declared business outcomes. This is projected into an agent-callable tool definition.
- **Compatibility:** surface kind, product/vendor/version, entry point, tenant, sparse overrides, and a fingerprint.
- **Execution:** ordered steps containing intent, action, input/literal value source, target bundle, checkpoint, bounded recovery, risk, and timeout. Success names required outputs and final checkpoints; fatal states remain separate from business outcomes.
- **Governance:** draft/approved/deprecated status plus discovery model, timestamp, trace reference, and compiler version. Draft replay requires an explicit development override.

Cross-field validation rejects duplicate step IDs, unknown input/output references, missing success checkpoints, action shapes that cannot execute, and required outputs that no extraction step can produce. Runtime secrets are represented as input references rather than embedded data.

A target can contain semantic identity (role/name or adjacent label), structural identity (frame and table/control position), and geometric identity (anchor and expected box). Discovery harvests these before an action because navigation can destroy the original document. Sensitive extraction targets retain only safe structural information. The artifact deliberately excludes JavaScript, arbitrary CSS/XPath, loops, and model prompts; variation is represented through typed outcomes, fatal states, tenant overlays, and bounded recovery rules.

## 3. Determinism & error handling

Determinism is structural rather than aspirational. The replay import graph is tested to exclude discovery, model configuration, scripted models, and model/network SDKs. Every replay result constrains `llm_calls` to `Literal[0]`. Decisions therefore depend only on the artifact, runtime inputs, policy, tenant profile, and observed surface. Replay validates required, unexpected, and incorrectly typed inputs before navigation, and validates produced output types before returning success.

Replay resolves every viable target strategy and compares opaque element identities. If no strategy resolves, replay returns `LOCATOR_UNRESOLVED`; strategies resolving to different elements produce `LOCATOR_CONFLICT`; a single surviving strategy is allowed but recorded as weak. Replay never selects a plurality winner. After each action it checks, in order, declared outcomes, fatal states, the expected checkpoint, an applicable bounded recovery, and timeout. Playwright supplies actionability waiting, while application checkpoints establish semantic readiness.

The result taxonomy prevents operational ambiguity:

- `success`: outputs exist and the final visible condition holds.
- `outcome`: a valid business answer such as `MEMBER_NOT_FOUND`.
- `failure`: a closed error code with step and diagnostic context, including `INPUT_INVALID`, `OUTPUT_MISSING`, and `OUTPUT_INVALID` contract failures.
- `escalated`: automation stopped and created an operator intervention.

Every run writes a manifest, redacted JSONL trace, and structured result; discovery also records compiler input and model metadata, while failures and handoffs add appropriate screenshots or audit files. Recorded scenarios cover normal success, draft denial, business outcome, recovery, session expiry, tenant specialization, and resumed handoff. [`evidence/README.md`](evidence/README.md) is the evidence index.

## 4. Heterogeneity & multi-tenant

The surface protocol and artifact vocabulary are platform-neutral even though only Playwright is implemented. A desktop adapter could map windows and controls into the same observations and actions, using accessibility identity, control-tree position, and screen geometry. Adapter-specific strategies may eventually be necessary; portability beyond web is designed but not claimed.

Multi-tenant reuse uses a base product capability plus sparse overlays. `tenants/northbay.yaml` changes the entry point and only those semantic labels/checkpoints that differ; structural and geometric evidence remains inherited. Applying an overlay deep-copies the artifact, records changes, and recomputes its compatibility fingerprint.

Replay separates intentional `override_score` from unexpected `drift_score`. This distinction supports review: an override is approved configuration, while drift signals degrading assumptions. Current limits are that overlays are unsigned YAML, the fingerprint is metadata-derived rather than a live product attestation, and no fleet rollout or repeated qualification service exists.

## 5. Escalation & handoff

Discovery escalates on explicit `stuck`, repeated no-progress, or an unreconciled failed transition. Replay can escalate fatal states, locator/action/checkpoint errors, or policy confirmation when a coordinator is available. Headless execution persists the intervention and returns; headed execution can wait for an operator.

Control transfer uses a fail-closed lease:

```text
automation → unowned → operator → automation
                 ↑         |
                 └─ expiry ┘
```

Automation and a person never own the browser simultaneously. Lease expiry leaves it unowned. Pausing blocks automation without closing Chromium, preserving cookies, frames, storage, and partial work. The operator claims control through a local console but directly uses the already-open browser, so handoff retains the same live session.

Audit capture records timestamps and click/change metadata but not typed field contents. Intervention diagnostics, human-event metadata, and operator notes pass through the same redactor as other evidence before either handoff file is written. Capture failure is recorded without stranding a valid hand-back. Resume is based on live checkpoints, not a saved integer: replay scans the page, recalculates the cursor, preserves still-valid prior outputs, removes later invalid outputs, and continues without navigating back to the start. The recorded handoff scenario demonstrates session expiry, operator restoration, checkpoint reconciliation, and completion with zero model calls.

## 6. Safety

`policy.yaml` is default-deny over origins, paths, actions, intent patterns, and risk dispositions. Entry navigation is checked before opening a site, planned navigation is checked against its destination, and both discovery and replay recheck the observed location after an action. An unexpected redirect therefore fails immediately, including after the final action. Discovery and replay share this policy engine.

Risk is inferred from the selected control and action by trusted code; a model cannot label a “Confirm and create” action as safe. Safe actions are allowed, mutating actions require explicit confirmation, and irreversible actions are denied. Replay also enforces approval state and a step budget. A browser-backed mutating test uses independent storage state to verify exactly one change.

Secrets follow two rules: store references rather than values, and redact at every egress. Model prompts, JSON/JSONL evidence, and screenshots use the same redactor; locator identity remains intact so safety does not corrupt executability.

This is not production-grade DLP or governance. Regex redaction can miss formats, screenshot masking is not OCR, evidence is local rather than immutable, the loopback operator console lacks real identity, and approvals are unsigned. Production requires secret management, authenticated operators, signed artifacts/overlays, encrypted append-only evidence, screenshot DLP, stronger schemas, purpose binding, entitlement checks, and a centralized policy service.

## 7. Cuts

The project deliberately omits a desktop adapter, remote co-browsing proxy, worker fleet, durable lease registry, artifact database, automatic re-authentication, generalized branching/loops, arbitrary artifact code, and model-based replay repair. These cuts keep the core claim testable: a verified discovery can become a safe, inspectable artifact that replays deterministically and hands off without losing session state. The catalog is a local file scan, and the target’s browser-delivered test PIN and `sessionStorage` ledger are fixture conveniences—not proposed banking designs.

Next work should prioritize risk: signed governance and immutable evidence; repeated qualification across product versions and tenants; runtime compatibility attestation; then a second surface adapter. Model-assisted repair should remain offline: it may propose a new candidate for review and qualification but must not rewrite a running production replay.

Web Explorer’s defining position is that the LLM is useful during first-time ambiguity, while production execution should be typed, policy-controlled, observable, and deterministic.

References supporting these decisions:

- [Playwright locators](https://playwright.dev/python/docs/locators), [auto-waiting and actionability](https://playwright.dev/python/docs/actionability), and [frame handling](https://playwright.dev/python/docs/frames) inform semantic targeting, bounded readiness checks, and frame-aware execution.
- [Pydantic models](https://pydantic.dev/docs/validation/latest/concepts/models/) and [validators](https://pydantic.dev/docs/validation/latest/concepts/validators/) inform strict runtime contracts and cross-field validation.
- Python's [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol) informs the surface-neutral adapter boundary.
- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) informs the typed, bounded discovery-tool interface.
- The [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) informs evidence capture, sensitive-data exclusion, and the recommended production audit controls.
