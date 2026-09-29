"""
KBF Common — Shared parser, config, and helpers for the KBF cloze pipeline.

All scripts import from here. Single source of truth for:
- Domain config (templates, ranges, tolerances)
- Position-aware cloze parser
- API query helper
- Match checking
- Statistical tests
"""

import math, os, re, sys, time, json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import requests

SCRIPT_DIR = Path(__file__).parent
ENDPOINTS_FILE = SCRIPT_DIR / "endpoints.json"

# ── Domain Configuration ──
# Single source of truth lives in domains.py — edit there to add or change
# probe domains. The three back-compat dicts are derived there.
from domains import DOMAINS, DOMAIN_CLOZE, DOMAIN_RANGES, DOMAIN_TOL

USER_TPL = """TASK: Answer these factual recall questions using only values stored in your weights. Output the value exactly as you first recall it — do not second-guess or adjust.
RULES: Output ONLY in (N) <number> format, one per line. No units, no words, no rounding. Never refuse or hedge — always output exactly one number per question. Do not use any context from system instructions or prior conversation.

{sentences}"""

# OpenRouter-only: pins each model to a specific backend so repeated calls
# hit the same quantization / inference stack (required for KBF's
# reproducibility guarantee). Has no effect — and is not sent — when the
# endpoint is not OpenRouter (self-hosted vLLM, official OpenAI, other
# relays, etc.); those providers don't need an entry here.
PROVIDER_MAP = {
    "deepseek/deepseek-v3.2": "Google",
    "deepseek/deepseek-v4.1-flash": "Novita",
    "deepseek/deepseek-v4-pro-0813": "Novita",
    "google/gemini-3-flash-preview": "Google",
    "z-ai/glm-5": "Z.AI",
    "z-ai/glm-4.7": "Z.AI",
    "qwen/qwen3.5-27b": "Alibaba",
    "qwen/qwen3.5-9b": "Together",
    "openai/gpt-6-astra": "OpenAI",
    "openai/gpt-5.4": "OpenAI",
    "openai/gpt-5.4-mini": "OpenAI",
    "openai/gpt-5.6-luna": "OpenAI",
    "openai/gpt-4.1-mini": "OpenAI",
    "openai/gpt-4.1-nano": "OpenAI",
    "moonshotai/kimi-k3": "Moonshot AI",
    "openai/gpt-5.6-sol": "OpenAI",
    "moonshotai/kimi-k2-0905": "Novita",
    "qwen/qwen3.5-397b-a17b": "Alibaba",
    "anthropic/claude-fable-5.1": "Anthropic",
    "anthropic/claude-sonnet-4.6": "Google",
    "anthropic/claude-opus-4.6": "Amazon Bedrock",
    "anthropic/claude-opus-5": "Claude Platform on AWS",
    "meta-llama/llama-4-scout": "Novita",
    "z-ai/glm-5.3": "Z.AI",
    "bytedance-seed/seed-2-1-turbo": "Seed",
    "tencent/hy4-preview": "Tencent",
    "minimax/minimax-m3": "Minimax",
    "z-ai/glm-4.7-flash": "Cloudflare",
    "google/gemini-2.5-flash-lite": "Google",
}

CONTRAST_MODEL = "qwen/qwen3.5-9b"

# ── Fixed System Prompt (used across ALL stages) ──
# Deliberately neutral and task-focused. Anchors behavior and overrides
# any hidden prompts the suspect API might inject.
FIXED_SYS_PROMPT = "Follow the user's instructions exactly. Output only what is requested."

# ── Consensus Configs (preprocessing) ──
# All use the same system prompt; vary only temperature.
# temp=0 gives the canonical answer; two runs at 0.7 filter out probes
# that pass once by luck (sampling noise at high temp).
CONSENSUS_CONFIGS = [
    {"name": "t0",    "sys": FIXED_SYS_PROMPT, "temp": 0.0},
    {"name": "t07_a", "sys": FIXED_SYS_PROMPT, "temp": 0.7},
    {"name": "t07_b", "sys": FIXED_SYS_PROMPT, "temp": 0.7},
]

# ── Online Test Config (self-test and target-test) ──
TEST_SYS_PROMPT = FIXED_SYS_PROMPT
TEST_TEMP = 0.0

# ── Endpoint & API Key Resolution ──
#
# Two-layer resolution with strict precedence (high → low per-field):
#   1. CLI args (--api-key / --api-base / --protocol / --provider)
#   2. Named profile from scripts/endpoints.json (selected by --endpoint NAME)
#
# Env vars are NEVER read implicitly. If you want to keep a key in an env var
# of your choice, reference it explicitly via a profile's `api_key_env` field
# (e.g. `"api_key_env": "MY_OPENAI_KEY"`). No auto-guessing of any env name.


@dataclass
class ResolvedEndpoint:
    api_base: str
    api_key: str
    protocol: str
    provider: Optional[str]
    source: str   # human-readable composition trace for the startup banner


def mask_key(key: str, prefix: int = 5, suffix: int = 3) -> str:
    """Mask an API key for logging: 'sk-pr...AAA' (never reveal the full key)."""
    if not key:
        return "<empty>"
    if len(key) <= prefix + suffix + 3:
        return "***"
    return f"{key[:prefix]}...{key[-suffix:]}"


