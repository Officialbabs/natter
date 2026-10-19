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


def same_site(uri: str, page: str) -> bool:
    """Whether `uri` is on the site of `page` (the --url one): same scheme and host, or for files the same file."""
    here, there = urlparse(uri or ""), urlparse(page or "")
    if not here.scheme or here.scheme != there.scheme:
        return False
    if here.scheme == "file":
        return here.path == there.path
    return bool(here.netloc) and here.netloc == there.netloc


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


def short_link(uri: str) -> Optional[str]:
    """The wa.me form of a WhatsApp chat link, the way links to chats appear in messages."""
    target = chat_link(uri)
    if target is None:
        return None
    query = dict(parse_qsl(urlparse(target).query))
    link = "https://wa.me/" + query.get("phone", "")
    return link + ("?" + urlencode({"text": query["text"]}) if query.get("text") else "")
