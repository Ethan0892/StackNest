"""
api/email.py — Transactional email helper for StackNest.

Sends via SMTP (configurable) or, when SMTP is not configured, prints the
link to stdout so development works without an email server.

Required env vars for live email:
  SMTP_HOST      e.g. smtp.mailgun.org
  SMTP_PORT      e.g. 587
  SMTP_USER      your SMTP username / login
  SMTP_PASS      your SMTP password
  SMTP_FROM      From address, e.g. noreply@stacknests.com
  APP_BASE_URL   Public URL of the site, e.g. https://stacknests.com

Optional:
  SMTP_TLS       'starttls' (default) | 'ssl' | 'none'
"""

import os
import smtplib
import ssl
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Config (resolved once at import time)
# ---------------------------------------------------------------------------
_SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
_SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
_SMTP_USER = os.getenv("SMTP_USER", "").strip()
_SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
_SMTP_FROM = os.getenv("SMTP_FROM", "noreply@stacknests.com").strip()
_SMTP_TLS  = os.getenv("SMTP_TLS", "starttls").strip().lower()
_BASE_URL  = os.getenv("APP_BASE_URL", "http://localhost:5000").rstrip("/")
_ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip() or _SMTP_FROM

_CONFIGURED = bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASS)


def _resolve_base_url(request_base_url: str | None = None) -> str:
  """Pick a safe public URL for email links.

  Priority:
  1) explicit APP_BASE_URL env
  2) per-request host URL from Flask
  3) localhost fallback (dev only)
  """
  env_base = _BASE_URL.strip()
  if env_base and "localhost" not in env_base and "127.0.0.1" not in env_base:
    return env_base
  if request_base_url:
    return request_base_url.rstrip("/")
  return env_base or "http://localhost:5000"


# ---------------------------------------------------------------------------
# Core send helper
# ---------------------------------------------------------------------------
def _send(to: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = _SMTP_FROM
    msg["To"]      = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html",  "utf-8"))

    ctx = ssl.create_default_context()
    if _SMTP_TLS == "ssl":
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, context=ctx) as s:
            s.login(_SMTP_USER, _SMTP_PASS)
            s.sendmail(_SMTP_FROM, to, msg.as_string())
    elif _SMTP_TLS == "starttls":
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(_SMTP_USER, _SMTP_PASS)
            s.sendmail(_SMTP_FROM, to, msg.as_string())
    else:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as s:
            s.login(_SMTP_USER, _SMTP_PASS)
            s.sendmail(_SMTP_FROM, to, msg.as_string())


def _dev_log(to: str, subject: str, text: str) -> None:
    print(f"\n{'='*60}")
    print(f"[DEV EMAIL] To: {to}")
    print(f"[DEV EMAIL] Subject: {subject}")
    print(text)
    print("="*60 + "\n", flush=True)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def send_verification_email(to: str, display_name: str, token: str, request_base_url: str | None = None) -> bool:
    """Send account verification email. Returns True on success."""
    base_url = _resolve_base_url(request_base_url)
    link = f"{base_url}/verify?token={token}"

    subject = "Verify your StackNest account"
    text = textwrap.dedent(f"""\
        Hi {display_name},

        Thanks for signing up to StackNest!

        Please verify your email address by visiting the link below.
        This link expires in 48 hours.

        {link}

        If you didn't create an account, you can safely ignore this email.

        — The StackNest Team
    """)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0">
  <tr><td align="center" style="padding:40px 20px">
    <table width="520" cellpadding="0" cellspacing="0"
           style="background:#1a1d26;border:1px solid #2e3350;border-radius:14px;overflow:hidden">
      <tr>
        <td style="padding:28px 32px 20px;border-bottom:1px solid #2e3350">
          <span style="font-size:1.2rem;font-weight:700;color:#e2e4f0">Stack<span style="color:#5c6fff">Nest</span></span>
        </td>
      </tr>
      <tr>
        <td style="padding:28px 32px">
          <p style="color:#b0b3c8;font-size:0.9rem;margin:0 0 10px">Hi {display_name},</p>
          <p style="color:#e2e4f0;font-size:1rem;font-weight:600;margin:0 0 10px">Verify your email address</p>
          <p style="color:#7f85a3;font-size:0.85rem;margin:0 0 22px;line-height:1.6">
            Thanks for signing up! Click the button below to verify your email. The link expires in 48 hours.
          </p>
          <a href="{link}"
             style="display:inline-block;background:#5c6fff;color:#fff;font-weight:600;
                    font-size:0.9rem;padding:12px 24px;border-radius:8px;text-decoration:none">
            Verify Email Address
          </a>
          <p style="color:#7f85a3;font-size:0.75rem;margin:20px 0 0;line-height:1.6">
            Or copy this link:<br/>
            <a href="{link}" style="color:#5c6fff;word-break:break-all">{link}</a>
          </p>
        </td>
      </tr>
      <tr>
        <td style="padding:16px 32px;border-top:1px solid #2e3350">
          <p style="color:#7f85a3;font-size:0.75rem;margin:0">
            &copy; 2026 StackNest &mdash; If you didn't create an account, ignore this email.
          </p>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

    if not _CONFIGURED:
        _dev_log(to, subject, text)
        return True  # Dev mode — always "succeeds"
    try:
        _send(to, subject, html, text)
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}", flush=True)
        return False


