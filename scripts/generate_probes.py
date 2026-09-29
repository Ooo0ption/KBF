#!/usr/bin/env python3
"""
Probe Generation for Knowledge Boundary Fingerprinting (KBF)
=============================================================
Generates factual cloze probes for a reference model using adaptive frontier
search, with cloze-format verification (3-config consensus).

Usage:
    python3 generate_probes.py --reference "google/gemini-2.5-flash-lite" --min-probes 100
    python3 generate_probes.py --reference "openai/gpt-5.4" --max-rounds 6
"""

import json, time, re, sys, argparse, random
from collections import defaultdict
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
GENERATED_DIR = REPO_ROOT / "probes" / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

# Import shared components from kbf_common
sys.path.insert(0, str(SCRIPT_DIR))
from kbf_common import (
    PROVIDER_MAP, FIXED_SYS_PROMPT, CONSENSUS_CONFIGS, SUPPORTED_PROTOCOLS,
    ResolvedEndpoint, resolve_endpoint, print_endpoints, print_auth_banner,
    query_api, cloze_query_batch, json_safe, probe_consensus,
    auto_pin_provider, check_match, _round_half_up,
)
from domains import DOMAINS, DIFFICULTY_TIERS


# ── API Layer (delegates to kbf_common) ──

def query_model(resolved: ResolvedEndpoint, model, user_prompt,
                sys_prompt="", temp=0.0, max_tokens=2000):
    """Thin wrapper around kbf_common.query_api for probe generation."""
    provider = auto_pin_provider(model, resolved.api_base, resolved.protocol)
    text, _ = query_api(model, sys_prompt, user_prompt, temp=temp,
                        max_tokens=max_tokens, provider=provider,
                        api_base=resolved.api_base, api_key=resolved.api_key,
                        protocol=resolved.protocol)
    return text


# ── Parsing ──────────────────────────────────────────────────────────

def parse_generation(text):
    """Parse 'name | value' lines from generation response."""
    items = []
    if not text: return items
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('---'):
            continue
        line = re.sub(r'^\d+[\.)]\s*', '', line).strip()
        line = re.sub(r'^[-*]\s*', '', line).strip()
        line = line.replace('**', '')
        if '|' in line:
            parts = line.split('|')
            if len(parts) >= 2:
                name = parts[0].strip().strip('*').strip()
                val_str = parts[-1].strip()
                val_str = re.sub(r'[°℃℉KFCkmsnmμ²³]', '', val_str).strip()
                val_str = val_str.replace('~', '').replace('≈', '').strip()
                val_str = val_str.replace('\u2212', '-').replace('\u2013', '-').replace(',', '')
                nums = re.findall(r'-?\d+\.?\d*', val_str)
                if nums and name and len(name) > 2:
                    try:
                        # Use the LAST number on the value side, matching
                        # extract_nums in kbf_common.py. A "value side" like
                        # "1.5 to 2.0" or "3 (rev 2)" otherwise causes the
                        # generation pipeline and the verification pipeline
                        # to disagree on which number is the answer.
                        items.append({"name": name.lower(), "value": float(nums[-1])})
                    except ValueError:
                        pass
    return items


# Consensus is stricter than the test: a probe whose three answers only just
# agree under the test's own tolerance is one the fourth query is likely to
# miss. Relative-tolerance domains require the three answers to sit inside a
# fixed band around their mean, tighter than every relative domain's test
# tolerance (5% / 10%); absolute-tolerance domains require the three to be the
# same integer, which is how kbf_common.check_match compares them.
CONSENSUS_REL_BAND = 0.02

# Fixed so a --layout-filter run is reproducible.
LAYOUT_FILTER_SEED = 9000


def consensus_ok(answers, dcfg):
    """Do these three answers count as one answer?

    Admission only — the label is chosen separately (and is always the t0
    answer verbatim). Absolute domains hold integer quantities (years, counts,
    key sizes) and are judged as integers; relative domains hold continuous
    ones and are judged by spread.
    """
    vals = [a for a in answers if a is not None]
    if len(vals) != len(answers) or not vals:
        return False
    if dcfg.get("tolerance_mode", "absolute") == "relative":
        mean_val = sum(vals) / len(vals)
        if mean_val == 0:
            return False
        return max(abs(a - mean_val) / abs(mean_val) for a in vals) <= CONSENSUS_REL_BAND
    return len({_round_half_up(a) for a in vals}) == 1


