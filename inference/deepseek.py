"""
inference/deepseek.py — DeepSeek API client for StackNest.

DeepSeek V3 is an excellent code-generation model at a fraction of the cost of
other providers.  Pricing (2026-06): ~$0.27 / million output tokens — roughly
10-20× cheaper than Claude or Kimi.

API is 100% OpenAI-compatible (same endpoint format as Kimi).

Requirements:
  pip install requests  (already in requirements.txt)
  Set DEEPSEEK_API_KEY in your .env
  (get a key at https://platform.deepseek.com/)

Optional env vars:
  DEEPSEEK_MODEL      — model ID  (default: deepseek-chat  = DeepSeek V3)
  DEEPSEEK_BASE_URL   — API base  (default: https://api.deepseek.com/v1)
  DEEPSEEK_MAX_TOKENS — max output tokens (default: 8192)
"""

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
DEEPSEEK_API_KEY    = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL      = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")   # = DeepSeek V3
DEEPSEEK_BASE_URL   = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "8192"))


def is_available() -> bool:
    return bool(DEEPSEEK_API_KEY)


# --------------------------------------------------------------------------- #
# Core HTTP helper                                                             #
# --------------------------------------------------------------------------- #

def _post(
    system: str,
    user: str,
    max_tokens: int = 8192,
    temperature: float = 0.1,
) -> str:
    """Send a single chat completion request to the DeepSeek API."""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Add it to your .env file."
        )

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    resp = requests.post(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
    )

    # Billing / quota errors — mark unhealthy so workers stop trying immediately
    if resp.status_code in (402, 429):
        err_text = resp.text.lower()
        if any(k in err_text for k in ("insufficient", "balance", "quota", "billing", "suspended")):
            try:
                from inference.watchdog import _write_cross_proc_down
                _write_cross_proc_down("deepseek", f"billing/quota: {resp.text[:80]}")
            except Exception:
                pass
        resp.raise_for_status()

    resp.raise_for_status()

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("DeepSeek returned an empty response.")
    return content.strip()


# --------------------------------------------------------------------------- #
# Public generation function (matches the signature used by server.py)        #
# --------------------------------------------------------------------------- #

def deepseek_generate(
    instruction: str,
    system_prompt: str,
    max_tokens: int = DEEPSEEK_MAX_TOKENS,
) -> str:
    """
    Generate plugin / mod code via DeepSeek V3.

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
        raise RuntimeError(f"DeepSeek API HTTP error: {status} — {body}") from exc
    except requests.Timeout:
        raise RuntimeError("DeepSeek API timed out after 120 s.")
    except Exception as exc:
        raise RuntimeError(f"DeepSeek API error: {exc}") from exc
