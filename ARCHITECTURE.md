# Architecture

## System overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Client tier                                                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐   │
│  │  Claude Desktop  │  │  Cursor / Cline  │  │  Custom MCP client    │   │
│  └────────┬─────────┘  └────────┬─────────┘  └──────────┬───────────┘   │
└───────────┼─────────────────────┼─────────────────────────┼─────────────┘
            │       MCP (stdio or SSE)                       │
            └──────────────────────▼─────────────────────────┘
                       ┌────────────────────────┐
                       │   underwriting-mcp     │
                       │   ┌────────────────┐   │
                       │   │  LangGraph     │   │
                       │   │  orchestrator  │   │
                       │   └─┬──────────┬───┘   │
                       │     │          │       │
                       │     ▼          ▼       │
                       │ MCP client  MCP client │
                       └────┬──────────┬────────┘
                            │          │
                            │  MCP     │  MCP
                            ▼          ▼
                  ┌──────────────┐  ┌──────────────┐
                  │   risk-mcp   │  │ pricing-mcp  │
                  │              │  │              │
                  │ Tools:       │  │ Tools:       │
                  │ • score_risk │  │ • lookup_rate│
                  │ • loss_hist  │  │ • calc_prem  │
                  │ • assess_prop│  │ • modifiers  │
                  └──────────────┘  └──────────────┘
```

## Quote-generation sequence

```
Client                underwriting-mcp         risk-mcp           pricing-mcp
  │                         │                     │                    │
  │  generate_quote(app_id) │                     │                    │
  │────────────────────────►│                     │                    │
  │                         │                     │                    │
  │                         │ [orchestrator]      │                    │
  │                         │  lookup_applicant   │                    │
  │                         │  check_eligibility  │                    │
  │                         │                     │                    │
  │                         │ score_risk(app, peril)                   │
  │                         │────────────────────►│                    │
  │                         │   risk_score, factors                    │
  │                         │◄────────────────────│                    │
  │                         │                     │                    │
  │                         │ pull_loss_history(app)                   │
  │                         │────────────────────►│                    │
  │                         │   prior_claims[]                         │
  │                         │◄────────────────────│                    │
  │                         │                     │                    │
  │                         │ lookup_base_rate(state, product, peril)  │
  │                         │──────────────────────────────────────────►│
  │                         │   base_rate                              │
  │                         │◄──────────────────────────────────────────│
  │                         │                     │                    │
  │                         │ calculate_premium(rate, risk, mods)      │
  │                         │──────────────────────────────────────────►│
  │                         │   premium, breakdown                     │
  │                         │◄──────────────────────────────────────────│
  │                         │                     │                    │
  │                         │ [orchestrator]      │                    │
  │                         │  compile_quote      │                    │
  │                         │  generate_rationale │                    │
  │                         │                     │                    │
  │  quote_decision         │                     │                    │
  │◄────────────────────────│                     │                    │
```

## Key design decisions

| ADR | Decision |
|---|---|
| [0001](./docs/ADRs/0001-multi-mcp-orchestrator-pattern.md) | Three MCP servers (not one monolith) with a LangGraph orchestrator inside `underwriting-mcp`. |
| [0002](./docs/ADRs/0002-gateway-and-transport.md) | `ToolGateway` protocol with real-MCP and in-process implementations; stdio transport; real MCP is the default path. |
| 0003 *(planned)* | SSE / streamable-HTTP transport for long-lived peer services. |

## A2A (agent-to-agent) communication pattern

The orchestrator inside `underwriting-mcp` acts as a **supervisor agent**. It delegates to:

- `risk-mcp` — for risk scoring and loss history (read-mostly)
- `pricing-mcp` — for rate lookup and premium computation (read + compute)

Communication happens over the MCP protocol itself — every cross-server call is a `ClientSession.call_tool()` invocation against a peer running in a separate OS process. This is "A2A via MCP": the orchestrator and its peers share no memory and no database, and could be deployed on different hosts. The only contract between them is the MCP tool schema.

Verified by `tests/test_mcp_integration.py`, which asserts capability discovery, cross-process round trips, and that the in-process test double produces identical results to the real transport.

This pattern maps cleanly to:
- **Google A2A protocol** — `underwriting-mcp` is the agent-card-discoverable entry point.
- **LangGraph supervisor** — the orchestrator is the supervisor node; risk/pricing are worker agents.
- **OpenAI Swarm** — the orchestrator is the routing function.

## Non-goals

- This is **not** a complete underwriting system. It is a reference architecture for MCP + multi-agent orchestration patterns.
- It does **not** model real-world insurance complexity (state-specific filings, reinsurance, multi-product portfolios).
- It does **not** include a UI. The MCP client (Claude Desktop, Cursor, etc.) is the UI.
