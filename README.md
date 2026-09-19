# Pigeonhole

Pigeonhole is a discover-once, replay-many computer-use system for legacy applications that do not expose an API. An LLM operates a real browser during discovery. A compiler turns the verified run into a typed, reviewable capability. Production-style replay executes that capability without an LLM and returns one of four explicit results: `success`, `outcome`, `failure`, or `escalated`.

```text
natural-language goal
        |
        v
  DISCOVERY (LLM) ---- redacted trace + recording
        |
        v
  COMPILER ----------- typed capability JSON (draft)
        |
        v
  FRESH-SESSION VALIDATION (qualification, no LLM)
        |
        v
  REPLAY (no LLM) ----- success | outcome | failure | escalated
                                                    |
                                                    v
                                      same-session human handoff
```

The bundled target is **Test Bank Operations**, a fictional local back-office banking console. It is intentionally inconvenient: frames, nested tables, weak semantics, per-session salted field names, runtime interstitials, session expiry, and a second tenant variant. No real credentials or customer data are used.

## What is implemented

| Requirement | Implementation |
| --- | --- |
| Goal-driven discovery | OpenAI-compatible tool-calling loop over a live Playwright browser |
| Structured artifact | Strict Pydantic capability schema with contract, compatibility, execution, and governance sections |
| Deterministic replay | Separate replay engine with no discovery/model dependency and `llm_calls: 0` in every result type |
| Robust targeting | Semantic, structural, and geometry candidates resolved independently and checked for element identity agreement |
| Runtime errors | Declared business outcomes, bounded recovery rules, fatal states, typed failure codes, screenshots on failure |
| Safety | Default-deny origin/path/action policy, runtime risk inference, confirmation for mutation, redaction before model and disk sinks |
| Human handoff | Fail-closed session lease, intervention evidence, same live Chromium session, checkpoint-based resume |
| Multi-tenant reuse | Product/version metadata, compatibility fingerprint, sparse tenant target/checkpoint overlays, drift metrics |
| Agent invocation | Local capability catalog plus OpenAI-style tool definitions |

This is a focused vertical slice, not a production banking integration. The implemented/cut boundary is documented in [REPORT.md](REPORT.md).

## Requirements and setup

- Python 3.11 or newer
- Playwright Chromium
- A model API key only for genuine discovery
- No external service or model key for replay and tests

```bash
python -m venv .venv
```

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# macOS/Linux
source .venv/bin/activate
```

```bash
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Create `.env` from `.env.example` using `Copy-Item .env.example .env` on PowerShell or `cp .env.example .env` on macOS/Linux.

| Variable | Used by | Default / example |
| --- | --- | --- |
| `CUA_LLM_API_KEY` | Genuine discovery only | Required for `discover` |
| `CUA_LLM_BASE_URL` | Genuine discovery only | Any compatible `/chat/completions` endpoint |
| `CUA_LLM_MODEL` | Genuine discovery only | Model name exposed by that endpoint |
| `NIGHT_WINDOW_URL` | Local fixture | `http://127.0.0.1:8765` |
| `NIGHT_WINDOW_PIN` | Runtime sign-in input | `1937` |

The CLI supplies fictional operator ID `teller7` and the runtime PIN when those declared inputs are omitted. The capability stores input *references*, never the PIN value. Do not commit a real model key.

## End-to-end demo

### 1. Start the target

In terminal one:

```bash
python -m target.server
```

The fixture is now available at `http://127.0.0.1:8765`.

### 2. Run genuine discovery

In terminal two:

```bash
python -m pigeonhole.cli discover \
  --job jobs/read_savings.yaml \
  --input member_id=12345 \
  --headed
```

PowerShell accepts the command on one line, or with backticks instead of `\`. Discovery drives the UI, writes a run under `evidence/`, compiles a draft capability, and immediately qualifies it in a fresh browser session. The file is published to `capabilities/` only if qualification succeeds.

Qualification is the candidate capability's first deterministic replay in a new browser. It is intentionally separate from discovery so stale cookies, navigation state, or other discovery-session residue cannot make a broken artifact appear valid. It makes no model calls and is retained as a publication gate.

### 3. Replay without a model

```bash
python -m pigeonhole.cli replay \
  --capability capabilities/member.read_savings_balance.json \
  --input member_id=54321 \
  --allow-draft