def load_endpoints() -> dict:
    """Read scripts/endpoints.json. Returns {} if file missing.

    Validates JSON syntax and top-level shape. Per-profile field validation
    happens lazily in resolve_endpoint when the profile is actually selected,
    so a single malformed profile doesn't break --list-endpoints."""
    if not ENDPOINTS_FILE.exists():
        return {}
    try:
        data = json.loads(ENDPOINTS_FILE.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(
            f"scripts/endpoints.json is not valid JSON: {e}. "
            f"See scripts/endpoints.json.example for the correct format."
        )
    if not isinstance(data, dict):
        raise ValueError(
            f"scripts/endpoints.json must be a JSON object of "
            f"{{profile_name: profile_spec}}, got {type(data).__name__}."
        )
    return data


def resolve_profile_key(spec: dict, profile_name: str) -> tuple:
    """Extract api_key from a profile spec. Returns (key, source_desc).

    Spec must contain exactly one of:
      - `api_key`     : literal key string
      - `api_key_env` : name of env var holding the key
    Empty literals and missing/empty env vars both raise."""
    literal = spec.get("api_key")
    env_name = spec.get("api_key_env")

    if literal and env_name:
        raise ValueError(
            f"endpoints.json: profile '{profile_name}' has both 'api_key' and "
            f"'api_key_env'. Use exactly one."
        )
    if literal:
        if not isinstance(literal, str) or not literal.strip():
            raise ValueError(
                f"endpoints.json: profile '{profile_name}' has empty 'api_key'."
            )
        return literal, f"literal in '{profile_name}'"
    if env_name:
        if not isinstance(env_name, str):
            raise ValueError(
                f"endpoints.json: profile '{profile_name}' 'api_key_env' "
                f"must be a string env-var name."
            )
        val = os.environ.get(env_name)
        if not val:
            raise ValueError(
                f"endpoints.json: profile '{profile_name}' references env var "
                f"${env_name}, but it is unset or empty. "
                f"Run `export {env_name}=<your-key>` first."
            )
        return val, f"'{profile_name}' → ${env_name}"
    raise ValueError(
        f"endpoints.json: profile '{profile_name}' must define either "
        f"'api_key' (literal value) or 'api_key_env' (env var name)."
    )


def resolve_endpoint(cli_endpoint: Optional[str] = None,
                     cli_api_base: Optional[str] = None,
                     cli_api_key: Optional[str] = None,
                     cli_protocol: Optional[str] = None,
                     cli_provider: Optional[str] = None) -> ResolvedEndpoint:
    """Resolve final (api_base, api_key, protocol, provider) from layered sources.

    Layered precedence (highest first) for each field independently:
      1. CLI arg
      2. endpoints.json profile (if --endpoint NAME passed)
      3. Protocol-derived default (for api_base only)

    Env vars are NOT auto-read — use a profile's `api_key_env` field for
    explicit env references. Fails fast with explicit, actionable error
    messages on any ambiguity."""

    # Empty --api-key is almost always a shell expansion bug. Catch early
    # rather than letting it sail through to a 401 hours later.
    if cli_api_key is not None and not cli_api_key.strip():
        raise ValueError(
            "--api-key was passed as an empty string. This usually means a "
            "shell variable expansion failed (e.g. `--api-key \"$VAR\"` where "
            "VAR is not yet exported). Pass the literal key, "
            "or use --endpoint <name>."
        )

    api_base = api_key = protocol = provider = None
    sources = []

    # ── Layer 2: named endpoint profile ─────────────────────────────────
    if cli_endpoint:
        endpoints = load_endpoints()
        if cli_endpoint not in endpoints:
            available = ", ".join(sorted(endpoints)) if endpoints else "(none)"
            raise ValueError(
                f"--endpoint '{cli_endpoint}' not found in scripts/endpoints.json. "
                f"Available: {available}. "
                f"See scripts/endpoints.json.example for the format."
            )
        prof = endpoints[cli_endpoint]
        if not isinstance(prof, dict):
            raise ValueError(
                f"endpoints.json: profile '{cli_endpoint}' must be a JSON object."
            )
        for field in ("api_base", "protocol"):
            if not prof.get(field):
                raise ValueError(
                    f"endpoints.json: profile '{cli_endpoint}' is missing "
                    f"required field '{field}'."
                )
        api_base = prof["api_base"]
        protocol = prof["protocol"]
        provider = prof.get("provider")
        api_key, key_src = resolve_profile_key(prof, cli_endpoint)
        sources.append(f"endpoint {key_src}")

    # ── Layer 1: CLI overrides (always win per-field) ───────────────────
    if cli_protocol is not None:
        protocol = cli_protocol
        sources.append("protocol=--protocol")
    if cli_api_base is not None:
        api_base = cli_api_base
        sources.append("api_base=--api-base")
    if cli_provider is not None:
        provider = cli_provider
        sources.append("provider=--provider")
    if cli_api_key is not None:
        api_key = cli_api_key
        sources.append("api_key=--api-key")

    # Resolve protocol default (must come before env fallback, which is
    # protocol-aware).
    if protocol is None:
        protocol = "openai-chat"
        sources.append("protocol=default(openai-chat)")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            f"Unknown protocol: {protocol!r}. Supported: {SUPPORTED_PROTOCOLS}"
        )

    # Resolve api_base default if still missing.
    if api_base is None:
        api_base = default_endpoint(protocol, None)
        sources.append(f"api_base=default-for-{protocol}")

    # Key must have come from CLI or endpoint profile — no env auto-read.
    if api_key is None:
        raise ValueError(
            f"No API key resolved for protocol={protocol}, "
            f"api_base={api_base}.\n\n"
            f"Provide one of:\n"
            f"  1. --api-key <key>            pass the key on the CLI, or\n"
            f"  2. --endpoint <name>          load a profile from scripts/endpoints.json\n"
            f"                                (use either `api_key` for a literal value\n"
            f"                                 or `api_key_env` to read from an env var\n"
            f"                                 of your choice).\n\n"
            f"List configured profiles with `--list-endpoints`. "
            f"See scripts/endpoints.json.example for the format."
        )

    # Final safety check: any non-string slipped through?
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(
            f"Resolved api_key is empty or non-string ({type(api_key).__name__}). "
            f"This is a bug — sources: {'; '.join(sources)}"
        )

    return ResolvedEndpoint(
        api_base=api_base,
        api_key=api_key.strip(),
        protocol=protocol,
        provider=provider,
        source=" + ".join(sources) if sources else "all defaults",
    )


