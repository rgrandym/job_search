"""Manual Gmail alert sync, OAuth account checks, and per-CV new-job tracking."""

from __future__ import annotations

import base64
import time
from email.message import EmailMessage

import httpx
import pytest
from pydantic import SecretStr

from src.core.config import Settings
from src.jobs.models import SearchQuery
from src.jobs.sources.base import HttpFetcher, SourceError
from src.jobs.sources.gmail_alerts import GMAIL_SCOPE, GmailAlertSource, GmailAuth, GmailToken


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "gmail_account": "alerts@example.com",
            "gmail_client_id": "client-id",
            "gmail_client_secret": SecretStr("client-secret"),
        }
    )


def _raw_alert() -> str:
    message = EmailMessage()
    message["Subject"] = "New jobs"
    message.set_content("New job alert")
    message.add_alternative(
        '<a href="https://uk.indeed.com/rc/clk?jk=a1b2c3d4e5&amp;from=ja">'
        "Machine Learning Engineer</a><p>Orbit AI · London</p>",
        subtype="html",
    )
    return base64.urlsafe_b64encode(bytes(message)).decode()


def test_gmail_alerts_are_new_per_cv_and_messages_are_cached(settings: Settings) -> None:
    settings = _settings(settings)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer access"
        if request.url.path.endswith("/messages"):
            assert request.url.params["labelIds"] == "INBOX"
            return httpx.Response(200, json={"messages": [{"id": "mail-1"}]})
        return httpx.Response(200, json={"raw": _raw_alert()})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    GmailAuth(settings, http)._save(
        GmailToken(
            account="alerts@example.com",
            access_token=SecretStr("access"),
            refresh_token=SecretStr("refresh"),
            expires_at=time.time() + 3600,
        )
    )

    def source(cv: str, only_new: bool = True) -> GmailAlertSource:
        return GmailAlertSource(settings, http, cv_key=cv, only_new=only_new)

    first = source("cv-one:content-one")
    [job] = first.fetch(SearchQuery(titles=["Machine Learning Engineer"]))
    assert job.id == "indeed:a1b2c3d4e5" and job.source == "indeed_alert"
    first.mark_analyzed(set())
    assert source("cv-one:content-one").fetch(SearchQuery())
    first.mark_analyzed({job.id})
    assert source("cv-one:content-one").fetch(SearchQuery()) == []  # "Search new alerts"
    # A normal search still returns it: its remembered verdict makes the result repeatable
    [again] = source("cv-one:content-one", only_new=False).fetch(SearchQuery())
    assert again.id == job.id
    [for_other_cv] = source("cv-two:content-two").fetch(SearchQuery())
    assert for_other_cv.id == job.id
    assert calls.count("/gmail/v1/users/me/messages/mail-1") == 1
    assert (settings.data_dir / "gmail_token.json").stat().st_mode & 0o777 == 0o600


def test_gmail_alert_cache_from_an_older_parser_is_rebuilt(settings: Settings) -> None:
    import json

    from src.jobs.sources.inbox import ALERT_PARSER_VERSION

    settings = _settings(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "mail-1"}]})
        return httpx.Response(200, json={"raw": _raw_alert()})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    GmailAuth(settings, http)._save(
        GmailToken(
            account="alerts@example.com",
            access_token=SecretStr("access"),
            refresh_token=SecretStr("refresh"),
            expires_at=time.time() + 3600,
        )
    )
    cache = settings.data_dir / "gmail_alert_cache.json"
    cache.write_text(json.dumps({"account": "alerts@example.com", "messages": {"mail-1": []}}))
    seen = settings.data_dir / "gmail_alert_seen.json"
    seen.write_text(json.dumps({"by_cv": {"alerts@example.com:cv": ["indeed:a1b2c3d4e5"]}}))

    [job] = GmailAlertSource(settings, http, cv_key="cv", only_new=True).fetch(SearchQuery())
    assert job.id == "indeed:a1b2c3d4e5"  # re-parsed, and analysable again
    assert json.loads(cache.read_text())["parser_version"] == ALERT_PARSER_VERSION


def test_gmail_oauth_rejects_wrong_account(settings: Settings) -> None:
    settings = _settings(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            assert request.url.host == "oauth2.googleapis.com"
            assert b"client-secret" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "scope": GMAIL_SCOPE,
                },
            )
        return httpx.Response(200, json={"emailAddress": "personal@example.com"})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SourceError, match="does not match"):
        GmailAuth(settings, http).exchange("code", "verifier")
    assert not (settings.data_dir / "gmail_token.json").exists()


def test_gmail_refreshes_expired_access_token(settings: Settings) -> None:
    settings = _settings(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        assert b"refresh_token=refresh" in request.content
        return httpx.Response(200, json={"access_token": "renewed", "expires_in": 3600})

    http = HttpFetcher(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    auth = GmailAuth(settings, http)
    auth._save(
        GmailToken(
            account="alerts@example.com",
            access_token=SecretStr("expired"),
            refresh_token=SecretStr("refresh"),
            expires_at=time.time() - 1,
        )
    )
    assert auth.access_token() == "renewed"
    assert auth._load().refresh_token.get_secret_value() == "refresh"


def test_gmail_source_requires_selected_cv(settings: Settings) -> None:
    settings = _settings(settings)
    GmailAuth(settings)._save(
        GmailToken(
            account="alerts@example.com",
            access_token=SecretStr("access"),
            refresh_token=SecretStr("refresh"),
            expires_at=time.time() + 3600,
        )
    )
    with pytest.raises(SourceError, match="Select a CV"):
        GmailAlertSource(settings, cv_key=None)