```

The replay output has `llm_calls: 0`. `--allow-draft` is an explicit local-development override. To approve the artifact instead:

```bash
python -m pigeonhole.cli approve \
  --capability capabilities/member.read_savings_balance.json \
  --by reviewer-name
```

### 4. Exercise the result contract

```bash
# Declared business outcome, not an automation failure
python -m pigeonhole.cli replay --capability capabilities/member.read_savings_balance.json --input member_id=00000 --allow-draft

# Known interstitial: bounded dismiss recovery, then success
python -m pigeonhole.cli replay --capability capabilities/member.read_savings_balance.json --input member_id=12345 --fault interstitial --allow-draft

# Same base capability, second tenant overlay
python -m pigeonhole.cli replay --capability capabilities/member.read_savings_balance.json --input member_id=12345 --tenant northbay --allow-draft

# Session expiry as a typed hard failure
python -m pigeonhole.cli replay --capability capabilities/member.read_savings_balance.json --input member_id=12345 --fault session_drop --no-handoff --allow-draft
```

| Scenario | Result |
| --- | --- |
| Existing member | `success` with `savings_balance` |
| Member `00000` | `outcome` / `MEMBER_NOT_FOUND` |
| Known interstitial | Bounded recovery, then `success` |
| Session expiry without handoff | `failure` / `SESSION_EXPIRED` |
| Session expiry with headed handoff | `escalated`, operator takeover, then `success` |
| Draft without override | `failure` / `APPROVAL_REQUIRED` before browser actions |

### 5. Exercise live human handoff

```bash
python -m pigeonhole.cli replay \
  --capability capabilities/member.read_savings_balance.json \
  --input member_id=54321 \
  --fault session_drop \
  --headed \
  --allow-draft
```

The terminal prints an intervention URL on port `8766` and waits. This idle state is intentional.

1. Keep the replay terminal and Playwright Chromium window open.
2. Open `http://127.0.0.1:8766` in a separate, regular browser window.
3. Click **Claim control**.
4. In the Playwright-controlled Test Bank window, sign in and restore the member profile.
5. Return to the operator console and click **Hand back to automation**.
6. Replay reconciles its cursor from live checkpoints and completes from the restored state; it does not restart the whole flow.

The successful example is committed at [`evidence/replay-success-human-handoff/`](evidence/replay-success-human-handoff/). Its `handoff.json` records the lease, intervention, timestamps, and click/change metadata without typed field values.

## What the LLM receives during discovery

Each turn is a redacted, typed `ModelTurn`; the model does not receive a raw Playwright `Page`, arbitrary DOM access, session storage, or runtime secrets.

| Field | Purpose |
| --- | --- |
| `goal` | Natural-language task |
| `target_guidance` | Preferred frame and job-authored hints |
| `declared_inputs` | Input names, types, descriptions, sensitivity; not secret values |
| `required_outputs` | Output names, types, descriptions, sensitivity |
| `terminal_states` | Declared business outcomes and fatal checkpoints |
| `outputs_already_extracted` | Prevents premature `done` |
| `recent_actions` | Last eight semantic actions and results, without reusable refs |
| `feedback` | Runtime rejection, failed checkpoint, or no-progress guidance |
| `budget` | Current step, steps remaining, and seconds remaining |
| `observation` | URL, title, frames, bounded controls, state, nearby text, geometry, and an observation digest |

Controls receive ephemeral refs such as `o4:c3`. The observation ID changes on every observation, and stale refs are rejected. The model must call exactly one tool: `click`, `type`, `select`, `extract`, `dismiss`, `done`, or `stuck`. It can choose only listed refs and declared input/output names; it never writes CSS or persists a visible secret. The runtime independently infers risk, enforces policy, harvests durable target strategies, performs the action, and verifies completion.

The current discovery adapter is structured DOM/frame perception rather than a vision model. Screenshots are evidence, not model input. A future visual or desktop adapter can implement the same `SurfaceDriver` contract.

## Architecture

### Discovery and compilation

`DiscoveryLoop` follows observe -> decide -> validate -> policy -> act -> verify. It records attempted actions even when their checkpoint fails, so a later model `done` cannot compile a fictional success. `CompletionVerifier` requires all outputs, successful actions, verified transition checkpoints, a still-visible final checkpoint, and absence of a terminal state.

