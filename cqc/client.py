"""Server-side client for the CQC Syndication API.

Endpoints and authentication follow the CQC developer portal (https://api-portal.service.cqc.org.uk):
requests carry the subscription key in the ``Ocp-Apim-Subscription-Key`` header and list endpoints
are paginated with ``page`` / ``perPage``. CQC does not publish a fixed request quota, so pacing and
retries are configurable and HTTP 429 ``Retry-After`` responses are honoured.
"""
import logging
import os
import time

import requests

log = logging.getLogger("cqc.client")

DEFAULT_BASE_URL = "https://api.service.cqc.org.uk/public/v1"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class CQCError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CQCNotFound(CQCError):
    pass


class CQCClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, *,
                 timeout: float | None = None, max_retries: int | None = None,
                 request_delay: float | None = None, per_page: int | None = None,
                 session: requests.Session | None = None):
        self.api_key = (api_key if api_key is not None else os.environ.get("CQC_API_KEY", "")).strip()
        self.base_url = (base_url or os.environ.get("CQC_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else float(os.environ.get("CQC_TIMEOUT", 30))
        self.max_retries = max_retries if max_retries is not None else int(os.environ.get("CQC_MAX_RETRIES", 4))
        self.request_delay = (request_delay if request_delay is not None
                              else float(os.environ.get("CQC_REQUEST_DELAY", 0.1)))
        self.per_page = per_page or int(os.environ.get("CQC_PER_PAGE", 500))
        self.session = session or requests.Session()
        self.request_count = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.configured:
            raise CQCError("CQC_API_KEY is not configured")
        url = f"{self.base_url}{path}"
        headers = {"Ocp-Apim-Subscription-Key": self.api_key, "Accept": "application/json",
                   "User-Agent": "VeridynRecruit-CQC-Sync/1.0"}
        attempt = 0
        while True:
            attempt += 1
            if self.request_delay:
                time.sleep(self.request_delay)
            started = time.monotonic()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("CQC GET %s failed (attempt %s): %s", path, attempt, type(exc).__name__)
                if attempt > self.max_retries:
                    raise CQCError(f"Network error calling CQC: {type(exc).__name__}") from exc
                time.sleep(min(2 ** attempt, 60))
                continue
            self.request_count += 1
            log.info("CQC GET %s params=%s -> %s in %.0fms", path, params or {}, resp.status_code,
                     (time.monotonic() - started) * 1000)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise CQCError("CQC returned a non-JSON response", 200) from exc
            if resp.status_code == 404:
                raise CQCNotFound(f"Not found: {path}", 404)
            if resp.status_code in (401, 403):
                raise CQCError("CQC rejected the subscription key (HTTP %s)" % resp.status_code, resp.status_code)
            if resp.status_code in RETRY_STATUSES and attempt <= self.max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else min(2 ** attempt, 60)
                log.warning("CQC GET %s -> %s, retrying in %ss", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            raise CQCError(f"CQC API error HTTP {resp.status_code}", resp.status_code)

    def list_page(self, resource: str, page: int = 1, per_page: int | None = None, **filters) -> dict:
        params = {"page": page, "perPage": per_page or self.per_page, **filters}
        return self._get(f"/{resource}", params)

    def iter_ids(self, resource: str, id_key: str, limit: int | None = None, **filters):
        """Yield summary records from a paginated list endpoint (``/providers`` or ``/locations``)."""
        page, seen = 1, 0
        while True:
            data = self.list_page(resource, page, **filters)
            items = data.get(resource) or []
            for item in items:
                if item.get(id_key):
                    yield item
                    seen += 1
                    if limit and seen >= limit:
                        return
            total_pages = data.get("totalPages") or 0
            if not items or page >= total_pages:
                return
            page += 1

    def get_provider(self, provider_id: str) -> dict:
        return self._get(f"/providers/{requests.utils.quote(provider_id, safe='')}")

    def get_location(self, location_id: str) -> dict:
        return self._get(f"/locations/{requests.utils.quote(location_id, safe='')}")

    def iter_changes(self, entity: str, start: str, end: str):
        """Yield IDs changed in a window via ``/changes/{provider|location}``."""
        page = 1
        while True:
            data = self._get(f"/changes/{entity}", {"startTimestamp": start, "endTimestamp": end,
                                                     "page": page, "perPage": self.per_page})
            changes = data.get("changes") or []
            for item in changes:
                if isinstance(item, dict):
                    item = item.get(f"{entity}Id") or item.get("id")
                if item:
                    yield str(item)
            if not changes or page >= (data.get("totalPages") or 0):
                return
            page += 1

    def test_connection(self) -> dict:
        data = self.list_page("providers", 1, per_page=1)
        return {"total_providers": data.get("total")}
