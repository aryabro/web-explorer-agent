# Design Report

Pigeonhole is a focused implementation of the assignment's central proposition: the model discovers a workflow once; a typed artifact becomes the durable product; deterministic replay is how another agent invokes it. Everything described below is either implemented and tested or explicitly identified as a cut.

## 1. Architecture

```text
goal + job
    |
    v
DiscoveryLoop --LLM/tool calls--> live SurfaceDriver
    |                                  |
    | verified Recording               | observations/actions/checkpoints
    v                                  |
compiler --> draft Capability ---------+
    |
    v
fresh-session qualification (ReplayEngine, no LLM)
    |
    v
catalog/tool definition --> deterministic replay --> success | outcome | failure | escalated
                                                                            |
                                                                            v
                                                              same-session human handoff
```

The architecture is deliberately a single Python process with files on disk. A queue, database, worker fleet, and distributed lease service would add operational realism but would obscure the load-bearing boundaries this exercise is evaluating. Python 3.11, Pydantic, Playwright, FastAPI, Typer, and JSON/YAML are sufficient for the complete vertical slice.

There are three principal seams:

- `SurfaceDriver` separates perception and action from workflow semantics. The interface exposes `observe`, `harvest`, `resolve`, `act_target`, `checkpoint`, recovery, pause/resume, screenshots, and human-event capture. Playwright is the only adapter, but the contract does not expose `Page`, `Frame`, `Locator`, CSS, or XPath to discovery, compilation, or replay.
- `Capability` is simultaneously an agent contract, a review document, and an executable plan. Strict runtime validation prevents downstream components from interpreting malformed artifacts differently.
- `ReplayResult` is a discriminated four-arm union. An expected `MEMBER_NOT_FOUND` outcome cannot be confused with a broken locator, and an escalation carries an intervention ID rather than masquerading as a failure.

Discovery is the only model-dependent path. `OpenAICompatibleModel` requests one required tool call at temperature zero. On each turn the model receives a redacted `ModelTurn`: goal; preferred frame and hints; typed input/output definitions; declared business/fatal states; extracted-output names; the last eight semantic actions; runtime feedback; remaining step/time budget; and a bounded, frame-aware observation. The observation contains ephemeral refs, control state, nearby text, geometry, dialogs/alerts, and a digest. It does not contain runtime input values, session storage, reusable selectors, or a Playwright object. On frame-rich pages, the duplicated page-level `visible_text` is blanked and the model uses bounded frame summaries instead.

The action tools constrain the model to `click`, `type`, `select`, `extract`, `dismiss`, `done`, or `stuck`. Every action must echo the current observation ID and choose a current ref. The runtime rejects stale/invented refs, invalid control/action combinations, undeclared inputs/outputs, source-less typing, and checkpoints that merely repeat the clicked label. Risk proposed by the model is advisory. Policy, target harvesting, action execution, and completion verification remain trusted runtime responsibilities.

Discovery records an attempted action even when it or its postcondition fails. This matters: omitting failed actions would allow a later `done` call to compile a fiction. `CompletionVerifier` independently requires all outputs, successful recorded actions, verified transition checkpoints, a still-visible final checkpoint, and no currently visible terminal state. The compiler then rejects unsuccessful recordings, unverified transitions, and missing outputs. Finally, the CLI validates the compiled draft by replaying it without an LLM in a new browser session and publishes it only if that qualification succeeds. This gate detects accidental dependence on discovery-session cookies, navigation, or transient page state.

The local Test Bank target is intentionally more representative than a clean demo form: a frameset, nested tables, weak labels, salted field names, tenant-specific copy, runtime interstitials, and session expiry. Its browser `sessionStorage` ledger is an independent test oracle; production replay uses only visible UI state.

The main trade-off is conservative failure over clever recovery. At replay time there is no hidden model fallback. A conflict, unknown state, policy decision, or unverified checkpoint becomes a typed result or a human intervention. This costs availability but protects determinism and auditability in a regulated workflow.

## 2. Artifact schema

`contracts.py` defines schema version `1.0.0` with Pydantic `extra="forbid"`. The artifact has four top-level sections.

**Contract.** `id`, semantic version, title/description, typed inputs, typed outputs, sensitivity classifications, and caller-visible business outcomes. The compiled read capability declares `operator_id`, `pin`, and `member_id`, returns `savings_balance`, and recognizes `MEMBER_NOT_FOUND` and `PERMISSION_DENIED`. A calling agent can inspect this section without learning browser mechanics. `catalog.py` projects it into an OpenAI-style function definition.

