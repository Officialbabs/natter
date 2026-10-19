"""Natter: WhatsApp for Linux that feels like part of the desktop."""

import os

# NATTER_APP_ID runs a separate instance for development (pair it with its own XDG_*_HOME).
APP_ID = os.environ.get("NATTER_APP_ID") or "io.natter.Natter"
NAME = "Natter"
VERSION = "0.1.0"
WHATSAPP_URL = "https://web.whatsapp.com/"
