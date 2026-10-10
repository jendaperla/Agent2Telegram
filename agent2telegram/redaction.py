"""Display-only defense in depth. Redact structured credentials before truncation."""
import json
import re
from urllib.parse import urlsplit, urlunsplit

MASK = "[REDACTED]"
KEY = r"(?:[\w-]*(?:api[_-]?key|token|password|passwd|secret|authorization|credential|private[_-]?key)|pass|heslo)"
QUOTED = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' '''
# Consume an existing marker whole: later passes must not leave its closing ].
VALUE = "(?:" + QUOTED.strip() + "|(?:" + re.escape(MASK) + r"|[^\s,;}\]&])+)"
PAIR = re.compile(r'''(?i)(["']?\b''' + KEY + r'''["']?[ \t]*[:=][ \t]*)''' + VALUE)
ARG = re.compile(r"(?i)(--?" + KEY + r"\s+)" + VALUE)
ENV = re.compile(r"(?<![\w-])(([A-Z_][A-Z0-9_]*)[ \t]*=[ \t]*)(" + VALUE + ")")
ENV_SECRET_KEY = re.compile(r"(?:^|_)(?:key|token|secret|password|passwd|pass|auth(?:orization)?|credentials?|heslo)(?:_|$)", re.I)
AUTH = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=:%-]+")
TOKENS = re.compile(r"(?:\d{6,16}:[A-Za-z0-9_-]{20,}|\b(?:sk-ant-|sk-proj-|sk_|ghp_|github_pat_|xox[bp]-|AIza|AKIA)[A-Za-z0-9_-]{6,})")
URL = re.compile(r"""https?://[^\s<>"']+""", re.I)

def sensitive(key):
    return bool(re.fullmatch(KEY, str(key), re.I))

def _env(match):
    key, value = match.group(2), match.group(3)
    if sensitive(key) or ENV_SECRET_KEY.search(key) or TOKENS.search(value):
        return match.group(1) + MASK
    return match.group()

def _url(match):
    try:
        u=urlsplit(match.group())
        host=u.hostname or ""
        if ":" in host:host="["+host+"]"
        if u.port:host+=":"+str(u.port)
        return urlunsplit((u.scheme,host,u.path, MASK if u.query else "", MASK if u.fragment else ""))
    except ValueError:
        return MASK

def _text(text):
    # Shell/JSON escaping can hide delimiters from a text regex. Diagnostic
    # output may be suppressed entirely; never partially expose such a value.
    if '\\' in text and re.search(KEY, text, re.I):
        return MASK
    text=URL.sub(_url,text)
    text=AUTH.sub(MASK,text)
    for pattern in (PAIR,ARG):
        text=pattern.sub(lambda m:m.group(1)+MASK,text)
    text=ENV.sub(_env,text)
    return TOKENS.sub(MASK,text)

def _tree(value):
    if isinstance(value,dict):
        return {k:MASK if sensitive(k) else _tree(v) for k,v in value.items()}
    if isinstance(value,list):return [_tree(v) for v in value]
    if isinstance(value,str):return _text(value)
    return value

def redact(value):
    text=str(value)
    try:parsed=json.loads(text)
    except (ValueError,RecursionError):return _text(text)
    try:return json.dumps(_tree(parsed),ensure_ascii=False) if isinstance(parsed,(dict,list)) else _text(text)
    except RecursionError:return MASK


class RedactingFilter:
    """logging filter: mask credentials in EVERY log line before it is written.

    Previews of inbound/outbound text are logged on purpose (they make incidents traceable), but a
    user once pasted an API key into a message and the 40-character preview stored it in the log
    (security audit 2026-10-10). Masking at the handler covers every present and future log call.
    """

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = _text(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


def install_log_redaction(logger=None):
    import logging
    root = logger or logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