# ── Cloze Verification ──────────────────────────────────────────────

def cloze_verify_batch(resolved: ResolvedEndpoint, model, items, dcfg,
                       sys_prompt="", temp=0.0, batch_size=10):
    """Verify items using cloze format. Returns list of extracted values.

    Thin wrapper around kbf_common.cloze_query_batch so the generation
    pipeline gets the same validate-and-retry protection self-test uses.
    ``dcfg`` is accepted for signature compatibility but ignored — the
    underlying batcher looks up the template/range from the items'
    ``domain`` field via DOMAIN_CLOZE / DOMAIN_RANGES. Shift detection is
    skipped (no canonical consensus exists at generation time)."""
    provider = auto_pin_provider(model, resolved.api_base, resolved.protocol)
    vals, _, _usage = cloze_query_batch(
        model, items,
        sys_prompt=sys_prompt, temp=temp, batch_size=batch_size,
        api_base=resolved.api_base, api_key=resolved.api_key,
        protocol=resolved.protocol, provider=provider,
        consensus_vals=None, quiet=True,
    )
    return vals


# ── Probe Generation (Adaptive Frontier) ─────────────────────────────

def split_quota(max_probes, domain_keys):
    """Split a total probe cap into per-domain quotas.

    Spreads the remainder over the first domains so the quotas sum to exactly
    `max_probes` (e.g. 400 over 15 domains -> ten 27s and five 26s). Returns
    {domain_key: quota}; an empty dict when no cap is set.
    """
    if not max_probes or not domain_keys:
        return {}
    n = len(domain_keys)
    base, extra = divmod(max_probes, n)
    return {dk: base + (1 if i < extra else 0)
            for i, dk in enumerate(domain_keys)}


