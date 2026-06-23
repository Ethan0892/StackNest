"""
inference/groq.py — Groq API client for StackNest.

Groq runs open-source models on custom LPU hardware at extreme speed.
The FREE tier (no credit card) includes:
  - llama-3.3-70b-versatile: 30 RPM, 6000 TPM, 500K tokens/day
  - qwen-qwq-32b: 30 RPM, 6000 TPM, 500K tokens/day (strong at code/reasoning)

Perfect as a Kimi replacement when Kimi is suspended — fast, free, reliable.

Requirements:
  pip install requests  (already in requirements.txt)
  Set GROQ_API_KEY in your .env
  (free key at https://console.groq.com — no credit card needed)

Optional env vars:
  GROQ_MODEL      — model ID (default: llama-3.3-70b-versatile)
  GROQ_BASE_URL   — API base (default: https://api.groq.com/openai/v1)
  GROQ_MAX_TOKENS — max output tokens (default: 8192)
"""

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL      = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL   = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "8192"))

# Rate-limit back-off: Groq free tier is 30 RPM / 6000 TPM.
# On 429, sleep and retry once before failing over.
_GROQ_RETRY_SLEEP = 8  # seconds


def is_available() -> bool:
    return bool(GROQ_API_KEY)


# --------------------------------------------------------------------------- #
# Core HTTP helper                                                             #
# --------------------------------------------------------------------------- #

def _post(
    system: str,
    user: str,
    max_tokens: int = GROQ_MAX_TOKENS,
    temperature: float = 0.1,
) -> str:
    """Send a single chat completion request to the Groq API."""
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to your .env file."
        )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    resp = requests.post(
        f"{GROQ_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        timeout=60,
    )

    # Groq free tier 429 = rate limit, not billing.  Retry once after a short sleep.
    if resp.status_code == 429:
        err_text = resp.text.lower()
        if "rate" in err_text or "limit" in err_text:
            log.warning("[Groq] Rate-limited — sleeping %ds before retry.", _GROQ_RETRY_SLEEP)
            time.sleep(_GROQ_RETRY_SLEEP)
            resp = requests.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
        # Daily quota exhausted — mark unhealthy so workers skip for a few hours
        elif "quota" in err_text or "daily" in err_text or "exceeded" in err_text:
            try:
                from inference.watchdog import _write_cross_proc_down
                _write_cross_proc_down("groq", f"daily quota: {resp.text[:80]}")
            except Exception:
                pass
            resp.raise_for_status()

    resp.raise_for_status()

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("Groq returned an empty response.")
    return content.strip()


# --------------------------------------------------------------------------- #
# Public generation function (matches the signature used by server.py)        #
# --------------------------------------------------------------------------- #

def groq_generate(
    instruction: str,
    system_prompt: str,
    max_tokens: int = GROQ_MAX_TOKENS,
) -> str:
    """
    Generate plugin / mod code via Groq (Llama 3.3 70B by default).

    Raises RuntimeError on API failure so server.py can fall through to
    the next backend.
    """
    try:
        return _post(
            system=system_prompt,
            user=instruction,
            max_tokens=max_tokens,
            temperature=0.1,
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body   = exc.response.text[:200] if exc.response is not None else ""
        raise RuntimeError(f"Groq API HTTP error: {status} — {body}") from exc
    except requests.Timeout:
        raise RuntimeError("Groq API timed out after 60 s.")
    except Exception as exc:
        raise RuntimeError(f"Groq API error: {exc}") from exc
