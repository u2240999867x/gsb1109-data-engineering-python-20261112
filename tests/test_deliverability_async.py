from typing import Any, Dict, List, Optional, Tuple

import asyncio
import pytest

import dns.asyncresolver
import dns.name
import dns.rdataclass
import dns.rdatatype

from email_validator import EmailUndeliverableError
from email_validator.deliverability import (
    validate_email_deliverability,
    validate_email_deliverability_async,
    _in_flight_checks,
)

from mocked_dns_response import MockedDnsResponseData, MockedDnsResponseDataCleanup  # noqa: F401

# A resolver for the synchronous entry point backed by the saved DNS
# responses, and the underlying data for the stub async resolver.
SYNC_RESOLVER = MockedDnsResponseData.create_resolver()
DATA = MockedDnsResponseData.INSTANCE


class StubAsyncResolver:
    # A dns.asyncresolver.Resolver-like object that answers from the
    # saved DNS responses, records every query it receives, and can be
    # paused (per record type) just as a query is about to be answered.
    def __init__(self, data: Any) -> None:
        self.data = data
        self.lifetime: Optional[float] = None
        self.queries: List[Tuple[str, str]] = []
        self.pause_gates: Dict[str, asyncio.Event] = {}
        self.started_events: Dict[str, asyncio.Event] = {}

    def pause_on(self, rdtype: str) -> Tuple[asyncio.Event, asyncio.Event]:
        # Returns (started, gate): 'started' is set once a query of this
        # type has been issued, and the query only completes once 'gate'
        # is set.
        started = asyncio.Event()
        gate = asyncio.Event()
        self.started_events[rdtype] = started
        self.pause_gates[rdtype] = gate
        return started, gate

    async def resolve(self, qname: Any, rdtype: Any, *args: Any, **kwargs: Any) -> Any:
        rdtype = str(rdtype)
        self.queries.append((str(qname), rdtype))
        started = self.started_events.get(rdtype)
        if started is not None:
            started.set()
        gate = self.pause_gates.get(rdtype)
        if gate is not None:
            await gate.wait()
        key = (dns.name.from_text(str(qname)),
               dns.rdatatype.from_text(rdtype),
               dns.rdataclass.IN)
        return self.data.get(key)


@pytest.mark.parametrize(
    'domain',
    [
        ('gmail.com'),  # MX records
        ('pages.github.com'),  # no MX, A record fallback
        ('xkxufoekjvjfjeodlfmdfjcu.com'),  # NXDOMAIN
        ('example.com'),  # Null MX record
        ('g.mail.com'),  # no MX record but invalid AAAA record fallback (issue #134)
        ('nellis.af.mil'),  # no MX record, A record fallback, reject-all SPF record
        ('justtxt.joshdata.me'),  # no MX or A/AAAA records
        ('ipv6only.joshdata.me'),  # no MX or A/AAAA records
        ('timeout.com'),  # DNS timeout
    ],
)
def test_async_deliverability_matches_sync(domain: str) -> None:
    sync_result: Any = None
    sync_error: Optional[EmailUndeliverableError] = None
    try:
        sync_result = validate_email_deliverability(domain, domain, dns_resolver=SYNC_RESOLVER)
    except EmailUndeliverableError as e:
        sync_error = e

    async def run() -> Tuple[Any, Optional[EmailUndeliverableError]]:
        try:
            return await validate_email_deliverability_async(domain, domain, dns_resolver=StubAsyncResolver(DATA)), None
        except EmailUndeliverableError as e:
            return None, e

    async_result, async_error = asyncio.run(run())

    if sync_error is not None:
        assert async_error is not None
        assert str(async_error) == str(sync_error)
    else:
        assert async_error is None
        assert async_result == sync_result


def test_async_concurrent_rows_share_in_flight_query() -> None:
    async def run() -> Tuple[Any, Any, StubAsyncResolver]:
        resolver = StubAsyncResolver(DATA)
        mx_started, mx_gate = resolver.pause_on("MX")
        task1 = asyncio.ensure_future(validate_email_deliverability_async("gmail.com", "gmail.com", dns_resolver=resolver))
        task2 = asyncio.ensure_future(validate_email_deliverability_async("gmail.com", "gmail.com", dns_resolver=resolver))
        await mx_started.wait()
        mx_gate.set()
        return await task1, await task2, resolver

    result1, result2, resolver = asyncio.run(run())

    expected = {'mx': [(5, 'gmail-smtp-in.l.google.com'), (10, 'alt1.gmail-smtp-in.l.google.com'), (20, 'alt2.gmail-smtp-in.l.google.com'), (30, 'alt3.gmail-smtp-in.l.google.com'), (40, 'alt4.gmail-smtp-in.l.google.com')], 'mx_fallback_type': None}
    assert result1 == expected
    assert result2 == expected
    # The two rows shared a single in-flight MX query.
    assert resolver.queries == [("gmail.com", "MX")]


def test_async_sharing_isolated_by_resolver() -> None:
    async def run() -> Tuple[StubAsyncResolver, StubAsyncResolver]:
        resolver1 = StubAsyncResolver(DATA)
        resolver2 = StubAsyncResolver(DATA)
        await asyncio.gather(
            validate_email_deliverability_async("gmail.com", "gmail.com", dns_resolver=resolver1),
            validate_email_deliverability_async("gmail.com", "gmail.com", dns_resolver=resolver2),
        )
        return resolver1, resolver2

    resolver1, resolver2 = asyncio.run(run())

    # Same domain but different resolvers: the queries are not shared.
    assert resolver1.queries == [("gmail.com", "MX")]
    assert resolver2.queries == [("gmail.com", "MX")]