def generate_domain(resolved: ResolvedEndpoint, model, dk, dcfg, max_rounds,
                    min_per_domain=5, batch_size=10, contrast_model=None,
                    quota=None, contrast_resolved=None):
    """Generate and verify probes for one domain.

    `quota` is a per-domain floor, not a cap: the round loop stops before the
    next round once the domain holds at least `quota` verified probes, and
    everything the crossing round produced is kept. A domain that runs out of
    rounds first simply keeps what it has.
    """
    print(f"\n  ── {dcfg['name']} ──", flush=True)
    seen = set()
    verified = []
    consecutive_zero = 0
    round_history = []

    for round_idx in range(max_rounds):
        if consecutive_zero >= 2 and len(verified) >= min_per_domain:
            print(f"    >> Frontier reached (2 consecutive zero rounds)", flush=True)
            break

        theme = dcfg["themes"][round_idx % len(dcfg["themes"])]
        tier_idx = min(round_idx, len(DIFFICULTY_TIERS) - 1)
        difficulty, difficulty_extra = DIFFICULTY_TIERS[tier_idx]

        exclude = ""
        if seen:
            exclude_list = ", ".join(sorted(seen)[:50])
            exclude = f"\n\nDo NOT include any of these (already used): {exclude_list}"

        base_prompt = dcfg["gen_prompt"].format(theme=theme)
        prompt = base_prompt.replace("obscure ", f"{difficulty} ")
        prompt = difficulty_extra + prompt + exclude

        resp = query_model(resolved, model, prompt, max_tokens=3000)
        items = parse_generation(resp)

        new_items = []
        lo, hi = dcfg["value_range"]
        for item in items:
            if item["name"] not in seen and lo <= item["value"] <= hi:
                seen.add(item["name"])
                item["domain"] = dk
                item["difficulty_tier"] = tier_idx
                new_items.append(item)

        label = f"    R{round_idx+1} T{tier_idx} ({theme[:35]}...) {len(items)} parsed, {len(new_items)} new"
        if not new_items:
            consecutive_zero += 1
            print(f"{label} -> skip (zero x{consecutive_zero})", flush=True)
            continue

        # ── Step A: First config (t0) — this is the reference answer ──
        cfg0 = CONSENSUS_CONFIGS[0]  # t0
        shuffled = list(new_items)
        random.shuffle(shuffled)
        vals0 = cloze_verify_batch(resolved, model, shuffled, dcfg,
                                   sys_prompt=cfg0["sys"], temp=cfg0["temp"],
                                   batch_size=batch_size)
        t0_map = {}  # name -> value
        for j, item in enumerate(shuffled):
            t0_map[item["name"]] = vals0[j]

        # Update items with t0 value and filter out Nones
        items_with_t0 = []
        for item in new_items:
            v = t0_map.get(item["name"])
            if v is not None:
                item["value"] = v  # use t0 as the initial value
                items_with_t0.append(item)

        # ── Step B: Contrastive screening BEFORE remaining consensus ──
        # Screen early to avoid wasting API calls on non-discriminative probes
        if contrast_model and items_with_t0:
            contrast_vals = cloze_verify_batch(contrast_resolved or resolved,
                                               contrast_model, items_with_t0, dcfg,
                                               sys_prompt="", temp=0.0,
                                               batch_size=batch_size)
            screened_items = []
            n_no_answer = 0
            for j, item in enumerate(items_with_t0):
                cv = contrast_vals[j]
                item["contrast_value"] = cv
                if cv is None:
                    # contrast model produced no parseable answer — we can't
                    # tell if the probe is discriminative. Drop it rather than
                    # bias the kept set toward parser failures.
                    n_no_answer += 1
                    continue
                if check_match(item["value"], cv, dk):
                    item["contrast_agrees"] = True
                    # contrast agrees → not discriminative, skip
                    continue
                item["contrast_agrees"] = False
                screened_items.append(item)
            n_screened_out = len(items_with_t0) - len(screened_items)
            if n_screened_out > 0:
                no_answer_note = f", {n_no_answer} no-answer" if n_no_answer else ""
                print(f" [screen: {len(screened_items)}/{len(items_with_t0)} survive"
                      f"{no_answer_note}]",
                      end="", flush=True)
            items_with_t0 = screened_items

        # ── Step C: Remaining 2 consensus configs (only on survivors) ──
        verify_raw = {}  # name -> {config_name: value}
        for item in items_with_t0:
            verify_raw[item["name"]] = {cfg0["name"]: t0_map[item["name"]]}

        for cfg in CONSENSUS_CONFIGS[1:]:  # t07_a, t07_b
            if not items_with_t0:
                break
            shuffled = list(items_with_t0)
            random.shuffle(shuffled)
            vals = cloze_verify_batch(resolved, model, shuffled, dcfg,
                                      sys_prompt=cfg["sys"], temp=cfg["temp"],
                                      batch_size=batch_size)
            for j, item in enumerate(shuffled):
                verify_raw[item["name"]][cfg["name"]] = vals[j]

        # ── Step D: Consensus — ALL 3 configs must agree with the t0 answer ──
        # The label IS the t0 answer, stored verbatim. t0 is the config the
        # self-test and kbf_test.py replicate (temp=0, FIXED_SYS_PROMPT), so
        # anchoring there keeps every label a value the model actually
        # produced — never a mean of three answers or a rounded stand-in.
        round_verified = []
        cfg0_name = CONSENSUS_CONFIGS[0]["name"]
        for item in items_with_t0:
            raw = verify_raw.get(item["name"], {})
            answers = [raw.get(cfg["name"]) for cfg in CONSENSUS_CONFIGS]
            if any(a is None for a in answers):
                continue

            label = raw.get(cfg0_name)
            if not consensus_ok(answers, dcfg):
                continue
            # the verified answer must still match what generation listed
            if not check_match(item["value"], label, dk):
                continue
            item["value"] = label
            item["consensus_raw"] = raw
            round_verified.append(item)

        n_v = len(round_verified)
        vrate = n_v / len(new_items) if new_items else 0
        verified.extend(round_verified)
        round_history.append({
            "round": round_idx + 1, "tier": tier_idx,
            "generated": len(new_items), "verified": n_v,
            "rate": round(vrate, 2),
        })

        if n_v == 0:
            consecutive_zero += 1
        else:
            consecutive_zero = 0

        print(f"{label} -> {n_v} verified ({vrate:.0%}) (total: {len(verified)})", flush=True)

        if quota and len(verified) >= quota:
            print(f"    >> Quota reached ({len(verified)}/{quota}), stopping", flush=True)
            break

        time.sleep(0.3)

    tier_counts = {}
    for p in verified:
        t = p.get("difficulty_tier", 0)
        tier_counts[t] = tier_counts.get(t, 0) + 1
    tier_str = " ".join(f"T{t}={c}" for t, c in sorted(tier_counts.items()))
    print(f"    {dcfg['name']}: {len(verified)} verified [{tier_str}]", flush=True)

    stats = {
        "total_verified": len(verified),
        "rounds_used": len(round_history),
        "tier_distribution": tier_counts,
        "round_history": round_history,
        "quota": quota,
        "quota_met": (len(verified) >= quota) if quota else None,
    }
    return verified, stats


