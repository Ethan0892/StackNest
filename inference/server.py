"""
inference/server.py — Cloud-only inference router.

Local llama.cpp has been removed — the server has no GPU and insufficient
CPU/RAM for on-device inference.

Priority order
──────────────
Free tier:    Kimi (k2-turbo) → Gemini → Claude
Premium tier: Claude → Kimi → Gemini

Public API is unchanged so all callers (feedback_loop, app.py) work as-is:
  GenerationParams, generate_with_fallback, generate_stream,
  health_check, get_stats, _cb, get_model_info
"""

from dataclasses import dataclass, field
from typing import Generator


# ── Params dataclass (kept for API compat) ─────────────────────────────────── #
@dataclass
class GenerationParams:
    temperature:    float = 0.2
    top_p:          float = 0.95
    top_k:          int   = 40
    repeat_penalty: float = 1.1
    max_tokens:     int   = 1400
    stop: list = field(default_factory=lambda: ["<|im_end|>", "###", "---END---"])


# ── Stubs so app.py health endpoint doesn't crash ─────────────────────────── #
_cb = {"state": "DISABLED"}


def health_check(timeout: float = 10.0) -> bool:
    """No local server — always False."""
    return False


def get_stats() -> dict:
    return {
        "requests":       0,
        "local_ok":       0,
        "local_fail":     0,
        "local_skipped":  0,
        "avg_latency_ms": 0.0,
        "circuit_state":  "DISABLED",
        "cb_open_until":  0.0,
        "note":           "cloud-only mode",
    }


def get_model_info() -> dict:
    return {"status": "cloud-only", "local_model": None}


# ── Core: cloud-only generation ────────────────────────────────────────────── #
def generate_with_fallback(
    prompt: str,
    params: GenerationParams | None = None,
    *,
    system_prompt: str | None = None,
    instruction: str | None = None,
    tier: str = "free",
    force_cloud: bool = False,          # kept for compat — always cloud now
    exclude_backends: frozenset[str] | None = None,  # skip backends that truncated
    complexity: str = "simple",         # 'simple' | 'medium' | 'complex'
) -> tuple[str, str]:
    """
    Generate plugin code using cloud APIs only.

    Free tier priority:    Kimi -> Gemini -> Claude
    Premium tier priority: Claude -> Kimi -> Gemini

    For free-tier + complex requests Gemini is tried last (its 6000-token cap
    risks truncation on large plugins); Kimi leads with its 8192-token budget.

    Returns (generated_text, source_label).
    """
    if instruction is None or system_prompt is None:
        raise RuntimeError(
            "'instruction' and 'system_prompt' must be provided for cloud generation."
        )

    errors: list[str] = []
    backends = _premium_backends() if tier == "premium" else _free_backends(complexity)

    def _try_backend(label, is_avail, gen_fn) -> "str | None":
        """Try one backend. Returns generated text or None. Appends to errors on failure."""
        if not is_avail():
            print(f"[{label}] API key not set — skipping.")
            return None
        try:
            from inference.watchdog import is_healthy as _wh
            if not _wh(label.lower()):
                print(f"[{label}] Watchdog reports unhealthy — skipping.")
                errors.append(f"{label}: skipped (unhealthy)")
                return None
        except Exception:
            pass
        try:
            result = gen_fn(instruction, system_prompt)
            if result.strip():
                print(f"[Inference] Generated via {label}.")
                return result
            print(f"[{label}] Empty response — trying next backend.")
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            print(f"[{label}] Failed ({exc}) — trying next backend.")
        return None

    skipped_excluded: list = []
    for label, is_avail, gen_fn in backends:
        if exclude_backends and label.lower() in exclude_backends:
            print(f"[{label}] Excluded (truncated last attempt) — trying next backend.")
            skipped_excluded.append((label, is_avail, gen_fn))
            continue
        result = _try_backend(label, is_avail, gen_fn)
        if result is not None:
            return result, label

    # Last resort: if all non-excluded backends are down, retry excluded ones.
    # This prevents a total blackout when e.g. Gemini truncated AND Kimi/Claude
    # are quota-suspended — Gemini truncating is better than total failure.
    if skipped_excluded:
        print(f"[Inference] All preferred backends failed — retrying excluded backends as last resort.")
        for label, is_avail, gen_fn in skipped_excluded:
            result = _try_backend(label, is_avail, gen_fn)
            if result is not None:
                return result, f"{label}(last-resort)"

    raise RuntimeError(
        "All backends failed for tier='{}': {}".format(
            tier, "; ".join(errors) if errors else "no API keys configured"
        )
    )


