# 0005 — RKE2 Kubernetes; docker compose for development

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

We need GPU scheduling (MIG), policy enforcement (Kyverno), autoscaling
(KEDA), default-deny networking and a hardened, supportable distribution that
runs on-premises without internet access.

## Decision

- Production and staging: **RKE2** (CIS-hardened profile, SELinux, etcd
  encryption) with the NVIDIA GPU Operator, Kyverno, KEDA, cert-manager and a
  CNI that enforces NetworkPolicy (Canal/Cilium).
- Development and CI integration tests: **docker compose** with mock models
  (ADR 0012). The compose stack mirrors the production topology at the service
  level (gateway → router → services) so integration tests are meaningful.

## Consequences

- Helm charts and Kubernetes policies arrive in Milestone 6; compose arrives in
  Milestone 2.
- Anything enforced only by Kubernetes (NetworkPolicy, Kyverno) is also
  checked by tests that run against staging.
