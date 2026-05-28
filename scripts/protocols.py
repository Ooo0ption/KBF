"""
LLM API Protocol Adapters
=========================
Per-protocol functions for building HTTP requests and parsing responses.
Edit this file to add support for a new provider/protocol.

To add a new protocol "my-proto":
  1. Write three functions:
       build_my_proto(model, sys_prompt, user_prompt, temp, max_tokens, api_key, provider)
           → (headers: dict, payload: dict, messages_or_None: list | None)
           Return `messages` only if the protocol uses a top-level messages list
           (needed so the `prefill` thinking-suppression strategy can mutate it).
           Otherwise return None.
       extract_my_proto(data) → assistant text str (or None)
       usage_my_proto(data)   → {"prompt_tokens", "completion_tokens", "total_tokens"}
  2. Register it: add `"my-proto": (build_my_proto, extract_my_proto, usage_my_proto)`
     to PROTOCOL_REGISTRY below.
  3. (Optional) Add a default endpoint URL to DEFAULT_ENDPOINTS so users can
     omit --api-base when using your protocol.
"""

# ── openai-chat ─────────────────────────────────────────────────────

def build_openai_chat(model, sys_prompt, user_prompt, temp, max_tokens, api_key, provider):
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": user_prompt})
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages,
               "temperature": temp, "max_tokens": max_tokens}
    if provider:
        payload["provider"] = {"order": [provider], "allow_fallbacks": False}
    return headers, payload, messages


def extract_openai_chat(data):
    return data["choices"][0]["message"].get("content")


def usage_openai_chat(data):
    return data.get("usage", {})


# ── anthropic-messages ──────────────────────────────────────────────

def build_anthropic_messages(model, sys_prompt, user_prompt, temp, max_tokens, api_key, provider):
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": user_prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    if sys_prompt:
        payload["system"] = sys_prompt
    return headers, payload, None


def extract_anthropic_messages(data):
    for blk in data.get("content", []):
        if blk.get("type") == "text":
            return blk.get("text")
    return None


def usage_anthropic_messages(data):
    u = data.get("usage", {})
    return {
        "prompt_tokens": u.get("input_tokens", 0),
        "completion_tokens": u.get("output_tokens", 0),
        "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
    }


# ── openai-responses ────────────────────────────────────────────────

def build_openai_responses(model, sys_prompt, user_prompt, temp, max_tokens, api_key, provider):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "input": [{"role": "user", "content": user_prompt}],
        "temperature": temp,
        "max_output_tokens": max_tokens,
    }
    if sys_prompt:
        payload["instructions"] = sys_prompt
    return headers, payload, None


def extract_openai_responses(data):
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    return c.get("text")
    return None


def usage_openai_responses(data):
    u = data.get("usage", {})
    return {
        "prompt_tokens": u.get("input_tokens", 0),
        "completion_tokens": u.get("output_tokens", 0),
        "total_tokens": u.get("total_tokens",
                              u.get("input_tokens", 0) + u.get("output_tokens", 0)),
    }


# ── Registry ────────────────────────────────────────────────────────
# Maps protocol name → (build_fn, extract_fn, usage_fn).
# Add new entries here to enable additional protocols.

PROTOCOL_REGISTRY = {
    "openai-chat":        (build_openai_chat,        extract_openai_chat,        usage_openai_chat),
    "anthropic-messages": (build_anthropic_messages, extract_anthropic_messages, usage_anthropic_messages),
    "openai-responses":   (build_openai_responses,   extract_openai_responses,   usage_openai_responses),
}

SUPPORTED_PROTOCOLS = tuple(PROTOCOL_REGISTRY)

DEFAULT_ENDPOINTS = {
    "openai-chat":        "https://openrouter.ai/api/v1/chat/completions",
    "anthropic-messages": "https://api.anthropic.com/v1/messages",
    "openai-responses":   "https://api.openai.com/v1/responses",
}


def build_request(protocol, model, sys_prompt, user_prompt, temp, max_tokens,
                  api_key, provider):
    """Dispatch to the per-protocol builder.

    Returns:
        (headers, payload, extract_fn, usage_fn, messages_or_None)
    """
    try:
        builder, extract, usage_fn = PROTOCOL_REGISTRY[protocol]
    except KeyError:
        raise ValueError(f"Unknown protocol: {protocol!r}. Supported: {SUPPORTED_PROTOCOLS}")
    headers, payload, messages = builder(
        model, sys_prompt, user_prompt, temp, max_tokens, api_key, provider,
    )
    return headers, payload, extract, usage_fn, messages


def default_endpoint(protocol, api_base=None):
    """Return `api_base` if set, otherwise the documented default for `protocol`."""
    if api_base:
        return api_base
    try:
        return DEFAULT_ENDPOINTS[protocol]
    except KeyError:
        raise ValueError(f"Unknown protocol: {protocol!r}. Supported: {SUPPORTED_PROTOCOLS}")