# ── Self-test helper ─────────────────────────────────────────────────

def layout_filter(resolved: ResolvedEndpoint, probe_path: Path, ref: str,
                  passes: int = 1, batch_size: int = 10) -> int:
    """Drop probes whose answer depends on which other probes share their prompt.

    Consensus asks a candidate alongside its own round's candidates — same theme,
    same difficulty tier. kbf_test.py asks it alongside its file-order neighbours,
    which mostly come from other rounds. A probe that agrees under the first
    layout but not the second inflates `self_error`, and therefore `p0` and the
    DIFF threshold, without carrying any fingerprint signal.

    So ask everything once more at temp=0 under a fresh domain-wide shuffle and
    delete whatever moves. Costs one extra pass over the finished set (~30
    prompts for 300 probes) per `passes`. Rewrites the probe file in place and
    returns how many probes were removed.
    """
    from kbf_common import cloze_query_batch, check_match as kbf_check_match, \
        FIXED_SYS_PROMPT, json_safe

    data = json.loads(Path(probe_path).read_text())
    probes = data["probes"]
    labels = [probe_consensus(p) for p in probes]
    provider = auto_pin_provider(ref, resolved.api_base, resolved.protocol)

    print(f"\n=== Layout filter: {passes} extra pass(es) over {len(probes)} probes ===",
          flush=True)
    sensitive = [False] * len(probes)
    for k in range(passes):
        by_domain = defaultdict(list)
        for i, p in enumerate(probes):
            by_domain[p["domain"]].append(i)
        rng = random.Random(LAYOUT_FILTER_SEED + k)
        order = []
        for dk in sorted(by_domain):
            idx = list(by_domain[dk])
            rng.shuffle(idx)
            order += idx

        vals, _, usage = cloze_query_batch(
            ref, [probes[i] for i in order], sys_prompt=FIXED_SYS_PROMPT, temp=0.0,
            batch_size=batch_size, api_base=resolved.api_base,
            api_key=resolved.api_key, protocol=resolved.protocol,
            provider=provider, consensus_vals=[labels[i] for i in order], quiet=True,
        )
        moved = 0
        for i, v in zip(order, vals):
            c = labels[i]
            if v is None or c is None or not kbf_check_match(c, v, probes[i]["domain"]):
                if not sensitive[i]:
                    moved += 1
                sensitive[i] = True
        print(f"  pass {k+1}: {moved} newly context-sensitive "
              f"(total {sum(sensitive)}/{len(probes)}), usage {usage}", flush=True)

    kept = [p for p, bad in zip(probes, sensitive) if not bad]
    dropped = len(probes) - len(kept)
    counts = {}
    for p in kept:
        counts[p["domain"]] = counts.get(p["domain"], 0) + 1
    data["probes"] = kept
    data["total_probes"] = len(kept)
    data["domains"] = dict(sorted(counts.items()))
    prev = data.get("layout_filter") or {}
    data["layout_filter"] = {"passes": prev.get("passes", 0) + passes,
                             "batch_size": batch_size,
                             "dropped": prev.get("dropped", 0) + dropped,
                             "kept": len(kept)}
    if "+layout_filter" not in data.get("verification_mode", ""):
        data["verification_mode"] = data.get("verification_mode", "") + "+layout_filter"
    data.pop("target_results", None)
    data.pop("self_error", None)
    with open(probe_path, "w") as f:
        json.dump(json_safe(data), f, indent=2, ensure_ascii=False)
    print(f"  dropped {dropped}, kept {len(kept)}", flush=True)
    return dropped


