"""
Scrapling integration for stealth web scraping.

Provides adapters between Scrapling's Response/Session API and JobQuest's
requests-style interface, plus utilities for Cloudflare bypass.
"""
from __future__ import annotations

import json
import logging
from itertools import cycle
from typing import Any

log = logging.getLogger("JobQuest:Stealth")

SCRAPLING_AVAILABLE = False
try:
    from scrapling.fetchers import Fetcher, FetcherSession, StealthyFetcher, DynamicFetcher
    from scrapling.engines.static import FetcherClient as _FetcherClient

    SCRAPLING_AVAILABLE = True
except ImportError:
    log.warning("scrapling not installed — stealth features unavailable, falling back to requests/tls_client")


class ResponseAdapter:
    """Adapts a Scrapling Response to the requests-style interface JobQuest scrapers expect."""

    def __init__(self, scrapling_response):
        self._resp = scrapling_response
        self.status_code: int = scrapling_response.status
        self.url: str = scrapling_response.url or ""
        self.headers: dict = dict(scrapling_response.headers) if scrapling_response.headers else {}
        self.cookies: dict = self._parse_cookies(scrapling_response.cookies)
        self._text_cache: str | None = None

    @staticmethod
    def _parse_cookies(raw_cookies) -> dict:
        if not raw_cookies:
            return {}
        if isinstance(raw_cookies, dict):
            return raw_cookies
        if isinstance(raw_cookies, (list, tuple)):
            result = {}
            for cookie in raw_cookies:
                if isinstance(cookie, dict) and "name" in cookie and "value" in cookie:
                    result[cookie["name"]] = cookie["value"]
                elif isinstance(cookie, (list, tuple)) and len(cookie) == 2:
                    result[cookie[0]] = cookie[1]
            return result
        return {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def text(self) -> str:
        if self._text_cache is None:
            body = self._resp.body
            self._text_cache = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
        return self._text_cache

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self):
        if not self.ok:
            raise Exception(f"HTTP {self.status_code}: {self.text[:500]}")


class StealthSession:
    """
    A requests.Session-compatible wrapper around Scrapling's FetcherSession.

    Provides .get(), .post() returning ResponseAdapter objects that have
    .status_code, .text, .json(), .ok — matching what JobQuest scrapers expect.
    """

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        impersonate: str | list[str] = "chrome",
        stealthy_headers: bool = True,
        retries: int = 3,
        retry_delay: int = 1,
        clear_cookies: bool = False,
    ):
        proxy = self._pick_proxy(proxies) if proxies else None
        self._factory = FetcherSession(
            impersonate=impersonate,
            stealthy_headers=stealthy_headers,
            verify=ca_cert if ca_cert else True,
            proxy=proxy,
            retries=retries,
            retry_delay=retry_delay,
        )
        self._session = None
        self._proxy_cycle = None
        self._clear_cookies = clear_cookies
        if proxies:
            plist = [proxies] if isinstance(proxies, str) else proxies
            self._proxy_cycle = cycle(plist)
        self.headers: dict = {}
        self.cookies: dict = {}
        self.verify = ca_cert if ca_cert else True
        self.allow_redirects = True

    @staticmethod
    def _pick_proxy(proxies) -> str | None:
        if isinstance(proxies, str):
            return proxies
        if isinstance(proxies, list) and proxies:
            return proxies[0]
        return None

    def _ensure_session(self):
        if self._session is None:
            self._session = self._factory.__enter__()
        return self._session

    def _next_proxy(self) -> str | None:
        if self._proxy_cycle:
            return next(self._proxy_cycle)
        return None

    def _prepare_kwargs(self, kwargs: dict) -> dict:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        timeout = kwargs.pop("timeout", None) or kwargs.pop("timeout_seconds", 30)
        verify = kwargs.pop("verify", None)
        kwargs["headers"] = headers
        kwargs["timeout"] = timeout
        if verify is not None:
            kwargs["verify"] = verify
        proxy = self._next_proxy()
        if proxy:
            kwargs.setdefault("proxy", proxy)
        kwargs.pop("allow_redirects", None)
        return kwargs

    def get(self, url: str, **kwargs) -> ResponseAdapter:
        session = self._ensure_session()
        kwargs = self._prepare_kwargs(kwargs)
        resp = session.get(url, **kwargs)
        return ResponseAdapter(resp)

    def post(self, url: str, **kwargs) -> ResponseAdapter:
        session = self._ensure_session()
        kwargs = self._prepare_kwargs(kwargs)
        resp = session.post(url, **kwargs)
        return ResponseAdapter(resp)

    def close(self):
        if self._session:
            try:
                self._factory.__exit__(None, None, None)
            except Exception:
                pass
            self._session = None

    def __del__(self):
        self.close()


def stealth_fetch(
    url: str,
    solve_cloudflare: bool = False,
    proxy: str | dict | None = None,
    headless: bool = True,
    timeout: int = 30000,
    wait: int = 2000,
    **kwargs,
) -> ResponseAdapter:
    """
    Single-page fetch that ACTIVELY solves Cloudflare, returning a ResponseAdapter
    with .status_code, .text, .cookies (incl. cf_clearance).

    Primary engine: Scrapling's StealthyFetcher (Camoufox) with solve_cloudflare=True
    — it detects the challenge type (non-interactive / managed / interactive /
    Turnstile) and actually clicks the Turnstile widget, so it clears managed/Turnstile
    challenges that a plain undetected-Chromium load cannot. Verified to obtain
    cf_clearance on Glassdoor (incl. on a flagged datacenter IP) in ~10-25s.

    Fallback: if Camoufox fails to launch (e.g. EPIPE in a constrained env), fall back
    to DynamicFetcher (patchright/undetected Chromium), which clears only the basic /
    non-interactive challenge. Requires `camoufox` + `patchright install chromium`.
    """
    if not SCRAPLING_AVAILABLE:
        raise ImportError("scrapling is required for stealth_fetch — pip install scrapling")

    try:
        resp = StealthyFetcher.fetch(
            url,
            headless=headless,
            solve_cloudflare=True,
            network_idle=True,
            timeout=max(timeout, 90000),
            **({"proxy": proxy} if proxy else {}),
        )
        return ResponseAdapter(resp)
    except Exception as e:
        log.warning(f"StealthyFetcher (Camoufox) failed ({e}); falling back to DynamicFetcher")
        resp = DynamicFetcher.fetch(
            url,
            headless=headless,
            network_idle=True,
            timeout=max(timeout, 60000),
            wait=max(wait, 6000),
            proxy=proxy,
        )
        return ResponseAdapter(resp)


def stealth_fetch_with_cookies(
    url: str,
    solve_cloudflare: bool = True,
    proxy: str | dict | None = None,
    headless: bool = True,
) -> tuple[str, dict]:
    """
    Fetch a page with StealthyFetcher and return (page_html, cookies_dict).

    Useful for getting past Cloudflare, then transferring cookies to a
    lighter HTTP session for subsequent API calls.
    """
    resp = stealth_fetch(url, solve_cloudflare=solve_cloudflare, proxy=proxy, headless=headless)
    return resp.text, resp.cookies
