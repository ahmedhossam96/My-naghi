# 0015 — Application boundary

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Customers want a voice agent, but business logic (CRM lookups, bookings) is
specific to each tenant and brings their data into scope.

## Decision

Triaxis exposes **model APIs** (STT, TTS, LLM, batch) plus an **optional
reference voice pipeline**. Tenant applications own business logic, CRM
integration and conversation state beyond a single session. Triaxis never
connects to tenant systems.

## Consequences

- No outbound connections to tenant systems are needed (supports rule 3).
- The voice pipeline is a reference implementation tenants may use or replace.
