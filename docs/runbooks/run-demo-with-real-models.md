# Runbook: run the demo with real (self-hosted) models

Goal: the AI Playground shows output from real models, not mock text. The
header must read "Real inference mode" and every model row must be `Ready`.
Background: ADR 0009, 0011, 0021; `docs/model-adapters.md`.

## What you need

| Need | Why |
|---|---|
| A machine with a **GPU** (≥ 24 GB VRAM for a 7–14B chat model; more for 27B+) or a large-RAM CPU box for tiny models (slow) | Serving the models |
| Internet **once**, on that machine or a model-preparation machine | Reading model cards/hashes and fetching weights |
| Python 3.12 + `uv`, this repo | Running Triaxis and the onboarding tool |
| vLLM (LLM, and Whisper-class STT) | OpenAI-compatible self-hosted server |
| An OpenAI-compatible speech (TTS) server | `/v1/audio/speech` |
| Your decision on **which models** | Registry ids must match the tenant config (below) |

Nothing is sent to third-party AI services at runtime; the internet is only for
the one-off model download.

## 1. Choose models and check the registry/tenant match

The tenant (`config/tenants/mynaghi.yaml`) selects four profiles. Either approve
those registry ids, or change the tenant to ids you approve:

| Profile | Current id | Task |
|---|---|---|
| `llm_chat` | `qwen3.5-27b` | llm |
| `llm_voice` | `qwen3.5-35b-a3b` | llm |
| `stt` | `whisper-large-v3` | stt |
| `tts` | `chatterbox-multilingual` | tts |

The ids in the registry are Triaxis names. The *hub repository* is a separate
argument and must be the real repository of the model you chose. For a first
smoke test, a smaller permissively licensed model is fine: add or edit a
registry entry (`models/registry.yaml`), point the tenant profile at it, and
use the same id as the vLLM served name.

## 2. Approve each model with evidence (on a machine with internet)

```bash
uv sync --all-packages
# Dry run: prints licence, pinned revision, safetensors files and sizes. Writes nothing.
uv run python -m tools.model_onboard --model-id whisper-large-v3 --repo <hub-org>/<repo>

# If the licence on the model card is Apache-2.0 / MIT / BSD / ISC and you accept it:
uv run python -m tools.model_onboard --model-id whisper-large-v3 --repo <hub-org>/<repo> \
  --approve --approved-by "Your Name"
```

The tool refuses non-commercial licences, models without `.safetensors`
weights, and any licence outside the allow-list (those need a human approval
record in the registry; Jais, ALLaM, AceGPT and pyannote fall here). It pins the
exact commit SHA and records each weight file's sha256 and size.

Then:

```bash
uv run python -m tools.license_gate      # must pass
uv run pytest                            # must pass
git add models/registry.yaml && git commit -m "models: approve <id> (<licence>, rev <sha12>)"
```

Review the diff of `models/registry.yaml` before committing: it is the
approval record.

## 3. Download the weights (safetensors only) and verify

```bash
huggingface-cli download <hub-org>/<repo> --revision <full-sha-from-registry> \
  --include "*.safetensors" "*.json" "*.txt" "*.model" --local-dir /srv/models/<id>
uv run python -m tools.model_registry verify <id> --dir /srv/models/<id>   # sha256 check
```

Never download `.bin`/`.pt`/`.pkl` files.

## 4. Serve the models

```bash
# Chat model (served name MUST equal the registry id)
vllm serve /srv/models/qwen3.5-27b --served-model-name qwen3.5-27b --port 8000 --host 127.0.0.1
# Voice LLM, if different: second vLLM instance or a multi-model gateway on its own port
vllm serve /srv/models/qwen3.5-35b-a3b --served-model-name qwen3.5-35b-a3b --port 8003 --host 127.0.0.1
# STT (Whisper-class): any OpenAI-compatible /v1/audio/transcriptions server, port 8001
# TTS: any OpenAI-compatible /v1/audio/speech server, port 8002
```

The demo uses one LLM endpoint for both chat and voice profiles. Serve both
model names from the same endpoint (for example a router in front of two vLLM
processes), or point both tenant profiles at the same approved model.
Bind everything to 127.0.0.1; do not expose model ports.

Check: `curl -s http://127.0.0.1:8000/v1/models` lists the served name.

## 5. Start the demo in real mode

```bash
TRIAXIS_INFERENCE_MODE=real \
TRIAXIS_LLM_URL=http://127.0.0.1:8000 \
TRIAXIS_STT_URL=http://127.0.0.1:8001 \
TRIAXIS_TTS_URL=http://127.0.0.1:8002 \
uv run python -m triaxis_demo
```

Open http://127.0.0.1:8080 → AI Playground.

## 6. Acceptance checks

1. Header: **Real inference mode**; Inference Services card: every row `Ready`.
   `Unapproved` = registry not approved; `Model Not Served` = served name differs
   from the registry id; `Unreachable` = server down.
2. Chat: replies differ per prompt, badge is **Live**, footer says "real model".
3. Synthetic ID sample: the model receives `[NATIONAL_ID]` (verify with a local
   proxy capture in the drill only).
4. Stop the LLM server → chat returns `model_unavailable`; **no mock text appears**.
5. STT: upload Arabic and English audio; transcript is real. TTS: real speech.
6. Voice Agent turn works for mynaghi.sa, BMW and MINI; denied for others.
7. Record date, model ids, revisions and results in
   `docs/status/capability-matrix.md`; only then mark the capability verified.

## Known limits

- Qwen3.5 ids and hub repositories in the registry came from the project brief
  and have not been verified by this tooling; confirm they exist and match the
  licence you expect before approving.
- No streaming (WebSocket) STT and no VAD in the demo; voice is whole-utterance.
- TTS server choice is open; Chatterbox does not ship an OpenAI-compatible
  server by itself, so you need a wrapper or another approved TTS model.
