# KBF — Knowledge Boundary Fingerprinting

A black-box auditing tool for LLM APIs. Given a probe set generated from a
reference model, KBF decides whether a target API is actually serving the
claimed model — by exploiting the fact that every LLM produces a unique
pattern of wrong answers at its knowledge boundary.

Two user-facing scripts under `scripts/`:

- `kbf_test.py` — query a target API with a probe set, OR re-score a saved
  results file with `--evaluate` (no API key needed in evaluate mode).
- `generate_probes.py` — build a new probe set for a model not already in
  `probes/reference/`.

16 ready-to-use reference probe sets ship under `probes/reference/`.

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
| `--contrast` | `qwen/qwen3.5-9b` | Contrast model for screening |
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
Clopper-Pearson 99 % upper bound for `p0`:

| field             | meaning |
|-------------------|---------|
| `ref self_error`  | how often the reference itself misses its own probes (baseline noise) |
| `target error`    | how often the target API misses the same probes |
| `CP99 bound p0`   | 99 % upper bound for the reference's true error rate |
| `p_value_binomial`| probability the target's error rate is consistent with `p0` |
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
  bounded. We ship several suppression strategies, but because different relays and vendors
  accept different fields, the built-in set is not guaranteed to work on every endpoint.
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
│   ├── reference/       ← 16 official probe sets (READ-ONLY)
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

The 16 reference probe sets bundled under `probes/reference/`. Prices are per
million tokens at the listed provider; `self-error` is the reference model's
own miss rate on its probes (the baseline noise floor `p0` is computed from).

| Tier | Model                  | Family    | Provider     | Input ($/M) | Output ($/M) | #Probes | Self-error |
|------|------------------------|-----------|--------------|------------:|-------------:|--------:|-----------:|
| T1   | Claude Opus 4.6        | Anthropic | Amazon Bedrock |       5.00 |        25.00 |     681 |       4.3% |
| T1   | Claude Sonnet 4.6      | Anthropic | Google         |       3.00 |        15.00 |     224 |       1.3% |
| T1   | GPT-5.4                | OpenAI    | OpenAI         |       2.50 |        10.00 |     317 |       1.6% |
| T1   | Gemini 3 Flash         | Google    | Google         |       0.50 |         2.50 |     315 |       2.2% |
| T1   | GLM-5                  | Z.AI      | Z.AI           |       0.72 |         2.20 |     415 |       4.1% |
| T1   | Qwen3.5-397B-A17B †    | Alibaba   | Alibaba        |       0.39 |         1.20 |     279 |       9.0% |
| T2   | DeepSeek-V3.2          | DeepSeek  | Google         |       0.26 |         0.42 |     364 |       3.3% |
| T2   | GPT-4.1-mini           | OpenAI    | OpenAI         |       0.40 |         1.60 |     134 |       6.0% |
| T2   | GLM-4.7                | Z.AI      | Z.AI           |       0.38 |         2.00 |     356 |       4.6% |
| T2   | Kimi-K2-0905           | Moonshot  | Moonshot AI    |       0.40 |         2.50 |     300 |       4.7% |
| T2   | Qwen3.5-27B            | Alibaba   | Alibaba        |       0.20 |         0.30 |     115 |       4.3% |
| T3   | GPT-4.1-nano           | OpenAI    | OpenAI         |       0.10 |         0.40 |     109 |       7.3% |
| T3   | LLaMA-4-Scout          | Meta      | Groq           |       0.08 |         0.30 |     146 |      11.7% |
| T3   | Qwen3.5-9B             | Alibaba   | Together       |       0.05 |         0.10 |     105 |       3.8% |
| T3   | GLM-4.7-Flash          | Z.AI      | DeepInfra      |       0.06 |         0.20 |     309 |      14.2% |
| T3   | Gemini 2.5 Flash Lite  | Google    | Google         |       0.10 |         0.40 |     210 |      13.8% |

Prices are sourced from OpenRouter as of March 2026 and may have drifted since.

† Regenerated in May 2026.

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the
full text. Copyright 2026 The KBF Authors.