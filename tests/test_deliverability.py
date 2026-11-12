from typing import Any

import pytest
import re

import dns.exception
import dns.resolver

from email_validator import EmailUndeliverableError, \
                            validate_email, caching_resolver
from email_validator import deliverability
from email_validator.deliverability import validate_email_deliverability

from mocked_dns_response import MockedDnsResponseData, MockedDnsResponseDataCleanup  # noqa: F401

RESOLVER = MockedDnsResponseData.create_resolver()


@pytest.mark.parametrize(
    'domain,expected_response',
    [
        ('gmail.com', {'mx': [(5, 'gmail-smtp-in.l.google.com'), (10, 'alt1.gmail-smtp-in.l.google.com'), (20, 'alt2.gmail-smtp-in.l.google.com'), (30, 'alt3.gmail-smtp-in.l.google.com'), (40, 'alt4.gmail-smtp-in.l.google.com')], 'mx_fallback_type': None}),
        ('pages.github.com', {'mx': [(0, 'pages.github.com')], 'mx_fallback_type': 'A'}),
    ],
)
def test_deliverability_found(domain: str, expected_response: str) -> None:
    response = validate_email_deliverability(domain, domain, dns_resolver=RESOLVER)
    assert response == expected_response


@pytest.mark.parametrize(
    'domain,error',
    [
        ('xkxufoekjvjfjeodlfmdfjcu.com', 'The domain name {domain} does not exist'),
        ('example.com', 'The domain name {domain} does not accept email'),  # Null MX record
        ('g.mail.com', 'The domain name {domain} does not accept email'),  # No MX record but invalid AAAA record fallback (issue #134)
        ('nellis.af.mil', 'The domain name {domain} does not send email'),  # No MX record, A record fallback, reject-all SPF record.

        # No MX or A/AAAA records, but some other DNS records must
        # exist such that the response is NOANSWER instead of NXDOMAIN.
        ('justtxt.joshdata.me', 'The domain name {domain} does not accept email'),
        ('ipv6only.joshdata.me', 'The domain name {domain} does not accept email'),
    ],
)
def test_deliverability_fails(domain: str, error: str) -> None:
    with pytest.raises(EmailUndeliverableError, match=error.format(domain=domain)):
        validate_email_deliverability(domain, domain, dns_resolver=RESOLVER)


@pytest.mark.parametrize(
    'email_input',
    [
        ('me@mail.example'),
        ('me@example.com'),
        ('me@mail.example.com'),
    ],
)
def test_email_example_reserved_domain(email_input: str) -> None:
    # Since these all fail deliverabiltiy from a static list,
    # DNS deliverability checks do not arise.
    with pytest.raises(EmailUndeliverableError) as exc_info:
        validate_email(email_input, dns_resolver=RESOLVER)
    # print(f'({email_input!r}, {str(exc_info.value)!r}),')
    assert re.match(r"The domain name [a-z\.]+ does not (accept email|exist)\.", str(exc_info.value)) is not None


def test_deliverability_dns_timeout() -> None:
    response = validate_email_deliverability('timeout.com', 'timeout.com', dns_resolver=RESOLVER)
    assert "mx" not in response
    assert response.get("unknown-deliverability") == "timeout"


def test_timeout_with_resolver_does_not_mutate_resolver() -> None:
    # A per-email timeout may now be combined with a shared resolver. It must
    # override the resolver's default budget for this email only and leave the
    # resolver's own lifetime untouched so other emails keep their budgets.
    class ImmediateTimeoutResolver:
        def __init__(self) -> None:
            self.lifetime = 15
            self.cache: Any = None

        def resolve(self, qname: Any, rdtype: Any, lifetime: Any = None) -> Any:
            # The per-query lifetime, not resolver.lifetime, bounds the query.
            assert lifetime is not None and lifetime <= 10
            raise dns.exception.Timeout()  # type: ignore [no-untyped-call]

    resolver: Any = ImmediateTimeoutResolver()
    response = validate_email_deliverability(
        'timeout.com', 'timeout.com', timeout=10, dns_resolver=resolver
    )
    assert response.get("unknown-deliverability") == "timeout"
    assert resolver.lifetime == 15


