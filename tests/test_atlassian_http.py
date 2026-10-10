from __future__ import annotations

import email.utils
import json
import ssl
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from knowledge.atlassian_http import (
    HttpResponse,
    RateLimitedClient,
    RequestBudget,
    SafeRedirectHandler,
    build_auth_headers,
    make_ssl_context,
    parse_retry_after,
)
from knowledge.models import (
    AtlassianError,
    BudgetExceeded,
    CrawlStopped,
    ForbiddenError,
    HostNotAllowed,
    InvalidResponseError,
    NotFoundError,
)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class FakeEvent:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self._set = False
        self.waits: list[float] = []
        self.stop_during_wait = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None:
            self.waits.append(timeout)
            if self.stop_during_wait:
                self._set = True
                return True
            self.clock.advance(timeout)
        return self._set


class TestAtlassianHttp(unittest.TestCase):
    def test_make_ssl_context(self):
        ctx_no_verify = make_ssl_context(verify_ssl=False)
        self.assertEqual(ctx_no_verify.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx_no_verify.check_hostname)

        ctx_verify = make_ssl_context(verify_ssl=True)
        self.assertNotEqual(ctx_verify.verify_mode, ssl.CERT_NONE)

    def test_build_auth_headers(self):
        self.assertEqual(build_auth_headers(None), {})
        self.assertEqual(build_auth_headers({"type": "none"}), {})
        self.assertEqual(
            build_auth_headers({"type": "bearer", "token": "secret123"}),
            {"Authorization": "Bearer secret123"},
        )
        basic = build_auth_headers({"type": "basic", "username": "admin", "token": "pass"})
        self.assertTrue(basic["Authorization"].startswith("Basic "))

    def test_budget_consumption_and_limits(self):
        clock = FakeClock(100.0)
        budget = RequestBudget(max_requests=2, max_duration_seconds=60, clock=clock)
        self.assertEqual(budget.used, 0)
        budget.consume()
        self.assertEqual(budget.used, 1)
        budget.consume()
        self.assertEqual(budget.used, 2)
        with self.assertRaises(BudgetExceeded):
            budget.consume()

    def test_budget_time_limit(self):
        clock = FakeClock(100.0)
        budget = RequestBudget(max_requests=10, max_duration_seconds=5, clock=clock)
        budget.consume()
        clock.advance(10.0)
        with self.assertRaises(BudgetExceeded):
            budget.consume()

    def test_sleep_spacing_between_request_starts(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)
        call_times = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            call_times.append(clock())
            clock.advance(0.2)  # transport takes 0.2s
            return HttpResponse(200, {"content-type": "application/json"}, b'{"ok": true}')

        client = RateLimitedClient(
            source="confluence",
            base_url="https://confluence.intern",
            allowed_bases=(),
            sleep_seconds=1.0,
            timeout=10,
            max_retries=2,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            clock=clock,
            rand=lambda: 0.0,
        )

        client.get_json("/rest/api/content/1")
        # Request 1 starts at 1000.0, transport finishes at 1000.2
        self.assertEqual(call_times[0], 1000.0)

        client.get_json("/rest/api/content/2")
        # Request 2 should wait 1.0 - 0.2 = 0.8s, starting at 1001.0
        self.assertEqual(call_times[1], 1001.0)
        self.assertAlmostEqual(event.waits[0], 0.8)

    def test_sleep_seconds_zero(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)

        def transport(url, headers, timeout, ssl_context, max_bytes):
            return HttpResponse(200, {"content-type": "application/json"}, b'{"ok": true}')

        client = RateLimitedClient(
            source="confluence",
            base_url="https://confluence.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=2,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            clock=clock,
        )

        client.get_json("/rest/api/1")
        client.get_json("/rest/api/2")
        self.assertEqual(len(event.waits), 0)

    def test_429_retry_after_seconds_and_http_date(self):
        # 1. Retry-After as seconds string
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)
        responses = [
            HttpResponse(429, {"retry-after": "5"}, b"rate limited"),
            HttpResponse(200, {"content-type": "application/json"}, b'{"status": "ok"}'),
        ]

        def transport(url, headers, timeout, ssl_context, max_bytes):
            return responses.pop(0)

        events = []
        client = RateLimitedClient(
            source="jira",
            base_url="https://jira.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=2,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            on_event=events.append,
            clock=clock,
        )

        data = client.get_json("/rest/api/2/issue/TEST-1")
        self.assertEqual(data, {"status": "ok"})
        self.assertIn(5.0, event.waits)
        wait_events = [e for e in events if e.get("phase") == "rate_limit_wait"]
        self.assertEqual(wait_events[0]["reason"], "429")
        self.assertEqual(wait_events[0]["seconds"], 5.0)

        # 2. Retry-After as HTTP date
        future_dt = datetime.now(timezone.utc) + timedelta(seconds=12)
        date_str = email.utils.format_datetime(future_dt)
        parsed = parse_retry_after(date_str)
        self.assertTrue(10.0 <= parsed <= 13.0)

    def test_503_retry_with_backoff(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)
        responses = [
            HttpResponse(503, {}, b"Service Unavailable"),
            HttpResponse(503, {}, b"Service Unavailable"),
            HttpResponse(200, {"content-type": "application/json"}, b'{"done": true}'),
        ]

        def transport(url, headers, timeout, ssl_context, max_bytes):
            return responses.pop(0)

        events = []
        client = RateLimitedClient(
            source="jira",
            base_url="https://jira.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=3,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            on_event=events.append,
            clock=clock,
            rand=lambda: 0.0,  # 0 jitter
        )

        res = client.get_json("/rest/api/2/issue/TEST-1")
        self.assertEqual(res, {"done": True})
        # Attempt 1 backoff: 2**1 = 2.0s; Attempt 2 backoff: 2**2 = 4.0s
        self.assertEqual(event.waits, [2.0, 4.0])
        wait_events = [e for e in events if e.get("phase") == "rate_limit_wait"]
        self.assertEqual(len(wait_events), 2)
        self.assertEqual(wait_events[0]["reason"], "retry")
        self.assertEqual(wait_events[1]["reason"], "retry")

    def test_max_retries_exhausted(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)

        def transport(url, headers, timeout, ssl_context, max_bytes):
            return HttpResponse(503, {}, b"Server error")

        client = RateLimitedClient(
            source="jira",
            base_url="https://jira.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=2,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            clock=clock,
            rand=lambda: 0.0,
        )

        with self.assertRaises(AtlassianError) as cm:
            client.get_json("/rest/api/2/issue/TEST-1")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(budget.used, 3)  # initial + 2 retries

    def test_each_attempt_counts_against_budget_and_no_call_when_exceeded(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        # Budget only allows 1 request
        budget = RequestBudget(max_requests=1, max_duration_seconds=60, clock=clock)
        calls = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            calls.append(url)
            return HttpResponse(503, {}, b"Unavailable")

        client = RateLimitedClient(
            source="confluence",
            base_url="https://confluence.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=3,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            clock=clock,
            rand=lambda: 0.0,
        )

        # Attempt 1 consumes the 1 budget token, then fails with 503.
        # Next attempt tries to consume budget and raises BudgetExceeded BEFORE transport.
        with self.assertRaises(BudgetExceeded):
            client.get_json("/rest/api/content/1")
        self.assertEqual(len(calls), 1)

    def test_stop_during_sleep_raises_crawl_stopped_and_no_transport(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)
        calls = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            calls.append(url)
            return HttpResponse(200, {"content-type": "application/json"}, b'{"ok": true}')

        client = RateLimitedClient(
            source="confluence",
            base_url="https://confluence.intern",
            allowed_bases=(),
            sleep_seconds=5.0,
            timeout=10,
            max_retries=0,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            clock=clock,
        )

        # Request 1 succeeds
        client.get_json("/rest/api/1")
        self.assertEqual(len(calls), 1)

        # Now configure fake event to stop during wait
        event.stop_during_wait = True
        with self.assertRaises(CrawlStopped):
            client.get_json("/rest/api/2")
        # No second call made to transport!
        self.assertEqual(len(calls), 1)

    def test_404_not_found_and_403_forbidden_no_retry(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)
        calls = 0

        def transport_404(url, headers, timeout, ssl_context, max_bytes):
            nonlocal calls
            calls += 1
            return HttpResponse(404, {}, b"Not found")

        client = RateLimitedClient(
            source="jira",
            base_url="https://jira.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=3,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport_404,
            clock=clock,
        )

        with self.assertRaises(NotFoundError):
            client.get_json("/rest/api/2/issue/NOPE-1")
        self.assertEqual(calls, 1)

        calls = 0

        def transport_403(url, headers, timeout, ssl_context, max_bytes):
            nonlocal calls
            calls += 1
            return HttpResponse(403, {}, b"Forbidden")

        client.transport = transport_403
        with self.assertRaises(ForbiddenError):
            client.get_json("/rest/api/2/issue/SECRET-1")
        self.assertEqual(calls, 1)

    def test_host_not_allowed_without_request(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)
        calls = []

        def transport(url, headers, timeout, ssl_context, max_bytes):
            calls.append(url)
            return HttpResponse(200, {}, b"{}")

        client = RateLimitedClient(
            source="confluence",
            base_url="https://confluence.intern",
            allowed_bases=("https://alias.intern",),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=0,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            clock=clock,
        )

        with self.assertRaises(HostNotAllowed):
            client.get_json("https://evil.external.com/rest/api/content")
        self.assertEqual(len(calls), 0)
        self.assertEqual(budget.used, 0)

        # Allowed alias works
        client.get_json("https://alias.intern/rest/api/content")
        self.assertEqual(len(calls), 1)

    def test_response_too_large(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)

        def transport(url, headers, timeout, ssl_context, max_bytes):
            return HttpResponse(200, {"content-type": "application/json"}, b"x" * 100)

        client = RateLimitedClient(
            source="confluence",
            base_url="https://confluence.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=0,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            max_response_bytes=50,
            transport=transport,
            clock=clock,
        )

        with self.assertRaises(InvalidResponseError):
            client.get_json("/rest/api/large")

    def test_html_200_login_redirect(self):
        clock = FakeClock(1000.0)
        event = FakeEvent(clock)
        budget = RequestBudget(max_requests=10, max_duration_seconds=60, clock=clock)

        def transport(url, headers, timeout, ssl_context, max_bytes):
            return HttpResponse(
                200,
                {"content-type": "text/html; charset=utf-8"},
                b"<html><body>Please log in</body></html>",
            )

        client = RateLimitedClient(
            source="confluence",
            base_url="https://confluence.intern",
            allowed_bases=(),
            sleep_seconds=0.0,
            timeout=10,
            max_retries=0,
            stop_event=event,  # type: ignore
            budget=budget,
            ssl_context=None,
            auth_headers={},
            transport=transport,
            clock=clock,
        )

        with self.assertRaises(ForbiddenError):
            client.get_json("/rest/api/login")

    def test_safe_redirect_handler(self):
        handler = SafeRedirectHandler()
        req = urllib.request.Request("https://internal.site/path")
        # Same host redirect allowed
        res = handler.redirect_request(req, None, 302, "Found", {}, "https://internal.site/other")  # type: ignore
        self.assertIsNotNone(res)
        # Foreign host redirect rejected (returns None)
        res_evil = handler.redirect_request(req, None, 302, "Found", {}, "https://evil.site/other")  # type: ignore
        self.assertIsNone(res_evil)


if __name__ == "__main__":
    unittest.main()
