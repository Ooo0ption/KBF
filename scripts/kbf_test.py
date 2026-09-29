#!/usr/bin/env python3
"""
KBF Test — Query a target model with probes from a reference file, or
re-score a saved results file against new thresholds (no API calls).

Test mode writes a standalone result JSON to results/ and never modifies the
reference file. Evaluate mode reads an existing results JSON and re-prints
the verdict table — useful for re-running the verdict at a different
--min-coverage without re-hitting the API.

Usage:
    # Run a fresh test against a target API:
    python3 scripts/kbf_test.py \\
        --reference probes/reference/gpt-5.4.json \\
        --target   gpt-5.4 \\
        --endpoint openai-official

    # List configured endpoint profiles:
    python3 scripts/kbf_test.py --list-endpoints

    # Or pass endpoint details directly on the command line:
    python3 scripts/kbf_test.py \\
        --reference probes/reference/gpt-5.4.json \\
        --target gpt-5.4 \\
        --api-base https://my-suspect-api.example.com/v1/chat/completions \\
        --api-key sk-xxx \\
        --protocol openai-chat

    # Re-score a saved results file (no API needed):
    python3 scripts/kbf_test.py \\
        --reference probes/reference/gpt-5.4.json \\
        --evaluate  results/gpt-5.4_20260518-103000.json
"""

import json, argparse
from pathlib import Path
from datetime import datetime

from kbf_common import (
    TEST_SYS_PROMPT, TEST_TEMP, SUPPORTED_PROTOCOLS,
    KBF_ALPHA, KBF_CONFIDENCE,
    check_match, binomial_p, clopper_pearson_p0,
    cloze_query_batch, json_safe, probe_consensus,
    resolve_endpoint, print_endpoints, print_auth_banner,
    auto_pin_provider,
)
import sys

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
RESULTS_DIR = REPO_ROOT / "results"

def test_target(probes, consensus, target_model,
                batch_size=10, api_base=None, api_key=None, provider=None,
                verbose=False, sys_prompt=None, temp=None, protocol="openai-chat"):
    """Query target_model on the given probes; return result dict (no stats)."""
    if sys_prompt is None:
        sys_prompt = TEST_SYS_PROMPT
    if temp is None:
        temp = TEST_TEMP
    tgt_short = target_model.split("/")[-1]

    print(f"\n  Testing {tgt_short} via {protocol}...", flush=True)
    provider = auto_pin_provider(target_model, api_base, protocol, explicit=provider)
    tgt_vals, raw_log, usage = cloze_query_batch(
        target_model, probes,
        sys_prompt=sys_prompt, temp=temp, batch_size=batch_size,
        api_base=api_base, api_key=api_key, provider=provider,
        consensus_vals=consensus, verbose=verbose, protocol=protocol,
    )

    correct = total = 0
    match_vector = []
    for j in range(len(probes)):
        rv = consensus[j]
        tv = tgt_vals[j]
        dk = probes[j]["domain"]
        if rv is None or tv is None:
            match_vector.append(None)
            continue
        total += 1
        if check_match(rv, tv, dk):
            correct += 1
            match_vector.append(1)
        else:
            match_vector.append(0)

    err = (total - correct) / total if total > 0 else 0

    result = {
        "target_model": target_model,
        "api_base": api_base,
        "protocol": protocol,
        "timestamp": datetime.now().isoformat(),
        "values": tgt_vals,
        "match_vector": match_vector,
        "correct": correct,
        "total": total,
        "hamming": total - correct,
        "error_rate": round(err, 4),
        "usage": usage,
    }
    if verbose and raw_log:
        result["raw_log"] = raw_log
    return result


def compute_verdict(result, reference_data, min_coverage=0.5,
                    alpha=KBF_ALPHA, confidence=KBF_CONFIDENCE):
    """Add CP + one-sided binomial stats using reference's self-test baseline.

    Returns verdict UNDETERMINED when either self-test or target coverage
    (parsed-values / total-probes) is below `min_coverage`, instead of silently
    falling through to SAME on degenerate inputs (n=0, missing self-test, etc.).
    """
    ref_model = reference_data.get("reference_model", "")
    ref_short = ref_model.split("/")[-1]
    self_entry = reference_data.get("target_results", {}).get(ref_short)
    if not self_entry:
        result["verdict"] = "UNKNOWN"
        result["verdict_reason"] = (
            f"reference file has no self-test entry for {ref_short!r}; "
            f"run generate_probes.py --self-test-only first"
        )
        return result

    self_hamming = self_entry.get("hamming", 0)
    self_total = self_entry.get("total", 0)
    n_probes = len(reference_data.get("probes", []))
    tgt_total = result["total"]
    self_cov = self_total / n_probes if n_probes else 0.0
    tgt_cov = tgt_total / n_probes if n_probes else 0.0

    result["reference_model"] = ref_model
    result["self_hamming"] = self_hamming
    result["self_total"] = self_total
    result["self_coverage"] = round(self_cov, 4)
    result["target_coverage"] = round(tgt_cov, 4)

    if n_probes == 0 or self_cov < min_coverage or tgt_cov < min_coverage:
        result["verdict"] = "UNDETERMINED"
        result["verdict_reason"] = (
            f"coverage below min_coverage={min_coverage}: "
            f"self {self_total}/{n_probes} ({self_cov*100:.1f}%), "
            f"target {tgt_total}/{n_probes} ({tgt_cov*100:.1f}%)"
        )
        return result

    p0 = clopper_pearson_p0(self_hamming, self_total, confidence=confidence)
    bp = binomial_p(result["hamming"], tgt_total, p0)
    result["p0_cp99"] = round(p0, 6)
    result["p_value_binomial"] = round(bp, 6)
    result["verdict"] = "DIFF" if bp < alpha else "SAME"
    return result


