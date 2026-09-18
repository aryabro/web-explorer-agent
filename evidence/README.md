# Evidence

`discovery-20260916T195717Z-60d257/` is the genuine Gemini discovery run
(`gemini-3.1-flash-lite`). It completed the live UI goal against Night Window's
frameset/table console. That folder is submission evidence; scripted pytest
models are not a replacement for it. Commit it with the repository.

`capabilities/member.read_savings_balance.json` is the compiled artifact from a
later genuine GLM compile (`discovery-20260918T020425Z-0f25df`), with trusted
risk inference (sign-on is `safe`; checkpoints are destination text). Replay
bundles below were produced against that artifact with `llm_calls: 0`.

- `replay-happy-12345/` — member `12345` success
- `replay-member-not-found/` — `member_id=00000` → outcome `MEMBER_NOT_FOUND`
- `replay-interstitial/` — `--fault interstitial`, recovered success
- `replay-session-expired/` — `--fault session_drop --no-handoff` →
  `SESSION_EXPIRED` hard failure
- `replay-draft-denied/` — draft replay without `--allow-draft` →
  `APPROVAL_REQUIRED`
- `replay-handoff/` — same-session lease handoff (`intervention.json` +
  `handoff.json`), then checkpoint resume to success
- `replay-northbay/` — same capability on `/tenant-b/` via `tenants/northbay.yaml`
- `replay-northbay-drift/` — unpatched tenant-b run; semantic/checkpoint drift

`catalog.json` is the `list` / `tools` dump of the local capability catalog.

Regenerate the replay bundles (does not overwrite the genuine discovery):

```bash
python -m target.server
python scripts/generate_parity_evidence.py
```

A new genuine discovery can be produced with:

```bash
pigeonhole discover --job jobs/read_savings.yaml --input member_id=12345 --headed
pigeonhole approve --capability capabilities/member.read_savings_balance.json --by reviewer
```

That run requires `CUA_LLM_API_KEY` and writes its actual model name into
`manifest.json`.

Persisted files are redacted. Locator identity (`target` / `strategies`) is not
rewritten. Runtime results returned to the local caller can contain declared
outputs; committed JSON/JSONL and screenshots must not.
