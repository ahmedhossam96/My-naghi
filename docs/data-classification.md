# Data classification and handling policy

Status: v1 (2026-10-09). Implements ADR 0020. Owner: Quantum Seal security
architecture; to be co-owned and approved by MY NAGHI's data protection
officer. This document describes Triaxis controls; it is **not** a statement
of legal compliance (KSA PDPL, PCI DSS, NCA ECC). The mapping to MY NAGHI
policies below is a proposal pending the tenant's review.

## 1. Levels

| Level | Examples | Default handling |
|---|---|---|
| **Public** | Approved marketing content, published opening hours | Standard approved processing |
| **Internal** | Non-public operational instructions, generic chat | Authorised tenant processing only |
| **Confidential** | Customer contact details, VINs, customer records, sales and pricing data, any voice recording or transcript | Approved self-hosted inference only, encryption at rest, never in telemetry |
| **Restricted** | Credentials and secrets, national ID / Iqama, payment cards, IBANs | Credentials blocked before any model; identifiers redacted before inference; no persistence or export; never in batch analytics or backups |

Levels are ordered. A result always takes the **highest** applicable level.

## 2. Tags

Data-type tags (`DataTag`) and privacy tags (`PrivacyTag`) are kept separate
from the level; one item can carry several.

| Data tag | Detector / source | Default level | Privacy tags |
|---|---|---|---|
| `credential` | PEM private keys, AWS/GitHub/Slack/OpenBao tokens, JWTs, bearer tokens, `password/secret/api_key/token = …` (English and Arabic) | Restricted | credentials_secrets |
| `payment_card` | 13–19 digits with separators, Luhn-valid | Restricted | personal_data, regulated_financial |
| `iban` | ISO 13616 mod-97 (SA length enforced) | Restricted | personal_data, regulated_financial |
| `national_id` | 10 digits starting 1/2; de-facto checksum (ADR 0018); checksum failures still flagged | Restricted | personal_data, sensitive_personal_data |
| `phone_number` | Saudi mobile (+966 / 00966 / 0, 5x) | Confidential | personal_data |
| `email_address` | RFC-like pattern | Confidential | personal_data |
| `vehicle_id` | 17-char VIN alphabet with ≥3 letters and ≥3 digits | Confidential | personal_data |
| `customer_record` | Field rules (`customer_name`, `client_id`, …) | Confidential | personal_data |
| `voice_recording` | Declared by the server for every audio input | Confidential | personal_data |

Detection runs on a normalised view: Unicode NFKC, zero-width/bidi characters
removed, Arabic-Indic and Extended Arabic-Indic digits mapped to ASCII. Spans
map back to the original text so redaction removes exactly the identifier.

Known gaps (pattern detection cannot see them): names, addresses, free-text
health or religious information, unusual identifier formats. Presidio-based
recognizers in the guardrails service (Milestone 3) add supplementary signals;
the deterministic engine remains the enforcement boundary.

## 3. Context signals

| Signal | Effect |
|---|---|
| Workload baseline (policy `baseline`) | Minimum level: `stt`/`voice`/`batch` start at Confidential |
| Field rules (structured records) | Level and tags implied by field names |
| Source propagation | A transcript inherits the audio's level and tags; analytics inherit the transcript's |
| Client-requested level | May only **raise** the level; never lowers it |

Reclassification to a lower level is not possible through any request. It
requires a reviewed change to the policy file.

## 4. Policy (`config/policies/<id>.yaml`)

Each tenant names its policy with `data_policy` (default: `default`). A policy
defines, per destination (`inference`, `client_response`, `persistence`,
`export`, `backup`), the decision for each level, stricter tag rules, allowed
workloads per level, required controls per level and brand overrides. The
strictest applicable rule wins; any missing policy or engine error fails
closed with `classification_unavailable` (HTTP 503).

Default policy summary:

| Destination | Public | Internal | Confidential | Restricted | Tag rules |
|---|---|---|---|---|---|
| inference | allow | allow | allow | redact | credential → block; card/IBAN/ID → redact |
| client_response | allow | allow | allow | redact | credential → redact |
| persistence | allow | allow | restrict | block | credential → block |
| export | allow | allow | restrict | block | credential → block |
| backup | allow | allow | block | block | — |

Restricted data is permitted only for `llm`, `stt`, `tts`, `voice` workloads
(never `batch`). The `strict` policy additionally blocks restricted data from
inference and redacts phone numbers and e-mail addresses before any model.

## 5. Where enforcement happens

| Boundary | Destination | Notes |
|---|---|---|
| Chat input (every message) | inference | Blocked content never reaches the adapter; redaction before the adapter |
| Model output (streamed segments) | client_response | Masked before release; one aggregated audit event |
| Audio before STT | inference | Voice-recording baseline |
| Transcript | client_response | Inherits audio classification |
| Transcript analytics | inference | Inherits transcript classification; may be blocked |
| TTS input | inference | |
| Voice turn | all of the above | Stateless per turn |
| Backup collection | backup | Restricted tags fail the backup |
| RAG indexing / retrieval, export, business-record persistence | respective destination | Not implemented yet; must call `PolicyEngine.enforce` |

## 6. Audit

`classification_decision` events carry level, decision, data tags, reason codes
and policy version, plus tenant, brand and workload. Never the text, never
the matched values.

## 7. Proposed mapping to MY NAGHI policies (to confirm)

| MY NAGHI category (assumed) | Triaxis level |
|---|---|
| Public / marketing | Public |
| Internal use | Internal |
| Confidential (customer, commercial) | Confidential |
| Highly confidential / regulated (identity, payment, credentials) | Restricted |
