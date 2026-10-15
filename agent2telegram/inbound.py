"""Bounded display metadata; never an authorization or routing input."""
import unicodedata


def _field(value: object, limit: int, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = unicodedata.normalize("NFKC", value[:512])
    value = "".join(
        " " if c in '[]"·' or unicodedata.category(c).startswith("C") else c
        for c in value
    )
    return " ".join(value.split())[:limit].strip() or fallback


def sender_label(message: dict) -> str:
    """Names/titles are user-controlled descriptions, not proof of identity."""
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    name = _field(sender.get("first_name"), 64, "uživatel")
    if chat.get("type") == "private":
        where = "DM"
    elif chat.get("type") in ("group", "supergroup"):
        title = _field(chat.get("title"), 80, "bez názvu")
        where = f'skupina "{title}"'
    else:
        where = "chat"
    return f"[Od: {name} · {where}]"
