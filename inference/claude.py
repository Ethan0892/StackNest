"""
inference/claude.py — Anthropic Claude API client for StackNest.

Used as a high-quality generation backend when CLAUDE_API_KEY is set.
Claude (claude-3-5-haiku / claude-3-5-sonnet) produces significantly better
Paper 26.1-targeted plugin code than a fine-tuned 3B local model, with no GPU needed.

Requirements:
  pip install anthropic
  Set CLAUDE_API_KEY in your .env (from https://console.anthropic.com/)

Optional env vars:
  CLAUDE_MODEL         — Model ID  (default: claude-haiku-4-5-20251001)
  CLAUDE_FALLBACK_ONLY — If 'true', only use Claude when local model fails
                         (default: false → Claude is used as primary when key is set)
  CLAUDE_MAX_TOKENS    — Max output tokens (default: 16384)
                         claude-haiku-4-5 supports up to 64k output; 16384 covers
                         the largest multi-file plugins without hitting the limit.
"""

import logging
import os
import random
import time

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
CLAUDE_API_KEY     = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL       = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_FALLBACK_ONLY = os.getenv("CLAUDE_FALLBACK_ONLY", "false").lower() == "true"
CLAUDE_MAX_TOKENS  = int(os.getenv("CLAUDE_MAX_TOKENS", "32768"))
CLAUDE_MAX_RETRIES = int(os.getenv("CLAUDE_MAX_RETRIES", "2"))

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
    """Return True if CLAUDE_API_KEY is configured."""
    return bool(CLAUDE_API_KEY)


def is_primary() -> bool:
    """Return True if Claude should be used as the primary backend (not fallback-only)."""
    return is_available() and not CLAUDE_FALLBACK_ONLY


def _is_hard_quota_error(exc: Exception) -> bool:
    """True only for hard billing/quota exhaustion — marks all workers down immediately."""
    msg = str(exc).lower()
    return (
        "credit balance" in msg
        or "billing" in msg
        or "spend limit" in msg
    )


def _is_transient_rate_limit(exc: Exception) -> bool:
    """True for transient overload/429 — should NOT immediately mark the backend down."""
    msg = str(exc).lower()
    return (
        "rate" in msg
        or "429" in str(exc)
        or "overloaded" in msg
    )


def _notify_watchdog(success: bool, error: str = "", is_quota: bool = False) -> None:
    try:
        if success:
            from inference.watchdog import report_cloud_success  # noqa: PLC0415
            report_cloud_success("claude")
        else:
            from inference.watchdog import report_cloud_failure  # noqa: PLC0415
            report_cloud_failure("claude", error, is_quota=is_quota)
    except Exception:
        pass


def _get_client():
    """Create an Anthropic client, raising a clean RuntimeError on import failure."""
    if not CLAUDE_API_KEY:
        raise RuntimeError("CLAUDE_API_KEY is not set")
    try:
        import anthropic
        return anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    except ImportError:
        raise RuntimeError(
            "The 'anthropic' package is not installed. "
            "Run: pip install anthropic"
        )


