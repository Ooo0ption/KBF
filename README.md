# KBF — Knowledge Boundary Fingerprinting

> 🆕 **September 2026 Update:** We have generated a fresh batch of **probe sets for 28 models**, including the latest models as of September 2026. 🚀 [Explore the new probe sets](probes/reference_202609/) — give them a try!

A black-box auditing tool for LLM APIs. Given a probe set generated from a
reference model, KBF decides whether a target API is actually serving the
claimed model — by exploiting the fact that every LLM produces a unique
pattern of wrong answers at its knowledge boundary.

Two user-facing scripts under `scripts/`:

- `kbf_test.py` — query a target API with a probe set, OR re-score a saved
  results file with `--evaluate` (no API key needed in evaluate mode).
- `generate_probes.py` — build a new probe set for a model not already in
  `probes/reference/`.

Two reference collections ship with this release: the original 16 sets under
`probes/reference/` and 28 September 2026 sets under
[`probes/reference_202609/`](probes/reference_202609/README.md). The September
collection uses dated filenames to preserve enrollment provenance. Choose a collection explicitly with `--reference`.

The scripts were synchronized from `ICLR_exp/scripts` on 2026-09-26. The new
generator includes stricter consensus, per-domain generation budgets, optional
layout filtering, and a separate contrast endpoint. The shared scorer rounds
absolute-domain values to integers (halves away from zero) before applying the
domain tolerance. Provider defaults also reflect the updated source; use the
provider recorded by a probe set when reproducing its enrollment conditions.

---

## Install

```bash
pip install -r requirements.txt
cp scripts/endpoints.json.example scripts/endpoints.json   # fill in your endpoints
```

Each top-level key in `endpoints.json` is a **profile name** you pass via
`--endpoint <name>`. Per profile, fill in:

| Field | Required? | Why it's needed |
|-------|-----------|-----------------|
| `api_base` | yes | The model API URL to send requests to — where KBF reaches the reference or target model. |
| `api_key` | yes | Auth credential. |
| `protocol` | yes | `openai-chat` / `anthropic-messages` / `openai-responses` |

---

## Usage


### Quickstart

Test a target API against one of the bundled reference probe sets — one
command, no probe generation needed:

```bash
python3 scripts/kbf_test.py \
    --reference probes/reference/gpt-5.4.json \
    --target    gpt-5.4 \
    --endpoint  openrouter
```

Writes `results/<target>_<YYYYMMDD-HHMMSS>.json` and prints a one-line
SAME / DIFF / UNDETERMINED verdict. 

### Full workflow

#### Step 1 — Generate a probe set

Only needed if your reference model isn't already under `probes/reference/`.

```bash
python3 scripts/generate_probes.py \
    --reference  openai/gpt-5.4 \
    --endpoint   openrouter
```

Writes `probes/generated/<model>_<YYYYMMDD>.json` (refuses to overwrite unless
`--force`) and ends with a self-test pass unless you pass `--no-self-test`.

**`generate_probes.py` defaults:**

| Flag | Default | Notes |
|------|---------|-------|
| `--min-probes` | `100` | Minimum total probes |
| `--max-rounds` | `6` | Max rounds per domain |
| `--max-probes` | unset | Generation budget split into per-domain quotas; the crossing round is retained, so this is not a strict final cap |
| `--contrast` | `qwen/qwen3.5-9b` | Contrast model for screening |
| `--contrast-endpoint` | reference endpoint | Separate named profile for contrast screening, e.g. `openrouter` when the reference uses the official OpenAI endpoint |
| `--layout-filter` | `0` (off) | Extra passes that delete prompt-layout-sensitive probes|
| `--output` | `probes/generated/<model>_<YYYYMMDD>.json` | Output path |


#### Step 2 — Test a target

```bash
python3 scripts/kbf_test.py \
    --reference probes/reference/gpt-5.4.json \
    --target    gpt-5.4 \
    --endpoint  openrouter 
```

Or fully on the command line with no profile — e.g. a suspect third-party relay:

```bash
python3 scripts/kbf_test.py \
    --reference probes/reference/gpt-5.4.json \
    --target    gpt-5.4 \
    --api-base  https://suspect-api.example.com/v1/chat/completions \
    --api-key   sk-xxx \
    --protocol  openai-chat
```

#### Step 3 — Re-score saved results (no API calls)

