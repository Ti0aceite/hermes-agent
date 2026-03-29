"""Regex-based secret redaction for logs and tool output.

Applies pattern matching to mask API keys, tokens, and credentials
before they reach log files, verbose output, or gateway logs.

Short tokens (< 18 chars) are fully masked. Longer tokens preserve
the first 6 and last 4 characters for debuggability.
"""

import logging
import os
import re
import unicodedata

logger = logging.getLogger(__name__)

# Known API key prefixes -- match the prefix + contiguous token chars
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",          # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
    r"bb_live_[A-Za-z0-9_-]{10,}",      # BrowserBase
    r"gAAAA[A-Za-z0-9_=-]{20,}",        # Codex encrypted tokens
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe secret key (live)
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe secret key (test)
    r"rk_live_[A-Za-z0-9]{10,}",        # Stripe restricted key
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace token
    r"r8_[A-Za-z0-9]{10,}",             # Replicate API token
    r"npm_[A-Za-z0-9]{10,}",            # npm access token
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI API token
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",         # DigitalOcean OAuth
    r"am_[A-Za-z0-9_-]{10,}",           # AgentMail API key
]

# ENV assignment patterns: KEY=value where KEY contains a secret-like name
_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z_]*{_SECRET_ENV_NAMES}[A-Z_]*)\s*=\s*(['\"]?)(\S+)\2",
    re.IGNORECASE,
)

# JSON field patterns: "apiKey": "value", "token": "value", etc.
_JSON_KEY_NAMES = r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer|secret_value|raw_secret|secret_input|key_material)"
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# Authorization headers
_AUTH_HEADER_RE = re.compile(
    r"(Authorization:\s*Bearer\s+)(\S+)",
    re.IGNORECASE,
)

# Telegram bot tokens: bot<digits>:<token> or <digits>:<token>,
# where token part is restricted to [-A-Za-z0-9_] and length >= 30
_TELEGRAM_RE = re.compile(
    r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})",
)

# Private key blocks: -----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# Database connection strings: protocol://user:PASSWORD@host
# Catches postgres, mysql, mongodb, redis, amqp URLs and redacts the password
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)

# E.164 phone numbers: +<country><number>, 7-15 digits
# Negative lookahead prevents matching hex strings or identifiers
_SIGNAL_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")

# Secret-like env var names whose resolved values should be masked when they
# appear elsewhere in logs or tool previews.
_ENV_ASSIGN_RE_NAME = re.compile(
    r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIAL|AUTH)",
    re.IGNORECASE,
)

# Login identifier env vars that belong to application credentials, not generic
# shell variables like USER/LOGNAME. Examples: DENTIDESK_USER, APP_LOGIN.
_SCOPED_LOGIN_ENV_NAME_RE = re.compile(
    r"^(?:[A-Z0-9]+_)+(?:USER|USERNAME|LOGIN|EMAIL)$",
    re.IGNORECASE,
)

# Chilean RUT values commonly appear in Dentidesk responses.
_CHILEAN_RUT_RE = re.compile(
    r"\b(?:\d{1,2}(?:\.\d{3}){2}|\d{7,8})-[\dkK]\b"
)

# Local mobile/phone values as they appear in patient detail rows.
_LOCAL_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?56\s*)?(?:9\s*)?\d{8}(?!\d)"
)

_CLINICAL_PHONE_LABEL_RE = re.compile(
    r"\b(?:celular|tel[eé]fono(?:\s+(?:celular|fijo))?|fono|m[oó]vil)\b",
    re.IGNORECASE,
)

_LABELED_PHONE_RE = re.compile(
    r"(?i)\b(celular|tel[eé]fono(?:\s+(?:celular|fijo))?|fono|m[oó]vil)\b(\s*:?\s*)(?:\+?56\s*)?(?:9\s*)?\d{8}(?!\d)"
)

_PATIENT_LABEL_RE = re.compile(
    r"(?i)(?<![\"'])\b(nombre(?:\s+del)?\s+paciente|paciente)\b(\s*:\s*)([^,\n]+)"
)

_JSON_CLINICAL_FIELD_RE = re.compile(
    r'("(?P<key>Nombre(?:\s+del)?\s+Paciente|Paciente|Rut|RUT|Tel[eé]fono(?:\s+(?:Celular|Fijo))?|Celular|Fono|Observaciones)")(\s*:\s*)"(?P<value>[^"]*)"'
)

_PATIENT_LINE_PREFIX_RE = re.compile(r"^(\s*(?:[-*]|\d+\.)\s+)")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")

_CLINICAL_FIELD_PLACEHOLDERS = {
    "paciente": "[REDACTED PATIENT]",
    "nombrepaciente": "[REDACTED PATIENT]",
    "nombre": "[REDACTED NAME]",
    "rut": "[REDACTED RUT]",
    "celular": "[REDACTED PHONE]",
    "telefono": "[REDACTED PHONE]",
    "telefonocelular": "[REDACTED PHONE]",
    "telefonofijo": "[REDACTED PHONE]",
    "fono": "[REDACTED PHONE]",
    "movil": "[REDACTED PHONE]",
    "observacion": "[REDACTED CLINICAL NOTE]",
    "observaciones": "[REDACTED CLINICAL NOTE]",
    "comentario": "[REDACTED CLINICAL NOTE]",
    "comentarios": "[REDACTED CLINICAL NOTE]",
    "nota": "[REDACTED CLINICAL NOTE]",
    "notas": "[REDACTED CLINICAL NOTE]",
}