def claude_generate(instruction: str, system_prompt: str) -> str:
    """
    Generate plugin code using the Anthropic Claude API.
    Retries up to CLAUDE_MAX_RETRIES times on transient errors.
    Aborts immediately on auth errors (no point retrying).
    """
    import anthropic  # noqa: PLC0415

    client = _get_client()
    last_exc: Exception | None = None

    for attempt in range(CLAUDE_MAX_RETRIES + 1):
        try:
            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": instruction}],
            ) as stream:
                text = stream.get_final_text()
                final_msg = stream.get_final_message()

            text = text.strip()
            if not text:
                raise RuntimeError("Claude returned an empty response")
            # Detect truncation — stop_reason 'max_tokens' means output was cut off mid-file.
            # Rather than raising (which discards all generated code and causes "All backends
            # failed"), return the partial text.  The feedback_loop will detect
            # "reached end of file while parsing" from javac, invoke _close_open_braces /
            # _surgical_truncation_heal, or fall back to compact regeneration on another
            # backend — all of which need the partial output to work from.
            if getattr(final_msg, "stop_reason", None) == "max_tokens":
                log.warning(
                    "[Claude] stop_reason=max_tokens — output truncated at %d chars; "
                    "returning partial output for feedback-loop healing",
                    len(text),
                )
                _notify_watchdog(success=True)
                return text
            log.info("[Claude] Generated %d chars using %s", len(text), CLAUDE_MODEL)
            _notify_watchdog(success=True)
            return text

        except anthropic.AuthenticationError as exc:
            _notify_watchdog(success=False, error=str(exc))
            raise RuntimeError("Claude API key is invalid or expired") from exc

        except Exception as exc:
            last_exc = exc
            is_hard_quota = _is_hard_quota_error(exc)
            is_transient  = _is_transient_rate_limit(exc)
            # Hard quota (billing exhausted): mark ALL workers down immediately.
            # Transient 429/overloaded: only increment in-process failure counter.
            _notify_watchdog(success=False, error=str(exc), is_quota=is_hard_quota)
            if is_hard_quota:
                raise RuntimeError(f"Claude quota exhausted: {exc}") from exc
            if is_transient:
                # Per-minute rolling window — extract retry-after or default to 65s
                wait = 65
                try:
                    ra = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
                    if ra:
                        wait = int(ra) + 2
                except Exception:
                    pass
                if attempt < CLAUDE_MAX_RETRIES:
                    log.warning("[Claude] Rate-limited (transient), waiting %ds before retry %d/%d: %s",
                                wait, attempt + 1, CLAUDE_MAX_RETRIES + 1, exc)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Claude rate-limited — exhausted retries: {exc}") from exc
            if attempt < CLAUDE_MAX_RETRIES:
                base   = 2 ** attempt
                jitter = random.uniform(0, base * 0.4)
                wait   = base + jitter
                log.warning("[Claude] Attempt %d/%d failed (%s). Retrying in %.1fs.",
                            attempt + 1, CLAUDE_MAX_RETRIES + 1, exc, wait)
                time.sleep(wait)

    raise RuntimeError(
        f"Claude API failed after {CLAUDE_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc


def claude_heal(code: str, errors: list[str], instruction: str) -> str:
    """
    Use Claude to fix build/compile errors in generated plugin code.
    Retries on transient errors, aborts on auth/quota errors.
    """
    import anthropic  # noqa: PLC0415

    client = _get_client()
    # Choose system prompt based on platform: Velocity vs Paper
    system = _HEAL_SYSTEM_VELOCITY if "import com.velocitypowered." in code else _HEAL_SYSTEM
    error_block = "\n".join(f"- {e}" for e in errors)
    user_msg = (
        f"Original request: {instruction}\n\n"
        f"Current plugin code:\n{code}\n\n"
        f"Build errors to fix:\n{error_block}"
    )

    last_exc: Exception | None = None
    for attempt in range(CLAUDE_MAX_RETRIES + 1):
        try:
            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_msg}],
            ) as stream:
                text = stream.get_final_text()

            text = text.strip()
            if not text:
                raise RuntimeError("Claude returned empty heal response")
            _notify_watchdog(success=True)
            return text

        except anthropic.AuthenticationError as exc:
            _notify_watchdog(success=False, error=str(exc))
            raise RuntimeError("Claude API key is invalid or expired") from exc

        except Exception as exc:
            last_exc = exc
            is_hard_quota_h = _is_hard_quota_error(exc)
            is_transient_h  = _is_transient_rate_limit(exc)
            _notify_watchdog(success=False, error=str(exc), is_quota=is_hard_quota_h)
            if is_hard_quota_h:
                raise RuntimeError(f"Claude quota exhausted: {exc}") from exc
            if is_transient_h:
                wait = 65
                try:
                    ra = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
                    if ra:
                        wait = int(ra) + 2
                except Exception:
                    pass
                if attempt < CLAUDE_MAX_RETRIES:
                    log.warning("[Claude] Heal rate-limited (transient), waiting %ds before retry %d/%d: %s",
                                wait, attempt + 1, CLAUDE_MAX_RETRIES + 1, exc)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Claude heal rate-limited — exhausted retries: {exc}") from exc
            if attempt < CLAUDE_MAX_RETRIES:
                base   = 2 ** attempt
                jitter = random.uniform(0, base * 0.4)
                wait   = base + jitter
                log.warning("[Claude] Heal attempt %d/%d failed (%s). Retrying in %.1fs.",
                            attempt + 1, CLAUDE_MAX_RETRIES + 1, exc, wait)
                time.sleep(wait)

    raise RuntimeError(
        f"Claude heal failed after {CLAUDE_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc


def is_available() -> bool:
    """Return True if CLAUDE_API_KEY is configured."""
    return bool(CLAUDE_API_KEY)


def is_primary() -> bool:
    """Return True if Claude should be used as the primary backend (not fallback-only)."""
    return is_available() and not CLAUDE_FALLBACK_ONLY
