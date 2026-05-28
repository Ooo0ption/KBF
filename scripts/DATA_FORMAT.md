# KBF Data Format Specification

KBF writes two kinds of JSON files:

| File              | Path                             | Produced by              |
|-------------------|----------------------------------|--------------------------|
| Reference probes  | `probes/reference/<model>.json`  | `generate_probes.py` (then `--self-test-only` populates the self-test entry; the 16 files shipped with this repo are already populated) |
| Target results    | `results/<target>_<ts>.json`     | `kbf_test.py` (one per `--target` per run) |

Both formats are described below.

---

## Reference probe file

### Required fields (what `kbf_test.py` reads)

`kbf_test.py` only depends on this minimal subset — additional metadata is
preserved verbatim but never interpreted:

```jsonc
{
  "reference_model": "openai/gpt-5.4",       // full model ID, e.g. "openai/gpt-5.4"
  "probes": [ /* Probe objects, see below */ ],
  "self_error": 0.0158,                      // populated by --self-test-only
  "target_results": {
    "gpt-5.4": { /* TargetResult, is_self=true */ }
  }
}
```

### Full schema

The 16 reference files shipped with this repo carry richer provenance
metadata (consensus templates, provider pins, FP control p₀, etc.) from
their generation pipeline. Newly generated files use a slimmer header.
Both layouts are valid; nothing else in the codebase reads the extra
fields, but they are preserved so a reader can audit *how* the probes
were produced:

```jsonc
{
  // ── Always present (required by kbf_test.py) ──
  "reference_model": "openai/gpt-5.4",
  "probes":          [ /* Probe objects */ ],
  "self_error":      0.0158,                 // added by --self-test-only
  "target_results":  { "gpt-5.4": { ... } }, // added by --self-test-only

  // ── Shipped reference files only (legacy provenance) ──
  "protocol":                "cloze_stage1",
  "timestamp":               "2026-03-16T...",
  "contrast_model":          "qwen/qwen3.5-9b",
  "n_original_probes":       400,
  "n_config_invariant":      370,
  "n_final_probes":          317,
  "consensus_configs":       [ ... ],        // configs used during generation
  "cloze_templates":         { ... },        // per-domain templates
  "user_prompt_template":    "TASK: ...",
  "domain_tolerances":       { ... },
  "provider_pinning":        { ... },
  "batch_size":              10,
  "adaptive_p0":             0.0658,
  "config_invariant_indices":[ ... ],
  "config_variant_indices":  [ ... ],

  // ── Newly generated files only (alternative provenance) ──
  "total_probes":          317,
  "generation_mode":       "adaptive_frontier",
  "verification_mode":     "cloze_3config_consensus",
  "numbering_format":      "(N)",
  "min_probes_target":     300,
  "contrast_screening":    { ... },
  "provider_map":          { ... },
  "domains":               { ... },
  "frontier_stats":        { ... }
}
```

### Probe object

Each probe is a fill-in-the-blank question targeting one factual knowledge point.
Probes here have passed strict 3-config consensus (all 3 of
`CONSENSUS_CONFIGS` in `kbf_common.py` agree within domain tolerance) and
contrastive screening (the contrast model returned a *different* answer).

```jsonc
{
  "name": "tantalum carbide",      // entity name (inserted into the domain template)
  "domain": "chemistry_mp",        // domain key — see DOMAIN_CLOZE in kbf_common.py

  // The canonical answer. The 16 shipped reference files use the legacy field
  // name `cloze_consensus` (paired with `original_value` for the raw
  // generation-time value). Probe files newly produced by generate_probes.py
  // use a single `value` field instead. Code uses `probe_consensus(p)` in
  // kbf_common.py which transparently prefers cloze_consensus and falls back
  // to value, so both layouts work side-by-side.
  "cloze_consensus": 3880.0,       // (shipped files) — agreed-upon answer
  "original_value":  3880.0,       // (shipped files) — value at first generation pass
  // OR
  "value":           3880.0,       // (newly generated) — single canonical answer

  "consensus_raw": {               // raw values from each consensus pass
    "t0":    3880.0,               //   FIXED_SYS_PROMPT, temp=0.0
    "t07_a": 3880.0,               //   FIXED_SYS_PROMPT, temp=0.7 (pass A)
    "t07_b": 3880.0                //   FIXED_SYS_PROMPT, temp=0.7 (pass B)
  },
  "contrast_value": 3950.0,        // contrast model's answer (null if not yet probed)
  "contrast_agrees": false,        // (newly generated only) true iff contrast within
                                   //   tolerance of the canonical answer
  "difficulty_tier": 2             // (newly generated only) 0-based adaptive-frontier tier
}
```

### TargetResult object

