# Evidence Guide

This directory contains the redacted, inspectable proof for Pigeonhole's end-to-end path:

```text
genuine LLM discovery
        -> verified recording
        -> compiled candidate
        -> fresh-session validation (qualification)
        -> deterministic replay scenarios
        -> same-session human handoff and resume
```

The directories use scenario names so a reviewer can identify each claim without decoding timestamps. Original execution times remain unchanged in every `manifest.json` and trace event. Renaming the directories did not regenerate or alter the recorded actions.

## Submission evidence

Read these in order for the shortest review path:

| Directory | Result | What it demonstrates | Start time (UTC) |
| --- | --- | --- | --- |
| [`discovery-live-read-savings`](discovery-live-read-savings/) | Discovery `success` | A genuine model drove the real framed UI, completed six verified actions, extracted `savings_balance`, and called `done` only after independent completion verification | 2026-09-19 00:06:47 |
| [`qualification-fresh-session`](qualification-fresh-session/) | `success` | The newly compiled draft replayed without an LLM in a fresh browser before publication, ruling out dependence on discovery-session state | 2026-09-19 00:08:23 |
| [`replay-success-happy-path`](replay-success-happy-path/) | `success` | Normal deterministic execution of all six steps with `llm_calls: 0` | 2026-09-19 00:10:30 |
| [`replay-failure-draft-approval`](replay-failure-draft-approval/) | `failure / APPROVAL_REQUIRED` | Governance stops an unapproved capability before UI execution when `--allow-draft` is absent | 2026-09-19 01:31:30 |
| [`replay-outcome-member-not-found`](replay-outcome-member-not-found/) | `outcome / MEMBER_NOT_FOUND` | A declared business answer is returned separately from an automation failure | 2026-09-19 01:41:50 |
| [`replay-recovery-interstitial`](replay-recovery-interstitial/) | `success` | A known interstitial triggers one bounded dismiss recovery before replay continues | 2026-09-19 01:43:45 |
| [`replay-failure-session-expired`](replay-failure-session-expired/) | `failure / SESSION_EXPIRED` | Session expiry becomes a typed hard failure when handoff is disabled | 2026-09-19 01:44:52 |
| [`replay-success-northbay-tenant`](replay-success-northbay-tenant/) | `success` | The base artifact replays against the Northbay tenant through sparse label/checkpoint overrides | 2026-09-19 01:47:05 |
| [`replay-success-human-handoff`](replay-success-human-handoff/) | `success` | Replay detects session expiry, cedes the same browser to an operator, records human activity, reconciles live checkpoints, and resumes with no LLM | 2026-09-19 03:03:02 |

The genuine discovery made eight model calls: six successful UI actions, one or more state-dependent decisions, and the final `done` decision. The compiled artifact contains six executable steps. Model-call count and step count are therefore intentionally different.

Only genuine discovery and replay runs are retained here. Deterministic model doubles remain test-only utilities and do not generate committed evidence.

## File contract

Not every scenario needs every file.

| File | Meaning |
| --- | --- |
| `manifest.json` | Run identity, kind, goal, target, model (discovery only), original start time, and evidence schema version |
| `trace.jsonl` | Ordered, timestamped, redacted events: observation, decision, policy, locator vote, action, checkpoint, recovery, result, and handoff lifecycle |
| `recording.jsonl` | Discovery actions after runtime validation and target harvesting; compiler input rather than a raw chat transcript |
| `discovery-result.json` | Discovery stop reason, outputs, model-call count, and full verified recording |
| `result.json` | Discriminated replay result: `success`, `outcome`, `failure`, or the intermediate `escalated` state |
| `intervention.json` | Current intervention status plus capability, step, and redacted diagnostic context |
| `handoff.json` | Lease holder, operator ID, reason, return time, capture status, and click/change metadata from the human-control interval |
| `screenshots/failure.png` | Redacted visual state for a hard failure |
| `screenshots/intervention.png` | Redacted visual state at escalation |

## Suggested review path

### 1. Confirm genuine discovery

Open:

- [`discovery-live-read-savings/manifest.json`](discovery-live-read-savings/manifest.json) for model and run identity.
- [`discovery-live-read-savings/trace.jsonl`](discovery-live-read-savings/trace.jsonl) for `observed`, `model_decided`, `risk_inferred`, `policy_evaluated`, `acted`, and `completion_verified` events.
- [`discovery-live-read-savings/recording.jsonl`](discovery-live-read-savings/recording.jsonl) for the six compiler-ready actions.
- [`discovery-live-read-savings/discovery-result.json`](discovery-live-read-savings/discovery-result.json) for `goal_met`, outputs, and model-call count.

The capability's governance provenance points back to this trace at `evidence/discovery-live-read-savings/trace.jsonl`.

### 2. Confirm replay is deterministic

Open [`replay-success-happy-path/result.json`](replay-success-happy-path/result.json). It records all six completed steps, locator votes, drift/override scores, and `llm_calls: 0`. The associated trace contains no model-decision events.

### 3. Confirm error taxonomy

Compare:

- [`replay-outcome-member-not-found/result.json`](replay-outcome-member-not-found/result.json): a valid caller-visible outcome.
- [`replay-failure-session-expired/result.json`](replay-failure-session-expired/result.json): an automation failure with step, expected state, and observed state.
- [`replay-recovery-interstitial/trace.jsonl`](replay-recovery-interstitial/trace.jsonl): a bounded recovery event followed by success.
- [`replay-failure-draft-approval/result.json`](replay-failure-draft-approval/result.json): governance rejection before any capability step completes.

### 4. Confirm same-session handoff

Open:

- [`replay-success-human-handoff/intervention.json`](replay-success-human-handoff/intervention.json) for the returned intervention.
- [`replay-success-human-handoff/handoff.json`](replay-success-human-handoff/handoff.json) for operator ownership and captured human activity.
- [`replay-success-human-handoff/trace.jsonl`](replay-success-human-handoff/trace.jsonl) for `lease_ceded`, `handoff_raised`, `handoff_claimed`, `handoff_returned`, `resumed`, and final `succeeded` events.
- [`replay-success-human-handoff/result.json`](replay-success-human-handoff/result.json) for all six completed steps and `llm_calls: 0`.

The trace's `resumed` event names `live checkpoints` as the cursor basis. That is the evidence that replay resumed from observed browser state rather than restarting from a saved integer.

## Redaction and interpretation

Evidence is intentionally less privileged than the live caller:

- Runtime identifiers, PINs, financial values, and recognized fixture PII are replaced with `[REDACTED]` before JSON/JSONL is written.
- The local caller may receive a real output while committed `result.json` shows `[REDACTED]`.
- Screenshots mask known runtime values before capture and restore the live page afterward.
- Human capture records that an input changed, not what the operator typed.
- Locator `target` and `strategies` objects remain intact because changing locator identity would corrupt the executable record.

Redaction is implemented with exact known values and regular expressions. It is suitable for this fictional fixture, not a claim of production-grade DLP or immutable audit storage. Those limitations are discussed in [`../REPORT.md`](../REPORT.md).

## Reproduce the scenarios

Start the local target:

```bash
python -m target.server
```

Then follow the exact commands in [`../README.md`](../README.md), under **End-to-end demo**. New runs use timestamped directory names by default; the friendly names in this directory are curated names for the committed submission evidence.