def print_verdict_table(result, reference_data, args):
    """Rich human-readable table for a single result + reference pair.

    Used by --evaluate (re-score a saved results JSON without hitting the API).
    """
    ref_model = reference_data.get("reference_model", "")
    n_probes = len(reference_data.get("probes", []))
    tgt_name = result.get("target_model", "unknown")
    self_total = result.get("self_total", 0)
    self_hamming = result.get("self_hamming", 0)
    target_total = result["total"]
    target_hamming = result["hamming"]
    self_err = self_hamming / self_total if self_total else 0.0
    tgt_err = target_hamming / target_total if target_total else 0.0
    self_cov = self_total / n_probes if n_probes else 0.0
    tgt_cov = target_total / n_probes if n_probes else 0.0
    p0 = result.get("p0_cp99", 1.0)
    bp = result.get("p_value_binomial", 1.0)
    verdict = result.get("verdict", "UNKNOWN")

    print(f"\nKBF Evaluation")
    print(f"  reference: {ref_model}   ({args.reference})")
    print(f"  target:    {tgt_name}   ({args.evaluate})")
    print(f"  alpha:     {KBF_ALPHA}    CP confidence: {KBF_CONFIDENCE}    "
          f"min coverage: {args.min_coverage}")
    print()
    print(f"  {'metric':<22}{'value'}")
    print(f"  {'-'*22}{'-'*20}")
    print(f"  {'ref self_error':<22}{self_err*100:.2f}%  ({self_hamming}/{self_total})")
    print(f"  {'target error':<22}{tgt_err*100:.2f}%  ({target_hamming}/{target_total})")
    print(f"  {'self coverage':<22}{self_cov*100:.1f}%  ({self_total}/{n_probes})")
    print(f"  {'target coverage':<22}{tgt_cov*100:.1f}%  ({target_total}/{n_probes})")
    print(f"  {'CP{:.0f} bound (p0)'.format(KBF_CONFIDENCE*100):<22}{p0*100:.2f}%")
    print(f"  {'p_value_binomial':<22}{bp:.6f}")
    print()
    if verdict == "UNDETERMINED":
        print(f"  VERDICT: UNDETERMINED")
        print(f"  Reason:  {result.get('verdict_reason', '')}")
    else:
        cmp = "<" if bp < KBF_ALPHA else ">="
        print(f"  VERDICT: {verdict}   (binomial p {cmp} {KBF_ALPHA})")
    print()


