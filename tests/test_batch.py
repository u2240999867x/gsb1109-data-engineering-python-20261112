import threading
import time

import dns.exception
import dns.resolver

from email_validator import (
    EmailSyntaxError,
    EmailUndeliverableError,
    ValidatedEmail,
    validate_emails,
)


class MXRecord:
    preference = 10
    exchange = "mx.test."


class ControlledResolver:
    """A mock dns.resolver.Resolver that counts queries and can block
    selected domains until a gate is opened."""

    def __init__(self, blocked=()):
        self.queries = []
        self.lock = threading.Lock()
        self.gate = threading.Event()
        self.blocked = set(blocked)

    def resolve(self, domain, rdtype):
        with self.lock:
            self.queries.append((domain, rdtype))
        if domain in self.blocked:
            self.gate.wait(10)
        return [MXRecord()]

    def query_count(self, domain=None):
        with self.lock:
            return len([q for q in self.queries if domain is None or q[0] == domain])


class TrackingSource:
    """A non-rewindable iterator that records how many rows were pulled."""

    def __init__(self, emails):
        self.remaining = list(emails)
        self.pulled = []
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self.remaining:
            raise StopIteration
        email = self.remaining.pop(0)
        self.pulled.append(email)
        return email

    def close(self):
        self.closed = True


def test_batch_order_pulls_and_queries_with_blocked_first_domain():
    emails = [
        "user1@slow-example.com",
        "user2@fast-example.com",
        "user3@fast-example.com",
        "user4@slow-example.com",
        "user5@fast-example.com",
        "user6@fast-example.com",
        "user7@slow-example.com",
        "user8@fast-example.com",
    ]
    resolver = ControlledResolver(blocked={"slow-example.com"})
    source = TrackingSource(emails)
    max_lookahead = 4

    results = []
    gen = validate_emails(source, max_lookahead=max_lookahead, dns_resolver=resolver)
    consumer = threading.Thread(target=lambda: results.extend(gen))
    consumer.start()
    try:
        # While the first domain is blocked, no results come out and the
        # window stays bounded: only max_lookahead rows are pulled, and
        # each unique domain in the window is queried exactly once even
        # though the completed fast rows are stuck behind the slow head.
        time.sleep(0.3)
        assert results == []
        assert len(source.pulled) == max_lookahead
        assert resolver.query_count("slow-example.com") == 1
        assert resolver.query_count("fast-example.com") == 1

        resolver.gate.set()
        consumer.join(10)
        assert not consumer.is_alive()

        # Results come out in input order, the whole source was pulled,
        # and each domain was checked only once for the whole batch.
        assert [email for email, _ in results] == emails
        assert all(isinstance(result, ValidatedEmail) for _, result in results)
        assert all(result.mx == [(10, "mx.test")] for _, result in results)
        assert len(source.pulled) == len(emails)
        assert resolver.query_count("slow-example.com") == 1
        assert resolver.query_count("fast-example.com") == 1
        assert resolver.query_count() == 2
    finally:
        resolver.gate.set()
        consumer.join(10)


def test_unknown_deliverability_shared_within_window_and_retried_later():
    class TimeoutResolver:
        def __init__(self):
            self.queries = []

        def resolve(self, domain, rdtype):
            self.queries.append((domain, rdtype))
            raise dns.exception.Timeout()

    resolver = TimeoutResolver()

    # Rows of the same domain that are incomplete at the same time share
    # a single (unknown) result.
    emails = ["a@flaky-example.com", "b@flaky-example.com"]
    results = list(validate_emails(iter(emails), max_lookahead=2, dns_resolver=resolver))
    assert resolver.queries == [("flaky-example.com", "MX")]
    assert all(isinstance(result, ValidatedEmail) for _, result in results)
    assert all(not hasattr(result, "mx") for _, result in results)

    # Once the group has been fully consumed, a later window retries.
    resolver.queries.clear()
    emails = ["a@flaky-example.com", "b@other-example.com", "c@flaky-example.com"]
    results = list(validate_emails(iter(emails), max_lookahead=2, dns_resolver=resolver))
    assert [q[0] for q in resolver.queries].count("flaky-example.com") == 2
    assert [q[0] for q in resolver.queries].count("other-example.com") == 1
    assert [email for email, _ in results] == emails


def test_syntax_errors_are_kept_per_row():
    resolver = ControlledResolver()
    emails = [
        "good@fast-example.com",
        "not-an-email",
        "also.good@fast-example.com",
        "bad..dots@fast-example.com",
    ]
    results = list(validate_emails(iter(emails), max_lookahead=4, dns_resolver=resolver))

    assert [email for email, _ in results] == emails
    assert isinstance(results[0][1], ValidatedEmail)
    assert isinstance(results[1][1], EmailSyntaxError)
    assert isinstance(results[2][1], ValidatedEmail)
    assert isinstance(results[3][1], EmailSyntaxError)
    # Each failing row keeps its own error...
    assert str(results[1][1]) != str(results[3][1])
    # ...and only the valid rows' shared domain was checked, once.
    assert resolver.queries == [("fast-example.com", "MX")]


def test_undeliverable_domain_is_yielded_as_error_value():
    class NXDomainResolver:
        def resolve(self, domain, rdtype):
            raise dns.resolver.NXDOMAIN()

    emails = ["a@nope-example.com", "b@nope-example.com"]
    results = list(validate_emails(iter(emails), max_lookahead=2, dns_resolver=NXDomainResolver()))
    assert all(isinstance(result, EmailUndeliverableError) for _, result in results)


def test_early_stop_stops_pulling_from_source():
    resolver = ControlledResolver()
    emails = [f"user{i}@fast-example.com" for i in range(10)]
    source = TrackingSource(emails)

    gen = validate_emails(source, max_lookahead=3, dns_resolver=resolver)
    assert next(gen)[0] == emails[0]
    assert next(gen)[0] == emails[1]
    gen.close()

    # Only the primed window plus one refetch per consumed row was pulled.
    assert len(source.pulled) <= 3 + 2
    assert len(source.pulled) < len(emails)
    assert source.closed


def test_no_deliverability_check_needs_no_dns():
    class ExplodingResolver:
        def resolve(self, domain, rdtype):
            raise AssertionError("DNS must not be queried")

    emails = ["a@fast-example.com", "b@fast-example.com"]
    results = list(validate_emails(
        iter(emails), max_lookahead=2,
        check_deliverability=False, dns_resolver=ExplodingResolver(),
    ))
    assert all(isinstance(result, ValidatedEmail) for _, result in results)
