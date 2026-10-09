# 0018 — Saudi national ID / Iqama check digit

- Status: accepted (provisional — awaiting an authoritative specification)
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

The guardrails service (Milestone 3) must detect Saudi national ID and Iqama
numbers: 10 digits, first digit `1` (citizen) or `2` (resident), with a check
digit. The brief asks for validation against an authoritative specification.
We have not found a publicly published specification from the National
Information Center (NIC) or Absher.

## Decision

- Implement the **de-facto algorithm** used widely by Saudi government-facing
  form validators: a Luhn-style checksum over all 10 digits (double the digits
  in odd positions counting from the left, sum digit-wise, total mod 10 = 0).
- Label it in code and documentation as **"de-facto, not verified against an
  authoritative specification"**.
- Because masking is a privacy control, the recognizer also reports 10-digit
  numbers starting with 1 or 2 that fail the checksum, at a lower confidence,
  so a wrong checksum assumption fails towards masking (the safer option).
- Replace the algorithm if the product owner provides an authoritative
  specification; property-based tests are written against the algorithm
  interface so they can be re-pointed.

## Consequences

- Possible false positives on unrelated 10-digit numbers starting with 1/2;
  acceptable for a masking control.
