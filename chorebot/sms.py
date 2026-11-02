"""Sending texts, and fetching photos from incoming MMS."""

from __future__ import annotations

import base64
import os
import urllib.request
from typing import Callable

from .outbox import OptedOutError

# Twilio error codes meaning the recipient has unsubscribed at the carrier/gateway.
TWILIO_OPTED_OUT = {21610}


class ConsoleSender:
    """Used by the simulator: hands each message to a callback instead of sending it."""

    def __init__(self, on_send: Callable[[str, str, str, str], None] | None = None):
        self.on_send = on_send
        self.sent: list[dict] = []

    def send(self, phone: str, body: str, purpose: str = "", name: str = "") -> str:
        self.sent.append({"phone": phone, "body": body, "purpose": purpose, "name": name})
        if self.on_send:
            self.on_send(name, phone, purpose, body)
        return f"SIM{len(self.sent)}"


class TwilioSender:
    def __init__(self, account_sid: str | None = None, auth_token: str | None = None, from_number: str | None = None):
        from twilio.rest import Client

        self.account_sid = account_sid or os.environ["TWILIO_ACCOUNT_SID"]
        self.auth_token = auth_token or os.environ["TWILIO_AUTH_TOKEN"]
        self.from_number = from_number or os.environ["TWILIO_FROM_NUMBER"]
        self.client = Client(self.account_sid, self.auth_token)

    def send(self, phone: str, body: str, purpose: str = "", name: str = "") -> str:
        from twilio.base.exceptions import TwilioRestException

        try:
            msg = self.client.messages.create(to=phone, from_=self.from_number, body=body)
        except TwilioRestException as e:
            if e.code in TWILIO_OPTED_OUT:
                raise OptedOutError(phone) from e
            raise
        return msg.sid

    def fetch_media(self, url: str) -> bytes:
        """Twilio media URLs need the account credentials."""
        req = urllib.request.Request(url)
        token = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