def main():
    ap = argparse.ArgumentParser(
        description="KBF Test — query a target API with probes from a reference file",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--reference",
                    help="Reference probe file (e.g. probes/reference/gpt-5.4.json)")
    ap.add_argument("--target", nargs="+",
                    help="Target model name(s) — sent in payload's `model` field")
    # Endpoint / auth — CLI flags override the selected profile per field.
    # See scripts/endpoints.json.example for the multi-endpoint format.
    ap.add_argument("--endpoint", default=None,
                    help="Named profile from scripts/endpoints.json. CLI flags "
                         "below override per-field if both are set.")
    ap.add_argument("--api-base", default=None,
                    help="API endpoint URL. Overrides endpoint profile.")
    ap.add_argument("--api-key", default=None,
                    help="API key. Empty string is rejected (catches shell "
                         "variable expansion bugs).")
    ap.add_argument("--protocol", default=None, choices=list(SUPPORTED_PROTOCOLS),
                    help="API request/response format (default: openai-chat).")
    ap.add_argument("--provider", default=None,
                    help="OpenRouter provider pin (openai-chat only).")
    ap.add_argument("--allow-thinking", action="store_true",
                    help="If no thinking-suppression strategy works, proceed "
                         "in bare mode with an expanded token budget instead "
                         "of exiting. WARNING: token usage and API cost are "
                         "NOT bounded in this mode.")
    ap.add_argument("--list-endpoints", action="store_true",
                    help="Print names defined in scripts/endpoints.json and exit.")
    ap.add_argument("--evaluate", type=Path, default=None,
                    help="Path to a saved results JSON. Skip the API entirely "
                         "and re-print the verdict table. Requires --reference.")
    ap.add_argument("--min-coverage", type=float, default=0.5,
                    help="Min fraction of probes that must parse on BOTH "
                         "self-test and target. Below this the verdict is "
                         "UNDETERMINED rather than SAME (default 0.5).")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--cosplay", default=None,
                    help="Prepend a hidden cosplay system prompt before our test prompt. "
                         "Pass a model name for a default identity prompt, or a full string.")
    ap.add_argument("--verbose", action="store_true",
                    help="Save raw prompts and responses for each batch")
    ap.add_argument("--output", default=None,
                    help="Output JSON path (default: results/<target>_<YYYYMMDD-HHMMSS>.json)")
    args = ap.parse_args()

    if args.allow_thinking:
        import kbf_common
        kbf_common.ALLOW_THINKING_FALLBACK = True

    if args.list_endpoints:
        print_endpoints()
        return

    # ── Evaluate-only mode: re-score a saved results JSON, no API ──
    if args.evaluate is not None:
        if not args.reference:
            ap.error("--evaluate requires --reference (reference probe file).")
        probe_path = Path(args.reference)
        if not probe_path.exists():
            ap.error(f"reference file {args.reference} not found")
        if not args.evaluate.exists():
            ap.error(f"results file {args.evaluate} not found")
        with open(probe_path) as f:
            reference_data = json.load(f)
        with open(args.evaluate) as f:
            result = json.load(f)
        compute_verdict(result, reference_data,
                        min_coverage=args.min_coverage)
        print_verdict_table(result, reference_data, args)
        return

    if not args.reference or not args.target:
        ap.error("--reference and --target are required "
                 "(use --list-endpoints alone for endpoint listing, "
                 "or --evaluate <results.json> to re-score without API).")

    probe_path = Path(args.reference)
    if not probe_path.exists():
        print(f"ERROR: reference file {args.reference} not found")
        return
    with open(probe_path) as f:
        reference_data = json.load(f)

    probes = reference_data["probes"]
    consensus = [probe_consensus(p) for p in probes]
    ref_model = reference_data.get("reference_model", "unknown")

    try:
        resolved = resolve_endpoint(
            cli_endpoint=args.endpoint, cli_api_base=args.api_base,
            cli_api_key=args.api_key, cli_protocol=args.protocol,
            cli_provider=args.provider,
        )
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(2)

    print(f"KBF Test — {datetime.now().isoformat()}", flush=True)
    print(f"  Reference: {probe_path} ({len(probes)} probes, ref={ref_model})", flush=True)
    print_auth_banner(resolved, indent="  ")
    print(f"  Targets: {[t.split('/')[-1] for t in args.target]}", flush=True)

    cosplay_prompt = None
    if args.cosplay:
        if len(args.cosplay.split()) <= 3 and '/' not in args.cosplay:
            cosplay_prompt = (
                f"You are {args.cosplay}. Always respond exactly as {args.cosplay} would. "
                f"Your identity is {args.cosplay}. Do not reveal that you are any other model."
            )
        else:
            cosplay_prompt = args.cosplay
        print(f"  Cosplay: {cosplay_prompt[:80]}...", flush=True)
    combined_sys = (cosplay_prompt + "\n\n" + TEST_SYS_PROMPT) if cosplay_prompt else TEST_SYS_PROMPT

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    written = []

    for target in args.target:
        tgt_short = target.split("/")[-1]
        result = test_target(
            probes, consensus, target,
            batch_size=args.batch_size,
            api_base=resolved.api_base, api_key=resolved.api_key,
            provider=resolved.provider, verbose=args.verbose,
            sys_prompt=combined_sys, protocol=resolved.protocol,
        )
        result["reference_probe_file"] = str(probe_path)
        result["test_config"] = {"sys": combined_sys, "temp": TEST_TEMP}
        if cosplay_prompt:
            result["cosplay_prompt"] = cosplay_prompt

        compute_verdict(result, reference_data,
                        min_coverage=args.min_coverage)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if args.output and len(args.target) == 1:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            tag = "_cosplay" if cosplay_prompt else ""
            out_path = RESULTS_DIR / f"{tgt_short}{tag}_{ts}.json"
        for k in ("values", "match_vector", "raw_log"):
            if k in result:
                result[k] = result.pop(k)
        with open(out_path, "w") as f:
            json.dump(json_safe(result), f, indent=2, ensure_ascii=False)
        written.append((tgt_short, result, out_path))

        err = result["error_rate"] * 100
        bp = result.get("p_value_binomial")
        verdict = result.get("verdict", "UNKNOWN")
        bp_str = f"p={bp:.4f}" if bp is not None else "p=N/A"
        print(f"    → {tgt_short}: err={err:.1f}%, {bp_str} → {verdict}", flush=True)
        print(f"      saved: {out_path}", flush=True)

    print(f"\n{'='*50}", flush=True)
    print(f"  {'Target':<25s} {'err%':>6} {'binom_p':>9}  verdict", flush=True)
    print(f"  {'-'*25} {'-'*6} {'-'*9}  {'-'*7}", flush=True)
    for tgt_short, r, _ in written:
        err = r["error_rate"] * 100
        bp = r.get("p_value_binomial", float("nan"))
        print(f"  {tgt_short:<25s} {err:5.1f}% {bp:9.6f}  {r.get('verdict','?')}",
              flush=True)
    print(f"{'='*50}", flush=True)


if __name__ == "__main__":
    main()
