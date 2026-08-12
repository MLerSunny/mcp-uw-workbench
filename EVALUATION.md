# Evaluation Strategy

## Why this file exists

Most public MCP demos lack any evaluation harness. A senior engineering portfolio piece should show how you'd measure tool-use accuracy, not just whether your code runs.

Implemented in `tests/eval/`. Run it with `pytest tests/eval -q -s`.

## What we evaluate

| Dimension | What it measures | How |
|---|---|---|
| **Delegation accuracy** | Did the supervisor call the right peer tools, in the right order? | Exact-sequence comparison against ground truth in `scenarios.json`. |
| **Decision quality** | Did the final decision match the expected `quote` / `refer` / `decline`? | Per-scenario ground truth. |
| **Risk banding** | Did the risk score land in the expected band? | `low` / `medium` / `high` thresholds; `any` opts a scenario out. |
| **Call efficiency** | Did the orchestrator make redundant calls? | Asserts no peer tool is invoked twice in a run. |

## SLOs (enforced in CI)

| Metric | Target | Current |
|---|---|---|
| Delegation accuracy | ≥ 95% | 100% |
| Decision accuracy | ≥ 90% | 100% |
| Redundant peer calls | 0 | 0 |
| Scenarios | — | 6 |

Latency is deliberately **not** an SLO yet. Under stdio each peer is a
short-lived subprocess, so measured time is dominated by process spawn
rather than by the orchestration being evaluated. Latency SLOs land with
the long-lived SSE / streamable-HTTP transport in v0.4.

## Eval harness design

A pytest-driven harness in `tests/eval/`:

```
tests/eval/
├── scenarios.json           # labeled scenarios with ground truth
└── test_eval_harness.py     # per-scenario checks + aggregate scorecard
```

Each scenario in `scenarios.json` carries:
- `input` — the arguments as they would arrive from an MCP client
- `expected_delegations` — the peer tool calls, in required order
- `expected_decision` — `quote` / `refer` / `decline`
- `expected_risk_band` — `low` / `medium` / `high` / `any`

## What it caught

The harness paid for itself during v0.3. The `unratable-peril-earthquake`
scenario exposed a defect where a peril with no filed rate produced a
**$0 premium that passed through as a valid quote** — in an underwriting
system, a free policy. The orchestrator now detects the missing rate and
forces a referral. Pinned by
`test_missing_filed_rate_forces_referral_not_zero_premium`.

## Why this matters for hiring

A working MCP server is the table-stakes deliverable. A working MCP server whose orchestration is *measured* — with SLOs that fail the build on regression — is the difference between a demo and something you would put in front of an underwriter.