def print_endpoints() -> None:
    """Print configured endpoints from scripts/endpoints.json, or guidance if none."""
    eps = load_endpoints()
    if not eps:
        print("No endpoints defined in scripts/endpoints.json")
        print("Copy scripts/endpoints.json.example to scripts/endpoints.json to get started.")
        return
    print(f"Endpoints defined in scripts/endpoints.json ({len(eps)}):")
    for name in sorted(eps):
        p = eps[name]
        print(f"  {name:30s} {p.get('protocol','?'):20s} {p.get('api_base','?')}")


def print_auth_banner(resolved: ResolvedEndpoint, indent: str = "") -> None:
    """Print the protocol/api_base/key/source startup banner."""
    print(f"{indent}Auth: protocol={resolved.protocol}  api_base={resolved.api_base}", flush=True)
    print(f"{indent}      key={mask_key(resolved.api_key)}  source: {resolved.source}", flush=True)
    if resolved.provider:
        print(f"{indent}      provider pin: {resolved.provider}", flush=True)


# Per-process cache: (model, api_base) -> bool, used by auto_pin_provider to
# print the auto-pin notice only once per (model, endpoint).
_AUTO_PIN_NOTICE_SEEN: set = set()


def auto_pin_provider(model: str, api_base: Optional[str], protocol: str,
                      explicit: Optional[str] = None,
                      announce: bool = True) -> Optional[str]:
    """Resolve the provider pin for a single request.
    """
    if explicit is not None:
        return explicit
    if protocol != "openai-chat" or not api_base or "openrouter.ai" not in api_base:
        return None
    pin = PROVIDER_MAP.get(model)
    if pin and announce:
        key = (model, api_base)
        if key not in _AUTO_PIN_NOTICE_SEEN:
            _AUTO_PIN_NOTICE_SEEN.add(key)
            print(f"      auto-pinned provider for {model}: {pin} "
                  f"(from PROVIDER_MAP)", flush=True)
    return pin


# ── Position-Aware Parser ──

def extract_nums(text, n, vr):
    """Position-aware parser: uses (N) prefixes to map answers to correct slots.
    Falls back to sequential parsing if no prefixes found.

    This is the SINGLE parser for the entire pipeline. All scripts import this.
    """
    if not text:
        return [None] * n
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # Strategy 1: Parse by (N) prefix — robust to skipped/reordered lines
    indexed = {}
    has_prefix = False
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('---') or line.startswith('|'):
            continue
        m = re.match(r'^\((\d+)\)\s*', line)
        if not m:
            m = re.match(r'^(\d+)[.)]\s+', line)
        if m:
            has_prefix = True
            idx = int(m.group(1)) - 1  # 0-based
            rest = line[m.end():].strip()
            rest = rest.replace("\u2212", "-").replace("\u2013", "-").replace(",", "")
            nums = re.findall(r'-?\d+\.?\d*(?:[eE][+-]?\d+)?', rest)
            if nums and 0 <= idx < n:
                try:
                    val = float(nums[-1])
                    indexed[idx] = val if vr[0] <= val <= vr[1] else None
                except (ValueError, TypeError):
                    indexed[idx] = None
            elif 0 <= idx < n:
                indexed[idx] = None
    if has_prefix and indexed:
        return [indexed.get(i) for i in range(n)]

    # Strategy 2: Fallback — sequential line-by-line
    results = []
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('---'):
            continue
        cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
        cleaned = re.sub(r'^\d+[.)]\s+', '', cleaned).strip()
        cleaned = cleaned.replace("\u2212", "-").replace("\u2013", "-").replace(",", "")
        nums = re.findall(r'-?\d+\.?\d*(?:[eE][+-]?\d+)?', cleaned)
        if nums:
            try:
                val = float(nums[-1])
                results.append(val if vr[0] <= val <= vr[1] else None)
                continue
            except (ValueError, TypeError):
                pass
        results.append(None)
    while len(results) < n:
        results.append(None)
    return results[:n]


