"""Error alerts: problems become an email to the owner, without flooding."""
import pytest

import alerts
import app as app_module
import auth
import pages


def crash(*args, **kwargs):
    raise RuntimeError("database fell over")


@pytest.fixture
def real_errors(monkeypatch):
    """Show a normal error page, like the live site, instead of stopping."""
    monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)


def test_a_crashing_page_emails_the_owner(user, alerts_sent, real_errors,
                                          monkeypatch):
    monkeypatch.setattr(pages, "compute_metrics", crash)
    assert user.get("/dashboard").status_code == 500
    assert len(alerts_sent) == 1
    subject, body = alerts_sent[0]
    assert subject == "Agenndar error: GET /dashboard"
    assert "database fell over" in body


def test_many_errors_send_one_email_then_a_count(user, alerts_sent, real_errors,
                                                monkeypatch):
    monkeypatch.setattr(pages, "compute_metrics", crash)
    for _ in range(3):
        user.get("/dashboard")
    assert len(alerts_sent) == 1                    # not three emails
    alerts._state["last_sent"] -= alerts.QUIET_SECONDS   # 15 minutes later
    user.get("/dashboard")
    assert len(alerts_sent) == 2
    assert "2 more errors" in alerts_sent[1][1]


def test_private_links_stay_out_of_the_email(client, alerts_sent, real_errors,
                                             monkeypatch):
    monkeypatch.setattr(auth, "get_db", crash)
    assert client.get("/reset-password/SECRET-TOKEN-123").status_code == 500
    subject, body = alerts_sent[0]
    assert "SECRET-TOKEN-123" not in subject + body
    assert "/reset-password/<token>" in subject


def test_ordinary_problems_like_a_missing_page_dont_alert(client, alerts_sent):
    assert client.get("/no-such-page").status_code == 404
    assert alerts_sent == []