# Compile known prefix patterns into one alternation
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def _mask_token(token: str) -> str:
    """Mask a token, preserving prefix for long tokens."""
    if len(token) < 18:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


def _normalize_clinical_field_name(field_name: str) -> str:
    normalized = unicodedata.normalize("NFKD", field_name)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", normalized.lower())


def clinical_field_placeholder(field_name: str) -> str | None:
    if not field_name:
        return None
    return _CLINICAL_FIELD_PLACEHOLDERS.get(_normalize_clinical_field_name(field_name))


def redact_clinical_field_value(field_name: str, value: str) -> str:
    if not isinstance(value, str):
        return value
    placeholder = clinical_field_placeholder(field_name)
    if not placeholder or not value.strip():
        return value
    return placeholder


def _looks_like_patient_detail_line(line: str) -> bool:
    if not line:
        return False
    if not _PATIENT_LINE_PREFIX_RE.match(line):
        return False
    if _CHILEAN_RUT_RE.search(line):
        return True
    if _LABELED_PHONE_RE.search(line):
        return True
    return bool(_TIME_RE.search(line) and _LOCAL_PHONE_RE.search(line))


def redact_persisted_text(text: str) -> str:
    """Apply secret redaction plus clinical PII scrubbing for persisted artifacts."""
    if not text:
        return text

    text = redact_sensitive_text(text)
    sanitized_lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        newline = line[len(stripped):]

        if _looks_like_patient_detail_line(stripped):
            prefix = _PATIENT_LINE_PREFIX_RE.match(stripped).group(1)
            sanitized_lines.append(f"{prefix}[REDACTED PATIENT DETAIL]{newline}")
            continue

        stripped = _PATIENT_LABEL_RE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}[REDACTED PATIENT]",
            stripped,
        )
        stripped = _JSON_CLINICAL_FIELD_RE.sub(
            lambda m: (
                f'{m.group(1)}{m.group(3)}"'
                f'{clinical_field_placeholder(m.group("key")) or "[REDACTED CLINICAL FIELD]"}"'
            ),
            stripped,
        )
        stripped = _CHILEAN_RUT_RE.sub("[REDACTED RUT]", stripped)
        stripped = _LABELED_PHONE_RE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}[REDACTED PHONE]",
            stripped,
        )
        if _CLINICAL_PHONE_LABEL_RE.search(stripped):
            stripped = _LOCAL_PHONE_RE.sub("[REDACTED PHONE]", stripped)

        sanitized_lines.append(stripped + newline)

    return "".join(sanitized_lines)


def redact_sensitive_text(text: str) -> str:
    """Apply all redaction patterns to a block of text.

    Safe to call on any string -- non-matching text passes through unchanged.
    Disabled when security.redact_secrets is false in config.yaml.
    """
    if not text:
        return text
    if os.getenv("HERMES_REDACT_SECRETS", "").lower() in ("0", "false", "no", "off"):
        return text

    # Known prefixes (sk-, ghp_, etc.)
    text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)

    # ENV assignments: OPENAI_API_KEY=sk-abc...
    def _redact_env(m):
        name, quote, value = m.group(1), m.group(2), m.group(3)
        return f"{name}={quote}{_mask_token(value)}{quote}"
    text = _ENV_ASSIGN_RE.sub(_redact_env, text)

    # JSON fields: "apiKey": "value"
    def _redact_json(m):
        key, value = m.group(1), m.group(2)
        return f'{key}: "{_mask_token(value)}"'
    text = _JSON_FIELD_RE.sub(_redact_json, text)

    # Authorization headers
    text = _AUTH_HEADER_RE.sub(
        lambda m: m.group(1) + _mask_token(m.group(2)),
        text,
    )

    # Telegram bot tokens
    def _redact_telegram(m):
        prefix = m.group(1) or ""
        digits = m.group(2)
        return f"{prefix}{digits}:***"
    text = _TELEGRAM_RE.sub(_redact_telegram, text)

    # Private key blocks
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)

    # Database connection string passwords
    text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)

    # E.164 phone numbers (Signal, WhatsApp)
    def _redact_phone(m):
        phone = m.group(1)
        if len(phone) <= 8:
            return phone[:2] + "****" + phone[-2:]
        return phone[:4] + "****" + phone[-4:]
    text = _SIGNAL_PHONE_RE.sub(_redact_phone, text)

    # Replace any exact env values whose variable names look secret-like.
    for env_key, env_val in os.environ.items():
        should_redact_value = (
            _ENV_ASSIGN_RE_NAME.search(env_key)
            or _SCOPED_LOGIN_ENV_NAME_RE.match(env_key)
        )
        if should_redact_value and env_val and len(env_val) >= 3:
            if env_val in text:
                text = text.replace(env_val, "***")

    return text


class RedactingFormatter(logging.Formatter):
    """Log formatter that redacts secrets from all log messages."""

    def __init__(self, fmt=None, datefmt=None, style='%', **kwargs):
        super().__init__(fmt, datefmt, style, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_sensitive_text(original)