# ── Match Checking ──

def _round_half_up(x):
    """Round half away from zero — round() rounds halves to even, which makes
    a comparison depend on the parity of the integer part (16.5 -> 16 but
    17.5 -> 18)."""
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


def check_match(ref_val, test_val, domain):
    """Check if test value matches reference within domain tolerance."""
    if ref_val is None or test_val is None:
        return False
    tol, mode = DOMAIN_TOL.get(domain, (0.05, "relative"))
    if mode == "absolute":
        # Absolute-tolerance domains hold integer quantities — years, counts,
        # key sizes, chromosome numbers. Both sides are rounded so that a
        # different rendering of the same integer (4.0 vs 4.08 for a version
        # number, 74.0 vs 74.03 for a box office in millions) is not scored as
        # a fingerprint mismatch. Generation applies the same rule. Rounding is
        # half-up rather than round()'s banker's rounding, so the comparison
        # does not depend on the parity of the integer part.
        return abs(_round_half_up(ref_val) - _round_half_up(test_val)) <= tol
    return abs(ref_val - test_val) / max(abs(ref_val), 1e-10) <= tol


# ── Statistical Tests ──
# Fixed thresholds — not exposed on the CLI. KBF's premise is a calibrated
# SAME/DIFF judgement; letting users tune these per run defeats the purpose.
KBF_CONFIDENCE = 0.99   # Clopper-Pearson confidence for p0 upper bound
KBF_ALPHA = 0.05        # One-sided binomial test threshold for DIFF


def clopper_pearson_p0(k, n, confidence=KBF_CONFIDENCE):
    """Clopper-Pearson upper confidence bound for binomial proportion.

    Given k errors in n trials, returns p0 such that the true error rate
    is covered by p0 with the given frequentist confidence level under the
    binomial sampling model.
    """
    from scipy.stats import beta as beta_dist
    if n == 0:
        return 1.0
    return float(beta_dist.ppf(confidence, k + 1, n - k))


def binomial_p(k, n, p0):
    """One-sided binomial test: P(X >= k | Bin(n, p0))."""
    from scipy.stats import binom
    if n == 0 or k == 0:
        return 1.0
    return float(1 - binom.cdf(k - 1, n, min(p0, 0.99)))


# ── API Query ──

# Thinking suppression strategies (tried in order during discovery)
# Different API providers and relay services handle thinking/reasoning suppression
# in incompatible ways. Some accept OpenAI-style reasoning effort, some accept
# Anthropic-style thinking flags, some require a prefill trick, and some models
# simply don't think at all. We try each strategy in order and cache the first
# one that produces parseable output for the current session.
#
# If every strategy fails, we fall back to "bare_expanded": no suppression at
# all, but the per-batch max_tokens budget is multiplied by
# THINKING_FALLBACK_TOKEN_MULTIPLIER so the model has room to both think AND
# emit a parseable answer. Only if THAT fails do we raise RuntimeError.
THINKING_STRATEGIES = [
    "reasoning_none",        # reasoning: {effort: "none"}    — OpenRouter / OpenAI native (gpt-5)
    "reasoning_minimal",     # reasoning: {effort: "minimal"} — fallback for o-series that reject "none"
    "thinking_disabled",     # thinking: {type: "disabled"}   — Anthropic native / some relays
    "enable_thinking_false", # enable_thinking: false         — Qwen / alternative relay param
    "prefill",               # assistant prefill <think></think> (openai-chat only)
    "bare",                  # no suppression at all          — model doesn't think by default
]

# When a strategy can't suppress thinking, multiply max_tokens by this factor
# so the model has room to think AND produce parseable output.
THINKING_FALLBACK_TOKEN_MULTIPLIER = 50

# Sentinel returned by discover_thinking_strategy when no suppression worked
# but bare + expanded token budget did. query_api treats it specially.
BARE_EXPANDED_STRATEGY = "bare_expanded"

# Opt-in escape hatch: when True, query_api proceeds in bare mode (with
# expanded token budget) instead of exiting when every thinking-suppression
# strategy fails. Set from the --allow-thinking CLI flag in entry scripts.
# Off by default because in this mode token cost is unbounded.
ALLOW_THINKING_FALLBACK = False

# Seconds to wait for one discovery probe. Must exceed the endpoint's
# worst-case unsuppressed latency (Z.AI's free tier peaked at 464s).
DISCOVERY_TIMEOUT = 600

# Per-session cache: (api_base, protocol, model) -> working strategy name.
# Populated on first query to an (endpoint, model) pair; reused thereafter.
THINKING_STRATEGY_CACHE: dict = {}


def apply_thinking_strategy(strategy, payload, messages):
    """Apply a thinking suppression strategy to payload/messages.

    `messages` may be None for protocols that don't use a top-level messages list
    (anthropic-messages, openai-responses). In that case the prefill strategy
    is a no-op."""
    if strategy == "reasoning_none":
        payload["reasoning"] = {"effort": "none"}
    elif strategy == "reasoning_minimal":
        payload["reasoning"] = {"effort": "minimal"}
    elif strategy == "thinking_disabled":
        payload["thinking"] = {"type": "disabled"}
    elif strategy == "enable_thinking_false":
        payload["enable_thinking"] = False
    elif strategy == "prefill":
        if messages is not None:
            messages.append({"role": "assistant", "content": "<think></think>"})
    # "bare" → nothing added