```bash
python3 scripts/kbf_test.py \
    --reference probes/reference/gpt-5.4.json \
    --evaluate  results/gpt-5.4_20260518-103000.json
```

Prints `self_error`, `target_error`, coverage, the CP99 bound `p0`, the
binomial p-value, and the final verdict. Useful for re-running at a different
`--min-coverage` without re-hitting the API.

To re-run only the self-test on an existing probe file (the one path that
writes back into a probe file):

```bash
python3 scripts/generate_probes.py \
    --reference      openai/gpt-5.4 \
    --self-test-only probes/reference/gpt-5.4.json
```

---

## How to read the verdict

`kbf_test.py` reports `SAME`, `DIFF`, or `UNDETERMINED` based on a one-sided
binomial test against the reference's own self-test error rate, with a
Clopper-Pearson 99 % upper bound for `p0`. Missing, unparseable, and
out-of-range answers are stored as `null` and excluded from scoring.
`total` counts valid scored answers, `hamming` counts mismatches among them,
and the error rate is `hamming / total`. Calibration uses its own valid-answer
count `self_total` for CP99, and each target uses its own `total` for the
binomial test.

| field             | meaning |
|-------------------|---------|
| `ref self_error`  | how often the reference itself misses its own probes (baseline noise) |
| `target error`    | how often the target API misses the same probes |
| `CP99 bound p0`   | 99 % upper bound for the reference's true error rate |
| `p_value_binomial`| one-sided tail probability `P(X >= observed errors | n, p0)`, not a probability that the null is true |
| **verdict**       | `DIFF` if `p_value_binomial < 0.05` else `SAME` |

`UNDETERMINED` means too few probes were answered to draw a conclusion —
typically when the target rejects the format or coverage drops below
`--min-coverage` (default 0.5).

---

## Configuring endpoints & keys

Two layers, highest wins per field:

1. **CLI flags** — `--api-base`, `--api-key`, `--protocol`, `--provider`
2. **Named profile** — `--endpoint <name>` reads from `scripts/endpoints.json`

If neither layer supplies a non-empty key, the script fails immediately with
explicit instructions. **No environment variable is ever read implicitly** —
reference one from a profile via `api_key_env` if you want that.

```json
{
  "anthropic-official": {
    "api_base": "https://api.anthropic.com/v1/messages",
    "api_key":  "sk-ant-...",
    "protocol": "anthropic-messages"
  },
  "target-relay-A": {
    "api_base": "https://relay-a.example.com/v1/chat/completions",
    "api_key_env": "RELAY_A_KEY",
    "protocol": "openai-chat"
  }
}
```

List configured profiles with `python3 scripts/kbf_test.py --list-endpoints`.

### Supported protocols

| `protocol`           | Use for                                  |
|----------------------|------------------------------------------|
| `openai-chat`        | OpenRouter, OpenAI chat, DeepSeek, GLM, Qwen, and most OpenAI-compatible relays. |
| `anthropic-messages` | Anthropic official Messages API and Claude-compatible relays. |
| `openai-responses`   | OpenAI gpt-5 / o-series via the Responses API. |

To add a new wire format, edit `scripts/protocols.py`.

### OpenRouter: provider pinning (special rule)

OpenRouter is a multi-vendor relay — the same model can be served by several
backends with different precision and behavior. To keep audits reproducible,
KBF **pins the exact provider we used in our experiments** and disables
OpenRouter's fallback. Concretely, every OpenRouter request carries:

```json
"provider": { "order": ["<pinned-provider>"], "allow_fallbacks": false }
```

**If a pinned provider is invalid or unavailable, change it in one of these places:**

- Edit the model's entry in **`PROVIDER_MAP` (`scripts/kbf_common.py`, near the
  top of the file)** to a backend that is currently live — this is the
  permanent fix and what shipped reference sets expect. Example already in the
  file: `"z-ai/glm-4.7-flash": "DeepInfra",  # Z.AI endpoint unreliable`.
- Or, for a one-off run, pass **`--provider <name>`** (or set `provider` in the
  profile) to override without editing code.

Find which backends are currently live for a model at
`https://openrouter.ai/<model>/providers`.

---

## Troubleshooting

- **Confirm the exact model name before a run.** Pass the model identifier
  exactly as the target provider expects it (e.g. `openai/gpt-5.4`). A wrong
  or stale name surfaces as a 4xx and wastes a run.