def _free_backends(complexity: str = "simple"):
    from inference.gemini   import is_available as gemini_ok,   gemini_generate    # noqa
    from inference.kimi     import is_available as kimi_ok,     kimi_generate      # noqa
    from inference.claude   import is_available as claude_ok,   claude_generate    # noqa
    from inference.deepseek import is_available as deepseek_ok, deepseek_generate  # noqa
    from inference.groq     import is_available as groq_ok,     groq_generate      # noqa
    # Cap Gemini at 6000 tokens for simple/medium free-tier requests (~350 lines).
    # For complex requests, raise to 8192 tokens to reduce truncation risk.
    _FREE_GEMINI_TOKENS    = 6000
    _COMPLEX_GEMINI_TOKENS = 8192
    gemini_limit = _COMPLEX_GEMINI_TOKENS if complexity == "complex" else _FREE_GEMINI_TOKENS
    def _gemini_free(instruction: str, system_prompt: str) -> str:
        return gemini_generate(instruction, system_prompt, max_tokens=gemini_limit)

    if complexity == "complex":
        # Complex: lead with highest-budget models to avoid truncation.
        # DeepSeek V3 has 8192 output token budget and is excellent at Java.
        # Groq (Llama 3.3 70B) is free and fast as final safety net.
        return [
            ("deepseek", deepseek_ok, deepseek_generate),
            ("kimi",     kimi_ok,     kimi_generate),
            ("gemini",   gemini_ok,   _gemini_free),
            ("claude",   claude_ok,   claude_generate),
            ("groq",     groq_ok,     groq_generate),
        ]
    return [
        ("gemini",   gemini_ok,   _gemini_free),
        ("deepseek", deepseek_ok, deepseek_generate),
        ("kimi",     kimi_ok,     kimi_generate),
        ("claude",   claude_ok,   claude_generate),
        ("groq",     groq_ok,     groq_generate),
    ]


def _premium_backends():
    from inference.claude   import is_available as claude_ok,   claude_generate    # noqa
    from inference.deepseek import is_available as deepseek_ok, deepseek_generate  # noqa
    from inference.kimi     import is_available as kimi_ok,     kimi_generate      # noqa
    from inference.gemini   import is_available as gemini_ok,   gemini_generate    # noqa
    from inference.groq     import is_available as groq_ok,     groq_generate      # noqa
    return [
        ("claude",   claude_ok,   claude_generate),
        ("deepseek", deepseek_ok, deepseek_generate),
        ("kimi",     kimi_ok,     kimi_generate),
        ("gemini",   gemini_ok,   gemini_generate),
        ("groq",     groq_ok,     groq_generate),
    ]


def generate_stream(
    prompt: str,
    params: GenerationParams | None = None,
) -> Generator[str, None, None]:
    """
    Simulate streaming for cloud generation.
    Extracts the instruction from the built prompt, generates via cloud,
    then yields the result in small chunks for SSE display.
    """
    instruction = _extract_instruction(prompt)
    from inference.router import SYSTEM_PROMPT  # noqa
    try:
        text, _ = generate_with_fallback(
            prompt, params,
            system_prompt=SYSTEM_PROMPT,
            instruction=instruction,
        )
    except Exception as exc:
        raise RuntimeError(f"Cloud generation failed: {exc}") from exc

    # Yield in ~8-word chunks to simulate token streaming on the frontend
    words = text.split(" ")
    chunk: list[str] = []
    for word in words:
        chunk.append(word)
        if len(chunk) >= 8:
            yield " ".join(chunk) + " "
            chunk = []
    if chunk:
        yield " ".join(chunk)


def _extract_instruction(prompt: str) -> str:
    """Pull the user instruction out of a built prompt string."""
    marker = "### Instruction:"
    idx = prompt.rfind(marker)
    if idx != -1:
        after = prompt[idx + len(marker):].strip()
        end = after.find("### Response:")
        return after[:end].strip() if end != -1 else after
    lines = [ln.strip() for ln in prompt.splitlines() if ln.strip()]
    return lines[-1] if lines else prompt[:500]