def run_self_test(resolved: ResolvedEndpoint, probe_path: Path, ref: str,
                  batch_size: int = 10) -> float:
    """Run the reference model against its own probe file and write the
    self-test entry back into target_results. Returns the error rate."""
    from kbf_common import (cloze_query_batch, check_match as kbf_check_match,
                            FIXED_SYS_PROMPT, json_safe)

    ref_short = ref.split("/")[-1]
    with open(probe_path) as f:
        data = json.load(f)
    probes = data["probes"]
    consensus = [probe_consensus(p) for p in probes]

    print(f"\n=== Self-Test: {ref} on {probe_path.name} ===", flush=True)
    print(f"  Probes: {len(probes)}, config: temp=0, sys='{FIXED_SYS_PROMPT}'", flush=True)

    provider = auto_pin_provider(ref, resolved.api_base, resolved.protocol)
    vals, _, usage = cloze_query_batch(
        ref, probes, sys_prompt=FIXED_SYS_PROMPT, temp=0.0,
        batch_size=batch_size, provider=provider, consensus_vals=consensus,
        api_base=resolved.api_base, api_key=resolved.api_key,
        protocol=resolved.protocol,
    )

    correct = total = 0
    match_vector = []
    for j in range(len(probes)):
        cv = consensus[j]
        tv = vals[j]
        if cv is None or tv is None:
            match_vector.append(None); continue
        total += 1
        if kbf_check_match(cv, tv, probes[j]["domain"]):
            correct += 1; match_vector.append(1)
        else:
            match_vector.append(0)

    err = (total - correct) / total if total > 0 else 0
    print(f"  Self-error: {err*100:.1f}% ({total-correct}/{total})", flush=True)
    print(f"  Usage: {usage}", flush=True)

    data.setdefault("target_results", {})[ref_short] = {
        "model": ref, "is_self": True,
        "values": vals, "match_vector": match_vector,
        "correct": correct, "total": total,
        "hamming": total - correct,
        "error_rate": round(err, 4),
        "usage": usage,
        "timestamp": datetime.now().isoformat(),
        "test_config": {"sys": FIXED_SYS_PROMPT, "temp": 0.0},
    }
    data["self_error"] = round(err, 4)

    with open(probe_path, "w") as f:
        json.dump(json_safe(data), f, indent=2, ensure_ascii=False)
    print(f"  Saved self-test to {probe_path}", flush=True)
    return err


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate KBF probes with cloze verification")
    parser.add_argument("--reference", help="Reference model (e.g. google/gemini-2.5-flash-lite)")
    parser.add_argument("--min-probes", type=int, default=100, help="Minimum total probes (default 100)")
    parser.add_argument("--max-rounds", type=int, default=6, help="Max rounds per domain (default 6)")
    parser.add_argument("--max-probes", type=int, default=None, metavar="N",
                        help="Probe budget: N split evenly into per-domain quotas (e.g. 400 "
                             "over 15 domains -> 27/26 each). A domain stops before its next "
                             "round once it holds its quota, so generation cost scales with N "
                             "instead of with --max-rounds. The quota is a floor: the round "
                             "that crosses it is kept whole, so the total lands at or a little "
                             "above N. Off by default (every domain runs all rounds).")
    parser.add_argument("--batch-size", type=int, default=10, help="Batch size for cloze queries (default 10)")
    parser.add_argument("--contrast", default="qwen/qwen3.5-9b", help="Contrast model for screening (default: qwen/qwen3.5-9b)")
    parser.add_argument("--contrast-endpoint", default=None,
                        help="Separate named endpoint profile for the contrast model "
                             "(default: use the reference endpoint).")
    parser.add_argument("--layout-filter", type=int, default=0, metavar="N",
                        help="After consensus, ask the whole set N more times at "
                             "temp=0 under a fresh domain-wide shuffle and delete "
                             "every probe whose answer moves. Costs one pass over "
                             "the finished set per N (~30 prompts per 300 probes). "
                             "Off by default; recommended for weaker reference "
                             "models — see README.md.")
    parser.add_argument("--no-contrast", action="store_true", help="Skip contrastive screening (use for T3 references)")
    parser.add_argument("--domains", nargs="*", default=None, help="Specific domains to generate (default: all)")
    parser.add_argument("--output", default=None,
                        help="Output path (default: probes/generated/<model>_<YYYYMMDD>.json)")
    parser.add_argument("--force", action="store_true",
                        help="Allow overwriting an existing output file")
    parser.add_argument("--self-test-only", default=None,
                        help="Skip probe generation. Load existing probe file and run self-test "
                             "only. THIS IS THE ONLY MODE THAT WRITES BACK TO A REFERENCE FILE.")
    parser.add_argument("--no-self-test", action="store_true",
                        help="Skip the post-generation self-test (otherwise it runs automatically "
                             "so the resulting probe file is immediately usable with kbf_test.py).")
    # Endpoint / auth — see scripts/endpoints.json.example
    parser.add_argument("--endpoint", default=None,
                        help="Named profile from scripts/endpoints.json. CLI flags below override "
                             "fields from the profile.")
    parser.add_argument("--api-base", default=None,
                        help="API endpoint URL (default: derived from --protocol).")
    parser.add_argument("--api-key", default=None,
                        help="API key. Empty string is rejected. If omitted, falls back to the "
                             "endpoint profile (use api_key_env for an environment variable).")
    parser.add_argument("--protocol", default=None, choices=list(SUPPORTED_PROTOCOLS),
                        help="API request/response format (default: openai-chat).")
    parser.add_argument("--list-endpoints", action="store_true",
                        help="Print names defined in scripts/endpoints.json and exit.")
    parser.add_argument("--allow-thinking", action="store_true",
                        help="If no thinking-suppression strategy works, "
                             "proceed in bare mode with an expanded token "
                             "budget instead of exiting. WARNING: token "
                             "usage and API cost are NOT bounded in this "
                             "mode.")
    args = parser.parse_args()

    if args.allow_thinking:
        import kbf_common
        kbf_common.ALLOW_THINKING_FALLBACK = True

    if args.list_endpoints:
        print_endpoints()
        return

    if not args.reference:
        parser.error("--reference is required (use --list-endpoints alone for endpoint listing).")

    try:
        resolved = resolve_endpoint(
            cli_endpoint=args.endpoint, cli_api_base=args.api_base,
            cli_api_key=args.api_key, cli_protocol=args.protocol,
        )
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(2)
    print_auth_banner(resolved)

    ref = args.reference
    ref_short = ref.split("/")[-1]

    # ── Self-test only mode ──
    if args.self_test_only:
        probe_path = Path(args.self_test_only)
        if not probe_path.exists():
            print(f"ERROR: {probe_path} not found")
            return
        if args.layout_filter > 0:
            layout_filter(resolved, probe_path, ref, passes=args.layout_filter,
                          batch_size=args.batch_size)
        run_self_test(resolved, probe_path, ref, batch_size=args.batch_size)
        return

    # ── Normal probe generation mode ──
    contrast_resolved = resolved
    if args.contrast_endpoint and not args.no_contrast:
        try:
            contrast_resolved = resolve_endpoint(cli_endpoint=args.contrast_endpoint)
        except ValueError as e:
            print(f"ERROR: contrast endpoint: {e}")
            sys.exit(2)
        print("Contrast endpoint:", flush=True)
        print_auth_banner(contrast_resolved, indent="  ")

    print(f"=== Probe Generation: {ref} ===", flush=True)
    print(f"Min probes: {args.min_probes}, Max rounds: {args.max_rounds}", flush=True)
    print(f"Verification: cloze format, (N) numbering, 3-config consensus", flush=True)
    if args.no_contrast:
        print(f"Contrastive screening: DISABLED (--no-contrast)", flush=True)
    else:
        print(f"Contrastive screening: {args.contrast}", flush=True)

    domains_to_run = args.domains or list(DOMAINS.keys())
    known_domains = [dk for dk in domains_to_run if dk in DOMAINS]
    quotas = split_quota(args.max_probes, known_domains)
    if quotas:
        lo, hi = min(quotas.values()), max(quotas.values())
        span = f"{lo}" if lo == hi else f"{lo}-{hi}"
        print(f"Probe cap: {args.max_probes} total over {len(known_domains)} domains "
              f"({span} per domain, stop on fill)", flush=True)
    all_probes = []
    frontier_stats = {}

    for dk in domains_to_run:
        if dk not in DOMAINS:
            print(f"  WARNING: unknown domain '{dk}', skipping", flush=True)
            continue
        dcfg = DOMAINS[dk]
        contrast = None if args.no_contrast else args.contrast
        probes, stats = generate_domain(resolved, ref, dk, dcfg,
                                        max_rounds=args.max_rounds,
                                        batch_size=args.batch_size,
                                        contrast_model=contrast,
                                        quota=quotas.get(dk),
                                        contrast_resolved=contrast_resolved)
        all_probes.extend(probes)
        frontier_stats[dk] = stats

    # Screening was done inline during generation (before consensus)
    # No post-hoc screening needed
    contrast_stats = None
    if not args.no_contrast:
        n_with_contrast = sum(1 for p in all_probes if p.get("contrast_value") is not None)
        contrast_stats = {"contrast_model": args.contrast, "screened_inline": True,
                          "n_with_contrast_data": n_with_contrast,
                          "api_base": contrast_resolved.api_base,
                          "protocol": contrast_resolved.protocol}

    print(f"\n{'='*60}", flush=True)
    print(f"TOTAL: {len(all_probes)} final probes (screening done inline)", flush=True)

    if len(all_probes) < args.min_probes:
        print(f"\nWARNING: {len(all_probes)} < {args.min_probes} minimum. "
              f"Consider running more rounds (--max-rounds) or adding domains.", flush=True)

    # Domain breakdown
    domain_counts = {}
    for p in all_probes:
        domain_counts[p["domain"]] = domain_counts.get(p["domain"], 0) + 1
    for dk, c in sorted(domain_counts.items()):
        print(f"  {dk}: {c}", flush=True)

    # Save
    if args.output:
        out_path = Path(args.output)
    else:
        date_tag = datetime.now().strftime("%Y%m%d")
        out_path = GENERATED_DIR / f"{ref_short}_{date_tag}.json"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not args.force:
        print(f"\nERROR: {out_path} already exists. Use --force to overwrite or "
              f"pick a different --output path.", flush=True)
        return
    output = {
        "reference_model": ref,
        "reference_endpoint": {"api_base": resolved.api_base,
                               "protocol": resolved.protocol},
        "timestamp": datetime.now().isoformat(),
        "total_probes": len(all_probes),
        "generation_mode": "adaptive_frontier",
        "verification_mode": "cloze_3config_consensus",
        "numbering_format": "(N)",
        "min_probes_target": args.min_probes,
        "max_probes_budget": args.max_probes,
        "domain_quotas": quotas or None,
        "contrast_screening": contrast_stats,
        "provider_map": {k: v for k, v in PROVIDER_MAP.items() if k in [ref, args.contrast]},
        "domains": domain_counts,
        "frontier_stats": frontier_stats,
        "probes": [
            {"name": p["name"], "value": p["value"], "domain": p["domain"],
             "difficulty_tier": p.get("difficulty_tier", 0),
             "consensus_raw": p.get("consensus_raw"),
             "contrast_value": p.get("contrast_value"),
             "contrast_agrees": p.get("contrast_agrees")}
            for p in all_probes
        ],
    }
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nSaved to {out_path}", flush=True)

    if args.no_self_test:
        print("\nSkipping self-test (--no-self-test). Run "
              f"`python3 scripts/generate_probes.py --reference {ref} "
              f"--self-test-only {out_path}` later to enable kbf_test.py.", flush=True)
    else:
        if args.layout_filter > 0:
            layout_filter(resolved, out_path, ref, passes=args.layout_filter,
                          batch_size=args.batch_size)
        run_self_test(resolved, out_path, ref, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