`compile_recording()` rejects unsuccessful recordings, failed actions, unverified transitions, and missing outputs. It strips runtime digests, converts input use into `InputValue` references, marks the final transition checkpoint as the postcondition, adds provenance, and emits a `draft` capability. A fresh qualification replay must succeed before the CLI publishes it.

### Capability artifact

The artifact has four sections:

- `contract`: agent-facing ID/version, typed inputs/outputs, sensitivity, and business outcomes.
- `compatibility`: surface kind, vendor/product/version, entry point, tenant, fingerprint, and applied overlays.
- `execution`: ordered steps, value sources, target bundles, checkpoints, recoveries, fatal states, and success conditions.
- `governance`: approval state and discovery/compiler provenance.

Pydantic uses `extra="forbid"` and cross-field validation. Duplicate step IDs, unknown input/output references, and success conditions referring to absent checkpoints are rejected while loading the artifact.

### Targeting and deterministic replay

Before an action, discovery harvests up to three independent target descriptions:

1. **Semantic**: test ID, role/name, adjacent label, and element type.
2. **Structural**: frame, table/row/cell position, element type, and type index.
3. **Geometry**: frame, anchor text, element type, and expected box.

Replay resolves every viable strategy and stamps the resulting elements with opaque identities. All resolved candidates must identify the same element. No candidate is `LOCATOR_UNRESOLVED`; disagreement is `LOCATOR_CONFLICT`; one remaining strategy is allowed but recorded as a weak vote. This is intentionally conservative: replay does not click a plurality winner or ask an LLM to guess.

After each action, replay checks outcomes, fatal states, recoveries, and the step checkpoint. It finally verifies required outputs and the postcondition. The session-storage ledger is only a test oracle and never establishes replay success.

### Safety and evidence

`policy.yaml` allowlists origins, paths, actions, and risk dispositions. The same policy runs during discovery and replay. Model-declared risk is advisory; trusted risk is inferred from the chosen control. Safe actions are allowed, mutating actions require `--allow-mutating`, and irreversible actions are denied.

Known sensitive runtime values and common identifier/financial patterns are redacted before model egress and before evidence is written. Screenshot capture temporarily masks known runtime values in every accessible frame and then restores the page. Locator identity fields are preserved because redacting them would make the capability unusable.

Each run directory contains a manifest, a redacted JSONL trace, and a structured result. Discovery additionally writes `recording.jsonl` and `discovery-result.json`; failures include a screenshot; handoff includes `intervention.json`, an intervention screenshot, and `handoff.json`.

## Agent-facing catalog

```bash
python -m pigeonhole.cli list
python -m pigeonhole.cli tools
python -m pigeonhole.cli call --id member.read_savings_balance --input member_id=54321 --allow-draft
```

`tools` projects capability inputs into OpenAI-style function definitions. The catalog is deliberately a local file scan, not a network service.

## Mutating workflow coverage

[`jobs/open_sub_account.yaml`](jobs/open_sub_account.yaml) defines the mutating example. The browser-backed test compiles and replays it with an independent storage oracle, proving that mutation requires `--allow-mutating` and occurs exactly once. A compiled copy is not committed because the previous one came from a scripted development run whose evidence was removed; submitted capability artifacts are now limited to genuine discovery.

## Evidence included in this repository

See [`evidence/README.md`](evidence/README.md) for the file contract, redaction rules, suggested review order, and direct links to the important artifacts.

| Run | Demonstrates |
| --- | --- |
| [`discovery-live-read-savings`](evidence/discovery-live-read-savings/) | Genuine model-driven discovery, six verified actions, extracted output |
| [`qualification-fresh-session`](evidence/qualification-fresh-session/) | Fresh-session deterministic qualification |
| [`replay-success-happy-path`](evidence/replay-success-happy-path/) | Normal deterministic success |
| [`replay-failure-draft-approval`](evidence/replay-failure-draft-approval/) | Draft approval gate |
| [`replay-outcome-member-not-found`](evidence/replay-outcome-member-not-found/) | `MEMBER_NOT_FOUND` business outcome |
| [`replay-recovery-interstitial`](evidence/replay-recovery-interstitial/) | Recoverable interstitial |
| [`replay-failure-session-expired`](evidence/replay-failure-session-expired/) | Session-expiry hard failure without handoff |
| [`replay-success-northbay-tenant`](evidence/replay-success-northbay-tenant/) | Northbay tenant overlay success |
| [`replay-success-human-handoff`](evidence/replay-success-human-handoff/) | Same-session operator handoff and successful resume |

