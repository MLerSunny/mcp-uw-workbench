# mcp-uw-workbench

> A multi-server **Model Context Protocol (MCP)** workbench demonstrating **agent-to-agent orchestration**. Three independent MCP servers — `risk-mcp`, `pricing-mcp`, `underwriting-mcp` — collaborate behind a LangGraph supervisor to turn a single natural-language request into a complete insurance underwriting decision.

Every cross-agent call in this repository is a real MCP `call_tool` over stdio to a separate OS process. There is no shared memory and no shared database between agents — the only contract is the MCP tool schema.

> ⚠️ **All data here is synthetic.** No real PII, PHI, policy, or claims data. See [`SECURITY.md`](./SECURITY.md).

---

## What it looks like

```
Connected to peer agents over MCP (stdio):
  risk-mcp         score_risk, pull_loss_history, assess_property
  pricing-mcp      lookup_base_rate, calculate_premium, apply_modifiers

========================================================================
APP-00003 · CA · fire · $500,000   (3 prior claims, extreme wildfire)
------------------------------------------------------------------------
  decision : REFER
  premium  : $5,724.60
  Eligibility: refer_to_underwriter
  Reasons: 3+ prior claims in 5 years
  Risk score 91.0 (confidence 85%); factors: 3 prior claims (severe), credit band C, extreme wildfire risk
  Final premium $5,724.60 (base $2,900.00, risk loading $1,189.00, surcharges $1,635.60, credits $0.00)
  Delegated to: risk.score_risk, risk.pull_loss_history, pricing.lookup_base_rate, pricing.calculate_premium, pricing.apply_modifiers
```

That last line is the point: five delegations, two peer processes, one supervisor.

---

## Architecture

```
                 ┌──────────────────────────────────────────┐
  MCP client ──► │  underwriting-mcp   (supervisor)         │
  (Claude        │  ├─ lookup_applicant                     │
   Desktop,      │  ├─ check_eligibility                    │
   Cursor, …)    │  └─ generate_quote → LangGraph           │
                 │                       │                  │
                 │              ToolGateway (MCP/stdio)     │
                 │                  │            │          │
                 └──────────────────┼────────────┼──────────┘
                                    ▼            ▼
                        ┌────────────────┐  ┌────────────────┐
                        │   risk-mcp     │  │  pricing-mcp   │
                        │  (subprocess)  │  │  (subprocess)  │
                        └────────────────┘  └────────────────┘
```

| Server | Tools | Responsibility |
|---|---|---|
| **`risk-mcp`** | `score_risk` · `pull_loss_history` · `assess_property` | Peril-specific risk scoring and loss history |
| **`pricing-mcp`** | `lookup_base_rate` · `calculate_premium` · `apply_modifiers` | Rate lookup, premium computation, surcharges and credits |
| **`underwriting-mcp`** | `lookup_applicant` · `check_eligibility` · `generate_quote` | Applicant context, eligibility policy, and orchestration |

Full sequence diagram in [`ARCHITECTURE.md`](./ARCHITECTURE.md). Design rationale in [`docs/ADRs/`](./docs/ADRs/).

---

## Quickstart

```bash
git clone https://github.com/MLerSunny/mcp-uw-workbench.git
cd mcp-uw-workbench
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

**Run the demo** (spawns both peer agents, runs four applicants end to end):

```bash
python examples/01_quote_flow.py
```

**Run the tests:**

```bash
pytest -m "not integration"   # 25 unit tests, ~1.5s
pytest -m integration         # 5 tests over real MCP subprocesses, ~14s
pytest tests/eval -s          # evaluation harness with scorecard
```

**Connect from Claude Desktop** — merge [`examples/claude_desktop_config.json`](./examples/claude_desktop_config.json) into your Claude Desktop config, restart, then ask:

> *"Generate a homeowners quote for applicant APP-00001 in Florida, $750K dwelling, hurricane peril."*

Claude calls `underwriting-mcp.generate_quote`, which orchestrates across `risk-mcp` and `pricing-mcp` and returns a structured decision with rationale.

---

## Why three servers instead of one

Deliberate. See [ADR-0001](./docs/ADRs/0001-multi-mcp-orchestrator-pattern.md).

- **Bounded contexts.** Risk, pricing, and underwriting policy are owned by different teams in a real carrier and evolve on different release cadences.
- **Independent deployability and scaling.** Risk scoring is CPU-heavy; pricing is rate-table-bound; orchestration is I/O-heavy.
- **It is the point.** A single MCP server cannot demonstrate agent-to-agent delegation.

## How the agents actually talk

See [ADR-0002](./docs/ADRs/0002-gateway-and-transport.md). The orchestrator depends on a `ToolGateway` protocol with two implementations:

- **`MCPGateway`** — real MCP over stdio, spawning each peer as a subprocess. This is the default path.
- **`DirectGateway`** — in-process calls used by unit tests to avoid subprocess cost, and which records call order so delegation sequence can be asserted.

`test_direct_and_mcp_gateways_agree` runs the same scenario through both and asserts identical output, so the fast path cannot silently drift from the real one.

---

## Evaluation

Most MCP demos ship without any measurement. This one has a harness — see [`EVALUATION.md`](./EVALUATION.md) and `tests/eval/`.

```
=== Orchestrator evaluation ===
  clean-ny-water                   delegation=PASS  decision=PASS (quote)
  clean-tx-wind                    delegation=PASS  decision=PASS (quote)
  fl-coastal-hurricane             delegation=PASS  decision=PASS (refer)
  ca-wildfire-heavy-claims         delegation=PASS  decision=PASS (refer)
  fl-hurricane-repeat-claimant     delegation=PASS  decision=PASS (quote)
  unratable-peril-earthquake       delegation=PASS  decision=PASS (refer)

  delegation accuracy : 100% (SLO 95%)
  decision accuracy   : 100% (SLO 90%)
```

The harness caught a real defect during development: an unratable peril produced a **$0 premium that passed through as a valid quote**. It now forces a referral. That regression is pinned by `test_missing_filed_rate_forces_referral_not_zero_premium`.

---

## Project layout

```
src/mcp_uw_workbench/
├── shared.py                  # Pydantic types — the cross-agent contract
├── gateway.py                 # ToolGateway protocol + MCP/Direct implementations
├── data_loader.py
├── risk/server.py             # risk-mcp
├── pricing/server.py          # pricing-mcp
└── underwriting/
    ├── server.py              # underwriting-mcp (supervisor)
    └── orchestrator.py        # LangGraph state machine
tests/
├── test_smoke.py              # unit, via DirectGateway
├── test_mcp_integration.py    # real MCP subprocesses
└── eval/                      # scenario-based evaluation harness
docs/ADRs/                     # architecture decision records
```

## Roadmap

- [x] **v0.1** — three MCP servers, LangGraph orchestrator, synthetic data
- [x] **v0.2** — real MCP client transport (`MCPGateway`), async orchestrator, MCP SDK 2.0, integration tests
- [x] **v0.3** — evaluation harness with delegation and decision SLOs
- [ ] **v0.4** — SSE / streamable-HTTP transport for long-lived peer services; demo recording
- [ ] **v0.5** — additional peer agent (`fnol-mcp`, claims intake) proving the pattern extends

## Requirements

Python 3.11+ · MCP Python SDK 2.0+ · LangGraph

## License

MIT — see [`LICENSE`](./LICENSE).
