# 0011 — Safetensors-only model loading

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Pickle-based formats (`.bin`, `.pt`, `.pth`, `.ckpt`, `.pkl`) execute
arbitrary code when loaded. Contract rule 6: models load from `safetensors`
only, and untrusted files are never unpickled.

## Decision

- Registry entries must declare `format: safetensors`. Weight artefacts must be
  `.safetensors`; only config/tokenizer files (`.json`, `.txt`, `.model`,
  `.tiktoken`, `.yaml`, `.jinja`, `.md`) are allowed besides weights.
- The sha256 of every artefact is recorded and verified before load.
- `pickle`, `marshal`, `shelve`, `dill` and `torch.load` are banned in service
  code by ruff `banned-api`. vLLM is started with safetensors loading only.
- Models distributed only in another format (e.g. Silero VAD in TorchScript /
  ONNX, older pyannote checkpoints as `pytorch_model.bin`) are converted to
  safetensors on the **isolated model-preparation server**, re-hashed, signed,
  and recorded as a separate registry version. If conversion is not possible
  the model is not used.
- `tools/check_no_weights.py` blocks weight files (by extension and by magic
  bytes) from being committed.

## Consequences

- VAD and diarization need a conversion step (Milestone 5). This is flagged in
  their registry `notes`.