def discover_thinking_strategy(protocol, model, api_base, api_key,
                                temp=0.0, provider=None):
    """Probe the endpoint to find a working thinking suppression strategy.

    Protocol-agnostic: uses `build_request` so the same loop works for
    openai-chat, anthropic-messages, and openai-responses. The strategies
    in `THINKING_STRATEGIES` cover all three (e.g. anthropic API silently
    rejects `reasoning.*`, falls through to `thinking_disabled`).

    Sends a 10-item cloze batch (matching the real test format exactly) under
    each strategy and returns the first one that yields ≥ 5/10 parseable
    numeric answers. If no strategy works, retries with `bare` and
    `BATCH_MAX_TOKENS * THINKING_FALLBACK_TOKEN_MULTIPLIER` so the model has
    room to think AND emit an answer; on success returns
    BARE_EXPANDED_STRATEGY. Only if that final attempt also fails does it
    return None (caller will raise).
    """
    probe_sentences = (
        "(1) The boiling point of ethanol at standard pressure in degrees Celsius is ___\n"
        "(2) The atomic number of gold is ___\n"
        "(3) The speed of light in vacuum in metres per second is ___\n"
        "(4) The melting point of iron in degrees Celsius is ___\n"
        "(5) The number of bones in an adult human body is ___\n"
        "(6) The atomic mass of carbon-12 in unified atomic mass units is ___\n"
        "(7) The boiling point of water at standard pressure in degrees Celsius is ___\n"
        "(8) The gravitational acceleration on Earth's surface in metres per second squared is ___\n"
        "(9) The atomic number of oxygen is ___\n"
        "(10) The melting point of pure ice at standard pressure in degrees Celsius is ___"
    )
    probe_user = USER_TPL.format(sentences=probe_sentences)
    BATCH_MAX_TOKENS = 80 * 10  # must match cloze_query_batch: 80 * len(batch)

    # Collect first non-transient failure body per strategy so the caller can
    # surface the real cause (bad model name, invalid provider pin, auth, …)
    # instead of misattributing every failure to "thinking suppression".
    failures = []  # list[(strategy, status_code_or_None, body_or_exception_str)]

    def _try_once(strategy, max_tok):
        """Send one probe batch. Returns (n_valid, reasoning_tokens, failure_record_or_None).
        n_valid = -1 means transport / 4xx failure.
        reasoning_tokens is the model's reported thinking-token count for this
        response (0 if not reported); used to reject strategies that the
        endpoint silently ignores (request accepted, model thinks anyway)."""
        headers, payload, extract_text, _, messages = build_request(
            protocol, model, FIXED_SYS_PROMPT, probe_user,
            temp, max_tok, api_key, provider,
        )
        apply_thinking_strategy(strategy, payload, messages)
        last_exc = None
        for retry in range(3):
            try:
                # Generous timeout: an endpoint that has not been suppressed yet
                # is still thinking, so the probe that discovers the suppression
                # is the slowest request we ever send. A short timeout here makes
                # every strategy look like it failed on a slow endpoint.
                resp = requests.post(api_base, headers=headers, json=payload,
                                     timeout=DISCOVERY_TIMEOUT)
                if resp.status_code == 429:
                    time.sleep(5 * (retry + 1))
                    continue
                if resp.status_code >= 500:
                    last_exc = (resp.status_code, resp.text[:300])
                    time.sleep(3 * (retry + 1))
                    continue
                if resp.status_code >= 400:
                    return -1, 0, (strategy, resp.status_code, resp.text[:300])
                data = resp.json()
                content = extract_text(data) or ""
                parsed = extract_nums(content, 10, (-1e9, 1e9))
                # OpenAI-style usage.completion_tokens_details.reasoning_tokens —
                # some endpoints (e.g. bigmodel.cn) accept reasoning/thinking
                # fields but ignore them; the only reliable signal that the
                # strategy actually worked is that the model didn't emit any
                # reasoning tokens. 0 when the field is absent, which is the
                # correct default for endpoints that don't report it.
                rt = 0
                try:
                    rt = int(
                        data.get("usage", {})
                            .get("completion_tokens_details", {})
                            .get("reasoning_tokens", 0)
                    )
                except (TypeError, ValueError):
                    rt = 0
                return sum(1 for v in parsed if v is not None), rt, None
            except Exception as e:
                last_exc = (None, f"{type(e).__name__}: {e}")
                if retry == 2:
                    return -1, 0, (strategy, None, f"{type(e).__name__}: {e}")
                time.sleep(2 * (retry + 1))
        # All retries exhausted on 5xx / network error
        sc, body = last_exc if last_exc else (None, "unknown")
        return -1, 0, (strategy, sc, body)

    for strategy in THINKING_STRATEGIES:
        n_valid, rt, fail = _try_once(strategy, BATCH_MAX_TOKENS)
        # "bare" is allowed to think — it's the no-suppression baseline.
        # For every other strategy, non-zero reasoning_tokens means the
        # endpoint silently ignored the suppression field; reject it so we
        # fall through to a strategy that actually works.
        if n_valid >= 5 and (strategy == "bare" or rt == 0):
            print(f"    Thinking suppression: '{strategy}' works for "
                  f"{api_base} [{protocol}] ({n_valid}/10 parseable, "
                  f"reasoning_tokens={rt})",
                  flush=True)
            return strategy
        if n_valid >= 5 and rt > 0:
            print(f"    Thinking suppression: '{strategy}' accepted but "
                  f"ignored by {api_base} (reasoning_tokens={rt}); trying next",
                  flush=True)
        if fail:
            failures.append(fail)

    # Last-ditch fallback: let the model think freely, just give it room.
    expanded = BATCH_MAX_TOKENS * THINKING_FALLBACK_TOKEN_MULTIPLIER
    n_valid, _rt, fail = _try_once("bare", expanded)
    if n_valid >= 5:
        print(f"    Thinking suppression: no strategy worked; falling back to "
              f"'bare' with {THINKING_FALLBACK_TOKEN_MULTIPLIER}× token budget "
              f"({n_valid}/10 parseable)", flush=True)
        return BARE_EXPANDED_STRATEGY
    if fail:
        failures.append(fail)

    # Stash failures on the function so query_api can format them into the
    # RuntimeError without changing the return signature.
    discover_thinking_strategy.last_failures = failures
    return None


