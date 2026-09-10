# Daiki Engineering Loop

Repeatable acceptance/load/quality loop for Daiki AIPass + Hermes. It intentionally uses only Python's standard library and `kubectl`.

The API suite opens a temporary port-forward to the cluster-internal backend and uses synthetic RFC-2544 Guest identities. It covers health/readiness, Guest policy, text and SSE chat, context continuity, invalid-request cooldown semantics, file upload/list/download/grounding, native vision, file generation, image generation, and bounded load ramps. Test attachments are deleted through the public Guest API.

The Hermes suite runs profile-scoped one-shots inside the existing Hermes pod. It records deterministic prompt-size budgets, general response quality, `skill_view`, delegation, and a read-only curator dry-run. It never creates a Daiki user or API key.

Run a full loop:

```bash
python3 tools/engineering-loop/daiki_engineering_loop.py --context match-infra --namespace daiki-ai-passport
```

Use `--skip-load` for feature-only validation or `--load-levels 1,2,5` for a smaller inference ramp. Reports are written under `tools/engineering-loop/reports/` and are intentionally ignored by Git.

Acceptance policy: functional failures are `FAIL`; a capability that is implemented but cannot run because an external provider is not configured (for example image generation) is `BLOCKED`, which makes the overall result `DEGRADED`. The load ramp stops after an error rate above 10% instead of intentionally hammering a provider already at saturation.

Skill growth policy: learning happens through the dedicated Hermes `skills` profile. The curator runs daily after idle time, creates backups, and can mark/archive stale agent-created skills. Automatic LLM consolidation stays disabled; use an engineering-loop quality run plus `hermes curator run --dry-run` before any manual consolidation/promotion.