In a reference file, `target_results[ref_short]` is the **self-test** entry
(`is_self: true`) used as the baseline for SAME/DIFF verdicts. The fields
are the same as the standalone result file below, minus the per-run
`reference_probe_file` / `api_base` / `protocol` / `cosplay_prompt` fields.

---

## Target result file (`results/<target>_<ts>.json`)

Written by `kbf_test.py` per `--target`. The reference probe file is opened
**read-only** — only `--self-test-only` ever writes back into a reference file.

```jsonc
{
  // ── Identity ──
  "target_model":         "claude-sonnet-4-6",        // sent as the `model` field
  "reference_model":      "anthropic/claude-sonnet-4.6",
  "reference_probe_file": "probes/reference/claude-sonnet-4.6.json",
  "timestamp":            "2026-05-18T05:45:08...",
  "api_base":             "https://api.anthropic.com/v1/messages",
  "protocol":             "anthropic-messages",
  "test_config":          { "sys": "...", "temp": 0.0 },
  "cosplay_prompt":       null,                       // present iff --cosplay used

  // ── Per-probe data ──
  "values":       [3880.0, 1970.0, null, ...],        // values[i] aligns with probes[i]
  "match_vector": [1, 0, null, ...],                  // 1 / 0 / null

  // ── Aggregate stats ──
  "correct":    214,
  "total":      224,            // non-null entries in match_vector
  "hamming":    10,             // total - correct
  "error_rate": 0.0446,
  "usage":      { "prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ... },

  // ── Verdict (added by compute_verdict) ──
  "self_hamming":     3,
  "self_total":       224,
  "self_coverage":    1.0,      // self_total / len(probes)
  "target_coverage":  1.0,      // total / len(probes)
  "p0_cp99":          0.0634,   // CP99 upper bound on the reference's true self-error
  "p_value_binomial": 0.5306,   // one-sided P(X >= hamming | Bin(total, p0_cp99))
  "verdict":          "SAME",   // SAME / DIFF / UNDETERMINED / UNKNOWN
  "verdict_reason":   "..."     // present only for UNDETERMINED / UNKNOWN
}
```

### Verdict values

| value          | meaning |
|----------------|---------|
| `SAME`         | `p_value_binomial >= alpha` (default α=0.05) — null hypothesis (same model) not rejected. |
| `DIFF`         | `p_value_binomial < alpha` — reject null; target differs from reference. |
| `UNDETERMINED` | Self-test or target coverage below `--min-coverage` (default 0.5). Not enough data to decide. `p0_cp99` and `p_value_binomial` are not computed. |
| `UNKNOWN`      | Reference file has no self-test entry. Run `generate_probes.py --self-test-only <ref>` first. |

### Self-test (preprocessing)

The self-test is a fresh query of the reference model **against its own
probes**, run after generation completes (`generate_probes.py` does this by
default; `--no-self-test` skips and `--self-test-only` populates it later).
It uses the standard online-test config — `FIXED_SYS_PROMPT` and `temp=0` —
NOT the 3-config consensus configs used during probe generation. The
resulting hamming/total feed the CP99 upper bound `p0` against which every
target is compared.

Self-error is stored both as the top-level `self_error` for quick access and
inside `target_results[ref_short]` as a full TargetResult with
`is_self: true`.

### Verbose mode (`--verbose`)

When `kbf_test.py` is run with `--verbose`, each TargetResult includes
a `raw_log` field containing the raw prompts and responses for debugging:

```jsonc
{
  // ... normal fields ...
  "raw_log": [                              // one entry per batch
    {
      "batch_num": 1,
      "domain": "chemistry_mp",
      "probe_indices": [0, 1, 2, ...],      // indices into probes array
      "probe_names": ["tantalum carbide", "gadolinium oxide", ...],
      "prompt": "TASK: Fill in each blank...\n(1) The melting point of ...",
      "response": "(1) The melting point of tantalum carbide is 3880°C.\n...",
      "parsed": [3880.0, 2420.0, ...],      // extracted values
      "attempts": [                         // only if retries happened
        {"retry": 0, "response": "...", "parsed": [...], "valid": 3, "usage": {...}},
        {"retry": 1, "response": "...", "parsed": [...], "valid": 8, "usage": {...}}
      ]
    },
    // ... more batches ...
  ]
}
```

Without `--verbose`, `raw_log` is not saved. The parsed `values` and
`match_vector` are always saved regardless of verbose mode.

### Interpretation

- **Self-test** (`is_self: true`): The reference model re-queried with a fresh config.
  Self-error should be low (typically 1-7%). This establishes the baseline noise.

- **Same model**: `significant_binomial = false` AND low error rate (~= self_error).
- **Different model**: `significant_binomial = true`. Error rate >> self_error.


## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────┐
│  PREPROCESSING (offline, one-time per reference model)  │
│                                                         │
│  1. generate_probes.py                                  │
│     → Generate cloze probes                             │
│     → Build 3-config consensus                          │
│     → Contrastive screening                             │
│     → Self-test (4th query, establishes baseline)       │
│     → Output: probe file with consensus + self-error    │
│                                                         │
│  Cost: ~$1-5 per model (consensus + screening + self)   │
│  Frequency: once per model version                      │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  ONLINE TESTING (per audit target)                      │
│                                                         │
│  2. kbf_test.py                                         │
│     → Load probe file (cached consensus + self-error)   │
│     → Query target API with cloze probes                │
│     → Compare against consensus                         │
│     → One-sided binomial test vs CP99(self_error)       │
│     → Output: verdict (SAME / DIFF / UNDETERMINED)      │
│                                                         │
│  Cost: ~$0.01-0.05 per target                           │
│  Frequency: per target API                              │
└─────────────────────────────────────────────────────────┘
```


## Domain Configuration

Each probe belongs to a domain that determines the cloze template, valid range,
and match tolerance.

### Templates

| Domain | Template | Example |
|--------|----------|---------|
| `chemistry_bp` | The boiling point of {name} at 1 atm is ___°C. | hexamethyldisilazane → 125°C |
| `chemistry_mp` | The melting point of {name} is ___°C. | tantalum carbide → 3880°C |
| `physics` | The numerical value of {name} in SI units is ___. | thermal conductivity of... |
| `astronomy` | The numerical value of {name} is ___. | orbital period of... |
| `biology` | The diploid chromosome number (2n) of {name} is ___. | domestic cat → 38 |
| `math` | The numerical value of {name} is ___. | Euler totient φ(999999) |
| `programming` | {name} occurred in the year ___. | first release of Python |
| `crypto_params` | The numerical value of {name} is ___. | CRYSTALS-Kyber parameter |
| `medical` | The elimination half-life of {name} is ___ hours. | rivaroxaban → 7 |
| `pop_culture` | The numerical value of {name} is ___. | box office, chart position |
| `internet_culture` | The numerical value of {name} is ___. | year meme originated |
| `chinese_history` | {name}发生在公元___年。 | historical events |
| `chinese_geography` | {name}的数值是___。 | river length, lake area |
| `chinese_internet` | {name}发生在___年。 | internet events |
| `chinese_literature` | {name}的数值是___。 | publication years |

### Valid ranges

Each domain has a `(min, max)` range. Values outside this range are treated as
parse errors (set to `null`). This prevents the parser from picking up spurious
numbers (e.g., years from prose text when expecting a temperature).

### Match tolerances

| Domain | Tolerance | Mode |
|--------|-----------|------|
| `chemistry_bp` | ±3 | absolute |
| `chemistry_mp` | ±5 | absolute |
| `physics` | ±5% | relative |
| `astronomy` | ±5% | relative |
| `biology` | exact | absolute (0) |
| `math` | exact | absolute (0) |
| `programming` | exact | absolute (0) |
| `crypto_params` | exact | absolute (0) |
| `medical` | ±10% | relative |
| `pop_culture` | exact | absolute (0) |
| `internet_culture` | exact | absolute (0) |
| `chinese_*` | exact or ±5% | varies |


## Parser: Position-Aware `extract_nums`

The parser converts a model's text response into a list of numeric values,
one per probe. This is the most critical component — misalignment here
corrupts all downstream results.

### The problem

We send N probes in a single batch, numbered `(1)` through `(N)`:

```
(1) The melting point of tantalum carbide is ___°C.
(2) The boiling point of diethyl ether at 1 atm is ___°C.
(3) The diploid chromosome number (2n) of domestic cat is ___.
```

The model responds with numbered lines. **But models sometimes skip probes**
they can't answer, causing all subsequent values to shift:

```
(2) The boiling point of diethyl ether at 1 atm is 34.6°C.
(3) The diploid chromosome number (2n) of domestic cat is 38.
```

Here probe (1) was skipped. A naive sequential parser would assign 34.6 to
probe 1 and 38 to probe 2 — **both wrong**.

### The solution: position-aware parsing

The parser extracts the `(N)` prefix from each response line and maps the
value to the correct probe slot:

```python
# Strategy 1: Parse by (N) prefix
for line in response:
    match = re.match(r'^\((\d+)\)\s*', line)  # or r'^(\d+)[.)]\s+'
    if match:
        idx = int(match.group(1)) - 1  # 0-based index
        value = extract_last_number(line)
        results[idx] = value           # map to correct slot

