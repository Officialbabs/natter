"""Natter Den: an unofficial WhatsApp Web client for Linux."""

import os

DEFAULT_APP_ID = "io.github.Officialbabs.Natter"
# Natter's id before the rename: install.sh moves its desktop files over, and its folders stay "natter".
OLD_APP_ID = "io.natter.Natter"
# NATTER_APP_ID runs a separate instance for development (pair it with its own XDG_*_HOME).
APP_ID = os.environ.get("NATTER_APP_ID") or DEFAULT_APP_ID
OBJECT_PATH = "/" + APP_ID.replace(".", "/")
NAME = "Natter Den"
VERSION = "0.1.5"
WHATSAPP_URL = "https://web.whatsapp.com/"
