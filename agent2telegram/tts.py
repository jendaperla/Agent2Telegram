"""Optional text-to-speech for Telegram voice replies (ElevenLabs).

Mirrors :mod:`stt.py`: enabled only when the user provides their own key, no third-party
dependency (the request is plain ``urllib``), and the key never lands in a log line or an
exception message — it only ever goes into the ``xi-api-key`` header.

Division of labour (a 2026-08-01 design decision): the AGENT writes the spoken text itself — short, spoken,
numbers as words, no paths — because it knows what it's saying and does it better than a regex
ever could. The bridge's job is to TELL the agent that voice mode is on (a marker in the injected
message, see attach.py). :func:`sanitize_for_speech` here is only a ROUGH SAFETY NET for leftover
markdown / blank lines; it deliberately does NOT rewrite numbers or guess at paths.

:func:`synthesize` turns text into mp3 bytes via the multilingual model, so the voice speaks
whatever language the reply is in (no hard-coded Czech). A bot MAY pin the language with the
optional ``tts_language`` config key (sent as ``language_code``) and MAY point
``tts_pronunciation_file`` at a JSON map of stems to respell for the voice only (see
:func:`apply_pronunciation`). Without those keys the request is exactly what it always was.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("agent2telegram.tts")

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
#: Eleven v3 (out of alpha 2026-08). Multilingual, so the voice follows the CONVERSATION's
#: language from the text itself. Against v2 it reads numbers more accurately and generates more
#: steadily (error rate 15.3% -> 4.9%). Speech-to-text (Scribe) is a separate path, unaffected.
DEFAULT_MODEL_ID = "eleven_flash_v2_5"
#: ElevenLabs returns mp3 here; the bridge converts to OGG/OPUS (ffmpeg) before sendVoice.
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
TRANSIENT_BACKOFFS = (1.0, 3.0)

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF️]")


class TTSError(Exception):
    pass


def sanitize_for_speech(text: str) -> str:
    """ROUGH safety net only. The agent is asked to write speakable text; this just removes
    leftover markdown noise and squashes blank lines so a stray ``**`` or table pipe doesn't get
    read aloud. It does NOT spell numbers, expand units, or strip paths — that is the agent's job
    (a regex guessing 'is this a path?' is exactly what we want to avoid)."""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)     # fenced code
    t = re.sub(r"`([^`]*)`", r"\1", t)               # inline code → keep text, drop backticks
    t = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", t)   # links → label
    t = _EMOJI_RE.sub(" ", t)
    t = t.replace("**", "").replace("__", "").replace("*", "").replace("#", "")
    t = t.replace("|", " ").replace(">", " ")
    t = re.sub(r"(?m)^\s*(?:[-•·–]|\d+[.)])\s+", "", t)   # bullet markers
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    t = re.sub(r"[ \t]*\n[ \t]*", ". ", t)           # line breaks → sentence breaks
    t = re.sub(r"(?:\.\s*){2,}", ". ", t)            # tidy doubled periods
    return t.strip()


def _describe_error(err: BaseException) -> str:
    if isinstance(err, urllib.error.HTTPError):
        reason = getattr(err, "reason", None) or getattr(err, "msg", "") or ""
        return f"HTTP {err.code}: {reason}".strip()
    return str(err) or err.__class__.__name__



def voice_settings_for(model_id: str) -> dict:
    """Steady, full-voice delivery. Petr (2026-09-05) heard a long narration drop into a whisper
    halfway through: with default settings the voice drifts over long generations. High
    stability keeps it level; v3 only accepts 0.0 / 0.5 / 1.0 (Creative / Natural / Robust)."""
    if str(model_id or "").startswith("eleven_v3"):
        return {"stability": 1.0, "similarity_boost": 0.8, "use_speaker_boost": True}
    return {"stability": 0.9, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}


def synthesize(text: str, *, api_key: str, voice_id: str, model_id: str = DEFAULT_MODEL_ID,
               output_format: str = DEFAULT_OUTPUT_FORMAT, opener=None, timeout: float = 60,
               retry_backoffs: tuple[float, ...] = TRANSIENT_BACKOFFS,
               sleeper=time.sleep, language_code: str = "") -> bytes:
    """Synthesize *text* to mp3 bytes with ElevenLabs. The key only ever goes into the header.
    *language_code* (ISO 639-1, e.g. ``cs``) is sent only when set; blank = model auto-detects."""
    if not api_key:
        raise TTSError("no ElevenLabs API key configured")
    if not (text or "").strip():
        raise TTSError("nothing to speak")
    url = TTS_URL.format(voice_id=voice_id) + f"?output_format={output_format}"
    payload = {"text": text, "model_id": model_id, "voice_settings": voice_settings_for(model_id)}
    if language_code:
        payload["language_code"] = language_code
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"xi-api-key": api_key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
        method="POST",
    )
    op = opener or urllib.request.build_opener()
    attempts = len(retry_backoffs) + 1
    for attempt in range(attempts):
        try:
            with op.open(req, timeout=timeout) as resp:
                audio = resp.read()
            if not audio:
                raise TTSError("ElevenLabs returned no audio")
            return audio
        except urllib.error.HTTPError as e:
            retryable = 500 <= e.code <= 599
            if not retryable or attempt == attempts - 1:
                raise TTSError(f"ElevenLabs TTS failed: {_describe_error(e)}") from e
            detail = _describe_error(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
            if attempt == attempts - 1:
                raise TTSError(f"ElevenLabs TTS failed after {attempt + 1} attempts: "
                               f"{_describe_error(e)}") from e
            detail = _describe_error(e)
        log.warning("ElevenLabs TTS transient failure (%d/%d): %s", attempt + 1, attempts, detail)
        sleeper(retry_backoffs[attempt])
    raise TTSError("ElevenLabs TTS failed")   # pragma: no cover


# ── Long narrations (2026-09-05) ─────────────────────────────────────────────────────────
# Petr: "klidně pět minut, ať to má délku skoro jako podcast, když řídíme". One ElevenLabs
# request takes a few thousand characters at most and long single requests are where the
# alpha v3 model gets flaky, so a long text is spoken in sentence-sized pieces and the
# caller glues the audio together. Splitting is by SENTENCE, never mid-word.
LONG_CHUNK_CHARS = 350   # ~20 s of speech per request: measured flat (±1.5 dB) where 700 already sagged 4–7 dB
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def split_for_tts(text: str, max_chars: int = LONG_CHUNK_CHARS) -> list[str]:
    """Split *text* into pieces of at most *max_chars*, cutting only at sentence ends (or,
    for a single over-long sentence, at the last space before the limit). Empty input → []."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    parts: list[str] = []
    for sentence in _SENTENCE_END.split((text or "").strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > max_chars:                      # one monster sentence
            cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if parts and len(parts[-1]) + 1 + len(sentence) <= max_chars:
            parts[-1] = f"{parts[-1]} {sentence}"
        else:
            parts.append(sentence)
    return [p for p in parts if p]


def synthesize_long(text: str, *, api_key: str, voice_id: str, model_id: str = DEFAULT_MODEL_ID,
                    max_chars: int = LONG_CHUNK_CHARS, **kwargs) -> list[bytes]:
    """Speak a long text as a list of mp3 segments (one per :func:`split_for_tts` piece), in
    order. The caller concatenates them (ffmpeg). Raises :class:`TTSError` like :func:`synthesize`."""
    pieces = split_for_tts(text, max_chars=max_chars)
    if not pieces:
        raise TTSError("nothing to speak")
    return [synthesize(piece, api_key=api_key, voice_id=voice_id, model_id=model_id, **kwargs)
            for piece in pieces]


# ── Pronunciation map (2026-11-22) ───────────────────────────────────────────────────────
# Inari's voice call already respells a few words for ElevenLabs (Alaigned -> Elajnd). The same
# JSON file serves the Telegram voice replies: the bridge READS it from the configured path, it
# never copies it. Format: {"kmeny": [{"z": "Alaigned", "na": "Elajnd"}, ...]}, any other keys
# ignored. Rule (mirrors Inari voice/lib/vyslovnost.mjs): the stem matches at the start of a
# whole word, case-insensitively, and the word's ending is kept (Alaignedu -> Elajndu).
# Only the text sent to TTS changes; the Telegram text is never touched.


def load_pronunciation(path) -> list[tuple[str, str]]:
    """Read the stem map from *path*. Any problem (missing, bad JSON, bad item) -> ``[]`` and a
    warning: the voice then speaks without the map, it never fails because of it. Like Inari,
    one bad item rejects the whole file, so a typo is loud instead of half-applied."""
    try:
        data = json.loads(Path(path).expanduser().read_text("utf-8"))
        stems = data.get("kmeny") if isinstance(data, dict) else None
        if not isinstance(stems, list):
            raise ValueError('missing "kmeny" list')
        rules = []
        for item in stems:
            z = item.get("z") if isinstance(item, dict) else None
            na = item.get("na") if isinstance(item, dict) else None
            if not (isinstance(z, str) and z.strip() and isinstance(na, str) and na.strip()):
                raise ValueError(f"bad item {item!r}")
            rules.append((z, na))
        return rules
    except (OSError, ValueError) as e:      # JSONDecodeError is a ValueError
        log.warning("pronunciation map %s unusable (%s); speaking without it", path, e)
        return []


def _stem_pattern(stem: str) -> re.Pattern:
    # Not preceded by a letter/digit/underscore; the ending is letters only ([^\W\d_] is a
    # Unicode letter, so Czech endings work); not followed by a digit/underscore.
    return re.compile(r"(?<!\w)" + re.escape(stem) + r"([^\W\d_]*)(?![\d_])", re.IGNORECASE)


def apply_pronunciation(text: str, rules) -> str:
    """Respell every whole word starting with a stem from *rules*, keeping its ending."""
    out = text or ""
    for stem, spoken in rules or ():
        out = _stem_pattern(stem).sub(lambda m, s=spoken: s + m.group(1), out)
    return out


class PronunciationMap:
    """The map at a path, re-read only when the file's mtime/size changes. One ``stat`` per voice
    reply, so an edit in Inari applies to the next reply without restarting the bridge, and a
    missing or broken file warns once per change, not on every reply."""

    _MISSING = object()

    def __init__(self):
        self._path = None
        self._stamp = None
        self._rules: list[tuple[str, str]] = []

    def rules(self, path) -> list[tuple[str, str]]:
        if not path:
            return []
        try:
            st = Path(path).expanduser().stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = self._MISSING
        if path == self._path and stamp == self._stamp:
            return self._rules
        self._path, self._stamp = path, stamp
        self._rules = load_pronunciation(path)
        return self._rules