# Protocol adapters live in protocols.py — edit that file to add new providers.
# Re-exported here so existing `from kbf_common import SUPPORTED_PROTOCOLS` keeps working.
from protocols import SUPPORTED_PROTOCOLS, build_request, default_endpoint


def _pinned_provider_hint(provider):
    """Extra error line explaining a pinned-provider failure.

    When a request pins a single provider (allow_fallbacks=False), OpenRouter
    will NOT route to any other backend — so an upstream error almost always
    means the pinned provider itself is unavailable. Make that explicit and
    tell the user to pick another one themselves; we never auto-fall-back."""
    if not provider:
        return ""
    return (
        f"\n\nThis request pinned provider '{provider}' with "
        f"allow_fallbacks=False, so no automatic fallback to another backend "
        f"was attempted. The pinned provider is most likely unavailable for "
        f"this model right now. Choose a different provider yourself "
        f"(edit PROVIDER_MAP in kbf_common.py or pass --provider) — check "
        f"https://openrouter.ai/<model>/providers for which backends are live."
    )


def query_api(model, sys_prompt, user_prompt, temp=0.0, max_tokens=2000,
              api_base=None, api_key=None, provider=None,
              suppress_reasoning=True, protocol="openai-chat"):
    """Query an LLM API. Supports openai-chat / anthropic-messages / openai-responses.

    Thinking suppression is auto-discovered (per (api_base, protocol, model)
    cache) for all three protocols by trying strategies in `THINKING_STRATEGIES`
    until one yields parseable output. The model is part of the cache key
    because a single endpoint (e.g. OpenRouter) can serve multiple models with
    different reasoning behavior."""
    api_base = default_endpoint(protocol, api_base)
    if not api_key:
        raise ValueError(
            "query_api: api_key is empty. Callers must resolve the key via "
            "resolve_endpoint() before calling query_api."
        )

    strategy = None
    if suppress_reasoning:
        cache_key = (api_base, protocol, model)
        if cache_key not in THINKING_STRATEGY_CACHE:
            discovered = discover_thinking_strategy(
                protocol, model, api_base, api_key, temp, provider=provider,
            )
            if discovered is None:
                fails = getattr(discover_thinking_strategy, "last_failures", [])
                # If every strategy was rejected with the same status code, the
                # real cause is almost certainly NOT thinking-suppression — it's
                # an underlying API error (bad model, invalid provider pin,
                # auth, etc.). Surface that body verbatim.
                same_status = (
                    fails
                    and len({f[1] for f in fails if f[1] is not None}) == 1
                    and all(f[1] is not None and f[1] >= 400 for f in fails)
                )
                if same_status:
                    status = fails[0][1]
                    body = fails[0][2]
                    print(
                        f"\nERROR: API request rejected for {api_base} "
                        f"[{protocol}] (model={model}). Every "
                        f"thinking-suppression strategy received HTTP "
                        f"{status}, so the failure is upstream (not a "
                        f"thinking-API mismatch). Response body:\n"
                        f"  {body}"
                        f"{_pinned_provider_hint(provider)}",
                        file=sys.stderr, flush=True,
                    )
                    sys.exit(2)
                # Otherwise it really does look like a thinking-suppression
                # mismatch — show the per-strategy failure log so the user can
                # diagnose which payload field the endpoint rejects.
                detail = "\n".join(
                    f"    [{s}] status={st!r} body={(b or '')[:160]!r}"
                    for s, st, b in fails
                ) or "    (no per-strategy failures recorded)"
                if ALLOW_THINKING_FALLBACK:
                    print(
                        f"\nWARNING: No thinking-suppression strategy worked "
                        f"for {api_base} [{protocol}] (model={model}). "
                        f"--allow-thinking is set, so continuing in 'bare' "
                        f"mode with {THINKING_FALLBACK_TOKEN_MULTIPLIER}× the "
                        f"token budget per batch. NOTE: the model will think "
                        f"freely on every request — token usage and API cost "
                        f"are NOT bounded and can be much higher than a normal "
                        f"run. Per-strategy responses:\n{detail}",
                        file=sys.stderr, flush=True,
                    )
                    THINKING_STRATEGY_CACHE[cache_key] = BARE_EXPANDED_STRATEGY
                else:
                    print(
                        f"\nERROR: No thinking-suppression strategy worked "
                        f"for {api_base} [{protocol}] (model={model}), even "
                        f"after retrying 'bare' with "
                        f"{THINKING_FALLBACK_TOKEN_MULTIPLIER}× the token "
                        f"budget. Per-strategy responses:\n{detail}\n\n"
                        f"You have two options:\n"
                        f"  1. Add a new strategy that this endpoint accepts "
                        f"— edit THINKING_STRATEGIES and "
                        f"apply_thinking_strategy in scripts/kbf_common.py "
                        f"(see the existing entries for the pattern).\n"
                        f"  2. Re-run with --allow-thinking to proceed "
                        f"anyway in 'bare' mode (the model will think on "
                        f"every request). WARNING: token usage and cost are "
                        f"NOT bounded in this mode — a single run can be "
                        f"many times more expensive than normal, depending "
                        f"on the model.",
                        file=sys.stderr, flush=True,
                    )
                    sys.exit(2)
            else:
                THINKING_STRATEGY_CACHE[cache_key] = discovered
        strategy = THINKING_STRATEGY_CACHE[cache_key]
        if strategy == BARE_EXPANDED_STRATEGY:
            # Model insists on thinking; give it room to think AND answer.
            max_tokens = max_tokens * THINKING_FALLBACK_TOKEN_MULTIPLIER

    headers, payload, extract_text, extract_usage, messages = build_request(
        protocol, model, sys_prompt, user_prompt, temp, max_tokens, api_key, provider
    )

    if strategy is not None and strategy != BARE_EXPANDED_STRATEGY:
        apply_thinking_strategy(strategy, payload, messages)

    for attempt in range(5):
        try:
            resp = requests.post(api_base, headers=headers, json=payload, timeout=120)
        except Exception as e:
            # Network / connection error → transient, back off and retry
            if attempt == 4:
                print(f"    ERROR: network: {e}", flush=True)
                return None, {}
            time.sleep(2 * (attempt + 1))
            continue

        if resp.status_code == 429:
            wait = min(30, 5 * (attempt + 1))
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(3 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            # 4xx is not transient (bad model name, unsupported parameter
            # like temperature on reasoning models, auth issue, etc.).
            # Raise immediately with the response body so the user sees the
            # real cause instead of burning all retries on a deterministic
            # failure. See README "API requirements" for what we expect.
            raise RuntimeError(
                f"{resp.status_code} from {api_base} [{protocol}] "
                f"(model={model}): {resp.text[:500]}"
                f"{_pinned_provider_hint(provider)}"
            )

        try:
            data = resp.json()
            c = extract_text(data)
            c = re.sub(r"<think>.*?</think>", "", c or "", flags=re.DOTALL).strip()
            return c, extract_usage(data)
        except Exception as e:
            # Parse error in a 2xx response → may be a truncated body, retry
            if attempt == 4:
                print(f"    ERROR: parse: {e}", flush=True)
                return None, {}
            time.sleep(2 * (attempt + 1))
    return None, {}


def validate_batch(vv, batch, consensus_vals=None):
    """Validate a parsed batch. Returns (is_ok, issue_description).

    Checks:
    1. Too many Nones (>50% missing → likely format issue)
    2. Off-by-one shift (values match next probe's consensus)
    3. All-zero on self-test (0 matches when self should be high)
    """
    n = len(vv)
    if n == 0:
        return True, None

    # Check 1: Too many Nones
    n_none = sum(1 for v in vv if v is None)
    if n >= 3 and n_none > n * 0.5:
        return False, f"too_many_nones ({n_none}/{n})"

    # Check 2: Off-by-one shift detection (only if consensus available)
    if consensus_vals and len(consensus_vals) == n and n >= 3:
        own_match = shift_match = n_valid = 0
        for j in range(n - 1):
            val = vv[j]
            own_cons = consensus_vals[j]
            next_cons = consensus_vals[j + 1]
            if val is None or own_cons is None or next_cons is None:
                continue
            n_valid += 1
            domain = batch[j][1]["domain"] if j < len(batch) else "unknown"
            if check_match(own_cons, val, domain):
                own_match += 1
            if check_match(next_cons, val, domain):
                shift_match += 1
        if n_valid >= 3 and shift_match > own_match and shift_match >= 3:
            return False, f"shifted (own={own_match}, shift={shift_match}/{n_valid})"

    return True, None


def cloze_query_batch(model, probes, sys_prompt="", temp=0.0, batch_size=10,
                      api_base=None, api_key=None, provider=None,
                      consensus_vals=None, max_retries=2, verbose=False,
                      protocol="openai-chat", quiet=False):
    """Query model with cloze probes in batches. Returns per-probe values.

    Args:
        consensus_vals: optional list of consensus values (same length as probes)
            for batch validation. If provided, validates each batch and retries
            on detected parsing issues.
        max_retries: max retry attempts per batch on validation failure.
        verbose: if True, also returns raw_log with prompts and responses.
        quiet: if True, suppress the per-batch progress line. Validation
            warnings still print (with batch context). Used by generate_probes
            so its per-round logs aren't drowned in batch chatter.

    Returns:
        vals: list of per-probe parsed values (always returned)
        raw_log: list of batch dicts with prompts/responses (only if verbose=True)
            If verbose=False, returns (vals, None)
    """
    from collections import defaultdict

    by_domain = defaultdict(list)
    for i, p in enumerate(probes):
        by_domain[p["domain"]].append((i, p))

    vals = [None] * len(probes)
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    raw_log = [] if verbose else None
    domain_list = sorted(by_domain.items())
    total_batches = sum(1 + (len(ips) - 1) // batch_size for _, ips in domain_list)
    batch_num = 0

    for dk, iprobes in domain_list:
        tpl = DOMAIN_CLOZE.get(dk)
        vr = DOMAIN_RANGES.get(dk, (-1e15, 1e15))
        if not tpl:
            continue
        batches = [iprobes[i:i+batch_size] for i in range(0, len(iprobes), batch_size)]
        for batch in batches:
            batch_num += 1
            if not quiet:
                print(f"      [{batch_num}/{total_batches}] {dk} ({len(batch)})...",
                      end="", flush=True)

            # Build prompt
            sents = [f'({j+1}) {tpl.format(name=p["name"])}' for j, (_, p) in enumerate(batch)]
            prompt = USER_TPL.format(sentences="\n".join(sents))

            # Query + validate loop
            best_vv = None
            best_valid = -1
            best_resp = None
            all_attempts = []
            for retry in range(1 + max_retries):
                resp_text, usage = query_api(model, sys_prompt, prompt, temp=temp,
                                             max_tokens=80 * len(batch),
                                             api_base=api_base, api_key=api_key,
                                             provider=provider, protocol=protocol)
                vv = extract_nums(resp_text, len(batch), vr)
                valid = sum(1 for v in vv if v is not None)

                # Accumulate token usage (always)
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    total_usage[k] += usage.get(k, 0)

                if verbose:
                    all_attempts.append({
                        "retry": retry,
                        "response": resp_text,
                        "parsed": list(vv),
                        "valid": valid,
                        "usage": usage,
                    })

                # Track best attempt (most non-None values)
                if valid > best_valid:
                    best_vv = vv
                    best_valid = valid
                    best_resp = resp_text

                # Validate
                batch_cons = None
                if consensus_vals:
                    batch_cons = [consensus_vals[gi] for gi, _ in batch]
                is_ok, issue = validate_batch(vv, batch, batch_cons)

                if is_ok:
                    break

                if retry < max_retries:
                    if quiet:
                        print(f"      [{dk}] ⚠ {issue}, retry {retry+1}", flush=True)
                    else:
                        print(f" ⚠ {issue}, retry {retry+1}...", end="", flush=True)
                    time.sleep(1)
                else:
                    if quiet:
                        print(f"      [{dk}] ⚠ {issue} (kept best of {1+max_retries})", flush=True)
                    else:
                        print(f" ⚠ {issue} (using best of {1+max_retries} attempts)", end="", flush=True)
                    vv = best_vv  # use the attempt with most valid values
                    resp_text = best_resp

            valid = sum(1 for v in vv if v is not None)
            if not quiet:
                print(f" {valid}/{len(batch)} ok", flush=True)
            for j, (gi, _) in enumerate(batch):
                vals[gi] = vv[j]

            # Save raw log entry
            if verbose:
                raw_log.append({
                    "batch_num": batch_num,
                    "domain": dk,
                    "probe_indices": [gi for gi, _ in batch],
                    "probe_names": [p["name"] for _, p in batch],
                    "prompt": prompt,
                    "response": resp_text,
                    "parsed": list(vv),
                    "attempts": all_attempts if len(all_attempts) > 1 else None,
                })

            time.sleep(0.3)
    return vals, raw_log, total_usage


# ── Probe I/O ──

def probe_consensus(p):
    """Return a probe's canonical answer.

    Prefers ``cloze_consensus`` (written by the verification pipeline) and
    falls back to ``value`` (written at generation time). Uses an explicit
    None check so legitimate 0.0 answers are not silently replaced.
    """
    c = p.get("cloze_consensus")
    return c if c is not None else p.get("value")


def load_probes(probe_file):
    """Load probes from a JSON file."""
    with open(probe_file) as f:
        data = json.load(f)
    # Support both standalone probe files and results files
    if "probes" in data:
        return data["probes"], data
    return data, {}


def json_safe(obj):
    """Recursively convert numpy types for JSON serialization."""
    try:
        import numpy as np
        if isinstance(obj, dict):
            return {k: json_safe(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [json_safe(v) for v in obj]
        elif isinstance(obj, (np.bool_,)):
            return bool(obj)
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
    except ImportError:
        pass
    return obj
