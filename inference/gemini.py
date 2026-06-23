"""
inference/gemini.py — Google Gemini API client for StackNest.

Used as the secondary generation backend for FREE-tier users when the local
fine-tuned model is unavailable.  Gemini 2.0 Flash has a very generous free
quota (1 500 req/day on the free tier), so this costs nothing for typical
community usage.

Requirements:
  pip install google-genai
  Set GEMINI_API_KEY in your .env
  (get a key for free at https://aistudio.google.com/app/apikey)

Optional env vars:
  GEMINI_MODEL      — model ID  (default: gemini-2.0-flash)
  GEMINI_MAX_TOKENS — max output tokens (default: 8192)
"""

import logging
import os
import random
import time
import threading

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_MAX_TOKENS = int(os.getenv("GEMINI_MAX_TOKENS", "16384"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))

# ── Quota / rate-limit tracking ───────────────────────────────────────────────
# Gemini free tier: 1500 req/day, 15 RPM.  Track consecutive 429s to detect
# daily exhaustion; reset counter once per 24h.
_quota_lock        = threading.Lock()
_quota_fails       = 0
_quota_reset_ts    = time.time() + 86400   # next midnight-ish reset
_QUOTA_FAIL_LIMIT  = 3                     # 3 consecutive 429s → mark exhausted


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc)
    msg = s.lower()
    return (
        "429" in s
        or "402" in s
        or "quota" in msg
        or "resource_exhausted" in msg
        or "rate" in msg
        or "billing" in msg
        or "spend" in msg
        or "permission_denied" in msg
        or "billing_disabled" in msg
    )


def _record_quota_fail() -> None:
    global _quota_fails, _quota_reset_ts
    with _quota_lock:
        now = time.time()
        if now >= _quota_reset_ts:
            _quota_fails = 0
            _quota_reset_ts = now + 86400
            log.info("[Gemini] Quota counter reset (24h window elapsed).")
        _quota_fails += 1
        if _quota_fails >= _QUOTA_FAIL_LIMIT:
            log.warning("[Gemini] Daily quota likely exhausted (%d consecutive 429s).", _quota_fails)
            try:
                from inference.watchdog import report_cloud_failure  # noqa: PLC0415
                report_cloud_failure("gemini", "Quota exhausted", is_quota=True)
            except Exception:
                pass
            # Send admin email alert (fire-and-forget, non-blocking)
            try:
                import threading as _t
                from api.mailer import send_quota_alert  # noqa: PLC0415
                _t.Thread(
                    target=send_quota_alert,
                    args=("gemini", f"{_quota_fails} consecutive quota/billing errors"),
                    daemon=True,
                ).start()
            except Exception:
                pass


def _record_success() -> None:
    global _quota_fails
    with _quota_lock:
        _quota_fails = 0
    try:
        from inference.watchdog import report_cloud_success  # noqa: PLC0415
        report_cloud_success("gemini")
    except Exception:
        pass

_HEAL_SYSTEM = (
    "You are a senior Java developer specialising in Paper 26.1 plugin development.\n"
    "You will receive plugin source code (in ```java / ```yaml fenced blocks) and a list of build errors.\n"
    "Your ONLY output: the COMPLETE corrected plugin, using the same ```java / ```yaml block structure.\n"
    "Rules:\n"
    "- The VERY FIRST character of your response must be a backtick (start of ```java). No preamble.\n"
    "- Fix EVERY listed error. Do NOT introduce new errors or remove any functionality.\n"
    "- Preserve ALL features and logic — do not simplify or stub out anything.\n"
    "- If the code appears truncated/unfinished, complete the missing sections.\n"
    "- Use Adventure API (net.kyori.adventure.text.Component) for all text — never ChatColor or BungeeCord TextComponent.\n"
    "- api-version in plugin.yml must be '1.21'. NEVER use deprecated bukkit APIs.\n"
    "- No explanations, diffs, or comments after the closing ``` — the response ends with ```.\n"
)

_HEAL_SYSTEM_VELOCITY = (
    "You are a senior Java developer specialising in Velocity proxy plugin development.\n"
    "You will receive plugin source code (in ```java fenced blocks) and a list of build errors.\n"
    "Your ONLY output: the COMPLETE corrected plugin, using the same ```java block structure.\n"
    "Rules:\n"
    "- The VERY FIRST character of your response must be a backtick (start of ```java). No preamble.\n"
    "- Fix EVERY listed error. Do NOT introduce new errors or remove any functionality.\n"
    "- Preserve ALL features and logic — do not simplify or stub out anything.\n"
    "- If the code appears truncated/unfinished, complete the missing sections.\n"
    "- Event handlers use @Subscribe (com.velocitypowered.api.event.Subscribe), NOT @EventHandler.\n"
    "- Velocity uses Guice DI — constructor injection with @Inject, NOT JavaPlugin extends.\n"
    "- There is NO plugin.yml — metadata lives in the @Plugin annotation on the main class.\n"
    "- Do NOT import org.bukkit.*, org.spigotmc.*, or io.papermc.* in Velocity plugins.\n"
    "- No explanations, diffs, or comments after the closing ``` — the response ends with ```.\n"
)


def is_available() -> bool:
    """Return True if GEMINI_API_KEY is configured."""
    return bool(GEMINI_API_KEY)