def send_quota_alert(backend: str, detail: str) -> None:
    """
    Send an admin alert when a cloud backend quota or billing cap is exhausted.
    Uses ADMIN_EMAIL env var (falls back to SMTP_FROM). Silent no-op if unconfigured.
    """
    if not _CONFIGURED or not _ADMIN_EMAIL:
        print(f"[QUOTA ALERT] {backend} quota/billing exhausted: {detail}", flush=True)
        return
    subject = f"[StackNest] {backend.title()} quota exhausted"
    text = (
        f"The {backend} API backend has hit its quota or billing cap.\n\n"
        f"Detail: {detail}\n\n"
        "Generations are automatically falling back to the next available backend, "
        "but quality may be reduced.\n\n"
        "Action: check your billing dashboard and increase the spend cap or wait for quota reset."
    )
    html = (
        f"<p>The <strong>{backend}</strong> API backend has hit its quota or billing cap.</p>"
        f"<p><strong>Detail:</strong> {detail}</p>"
        "<p>Generations are falling back to the next available backend automatically, "
        "but quality may be reduced.</p>"
        "<p><strong>Action:</strong> check your billing dashboard and increase the spend cap "
        "or wait for quota reset.</p>"
    )
    try:
        _send(_ADMIN_EMAIL, subject, html, text)
    except Exception as exc:
        print(f"[QUOTA ALERT] Failed to send email: {exc}", flush=True)


def send_password_reset_email(to: str, display_name: str, token: str, request_base_url: str | None = None) -> bool:
    """Send password-reset email. Returns True on success."""
    base_url = _resolve_base_url(request_base_url)
    link = f"{base_url}/reset-password?token={token}"

    subject = "Reset your StackNest password"
    text = textwrap.dedent(f"""\
        Hi {display_name},

        Someone requested a password reset for your StackNest account.
        Click the link below to reset your password. Expires in 1 hour.

        {link}

        If you didn't request this, you can safely ignore this email.

        — The StackNest Team
    """)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0">
  <tr><td align="center" style="padding:40px 20px">
    <table width="520" cellpadding="0" cellspacing="0"
           style="background:#1a1d26;border:1px solid #2e3350;border-radius:14px;overflow:hidden">
      <tr>
        <td style="padding:28px 32px 20px;border-bottom:1px solid #2e3350">
          <span style="font-size:1.2rem;font-weight:700;color:#e2e4f0">Stack<span style="color:#5c6fff">Nest</span></span>
        </td>
      </tr>
      <tr>
        <td style="padding:28px 32px">
          <p style="color:#b0b3c8;font-size:0.9rem;margin:0 0 10px">Hi {display_name},</p>
          <p style="color:#e2e4f0;font-size:1rem;font-weight:600;margin:0 0 10px">Reset your password</p>
          <p style="color:#7f85a3;font-size:0.85rem;margin:0 0 22px;line-height:1.6">
            Click the button below to set a new password. This link expires in 1 hour.
          </p>
          <a href="{link}"
             style="display:inline-block;background:#5c6fff;color:#fff;font-weight:600;
                    font-size:0.9rem;padding:12px 24px;border-radius:8px;text-decoration:none">
            Reset Password
          </a>
        </td>
      </tr>
      <tr>
        <td style="padding:16px 32px;border-top:1px solid #2e3350">
          <p style="color:#7f85a3;font-size:0.75rem;margin:0">
            &copy; 2026 StackNest &mdash; If you didn't request this, ignore this email.
          </p>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

    if not _CONFIGURED:
        _dev_log(to, subject, text)
        return True
    try:
        _send(to, subject, html, text)
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}", flush=True)
        return False


def send_ticket_reply(to: str, subject: str, reply_body: str, ticket_id: int) -> bool:
    """Send an admin reply to a support ticket. Returns True on success."""
    email_subject = f"Re: {subject} [Ticket #{ticket_id}]"
    safe_body = reply_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    text = textwrap.dedent(f"""\
        Hi,

        You have a reply to your support ticket #{ticket_id}: {subject}

        {reply_body}

        ---
        Reply to this email or visit stacknests.com/support if you have further questions.

        \u2014 The StackNest Team
    """)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0">
  <tr><td align="center" style="padding:40px 20px">
    <table width="520" cellpadding="0" cellspacing="0"
           style="background:#1a1d26;border:1px solid #2e3350;border-radius:14px;overflow:hidden">
      <tr>
        <td style="padding:28px 32px 20px;border-bottom:1px solid #2e3350">
          <span style="font-size:1.2rem;font-weight:700;color:#e2e4f0">Stack<span style="color:#5c6fff">Nest</span></span>
        </td>
      </tr>
      <tr>
        <td style="padding:28px 32px">
          <p style="color:#b0b3c8;font-size:0.85rem;margin:0 0 6px">Reply to ticket <strong style="color:#e2e4f0">#{ticket_id}</strong></p>
          <p style="color:#e2e4f0;font-size:1rem;font-weight:600;margin:0 0 18px">{subject}</p>
          <div style="background:#111420;border:1px solid #2e3350;border-radius:8px;padding:16px 20px;font-size:0.875rem;color:#c8cbde;line-height:1.7;white-space:pre-wrap">
            {safe_body}
          </div>
        </td>
      </tr>
      <tr>
        <td style="padding:16px 32px;border-top:1px solid #2e3350">
          <p style="color:#7f85a3;font-size:0.75rem;margin:0">
            &copy; 2026 StackNest &mdash; Need more help? Visit <a href="{_BASE_URL}/support" style="color:#5c6fff">stacknests.com/support</a>
          </p>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
    if not _CONFIGURED:
        _dev_log(to, email_subject, text)
        return True
    try:
        _send(to, email_subject, html, text)
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}", flush=True)
        return False
