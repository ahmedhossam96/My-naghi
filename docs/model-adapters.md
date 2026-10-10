# Model adapters and inference configuration

Implements ADR 0021. Code: `services/common/src/triaxis_common/inference.py`.

## Modes

| Mode | Where allowed | Behaviour |
|---|---|---|
| `mock` | dev, ci | Labelled simulated adapters (`triaxis_demo.mocks`). Every response says `mode: mock`. |
| `real` | everywhere (required in staging/prod) | OpenAI-compatible self-hosted endpoints. No fallback to mock. |

## Supported runtimes (OpenAI-compatible)

| Task | Endpoint | Typical runtime |
|---|---|---|
| LLM | `POST /v1/chat/completions` (`stream: true`), `GET /v1/models` | vLLM (`vllm serve <model> --served-model-name <registry-id> --revision <sha>`) |
| STT | `POST /v1/audio/transcriptions` (multipart: `file`, `model`, `language`, `response_format=json`) | vLLM (Whisper-class models) or an OpenAI-compatible faster-whisper server |
| TTS | `POST /v1/audio/speech` (`model`, `input`, `voice`, `response_format=wav`) | OpenAI-compatible speech server |

The served model name **must equal the registry model id** (for example
`qwen3.5-27b`); the tenant configuration selects ids per profile.

## Model gate

Before any call, `RegistryApproval` requires: model in `models/registry.yaml`,
task matches, `status: approved`, licence verified and cleared by the licence
gate. Otherwise `model_unavailable` (503) and the endpoint is not contacted.
To approve a model: verify the licence on the model card, pin
`source.revision`, record artefact sha256s, run `python -m tools.license_gate`
(see ADR 0009/0011). Never edit the registry to bypass the gate.

## Configuration (demo / local)

```bash
TRIAXIS_INFERENCE_MODE=real \
TRIAXIS_LLM_URL=http://127.0.0.1:8000 \
TRIAXIS_STT_URL=http://127.0.0.1:8001 \
TRIAXIS_TTS_URL=http://127.0.0.1:8002 \
uv run python -m triaxis_demo
```

URLs must be loopback/private/in-cluster (`require_in_cluster_url`); HTTPS is
mandatory in staging/prod. Endpoint credentials (vLLM `--api-key`) come from
OpenBao via `api_key_ref`, never from env files committed to Git.

`EndpointConfig` defaults: connect 2 s, read 30 s, total 120 s, 1 retry,
backoff 0.2 s (exponential).

## Error mapping

| Upstream | Client error code | Retried |
|---|---|---|
| connect error / timeout before output | `model_unavailable` | yes (bounded) |
| 502 / 503 before output | `model_unavailable` | yes (bounded) |
| 401 / 403 / 404 / other 5xx | `model_unavailable` | no |
| 429 | `rate_limited` | no |
| 408 / 504 / read timeout / total deadline | `timeout` | no |
| malformed, truncated or non-audio response | `upstream_error` | no |
| any error after partial output | delivered as an SSE `error` event | no |

## API examples (demo)

```bash
# session (demo identity provider)
TOKEN=$(curl -s -X POST localhost:8080/api/session -H 'content-type: application/json' \
  -d '{"tenant":"mynaghi","brand":"bmw"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
H=(-H "Authorization: Bearer $TOKEN" -H "X-Triaxis-Brand: bmw")

curl -sN "${H[@]}" -H 'content-type: application/json' localhost:8080/api/chat \
  -d '{"messages":[{"role":"user","content":"When is the showroom open?"}]}'
curl -s "${H[@]}" --data-binary @sample.wav 'localhost:8080/api/stt?language=ar&analytics=true'
curl -s "${H[@]}" -H 'content-type: application/json' localhost:8080/api/tts \
  -d '{"input":"Your car is ready."}' -o reply.wav
curl -s "${H[@]}" --data-binary @sample.wav 'localhost:8080/api/voice/turn?language=en'
curl -s "${H[@]}" localhost:8080/api/inference/status
curl -s "${H[@]}" -H 'content-type: application/json' localhost:8080/api/classify \
  -d '{"text":"IBAN SA0380000000608010167519","destination":"inference"}'
```