def _client():
    """Create and return a google-genai client.  Raises RuntimeError if not installed."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    try:
        from google import genai
        return genai.Client(api_key=GEMINI_API_KEY)
    except ImportError:
        raise RuntimeError(
            "google-genai package is not installed. "
            "Run: pip install google-genai"
        )


def gemini_generate(instruction: str, system_prompt: str, max_tokens: int | None = None) -> str:
    """
    Generate plugin code using the Google Gemini API.
    Retries up to GEMINI_MAX_RETRIES times with jittered exponential backoff.
    Pass max_tokens to override GEMINI_MAX_TOKENS (e.g. for free-tier budget cap).
    """
    from google.genai import types  # noqa: PLC0415
    _max_tok = max_tokens if max_tokens is not None else GEMINI_MAX_TOKENS

    client = _client()
    last_exc: Exception | None = None

    for attempt in range(GEMINI_MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=_max_tok,
                    temperature=0.2,
                ),
                contents=instruction,
            )
            text = response.text or ""
            if not text.strip():
                raise RuntimeError("Gemini returned an empty response")
            # Detect output truncation — finish_reason MAX_TOKENS means the model
            # hit the token ceiling mid-generation.  Raise so server.py falls through
            # to the next backend (Kimi / Claude) instead of returning broken Java.
            try:
                finish = response.candidates[0].finish_reason
                if finish and str(finish).upper() in ("MAX_TOKENS", "2"):
                    log.warning("[Gemini] finish_reason=MAX_TOKENS — output truncated at %d chars; falling back", len(text))
                    raise RuntimeError(
                        f"Gemini output truncated (finish_reason=MAX_TOKENS) at {len(text)} chars "
                        "— falling back to next backend"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass
            log.debug("[Gemini] generated %d chars", len(text))
            _record_success()
            return text
        except Exception as exc:
            last_exc = exc
            if _is_quota_error(exc):
                _record_quota_fail()
                # Don't retry quota errors — they won't resolve in seconds
                raise RuntimeError(f"Gemini quota/rate-limit error: {exc}") from exc
            base   = 2 ** attempt
            jitter = random.uniform(0, base * 0.4)
            wait   = base + jitter
            log.warning("[Gemini] Attempt %d/%d failed (%s). Retrying in %.1fs.",
                        attempt + 1, GEMINI_MAX_RETRIES, exc, wait)
            try:
                from inference.watchdog import report_cloud_failure  # noqa: PLC0415
                report_cloud_failure("gemini", str(exc))
            except Exception:
                pass
            if attempt < GEMINI_MAX_RETRIES - 1:
                time.sleep(wait)

    raise RuntimeError(f"Gemini API error after {GEMINI_MAX_RETRIES} attempts: {last_exc}") from last_exc


def gemini_heal(code: str, errors: list[str], instruction: str) -> str:
    """
    Ask Gemini to fix compile/build errors in generated plugin code.
    Retries on transient failures, aborts immediately on quota errors.
    """
    from google.genai import types  # noqa: PLC0415

    client = _client()
    # Choose system prompt based on platform: Velocity vs Paper
    system = _HEAL_SYSTEM_VELOCITY if "import com.velocitypowered." in code else _HEAL_SYSTEM
    err_block = "\n".join(f"- {e}" for e in errors)
    user_msg = (
        f"Plugin code:\n{code}\n\n"
        f"Build errors to fix:\n{err_block}\n\n"
        f"Original request: {instruction}"
    )

    last_exc: Exception | None = None
    for attempt in range(GEMINI_MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=GEMINI_MAX_TOKENS,
                    temperature=0.1,
                ),
                contents=user_msg,
            )
            result = (response.text or "").strip()
            if result:
                _record_success()
                return result
            raise RuntimeError("Gemini returned empty heal response")
        except Exception as exc:
            last_exc = exc
            if _is_quota_error(exc):
                _record_quota_fail()
                raise RuntimeError(f"Gemini heal quota error: {exc}") from exc
            base   = 2 ** attempt
            jitter = random.uniform(0, base * 0.4)
            wait   = base + jitter
            log.warning("[Gemini] Heal attempt %d/%d failed (%s). Retrying in %.1fs.",
                        attempt + 1, GEMINI_MAX_RETRIES, exc, wait)
            if attempt < GEMINI_MAX_RETRIES - 1:
                time.sleep(wait)

    raise RuntimeError(f"Gemini heal error after {GEMINI_MAX_RETRIES} attempts: {last_exc}") from last_exc


def gemini_simple(system: str, user: str, max_tokens: int = 2048) -> str:
    """
    Generic single-call Gemini request — used for non-code tasks such as
    the Server Setup Assistant (free-tier basic mode).
    Retries on transient errors; aborts immediately on quota errors.
    """
    from google.genai import types  # noqa: PLC0415

    client = _client()
    last_exc: Exception | None = None

    for attempt in range(GEMINI_MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    temperature=0.3,
                ),
                contents=user,
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response")
            _record_success()
            return text
        except Exception as exc:
            last_exc = exc
            if _is_quota_error(exc):
                _record_quota_fail()
                raise RuntimeError(f"Gemini quota/rate-limit error: {exc}") from exc
            base   = 2 ** attempt
            jitter = random.uniform(0, base * 0.4)
            wait   = base + jitter
            log.warning("[Gemini] simple attempt %d/%d failed (%s). Retrying in %.1fs.",
                        attempt + 1, GEMINI_MAX_RETRIES, exc, wait)
            if attempt < GEMINI_MAX_RETRIES - 1:
                time.sleep(wait)

    raise RuntimeError(f"Gemini simple error after {GEMINI_MAX_RETRIES} attempts: {last_exc}") from last_exc