Evidence outputs are intentionally redacted, so the caller may receive a value that appears as `[REDACTED]` in committed `result.json`.

## Tests

```bash
python -m pytest
python scripts/validate_repository.py
```

The suite currently collects 37 tests. Important coverage includes real-browser discovery/compile/replay, outcome and recovery paths, mutating policy, locator conflict, stale observation refs, compiler invariants, tenant overlays, draft approval, redaction, lease expiry, capture failure during hand-back, and same-session checkpoint resume. The repository validator separately checks committed capability schemas, provenance paths, evidence JSON/JSONL, run-directory identities, job definitions, and catalog freshness.

GitHub Actions runs the validator and the complete suite with Playwright Chromium on Python 3.12. Docker is intentionally not required: tests start the local FastAPI target in-process, while the automation controls a runner-local browser. Containerizing either side would add networking and browser-handoff complexity without strengthening the boundary under test.

Tests use a scripted decision model but still drive the real local UI. They do not replace the committed genuine discovery run.

## Repository guide

| Path | Responsibility |
| --- | --- |
| `src/pigeonhole/contracts.py` | Strict capability schema, result union, risk/sensitivity/failure enums |
| `src/pigeonhole/discovery.py` | Model tools, typed turn context, discovery loop, completion verification |
| `src/pigeonhole/compiler.py` | Recording validation and capability construction |
| `src/pigeonhole/replay.py` | Deterministic executor, error taxonomy, recovery, resume cursor |
| `src/pigeonhole/surface/base.py` | Surface-neutral protocol and observations |
| `src/pigeonhole/surface/perception.py` | Observation normalization and target harvesting |
| `src/pigeonhole/surface/resolution.py` | Surface-neutral identity-voting rule |
| `src/pigeonhole/surface/playwright.py` | Web/frame adapter, actions, checkpoints, screenshots, human-event capture |
| `src/pigeonhole/policy.py` | Default-deny location/action/risk decisions |
| `src/pigeonhole/redact.py` | Exact-value and pattern redaction |
| `src/pigeonhole/evidence.py` | Run directories and redacted JSON/JSONL sinks |
| `src/pigeonhole/handoff.py` | Session lease, interventions, operator console, hand-back evidence |
| `src/pigeonhole/tenants.py` | Sparse tenant specialization and fingerprint updates |
| `src/pigeonhole/catalog.py` | Local capability discovery and tool projection |
| `src/pigeonhole/cli.py` | User-facing orchestration commands |
| `jobs/` | Discovery contracts and fixture-specific guidance |
| `capabilities/` | Compiled, versioned capability artifacts |
| `tenants/` | Tenant-specific sparse overrides |
| `target/` | Local Test Bank fixture and launch profile |
| `evidence/` | Committed redacted discovery, replay, failure, and handoff runs |
| `tests/` | Contract, architecture, browser, safety, and handoff tests |

## Design influences

- [YYukin0/interface](https://github.com/YYukin0/interface) is a useful comparison for presenting a discover -> typed capability -> deterministic replay system honestly, with implementation status and explicit cuts.
- [browser-use/workflow-use](https://github.com/browser-use/workflow-use) uses the same broad record/generate-once and execute-without-AI premise. Pigeonhole is more conservative at replay time: it surfaces ambiguity rather than silently self-healing with an LLM.
- [Microsoft FSQ](https://github.com/microsoft/FSQ) is a strong example of the evidence-first distinction between dynamic exploration and strict replay, and of a common harness vocabulary across UI platforms.
- [Playwright locator guidance](https://playwright.dev/docs/locators), [actionability checks](https://playwright.dev/docs/actionability), and [frame support](https://playwright.dev/docs/frames) motivate user-facing semantic locators, bounded waiting, and explicit frame identity.
- [Playwright MCP live demo](https://www.youtube.com/live/CNzg1aPwrKI) illustrates a live browser exposed as bounded model tools. Pigeonhole uses its own narrow tool schema because the artifact compiler needs observation-scoped refs and declared input/output names.
- [Pydantic models](https://docs.pydantic.dev/latest/concepts/models/) provide the runtime validation used for recordings, capabilities, and replay results.

See [REPORT.md](REPORT.md) for the architectural argument, trade-offs, limits, and proposed production roadmap.
