"""Read a dedicated Gmail inbox on demand and cache only parsed job alerts locally."""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.core.config import Settings, get_settings
from src.jobs.models import JobPosting, SearchQuery
from src.jobs.sources.base import HttpFetcher, SourceError
from src.jobs.sources.inbox import ALERT_PARSER_VERSION, parse_alert_email

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GmailToken(_PrivateModel):
    account: str
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: float


class AlertCache(_PrivateModel):
    account: str
    parser_version: int = 1
    messages: dict[str, list[JobPosting]] = Field(default_factory=dict)


class AlertSeen(_PrivateModel):
    by_cv: dict[str, list[str]] = Field(default_factory=dict)


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write ignored credential or alert state with owner-only permissions."""
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as out:
            tmp = Path(out.name)
            json.dump(payload, out, indent=2)
            out.write("\n")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        raise SourceError("Could not save private Gmail state") from None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


class GmailAuth:
    """Local OAuth credential store for one explicitly configured Gmail account."""

    def __init__(self, settings: Settings | None = None, http: HttpFetcher | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        self.path = self.settings.data_dir / "gmail_token.json"

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.gmail_client_id
            and self.settings.gmail_client_secret
            and self.settings.gmail_account
        )

    @property
    def connected(self) -> bool:
        if not self.configured or not self.path.is_file():
            return False
        try:
            token = GmailToken.model_validate_json(self.path.read_bytes())
            return token.account.lower() == (self.settings.gmail_account or "").lower()
        except (OSError, ValueError):
            return False

    def authorization_url(self, state: str, challenge: str) -> str:
        """Return the Google consent URL for read-only, offline Gmail access."""
        if not self.configured:
            raise SourceError("Configure Gmail OAuth client ID, secret, and account in .env")
        params = {
            "client_id": self.settings.gmail_client_id or "",
            "redirect_uri": self.settings.gmail_redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPE,
            "access_type": "offline",
            "prompt": "consent select_account",
            "login_hint": self.settings.gmail_account or "",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange(self, code: str, verifier: str) -> None:
        """Exchange a consent code and store tokens only for the configured account."""
        payload = self._token_request(
            {
                "code": code,
                "client_id": self.settings.gmail_client_id,
                "client_secret": self._client_secret(),
                "redirect_uri": self.settings.gmail_redirect_uri,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
            }
        )
        if not payload.get("access_token") or not payload.get("refresh_token"):
            raise SourceError("Gmail did not return offline access tokens")
        scopes = payload.get("scope")
        if scopes is not None and set(str(scopes).split()) != {GMAIL_SCOPE}:
            raise SourceError("Gmail did not grant read-only offline access")
        account = self._profile(str(payload["access_token"]))
        if account.lower() != (self.settings.gmail_account or "").lower():
            raise SourceError("Connected Gmail account does not match JOBSEARCH_GMAIL_ACCOUNT")
        self._save(
            GmailToken(
                account=account,
                access_token=SecretStr(str(payload["access_token"])),
                refresh_token=SecretStr(str(payload["refresh_token"])),
                expires_at=time.time() + int(payload.get("expires_in", 3600)),
            )
        )

    def access_token(self) -> str:
        """Return a valid access token, refreshing it only when necessary."""
        token = self._load()
        if token.expires_at > time.time() + 60:
            return token.access_token.get_secret_value()
        payload = self._token_request(
            {
                "client_id": self.settings.gmail_client_id,
                "client_secret": self._client_secret(),
                "refresh_token": token.refresh_token.get_secret_value(),
                "grant_type": "refresh_token",
            }
        )
        if not payload.get("access_token"):
            raise SourceError("Gmail access could not be renewed; reconnect the account")
        refreshed = token.model_copy(
            update={
                "access_token": SecretStr(str(payload["access_token"])),
                "expires_at": time.time() + int(payload.get("expires_in", 3600)),
            }
        )
        self._save(refreshed)
        return refreshed.access_token.get_secret_value()

    def _client_secret(self) -> str:
        secret = self.settings.gmail_client_secret
        if not self.configured or secret is None:
            raise SourceError("Configure Gmail OAuth client ID, secret, and account in .env")
        return secret.get_secret_value()

    def _token_request(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            payload: dict[str, Any] = self.http.post(TOKEN_URL, data=data).json()
            return payload
        except (SourceError, ValueError):
            raise SourceError("Gmail authorization failed; reconnect the account") from None

    def _profile(self, access_token: str) -> str:
        try:
            payload = self.http.get(
                f"{GMAIL_API}/profile", headers={"Authorization": f"Bearer {access_token}"}
            ).json()
            return str(payload["emailAddress"])
        except (SourceError, ValueError, KeyError):
            raise SourceError("Could not verify the connected Gmail account") from None

    def _load(self) -> GmailToken:
        if not self.connected:
            raise SourceError("Connect the dedicated Gmail account before searching alerts")
        try:
            token = GmailToken.model_validate_json(self.path.read_bytes())
        except (OSError, ValueError):
            raise SourceError("Stored Gmail authorization is invalid; reconnect") from None
        if token.account.lower() != (self.settings.gmail_account or "").lower():
            raise SourceError("Stored Gmail account does not match JOBSEARCH_GMAIL_ACCOUNT")
        return token

    def _save(self, token: GmailToken) -> None:
        _write_private(
            self.path,
            {
                "account": token.account,
                "access_token": token.access_token.get_secret_value(),
                "refresh_token": token.refresh_token.get_secret_value(),
                "expires_at": token.expires_at,
            },
        )


class GmailAlertSource:
    """Fetch current inbox alerts. In a normal search every alert job within the filters is
    returned, judged before or not (remembered verdicts make repeats free, so the same search
    shows the same results); with `only_new` ("Search new alerts"), only jobs this CV has not
    had judged yet."""

    name = "gmail_alerts"

    def __init__(
        self,
        settings: Settings | None = None,
        http: HttpFetcher | None = None,
        *,
        cv_key: str | None = None,
        only_new: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.http = http or HttpFetcher(self.settings)
        self.only_new = only_new
        self.auth = GmailAuth(self.settings, self.http)
        if not self.auth.connected:
            raise SourceError("Connect the dedicated Gmail account before searching alerts")
        if cv_key is None:
            raise SourceError("Select a CV before searching new Gmail alerts")
        self.cv_key = f"{self.settings.gmail_account}:{cv_key}"
        self.cache_path = self.settings.data_dir / "gmail_alert_cache.json"
        self.seen_path = self.settings.data_dir / "gmail_alert_seen.json"
        self.fetched_ids: set[str] = set()

    def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """List the inbox, parse new messages once, then filter (and, with `only_new`, skip
        jobs this CV has had judged)."""
        token = self.auth.access_token()
        headers = {"Authorization": f"Bearer {token}"}
        cache = self._cache()
        if cache.parser_version != ALERT_PARSER_VERSION:
            # Alerts parsed by an older parser may have lost jobs: re-read them all, and let
            # every CV analyse them again (earlier verdicts saw incomplete alerts).
            cache = AlertCache(account=cache.account, parser_version=ALERT_PARSER_VERSION)
            _write_private(self.seen_path, AlertSeen().model_dump(mode="json"))
        message_ids = self._message_ids(headers)
        for message_id in message_ids:
            if message_id not in cache.messages:
                cache.messages[message_id] = self._message_jobs(message_id, headers)
        _write_private(self.cache_path, cache.model_dump(mode="json"))
        seen = set(self._seen().by_cv.get(self.cv_key, [])) if self.only_new else set()
        jobs: dict[str, JobPosting] = {}
        active_ids = set(message_ids)
        ordered_ids = [*message_ids, *(key for key in cache.messages if key not in active_ids)]
        for message_id in ordered_ids:
            for job in cache.messages[message_id]:
                if (
                    job.id not in seen
                    and query.is_relevant(job)
                    and query.is_recent(job)
                    and (not query.remote_only or job.work_arrangement == "remote")
                ):
                    jobs.setdefault(job.id, job)
        selected = list(jobs.values())[: query.limit]
        self.fetched_ids = {job.id for job in selected}
        return selected

    def mark_analyzed(self, job_ids: set[str]) -> None:
        """Remember jobs considered by this CV after a successful search."""
        completed = self.fetched_ids & job_ids
        if not completed:
            return
        seen = self._seen()
        seen.by_cv[self.cv_key] = sorted(set(seen.by_cv.get(self.cv_key, [])) | completed)
        _write_private(self.seen_path, seen.model_dump(mode="json"))

    def _message_ids(self, headers: dict[str, str]) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while True:
            params = {"labelIds": "INBOX", "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = self.http.get(
                    f"{GMAIL_API}/messages", params=params, headers=headers
                ).json()
            except (SourceError, ValueError):
                raise SourceError("Could not list Gmail alerts; reconnect") from None
            ids.extend(str(item["id"]) for item in payload.get("messages", []))
            next_page = payload.get("nextPageToken")
            if not next_page or next_page == page_token:
                return ids
            page_token = str(next_page)

    def _message_jobs(self, message_id: str, headers: dict[str, str]) -> list[JobPosting]:
        try:
            payload = self.http.get(
                f"{GMAIL_API}/messages/{message_id}", params={"format": "raw"}, headers=headers
            ).json()
            raw = base64.urlsafe_b64decode(str(payload["raw"]) + "===")
        except (SourceError, ValueError, KeyError, binascii.Error):
            raise SourceError("Could not read a Gmail alert") from None
        return [
            job
            for job in parse_alert_email(raw)
            if job.source in {"indeed_alert", "linkedin_alert"}
        ]

    def _cache(self) -> AlertCache:
        if self.cache_path.exists():
            try:
                cache = AlertCache.model_validate_json(self.cache_path.read_bytes())
            except (OSError, ValueError):
                raise SourceError("Stored Gmail alert cache is invalid") from None
            if cache.account.lower() == (self.settings.gmail_account or "").lower():
                return cache
        return AlertCache(
            account=self.settings.gmail_account or "", parser_version=ALERT_PARSER_VERSION
        )

    def _seen(self) -> AlertSeen:
        if not self.seen_path.exists():
            return AlertSeen()
        try:
            return AlertSeen.model_validate_json(self.seen_path.read_bytes())
        except (OSError, ValueError):
            raise SourceError("Stored Gmail alert history is invalid") from None