def test_async_sharing_isolated_by_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> StubAsyncResolver:
        resolver = StubAsyncResolver(DATA)
        monkeypatch.setattr(dns.asyncresolver, "get_default_resolver", lambda: resolver)
        mx_started, mx_gate = resolver.pause_on("MX")
        task1 = asyncio.ensure_future(validate_email_deliverability_async("gmail.com", "gmail.com", timeout=5))
        task2 = asyncio.ensure_future(validate_email_deliverability_async("gmail.com", "gmail.com", timeout=10))
        await mx_started.wait()
        await asyncio.sleep(0)  # let both checks reach the paused MX query
        mx_gate.set()
        await asyncio.gather(task1, task2)
        return resolver

    resolver = asyncio.run(run())

    # Same domain and resolver but different check policies (timeouts):
    # the queries are not shared.
    assert resolver.queries == [("gmail.com", "MX"), ("gmail.com", "MX")]


def test_async_timeout_and_resolver() -> None:
    async def run() -> None:
        await validate_email_deliverability_async('timeout.com', 'timeout.com', timeout=1, dns_resolver=StubAsyncResolver(DATA))

    with pytest.raises(ValueError, match="It's not valid to pass both timeout and dns_resolver."):
        asyncio.run(run())


def test_async_cancel_first_row_second_row_continues() -> None:
    async def run() -> Tuple[Any, StubAsyncResolver]:
        resolver = StubAsyncResolver(DATA)
        a_started, a_gate = resolver.pause_on("A")
        task1 = asyncio.ensure_future(validate_email_deliverability_async("pages.github.com", "pages.github.com", dns_resolver=resolver))
        task2 = asyncio.ensure_future(validate_email_deliverability_async("pages.github.com", "pages.github.com", dns_resolver=resolver))

        # Wait until the shared check has fallen back to the A record
        # query, then cancel the first row while the query is paused.
        await a_started.wait()
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1

        # The second row is not affected by the first row's cancellation.
        assert not task2.done()
        a_gate.set()
        result2 = await task2
        await asyncio.sleep(0)  # let the done callback clean up the registry
        return result2, resolver

    result2, resolver = asyncio.run(run())

    assert result2 == {'mx': [(0, 'pages.github.com')], 'mx_fallback_type': 'A'}
    # One shared query per step: no duplicate MX/A queries and, since the
    # A fallback succeeded, no AAAA query. The SPF TXT check follows.
    assert resolver.queries == [
        ("pages.github.com", "MX"),
        ("pages.github.com", "A"),
        ("pages.github.com", "TXT"),
    ]
    assert _in_flight_checks == {}


def test_async_last_waiter_leaving_stops_followup_queries() -> None:
    async def run() -> StubAsyncResolver:
        resolver = StubAsyncResolver(DATA)
        a_started, a_gate = resolver.pause_on("A")
        task1 = asyncio.ensure_future(validate_email_deliverability_async("pages.github.com", "pages.github.com", dns_resolver=resolver))
        task2 = asyncio.ensure_future(validate_email_deliverability_async("pages.github.com", "pages.github.com", dns_resolver=resolver))

        # Wait until the shared check is about to issue the A record
        # fallback query, then have both rows give up on it.
        await a_started.wait()
        task1.cancel()
        task2.cancel()
        for task in (task1, task2):
            with pytest.raises(asyncio.CancelledError):
                await task

        # Even once the paused query could have completed, no follow-up
        # queries are issued because no waiter is left.
        a_gate.set()
        await asyncio.sleep(0.05)
        return resolver

    resolver = asyncio.run(run())

    # The A query was started but cancelled in flight, and the AAAA/TXT
    # next hops were never issued.
    assert resolver.queries == [("pages.github.com", "MX"), ("pages.github.com", "A")]
    assert _in_flight_checks == {}


def test_async_each_waiter_keeps_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> Tuple[Any, Any, StubAsyncResolver]:
        resolver = StubAsyncResolver(DATA)
        monkeypatch.setattr(dns.asyncresolver, "get_default_resolver", lambda: resolver)
        a_started, a_gate = resolver.pause_on("A")
        task1 = asyncio.ensure_future(validate_email_deliverability_async("pages.github.com", "pages.github.com", timeout=1))
        await a_started.wait()
        await asyncio.sleep(0.5)  # both rows now share the paused check
        task2 = asyncio.ensure_future(validate_email_deliverability_async("pages.github.com", "pages.github.com", timeout=1))
        result1 = await task1  # first row's deadline expires while paused
        a_gate.set()
        result2 = await task2  # second row's later deadline is still met
        return result1, result2, resolver

    result1, result2, resolver = asyncio.run(run())

    # The first row gets the same "temporarily unknown" result that the
    # synchronous entry point returns on a DNS timeout...
    assert result1 == {"unknown-deliverability": "timeout"}
    # ...while the second row, with time left on its own deadline, gets
    # the real result from the shared check.
    assert result2 == {'mx': [(0, 'pages.github.com')], 'mx_fallback_type': 'A'}
    assert resolver.queries == [
        ("pages.github.com", "MX"),
        ("pages.github.com", "A"),
        ("pages.github.com", "TXT"),
    ]
