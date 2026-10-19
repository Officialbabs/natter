"""WhatsApp addresses: which links Natter opens itself, and the unread count in a title."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse

from . import WHATSAPP_URL

WHATSAPP_HOSTS = {"web.whatsapp.com"}


def unread_in(title: str) -> int:
    """WhatsApp puts the unread count in the page title: "(3) WhatsApp"."""
    match = re.match(r"\s*\((\d+)\)", title or "")
    return int(match[1]) if match else 0


def is_whatsapp(uri: str) -> bool:
    parsed = urlparse(uri or "")
    return parsed.scheme == "https" and parsed.hostname in WHATSAPP_HOSTS


def chat_link(uri: str) -> Optional[str]:
    """The WhatsApp Web address that opens a wa.me / whatsapp:// / api.whatsapp.com link."""
    parsed = urlparse(uri.strip())
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    phone, text = query.get("phone", ""), query.get("text", "")
    if parsed.scheme == "whatsapp":
        # whatsapp://send?phone=…&text=…
        if parsed.netloc not in ("send", "") and not parsed.path.strip("/").startswith("send"):
            return None
    elif parsed.scheme in ("http", "https") and parsed.hostname in ("wa.me", "www.wa.me"):
        phone = parsed.path.strip("/").split("/")[0]
        if phone.startswith("message"):
            return None  # business short links only resolve on WhatsApp's servers
    elif parsed.scheme in ("http", "https") and parsed.hostname in ("api.whatsapp.com", "web.whatsapp.com"):
        if not parsed.path.strip("/").startswith("send"):
            return None
    else:
        return None
    phone = re.sub(r"\D", "", phone)
    if not phone and not text:
        return None
    params = {k: v for k, v in (("phone", phone), ("text", text)) if v}
    return WHATSAPP_URL + "send?" + urlencode(params)
