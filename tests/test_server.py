import pytest
from twilio.request_validator import RequestValidator

from chorebot.server import create_app

SECRET = "s" * 40
TOKEN = "test-auth-token"
BASE = "https://bot.example.com"


@pytest.fixture
def client(h):
    app = create_app(h.eng, None, secret=SECRET, auth_token=TOKEN, public_base_url=BASE, start_workers=False)
    return app.test_client()


def signed(form):
    return RequestValidator(TOKEN).compute_signature(f"{BASE}/sms/{SECRET}", form)


FORM = {"From": "+15550000001", "Body": "done", "NumMedia": "0"}


def test_wrong_path_is_404(client):
    assert client.post("/sms/guess", data=FORM).status_code == 404
    assert client.post("/", data=FORM).status_code == 404
    assert client.get("/admin").status_code == 404


def test_missing_or_bad_signature_is_404(client):
    assert client.post(f"/sms/{SECRET}", data=FORM).status_code == 404
    bad = {"X-Twilio-Signature": "nope"}
    assert client.post(f"/sms/{SECRET}", data=FORM, headers=bad).status_code == 404


def test_valid_request_is_accepted(client):
    resp = client.post(f"/sms/{SECRET}", data=FORM, headers={"X-Twilio-Signature": signed(FORM)})
    assert resp.status_code == 200
    assert b"<Response>" in resp.data


def test_short_secret_refused(h):
    with pytest.raises(ValueError):
        create_app(h.eng, None, secret="short", auth_token=TOKEN, public_base_url=BASE, start_workers=False)
