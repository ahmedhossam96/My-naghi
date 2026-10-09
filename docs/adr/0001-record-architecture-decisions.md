# 0001 — Record architecture decisions

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Triaxis makes contractual promises (zero retention, residency, isolation,
signed supply chain). Auditors and customers need to see *why* the system is
built the way it is, and later engineers need to know which constraints are
deliberate.

## Decision

Every significant or security-relevant decision is recorded as an ADR in
`docs/adr/` using the template in `template.md`. When something is
ambiguous we choose the safer option, record it here, and tell the product
owner.

## Consequences

- ADRs are reviewed in the same pull request as the code they justify.
- Superseded ADRs stay in the repository with a pointer to their successor.
