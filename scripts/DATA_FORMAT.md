# KBF data formats

KBF reads reference probe JSON files and writes target result JSON files.
Generation writes to `probes/generated/`; target tests write to `results/`.
The bundled collections are in `probes/reference/` and
`probes/reference_202609/`.

## Reference probes

The fields needed for testing and calibration are:

| Field | Meaning |
|---|---|
| `reference_model` | Reference model identifier |
| `probes` | Ordered array of probe objects |
| `target_results[ref_short]` | Self-test result, keyed by the model identifier after its final `/` |
| `target_results[ref_short].hamming` | Self-test mismatch count |
| `target_results[ref_short].total` | Valid self-test answer count |
| `self_error` | Convenience copy of the self-test error rate |

The scorer derives its baseline from the self-test counts, not `self_error`.
Use `len(probes)` for the current probe count; generation-stage counters may
precede filtering.

Each probe contains:

| Field | Meaning |
|---|---|
| `name` | Entity or fact inserted into the domain's question template |
| `domain` | Domain key defined in `domains.py` |
| `cloze_consensus` or `value` | Reference answer; `cloze_consensus` takes precedence when present |
| `consensus_raw` | Optional answers from individual consensus passes |
| `original_value` | Optional initial generation answer |
| `contrast_value` | Optional contrast-model answer |
| `contrast_agrees` | Optional contrast agreement flag |
| `difficulty_tier` | Optional generation difficulty tier |

The current generator stores the temperature-zero answer as `value` after
three-pass consensus verification. Relative domains require answers within
2% of their mean; absolute domains require equal integers after rounding halves
away from zero. Bundled files may contain derived consensus labels; the scorer
uses the stored label as supplied.

Additional metadata records generation settings, endpoint and provider choices,
and optional layout filtering. `layout_filter` records its pass count, batch
size, and numbers of dropped and retained probes. Preserve this metadata when
sharing a reference set so its calibration conditions remain available.

Generation runs a self-test by default. `--no-self-test` skips it;
`--self-test-only <file>` updates the calibration in an existing file.
Target testing opens the reference file read-only.

## Target results

`kbf_test.py` writes one result per target:

| Field | Meaning |
|---|---|
| `target_model`, `reference_model` | Target and reference identifiers |
| `reference_probe_file` | Reference file used for this test |
| `timestamp`, `api_base`, `protocol` | Run time and endpoint |
| `test_config` | System prompt and temperature |
| `values` | Parsed answers, aligned with `probes` |
| `match_vector` | Per-probe `1` (match), `0` (mismatch), or `null` |
| `correct`, `total`, `hamming` | Matches, valid answers, and mismatches |
| `error_rate` | `hamming / total`, or zero when there are no valid answers |
| `usage` | Reported token usage |
| `self_hamming`, `self_total` | Reference calibration counts |
| `self_coverage`, `target_coverage` | Valid answers divided by probe count |
| `p0_cp99` | Clopper–Pearson 99% upper bound for the self-test error rate |
| `p_value_binomial` | One-sided binomial tail probability for the target's mismatch count |
| `verdict` | `SAME`, `DIFF`, `UNDETERMINED`, or `UNKNOWN` |
| `verdict_reason` | Explanation when calibration is missing or coverage is insufficient |

Statistical fields are added only when calibration and coverage allow the test.
`cosplay_prompt` is included when `--cosplay` is used. `--verbose` adds `raw_log`
with batch prompts, responses, parsed values, and retry details.

Missing, unparseable, and out-of-range answers are `null` and excluded from
scoring. `total` counts non-null matches, `correct` counts ones, and `hamming`
counts zeros. Probe order must remain unchanged when evaluating saved results.

## Scoring

Question templates, valid ranges, and tolerances are defined in `domains.py`.
Absolute-domain values are rounded to integers, with halves away from zero,
before applying the domain tolerance. Relative domains use their configured
relative tolerance.

The reference's valid-answer count is the denominator for its confidence bound;
the target's valid-answer count is used for the binomial test.

| Verdict | Condition |
|---|---|
| `SAME` | Binomial p-value is at least `0.05` |
| `DIFF` | Binomial p-value is below `0.05` |
| `UNDETERMINED` | Reference or target coverage is below `--min-coverage` (default `0.5`), or the set is empty |
| `UNKNOWN` | The reference lacks its self-test entry |

`--evaluate` recomputes the verdict from saved aggregate counts and reference
calibration without API calls. It does not re-parse responses or recompute
`match_vector` from `values`.