- **Too few probes after screening (T3 / budget references).** Contrastive
  screening against `qwen/qwen3.5-9b` can drop enough probes that the set falls
  short of `--min-probes`. Either skip screening with `--no-contrast`, or swap
  the contrast model with `--contrast <model>`. Keep the contrast model in the
  same low tier — using a T1/T2 model over-screens and can leave too few probes.
- **4xx error printed verbatim.** KBF does not silently work around endpoints
  that reject its requests. The response body is included in the error so
  the underlying cause (bad model name, unsupported parameter, malformed
  payload, auth failure) is visible. 429 / 5xx are treated as transient and
  retried.
- **OpenRouter pinned provider unavailable.** When a run pins a provider with
  `allow_fallbacks: false` and that backend is down, OpenRouter returns a 4xx
  (often `404 No endpoints found`). KBF fails fast and the error explicitly
  says the pinned provider is unavailable and that no fallback was attempted.
  Fix it by repointing the model's entry in `PROVIDER_MAP`
  (`scripts/kbf_common.py`) to a live backend, or pass `--provider <name>` —
  see "OpenRouter: provider pinning" above.
- **`UNDETERMINED` verdict.** Coverage fell below `--min-coverage`. Lower the
  threshold, or inspect the result file — the endpoint may be truncating
  output or stripping the system prompt.
- **Thinking-model timeout / empty output.** KBF auto-discovers a
  thinking-suppression strategy on first call and caches it. The reason we
  disable thinking is purely to keep token usage — and therefore audit cost —
  bounded. We ship several suppression strategies for vendor-specific fields.
  Discovery now also checks `usage.reasoning_tokens` so a strategy the
  endpoint accepts but silently ignores is rejected automatically. If none of
  the built-in strategies actually suppresses thinking on your endpoint, you
  have two options:
    1. **Add a strategy that this endpoint accepts** — extend
       `THINKING_STRATEGIES` and `apply_thinking_strategy` in
       `scripts/kbf_common.py` (follow the existing entries for the pattern,
       e.g. a new top-level payload field the endpoint recognises). This is
       the right fix when you plan to audit the same endpoint repeatedly.
    2. **Re-run with `--allow-thinking`** to drop the suppression requirement
       and let the model think freely, with a 30× per-batch token budget so
       answers still fit. Convenient for a one-off run, but **token usage and
       API cost are NOT bounded in this mode** — a single audit can end up
       many times more expensive than a normal one, depending on the model.

---

## API requirements

For an endpoint to be usable, the target must accept `temperature`, a system
prompt, a user prompt, and a max-output-tokens field on every request.

KBF sizes max output tokens at `80 × batch_size`; endpoints that silently
truncate below that break self-consistency.

---

## Directory layout

```
KBF/
├── probes/
│   ├── reference/       ← 16 original probe sets (READ-ONLY)
│   ├── reference_202609/ ← 28 September 2026 probe sets (READ-ONLY)
│   └── generated/       ← outputs of generate_probes.py (gitignored)
├── results/             ← outputs of kbf_test.py (gitignored)
├── scripts/
│   ├── kbf_test.py              ← test target API and/or re-score saved results
│   ├── generate_probes.py       ← build a new reference probe set
│   ├── kbf_common.py            ← shared parsers, stats, endpoint resolver
│   ├── protocols.py             ← per-protocol request/response adapters (extension point)
│   ├── domains.py               ← canonical probe domain definitions (extension point)
│   ├── DATA_FORMAT.md
│   └── endpoints.json.example   ← copy to endpoints.json (gitignored)
└── requirements.txt
```

## Reference probe sets

The September 2026 collection contains **28 reference sets**
under [`probes/reference_202609/`](probes/reference_202609/). Each model name
below links to its probe file. `#Probes` is `len(probes)`; `Self-error` is the
stored calibration `hamming / total`, using valid self-test answers only.
Providers are taken from each file's enrollment metadata. Self-error is not
an independent false-positive estimate.