# Skipped probes → None (not shifted)
return [results.get(i) for i in range(n)]
```

### Fallback: sequential parsing

If no `(N)` prefixes are found (rare), the parser falls back to sequential
line-by-line mapping. This is less robust but handles edge cases where models
output plain text without numbering.

### Number extraction rules

For each response line:
1. Strip the `(N)` prefix
2. Replace unicode minus `−` and en-dash `–` with ASCII `-`
3. Remove commas from numbers
4. Find ALL numbers matching `-?\d+\.?\d*([eE][+-]?\d+)?`
5. Take the **last** number on the line (the answer, not units/context)
6. Check against domain valid range — out-of-range → `null`

### Why "last number"?

Models often include context before the answer:

```
(1) The melting point of tantalum carbide (TaC, density 14.3 g/cm³) is 3880°C.
```

Numbers on this line: `14.3`, `3880`. We want `3880` (the answer), not `14.3`
(context). Taking the last number handles this correctly.

### Edge cases handled

| Case | Parser behavior |
|------|----------------|
| Model skips a probe | `None` at that index (not shifted) |
| Model reorders probes | Mapped by (N) prefix correctly |
| Model adds extra text lines | Ignored (no (N) prefix) |
| Model outputs `**3880**` (bold) | Numbers extracted through markdown |
| Model outputs `3,880` (comma) | Comma stripped, parsed as 3880 |
| Model uses `−` (unicode minus) | Replaced with ASCII `-` |
| Value out of domain range | Set to `null` |
| Model returns empty/error | All `null` |


## Batch Validation & Auto-Retry

After parsing each batch, the pipeline runs `validate_batch()` to detect
parsing failures before they corrupt results. On failure, the batch is
re-queried (up to 2 retries). If all retries fail, the attempt with the
most non-None values is used and a warning is logged.

### Checks

| Check | Trigger | What it catches |
|-------|---------|-----------------|
| **Too many Nones** | >50% of batch values are None | Model formatted response as prose, table, or other non-cloze format |
| **Off-by-one shift** | More values match next probe's consensus than their own | Model skipped a probe, sequential parser would have shifted everything |

### How shift detection works

For each batch, compare each parsed value against two consensus values:
- `own_cons[j]`: the consensus for probe j (where the value SHOULD go)
- `next_cons[j]`: the consensus for probe j+1 (where the value WOULD go if shifted)

If `shift_match > own_match` and `shift_match >= 3`, the batch is flagged.
This catches the case where the position-aware parser's (N) prefix matching
failed (e.g., model didn't use numbered prefixes) and the sequential fallback
produced shifted results.

### Retry strategy

On validation failure:
1. Re-query the same batch (model responses are somewhat non-deterministic)
2. Parse again with the position-aware parser
3. If still failing after max_retries, use the best attempt (most valid values)
4. Log a warning so the user can investigate manually

This is a **safety net**, not a substitute for the position-aware parser.
In practice, the parser handles >99% of responses correctly; the validator
catches the remaining edge cases.


## Statistical Tests

### Binomial test (primary)

Tests whether the target's error rate is significantly higher than expected
under the null hypothesis (same model, different config).

- **H0**: Target error rate ≤ p₀ = Clopper-Pearson 99% upper bound on the
  reference's true self-error rate (computed from `self_hamming / self_total`)
- **H1**: Target error rate > p₀
- **Test**: `P(X ≥ hamming | Bin(total, p₀))`
- **Threshold**: `p < KBF_ALPHA` (default 0.05)

Using the Clopper-Pearson upper bound rather than a fixed margin
automatically widens the tolerance band when the self-test has fewer probes
(higher uncertainty about the true self-error rate), and tightens it when
the self-test has many probes.

### Decision

| `p_value_binomial` | `verdict` |
|---|---|
| `< KBF_ALPHA`  | `DIFF` — reject H0; target differs from reference |
| `>= KBF_ALPHA` | `SAME` — fail to reject H0 |

When self-test or target coverage is below `--min-coverage` (default 0.5),
the verdict is instead `UNDETERMINED` and the p-value is not computed.


## Provider Pinning

Different API providers serve different quantizations of the same model.
Provider pinning ensures we always query the same provider for a given model,
eliminating quantization-induced false positives.

```python
PROVIDER_MAP = {
    "deepseek/deepseek-v3.2": "Google",       # bf16 (full precision)
    "z-ai/glm-5": "Z.AI",                     # first-party
    "z-ai/glm-4.7": "Z.AI",                   # first-party
    "openai/gpt-5.4": "OpenAI",               # first-party
    "anthropic/claude-sonnet-4.6": "Google",   # full precision via Google
    # ... etc
}
```

**Heuristic**: first-party > bf16 > fp8 >> never fp4.

When testing against a non-OpenRouter API (e.g., a reseller), provider pinning
is not used — the whole point is to test what that API actually serves.