**Compatibility.** Surface kind, product/vendor/version, entry point, optional version range and tenant, sparse overrides, and a fingerprint. This scopes an artifact to a vendor product rather than pretending every UI is interchangeable. The current fingerprint protects internal compatibility metadata consistency; it is not yet a live application attestation.

**Execution.** Ordered `Step` values contain a human-readable intent, action, optional `ValueSource`, target bundle, checkpoint, bounded recoveries, risk, and timeout. `InputValue` names a runtime argument. `LiteralValue` cannot represent a secret. A transition checkpoint is the definition of what the action achieved, not merely a testing convenience. `SuccessCondition` names required outputs and postcondition checkpoint IDs. Fatal states such as `SESSION_EXPIRED` remain distinct from business outcomes.

**Governance.** Approval (`draft`, `approved`, or `deprecated`) plus model, discovery time, trace reference, and compiler version. Compiled capabilities start as drafts. Replay rejects a draft unless the caller uses the explicit development override `--allow-draft`; `approve` records reviewer identity and time.

Cross-field validation rejects duplicate step IDs, missing success checkpoints, undeclared required outputs, steps reading unknown inputs, and steps writing unknown outputs. This validation occurs whenever the artifact is loaded, not in an optional linter.

Each target is a bundle of at most three independently useful strategies:

1. Semantic identity: test ID, role/name, adjacent text, element type, and frame.
2. Structural identity: frame, table/row/cell position, element type, and same-type index.
3. Geometric identity: frame, anchor text, element type, and expected bounding box.

Targets are harvested before the action because navigation may destroy the document and every fact about the clicked element. For sensitive financial extraction, the compiler retains only structural candidates; visible member/account values must not become durable semantic or geometry anchors. Evidence redaction preserves target/strategy objects because rewriting their identity would silently corrupt replay.

The schema is intentionally smaller than a general browser scripting language. It does not embed JavaScript, arbitrary CSS, conditionals, loops, or model prompts. Runtime variation is expressed as typed outcomes, fatal states, and bounded recoveries, which remain reviewable.

## 3. Determinism & error handling

Determinism is enforced structurally. `replay.py` does not import discovery or an LLM client; an architecture test checks that boundary; and every replay result constrains `llm_calls` to the literal value `0`. The artifact, inputs, policy, tenant profile, and live surface are the complete decision inputs.

Target resolution is agreement-based rather than an ordered fallback chain. The Playwright adapter resolves every viable strategy, stamps each resulting DOM element with an opaque identity, and passes `(strategy, identity)` pairs to the surface-neutral voting function. If none resolve, replay returns `LOCATOR_UNRESOLVED`. If strategies identify different elements, it returns `LOCATOR_CONFLICT`; it does not choose the plurality. If exactly one strategy survives, replay may continue but records a weak vote. This makes gradual semantic degradation observable through `locator_votes` and `drift_score` without making replay nondeterministic.

Playwright contributes auto-waiting and actionability checks, but readiness is established by application checkpoints, not sleeps or network-idle guesses. After every action, `_settle_step` repeatedly checks in this order: declared business outcome, fatal state, expected checkpoint, applicable bounded recovery, timeout. Recoveries are data in the artifact (`dismiss` or `wait`, timeout, maximum attempts), never open-ended agent behavior.

The result taxonomy is deliberate:

- `success`: required outputs exist and the final visible condition still holds.
- `outcome`: a declared business state such as `MEMBER_NOT_FOUND`; this is a valid answer to the caller.
- `failure`: a closed `FailureCode` with step, expected state, observed state, and message. Codes include approval, input, compatibility, policy, locator, checkpoint, session, output, success-condition, and action failures.
- `escalated`: automation stopped, ceded its lease, and created an operator intervention.

This avoids the common mistake of paging an operator for a legitimate “no such member” result. It also keeps runtime errors separate from UI drift: an expired session may occur with a perfectly stable layout, while locator conflict indicates the artifact no longer identifies one control safely.

Evidence makes the claim inspectable. Every run has a manifest, ordered redacted JSONL trace, and structured result. Discovery adds the recording and model metadata. Failure adds a screenshot. Locator events report winners, agreement, weak resolution, intended tenant overrides, and unexpected semantic loss. The repository includes real runs for normal success, draft denial, business outcome, recoverable interstitial, session failure, tenant specialization, and same-session handoff; [`evidence/README.md`](evidence/README.md) indexes each scenario and its important files.