class FakeClock:
    """A manually advanced clock standing in for time.time()."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class OfflineAnswer:
    """Mocks dns.resolver.Answer for the record types deliverability.py reads."""

    def __init__(self, rdatas: list[Any]) -> None:
        self.rrset = rdatas

    def __iter__(self) -> Any:
        return iter(self.rrset)


class OfflineResolver:
    """
    A fully offline resolver. resolve() never blocks on real time: every query
    consumes a simulated delay from the injected clock and is bounded by the
    per-query lifetime (this email's remaining budget). Successful answers go
    into a shared cache, and delay/timeout rules can differ per domain.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.lifetime = 15
        self.cache: dict[Any, Any] = {}
        # domain -> {rdtype -> (delay, answer-or-exception)}
        self.script: dict[str, dict[str, tuple[float, Any]]] = {}

    def add(self, domain: str, rdtype: str, delay: float, answer: Any) -> None:
        self.script.setdefault(domain, {})[rdtype] = (delay, answer)

    @staticmethod
    def _key(qname: Any, rdtype: Any) -> tuple[str, Any]:
        return (qname if isinstance(qname, str) else qname.to_text(omit_final_dot=True), rdtype)

    def resolve(self, qname: Any, rdtype: Any, lifetime: Any = None, **kwargs: Any) -> Any:
        if lifetime is None:
            lifetime = self.lifetime
        if lifetime <= 0:
            raise dns.exception.Timeout()  # type: ignore [no-untyped-call]

        key = self._key(qname, rdtype)
        cached = self.cache.get(key)
        if cached is not None:
            # Shared cache: a cached answer is instant and costs no budget.
            if isinstance(cached, Exception):
                raise cached
            return cached

        delay, answer = self.script[key[0]][key[1]]
        self.clock.advance(delay)
        if delay > lifetime:
            # The query's simulated wait exhausted this email's remaining budget.
            raise dns.exception.Timeout()  # type: ignore [no-untyped-call]

        if isinstance(answer, Exception):
            self.cache[key] = answer
            raise answer
        self.cache[key] = answer
        return answer


def _mx_answer(*exchanges: tuple[int, str]) -> OfflineAnswer:
    class MxRdata:
        def __init__(self, preference: int, exchange: str) -> None:
            self.preference = preference
            self.exchange = exchange

    return OfflineAnswer([MxRdata(pref, host) for pref, host in exchanges])


def _a_answer(*addresses: str) -> OfflineAnswer:
    class ARdata:
        def __init__(self, address: str) -> None:
            self.address = address

    return OfflineAnswer([ARdata(address) for address in addresses])


def _no_answer() -> dns.resolver.NoAnswer:
    return dns.resolver.NoAnswer()  # type: ignore [no-untyped-call]


@pytest.fixture
def patched_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(deliverability, "_time", clock.time)
    return clock


def test_second_step_gets_only_remaining_budget(patched_clock: FakeClock) -> None:
    # Reproduce "the second step only has partial time left": a 10s total
    # budget, the MX query takes 6s, so the A fallback must run with a 4s
    # per-query lifetime even though the resolver's default lifetime is 15s.
    resolver: Any = OfflineResolver(patched_clock)
    domain = "fallback.example"
    resolver.add(domain, "MX", 6, _no_answer())
    seen_lifetimes: list[float] = []

    original_resolve = resolver.resolve

    def recording_resolve(qname: Any, rdtype: Any, lifetime: Any = None, **kwargs: Any) -> Any:
        seen_lifetimes.append(float(lifetime))
        return original_resolve(qname, rdtype, lifetime, **kwargs)

    resolver.resolve = recording_resolve
    resolver.add(domain, "A", 3, _a_answer("8.8.8.10"))
    # A successful A fallback is followed by an SPF TXT check; no record here.
    resolver.add(domain, "TXT", 0, _no_answer())

    response = validate_email_deliverability(
        domain, domain, timeout=10, dns_resolver=resolver
    )

    assert response == {"mx": [(0, domain)], "mx_fallback_type": "A"}
    # MX got the full 10s; the A fallback only got the 4s that remained and
    # the TXT check got the 1s left after that.
    assert seen_lifetimes == [10.0, 4.0, 1.0]


def test_timeout_marks_only_that_email_unknown_and_keeps_resolver_usable(patched_clock: FakeClock) -> None:
    # The MX step consumes most of the budget; a slow A fallback then exceeds
    # the remaining time. Only that email is marked temporarily unknown. The
    # shared resolver keeps its configured lifetime and its cache, and another
    # email processed on it still succeeds.
    resolver: Any = OfflineResolver(patched_clock)
    slow_domain = "slow-fallback.example"
    healthy_domain = "healthy.example"
    resolver.add(slow_domain, "MX", 6, _no_answer())
    resolver.add(slow_domain, "A", 5, _a_answer("8.8.8.20"))
    resolver.add(healthy_domain, "MX", 1, _mx_answer((10, "mail.healthy.example")))

    response = validate_email_deliverability(
        slow_domain, slow_domain, timeout=10, dns_resolver=resolver
    )
    assert "mx" not in response
    assert response.get("unknown-deliverability") == "timeout"
    # The shared resolver's own configuration was never modified.
    assert resolver.lifetime == 15

    # Another email on the same resolver still completes normally.
    other = validate_email_deliverability(
        healthy_domain, healthy_domain, timeout=10, dns_resolver=resolver
    )
    assert other["mx"] == [(10, "mail.healthy.example")]

    # The slow email's cached MX negative answer is replayed instantly on retry.
    patched_clock.advance(100)
    with pytest.raises(dns.resolver.NoAnswer):
        resolver.resolve(slow_domain, "MX", lifetime=10)


def test_interleaved_emails_have_independent_deadlines_sharing_cache(patched_clock: FakeClock) -> None:
    # Two emails interleave on one shared resolver. The first email's slow MX
    # query times out against its own short deadline, while the second email's
    # later-started, longer deadline still succeeds using answers fetched via
    # the same resolver and cache.
    resolver: Any = OfflineResolver(patched_clock)
    shared_domain = "shared.example"
    resolver.add(shared_domain, "MX", 4, _mx_answer((10, "mail.shared.example")))

    # Email one: a 2s budget cannot cover the 4s MX wait.
    first = validate_email_deliverability(
        shared_domain, shared_domain, timeout=2, dns_resolver=resolver
    )
    assert first.get("unknown-deliverability") == "timeout"

    # The failed query must not have poisoned the cache.
    assert not any(name == shared_domain for name, _ in resolver.cache)

    # Time keeps moving; email two starts later with its own fresh, longer budget.
    patched_clock.advance(50)
    second = validate_email_deliverability(
        shared_domain, shared_domain, timeout=10, dns_resolver=resolver
    )
    assert second == {
        "mx": [(10, "mail.shared.example")],
        "mx_fallback_type": None,
    }

    # A third email now gets the MX answer from the shared cache for free,
    # regardless of its own tight deadline -- the answer was already obtained.
    patched_clock.advance(50)
    third = validate_email_deliverability(
        shared_domain, shared_domain, timeout=1, dns_resolver=resolver
    )
    assert third["mx"] == [(10, "mail.shared.example")]


@pytest.mark.network
def test_caching_dns_resolver() -> None:
    class TestCache:
        def __init__(self) -> None:
            self.cache: dict[Any, Any] = {}

        def get(self, key: Any) -> Any:
            return self.cache.get(key)

        def put(self, key: Any, value: Any) -> Any:
            self.cache[key] = value

    cache = TestCache()
    resolver = caching_resolver(timeout=1, cache=cache)
    validate_email("test@gmail.com", dns_resolver=resolver)
    assert len(cache.cache) == 1

    validate_email("test@gmail.com", dns_resolver=resolver)
    assert len(cache.cache) == 1