| Model | Family | Provider | #Probes | Self-error |
|---|---|---|---:|---:|
| [Claude Fable 5.1](probes/reference_202609/claude-fable-5.1_20260911.json) | Anthropic | Anthropic | 453 | 2.6% |
| [Claude Opus 4.6](probes/reference_202609/claude-opus-4.6_20260912.json) | Anthropic | Amazon Bedrock | 445 | 5.2% |
| [Claude Opus 5](probes/reference_202609/claude-opus-5_20260911.json) | Anthropic | Claude Platform on AWS | 414 | 1.7% |
| [Claude Sonnet 4.6](probes/reference_202609/claude-sonnet-4.6_20260912.json) | Anthropic | Google | 436 | 6.4% |
| [DeepSeek V3.2](probes/reference_202609/deepseek-v3.2_20260912.json) | DeepSeek | Google | 380 | 6.1% |
| [DeepSeek V4 Pro 0813](probes/reference_202609/deepseek-v4-pro-0813_20260911.json) | DeepSeek | Novita | 557 | 6.1% |
| [DeepSeek V4.1 Flash](probes/reference_202609/deepseek-v4.1-flash_20260911.json) | DeepSeek | Novita | 363 | 8.0% |
| [Gemini 2.5 Flash Lite](probes/reference_202609/gemini-2.5-flash-lite_20260918.json) | Google | Google | 319 | 10.7% |
| [Gemini 3 Flash Preview](probes/reference_202609/gemini-3-flash-preview_20260912.json) | Google | Google | 409 | 5.1% |
| [GLM-4.7](probes/reference_202609/glm-4.7_20260913.json) | Z.AI | Z.AI | 430 | 7.7% |
| [GLM-5.3](probes/reference_202609/glm-5.3_20260911.json) | Z.AI | Z.AI | 372 | 7.3% |
| [GLM-5](probes/reference_202609/glm-5_20260912.json) | Z.AI | Z.AI | 332 | 6.6% |
| [GPT-4.1-mini](probes/reference_202609/gpt-4.1-mini_20260915.json) | OpenAI | OpenAI | 265 | 6.4% |
| [GPT-4.1-nano](probes/reference_202609/gpt-4.1-nano_20260918.json) | OpenAI | OpenAI | 248 | 10.1% |
| [GPT-5.4-mini](probes/reference_202609/gpt-5.4-mini_20260916.json) | OpenAI | OpenAI | 194 | 13.4% |
| [GPT-5.4](probes/reference_202609/gpt-5.4_20260913.json) | OpenAI | OpenAI | 531 | 7.7% |
| [GPT-5.6-luna](probes/reference_202609/gpt-5.6-luna_20260915.json) | OpenAI | OpenAI | 305 | 7.6% |
| [GPT-5.6-sol](probes/reference_202609/gpt-5.6-sol_20260912.json) | OpenAI | OpenAI | 426 | 6.8% |
| [GPT-6-astra](probes/reference_202609/gpt-6-astra_20260910.json) | OpenAI | OpenAI | 538 | 1.5% |
| [HY4 Preview](probes/reference_202609/hy4-preview_20260911.json) | Tencent | Tencent | 538 | 5.8% |
| [Kimi K2 0905](probes/reference_202609/kimi-k2-0905_20260912.json) | Moonshot | Novita | 363 | 7.8% |
| [Kimi K3](probes/reference_202609/kimi-k3_20260912.json) | Moonshot | Moonshot AI | 424 | 8.5% |
| [Llama 4 Scout](probes/reference_202609/llama-4-scout_20260918.json) | Meta | Novita | 231 | 15.2% |
| [MiniMax M3](probes/reference_202609/minimax-m3_20260911.json) | MiniMax | Minimax | 311 | 8.1% |
| [Qwen3.5-27B](probes/reference_202609/qwen3.5-27b_20260913.json) | Alibaba | Alibaba | 149 | 6.7% |
| [Qwen3.5-397B-A17B](probes/reference_202609/qwen3.5-397b-a17b_20260912.json) | Alibaba | Alibaba | 254 | 6.3% |
| [Qwen3.5-9B](probes/reference_202609/qwen3.5-9b_20260913.json) | Alibaba | Together | 240 | 5.0% |
| [Seed 2.1 Turbo](probes/reference_202609/seed-2-1-turbo_20260911.json) | ByteDance | Seed | 400 | 6.0% |

The original 16 reference sets remain available under
[`probes/reference/`](probes/reference/). Choose the desired collection
explicitly with `--reference`; each set has its own probes and calibration.

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the
full text. Copyright 2026 The KBF Authors.
