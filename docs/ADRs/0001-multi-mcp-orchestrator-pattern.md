# ADR 0001 — Multi-MCP architecture with LangGraph orchestrator

**Status:** Accepted
**Date:** 2026-06-14

## Context

The workbench could have been built as a **single MCP server** with all underwriting, risk, and pricing tools registered together. That would be simpler — one process, one config, one transport. So why three servers?

The forces:

1. **Domain boundaries are real.** In a real insurance carrier, risk scoring, pricing, and underwriting decisions are owned by separate teams (actuarial, pricing, underwriting respectively). They evolve at different cadences and have different release processes.
2. **Independent scalability.** Risk scoring (especially when it wraps an ML model) is CPU-heavy. Pricing is rate-table-bound and latency-sensitive. Underwriting orchestration is I/O-heavy. A monolith forces them to scale together.
3. **MCP demonstrates well as multi-server.** Most public MCP demos are single-server toys. A multi-server pattern with deliberate boundaries is the architecture pattern that emerging A2A standards (Google A2A, LangGraph supervisor) formalize.
4. **A2A communication pattern.** This repo's portfolio purpose is to demonstrate the agent-to-agent pattern. A single MCP server cannot demonstrate that.

## Decision

Three MCP servers, each running as an independent process:

- `risk-mcp` — risk scoring + loss history retrieval
- `pricing-mcp` — rate lookup + premium computation
- `underwriting-mcp` — applicant context, eligibility, and **the orchestrator**

Inside `underwriting-mcp`, a **LangGraph state graph** sequences calls to itself and to the other two MCP servers. The orchestrator opens MCP client connections to `risk-mcp` and `pricing-mcp` on startup and treats them as remote tool registries.

## Consequences

### Positive

- **Clean bounded contexts.** Each server has a coherent responsibility and minimal coupling.
- **A2A pattern is visible.** Anyone reading the code sees three independent agents and one orchestrator delegating between them.
- **Independent deployability.** Each MCP can be containerized, versioned, and rolled out separately.
- **Resume-defensible.** Demonstrates senior engineering judgment — picking the right factoring even when a monolith would be simpler.

### Negative

- **Three processes to manage.** `docker-compose.yml` mitigates this for local dev.
- **Cross-server schema drift risk.** Mitigated by a `shared.py` Pydantic types module that all three servers depend on.
- **Slower local dev startup.** Trivial for a POC; would matter in real production.
- **Cross-process error handling.** Initial implementation defers retry / circuit-breaker concerns to v0.4. Mentioned here so reviewers know it was a conscious deferral.

## Alternatives considered

1. **Single MCP server with three tool groups.** Simplest. Rejected because it would not demonstrate the A2A pattern, which is the portfolio purpose.
2. **Three MCP servers with a non-MCP orchestrator** (e.g., a Python script that calls all three as clients). Cleaner separation but loses the "orchestrator is itself an MCP server" property — meaning Claude Desktop or other MCP clients couldn't address it directly.
3. **Microservices over REST with a separate MCP gateway.** Most "enterprise" choice but adds infrastructure (API gateway, service discovery) for zero portfolio benefit.

Chose option matching the goal: three MCP servers + LangGraph orchestrator inside `underwriting-mcp`.

## References

- [Anthropic — Model Context Protocol spec](https://modelcontextprotocol.io)
- LangGraph supervisor pattern: agent-to-worker delegation
- Google A2A protocol (announced 2025) for cross-agent interoperability
