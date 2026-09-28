"""Display-only defense in depth. Redact structured credentials before truncation."""
import json
import re
from urllib.parse import urlsplit, urlunsplit

MASK = "[REDACTED]"
KEY = r"(?:[\w-]*(?:api[_-]?key|token|password|passwd|secret|authorization|credential|private[_-]?key)|pass|heslo)"
QUOTED = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' '''
VALUE = "(?:" + QUOTED.strip() + r"|[^\s,;}\]&]+)"
PAIR = re.compile(r'''(?i)(["']?\b''' + KEY + r'''["']?\s*[:=]\s*)''' + VALUE)
ARG = re.compile(r"(?i)(--?" + KEY + r"\s+)" + VALUE)
ENV = re.compile(r"(?<![\w-])([A-Z_][A-Z0-9_]*\s*=\s*)" + VALUE)
AUTH = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=:%-]+")
TOKENS = re.compile(r"\b(?:\d{6,16}:[A-Za-z0-9_-]{20,}|(?:sk-ant-|sk-proj-|sk_|ghp_|github_pat_|xox[bp]-|AIza|AKIA)[A-Za-z0-9_-]{6,})")
URL = re.compile(r"""https?://[^\s<>"']+""", re.I)

def sensitive(key):
    return bool(re.fullmatch(KEY, str(key), re.I))

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
    text=URL.sub(_url,text)
    text=AUTH.sub(MASK,text)
    for pattern in (PAIR,ARG,ENV):
        text=pattern.sub(lambda m:m.group(1)+MASK,text)
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