## 4. Heterogeneity & multi-tenant

Heterogeneous surfaces are addressed at the `SurfaceDriver` and artifact-vocabulary boundaries. `perception.py` turns adapter snapshots into frame/control observations and harvests generic semantic/structural/geometric targets. `resolution.py` votes over opaque element identities without importing Playwright. A Windows UI Automation adapter could map windows and controls into the same observation, interpret semantic identity as automation role/name, structural identity as control-tree position, and geometry as a screen rectangle. The action and checkpoint vocabulary would remain useful, though adapter-specific target strategy variants may eventually be necessary. This is designed, not implemented.

Multi-tenant reuse is handled as a base vendor capability plus sparse specialization. The base artifact identifies product, vendor, version, and origin. `tenants/northbay.yaml` changes the entry point and patches only semantic labels/checkpoints whose copy differs. Structural and geometric evidence stays inherited. `apply_tenant()` deep-copies the artifact, applies patches by stable step/checkpoint ID, records those overrides, and recomputes the compatibility fingerprint.

Replay exposes two separate signals:

- `override_score`: fraction of targeted steps intentionally specialized for this tenant.
- `drift_score`: fraction of non-overridden steps that originally had a semantic strategy but no longer resolved semantically.

Keeping these separate matters. An override is reviewed configuration; drift is unexpected evidence. In production, trends would feed qualification and re-recording decisions: a small override set is a deployment quirk, while a very high score suggests a materially different product/version deserving its own base capability.

Current limits are important. The tenant registry is a YAML directory, overlays are not independently signed/approved, the compatibility fingerprint is metadata-derived rather than observed from the running application, and there is no version rollout or fleet qualification service. Those are natural extensions after the base/overlay model proves useful.

## 5. Escalation & handoff

Discovery escalates on explicit `stuck`, repeated no-progress (`dead_end`), or an action/checkpoint failure that cannot be reconciled. Replay escalates fatal states, policy confirmation, locator/action errors, or checkpoint failure whenever a `HandoffCoordinator` is present. Headless execution persists the intervention and returns; headed execution can wait for an operator and resume in the same command.

The control-transfer invariant is a fail-closed `SessionLease`:

```text
automation --cede--> unowned --claim--> operator --hand back--> automation
                           ^                 |
                           +---- expiry -----+
```

There is no state in which automation and an operator both own the browser. An expired operator lease becomes unowned, never automatically controlled by automation. Before every replay action the engine asserts that automation owns the lease. `pause()` clears the surface action gate but does not close or recreate the browser, preserving cookies, frames, session storage, and partially completed work.

An intervention includes capability, goal, step, redacted diagnostic, timestamp, and screenshot. The local FastAPI console on port `8766` lets an operator claim the lease and later hand it back. The person directly operates the already-open Playwright Chromium window, so this is genuinely the same live session; the console is coordination, not a remote-input proxy.

Human-event capture records timestamps and click/change metadata, including the clicked element's short visible label, but never the contents typed into inputs. `handoff.json` records the operator, reason, return time, events, and any capture error. Capture is audit telemetry, not a safety prerequisite: if navigation invalidates the capture context while stopping it, the verified lease hand-back still resumes automation and records the telemetry failure.

Resume is checkpoint-based, not a stored step counter. `ExecutionState` preserves completed steps, extracted outputs, and locator votes. On return, replay scans recorded checkpoints against the live page, derives a corrected cursor, removes outputs that belong after that cursor, keeps still-valid prior outputs, logs `resumed` with `basis: live checkpoints`, and continues without navigating to the entry page. Discovery similarly reconciles a failed transition from the operator-restored UI before allowing the model loop to continue.

The committed successful handoff demonstrates `SESSION_EXPIRED` at step `s5`, operator re-authentication/search in the same session, hand-back, live checkpoint reconciliation, and deterministic completion with `llm_calls: 0`.

## 6. Safety

Policy is external to both model intent and artifact execution. `policy.yaml` is default-deny over origins, paths, actions, intent deny-patterns, and risk disposition. Entry navigation is checked before opening the site; navigation evaluates the destination rather than the current page; post-action redirects are checked again during discovery. Discovery and replay share the same engine.

Risk is inferred by trusted code from the selected control's visible/accessibility label and action. A model cannot relabel “Confirm and create” as safe. Safe actions are allowed, mutating actions require explicit `--allow-mutating`, and irreversible actions are denied. Replay also enforces an artifact step budget and approval state. The browser-backed sub-account test compiles the mutating job, exercises this boundary, and verifies the resulting storage change occurs exactly once.

