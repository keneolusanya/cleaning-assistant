"""Twilio webhook + scheduler loop in one process.

The webhook path is a long random secret (CHOREBOT_WEBHOOK_SECRET); every other
path 404s. Requests must also carry a valid Twilio signature, and texts from
numbers that aren't in `people` are ignored (§12).
"""

from __future__ import annotations

import hmac
import logging
import os
import queue
import threading
import time

from flask import Flask, Response, abort, request

from . import inbound, scheduler

log = logging.getLogger("chorebot.server")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
MIN_SECRET_LEN = 32


def create_app(eng, interpreter, *, secret: str, auth_token: str, public_base_url: str,
               media_fetcher=None, start_workers: bool = True) -> Flask:
    if len(secret) < MIN_SECRET_LEN:
        raise ValueError(f"CHOREBOT_WEBHOOK_SECRET must be at least {MIN_SECRET_LEN} characters")
    from twilio.request_validator import RequestValidator

    validator = RequestValidator(auth_token)
    lock = threading.Lock()  # one writer at a time: webhook worker and scheduler
    jobs: queue.Queue = queue.Queue()
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.post("/sms/<path_secret>")
    def sms(path_secret: str):
        if not hmac.compare_digest(path_secret, secret):
            abort(404)
        url = public_base_url.rstrip("/") + f"/sms/{secret}"
        if not validator.validate(url, request.form, request.headers.get("X-Twilio-Signature", "")):
            abort(404)
        form = request.form.to_dict()
        jobs.put(form)
        # Reply via the REST API from the worker, so a slow model call never times out Twilio's webhook.
        return Response(EMPTY_TWIML, mimetype="text/xml")

    def handle(form: dict) -> None:
        media = []
        for i in range(int(form.get("NumMedia", "0") or 0)):
            m = {"url": form.get(f"MediaUrl{i}"), "content_type": form.get(f"MediaContentType{i}", "")}
            if media_fetcher and m["content_type"].startswith("image/"):
                try:
                    m["data"] = media_fetcher(m["url"])
                except Exception:
                    log.exception("couldn't fetch media")
            media.append(m)
        with lock:
            inbound.handle(eng, interpreter, form.get("From", ""), form.get("Body", ""), media)

    def worker() -> None:
        while True:
            form = jobs.get()
            try:
                handle(form)
            except Exception:
                log.exception("failed handling incoming text")

    def ticker() -> None:
        while True:
            try:
                with lock:
                    inbound.retry_unprocessed(eng, interpreter)
                    scheduler.tick(eng)
            except Exception:
                log.exception("tick failed")
            time.sleep(60)

    if start_workers:
        threading.Thread(target=worker, daemon=True, name="inbound").start()
        threading.Thread(target=ticker, daemon=True, name="scheduler").start()
    app.config["handle_job"] = handle
    return app


def config_from_env() -> dict:
    return {
        "secret": os.environ["CHOREBOT_WEBHOOK_SECRET"],
        "auth_token": os.environ["TWILIO_AUTH_TOKEN"],
        "public_base_url": os.environ["CHOREBOT_PUBLIC_BASE_URL"],
    }