Secrets and regulated values follow two rules: store references, not values; redact at every egress. Secret runtime values become `InputValue(name="pin")` in the artifact, and `LiteralValue` cannot classify a value as secret. The model sees declared input metadata but not the supplied value. The same `Redactor` processes model turns, trace/result sinks, and screenshots using exact known values plus identifier, financial, credential, and fixture-PII patterns. Target identity is preserved separately so safety does not destroy executability.

There are honest limitations. Regex redaction can miss unknown formats or over-redact harmless text. Screenshot masking only knows configured runtime values and accessible DOM text; it is not OCR/DLP. Human-event files are written locally rather than to immutable encrypted audit storage. The loopback operator console has no authentication beyond local access and uses a fixed development operator ID. Approval is a local edit with a reviewer string, not a signed review. Route prefix matching and label-based risk inference are intentionally small policy mechanisms, not substitutes for purpose binding, entitlement checks, transaction limits, or a real policy service.

For production, I would add a secret manager, authenticated operator identity, signed capability/overlay approvals, image DLP, encrypted append-only evidence, purpose-bound member verification, stronger input schemas, and a centralized policy decision point. I would retain the current separation: the model proposes, while trusted runtime code authorizes and verifies.

## 7. Cuts

The implementation deliberately omits a desktop adapter, remote co-browsing proxy, queues/workers, durable lease registry, artifact database, tenant fleet manager, automatic re-authentication, generalized branching/loops, arbitrary code in artifacts, and model-based self-healing during replay. The operator console is intentionally minimal. The local catalog is a file scan rather than an HTTP/MCP service. The test target's PIN delivered to browser JavaScript and `sessionStorage` ledger are fixture conveniences, not proposed authentication or data-storage designs.

The next work should be prioritized by risk rather than breadth:

1. Harden governance and evidence: signed approvals, immutable storage, authenticated operators, retention controls, and screenshot DLP.
2. Add repeated qualification and stability reporting across supported product versions and tenant overlays.
3. Improve compatibility attestation by observing application/version landmarks at runtime rather than trusting artifact metadata alone.
4. Add a second surface adapter, ideally Windows UI Automation, to test whether the current artifact vocabulary is genuinely portable.
5. Add bounded repair as an offline review workflow: a model may propose a new candidate artifact from failure evidence, but production replay should never silently rewrite or execute it.
6. Expose the catalog behind a small authenticated agent-facing service or MCP server only after approval, identity, and audit semantics are production-ready.

### Decision rationale and references

These sources are useful design influences, not claims that Pigeonhole implements every feature they contain:

- [YYukin0/interface](https://github.com/YYukin0/interface) demonstrates a clear submission style: state what runs, make the architecture legible, distinguish evidence from assertion, and document cuts. It also reinforces the value of ephemeral discovery refs, typed artifacts, four-way results, and explicit lease ownership.
- [browser-use/workflow-use](https://github.com/browser-use/workflow-use) supports the basic “execute once, generate a reusable workflow, then run without AI” choice. Pigeonhole intentionally diverges from its self-healing direction: replay-time model fallback would weaken repeatability and policy review, so repairs should produce a new candidate artifact for qualification.
- [Microsoft FSQ](https://github.com/microsoft/FSQ) provides a useful evidence-first framing and the dynamic-exploration/strict-replay split. Its shared harness concept supports the decision to keep surface perception/action behind a protocol.
- [Playwright's locator guidance](https://playwright.dev/docs/locators) recommends user-facing attributes over brittle CSS/XPath, while [actionability](https://playwright.dev/docs/actionability) and [frame support](https://playwright.dev/docs/frames) justify the chosen web adapter. Pigeonhole adds structural and geometry candidates because the target class explicitly includes legacy markup where accessibility semantics may be incomplete.
- [Playwright MCP live demo](https://www.youtube.com/live/CNzg1aPwrKI) is a practical reference for exposing browser interaction as bounded model tools. Pigeonhole keeps a narrower custom tool surface so actions must use observation-scoped refs and declared contract names.
- [Pydantic model validation](https://docs.pydantic.dev/latest/concepts/models/) supports using one runtime schema for artifact loading, cross-field invariants, and result serialization.

The defensible summary is: use the LLM where ambiguity is useful (first-time discovery), compile only independently verified behavior, execute reviewed data without a model, fail closed on disagreement, separate business answers from automation failures, and give a human exclusive ownership of the same session when deterministic automation cannot continue.
