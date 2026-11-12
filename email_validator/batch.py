from typing import Any, Iterator, Optional, Tuple, Union
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor

from .types import ValidatedEmail
from .validate_email import validate_email

BatchResult = Tuple[Any, Union[ValidatedEmail, Exception]]


class _DnsGroup:
    """One in-flight DNS deliverability check shared by all rows in the
    current window that have the same normalized ASCII domain and policy."""

    __slots__ = ("future", "refs")

    def __init__(self, future: Future) -> None:
        self.future = future
        self.refs = 0  # number of not-yet-consumed rows waiting on this check


class _Row:
    __slots__ = ("email", "result", "validated", "group", "key")

    def __init__(self, email: Any) -> None:
        self.email = email
        self.result: Union[ValidatedEmail, Exception, None] = None
        self.validated: Optional[ValidatedEmail] = None
        self.group: Optional[_DnsGroup] = None
        self.key: Any = None


def validate_emails(
    emails: Any,
    *,
    max_lookahead: int = 16,
    max_workers: Optional[int] = None,
    **kwargs: Any
) -> Iterator[BatchResult]:
    """
    Validate a stream of email addresses lazily and yield
    (input, result) tuples in input order, where result is a
    ValidatedEmail on success or the EmailNotValidError (or other
    exception) that the row raised. Errors are yielded as values so
    one bad row does not abort the batch.

    `emails` may be any iterable, including a long non-rewindable
    iterator. At most `max_lookahead` rows are read ahead of the row
    currently being yielded, so the source is only pulled as results
    are consumed, completed results buffered behind a slow row are
    bounded by the window, and stopping iteration early stops pulling
    from the source.

    Within the rows currently in flight, rows that share the same
    normalized ASCII domain and validation policy share a single DNS
    deliverability check. The shared result --- including "unknown"
    outcomes such as DNS timeouts --- is only shared while those rows
    are incomplete; once every row of a group has been consumed the
    group is dropped, so rows read in later windows are checked again
    (temporary failures may be retried). A row that fails syntax
    validation keeps its own error and triggers no DNS check.

    Keyword arguments are the same as for validate_email and apply to
    every row. Passing both timeout and dns_resolver is an error, as
    in validate_email.
    """

    if max_lookahead < 1:
        raise ValueError("max_lookahead must be at least 1")
    if kwargs.get("timeout") is not None and kwargs.get("dns_resolver") is not None:
        raise ValueError("It's not valid to pass both timeout and dns_resolver.")
    import inspect
    valid_kwargs = set(inspect.signature(validate_email).parameters) - {"email"}
    unknown_kwargs = set(kwargs) - valid_kwargs
    if unknown_kwargs:
        raise TypeError("Unexpected keyword argument(s): " + ", ".join(sorted(unknown_kwargs)))

    from . import CHECK_DELIVERABILITY, TEST_ENVIRONMENT
    check_deliverability = kwargs.get("check_deliverability")
    if check_deliverability is None:
        check_deliverability = CHECK_DELIVERABILITY
    test_environment = kwargs.get("test_environment")
    if test_environment is None:
        test_environment = TEST_ENVIRONMENT
    do_dns = bool(check_deliverability) and not test_environment

    # The dedup key combines the normalized ASCII domain with the
    # validation policy so only truly identical checks are shared.
    policy = tuple(sorted((key, repr(value)) for key, value in kwargs.items()))

    syntax_kwargs = dict(kwargs)
    syntax_kwargs["check_deliverability"] = False

    source = iter(emails)
    inflight: deque[_Row] = deque()
    groups: dict[Any, _DnsGroup] = {}
    executor: Optional[ThreadPoolExecutor] = None

    def submit_dns(ascii_domain: str, domain: str) -> Future:
        nonlocal executor
        if executor is None:
            workers = max_workers if max_workers is not None else min(32, max_lookahead)
            executor = ThreadPoolExecutor(max_workers=max(1, workers))
        # Lazy load `deliverability` as it is slow to import (due to dns.resolver).
        from .deliverability import validate_email_deliverability
        return executor.submit(
            validate_email_deliverability,
            ascii_domain, domain, kwargs.get("timeout"), kwargs.get("dns_resolver")
        )

    def pull() -> bool:
        # Read one row from the source and start whatever work it needs.
        try:
            email = next(source)
        except StopIteration:
            return False
        row = _Row(email)
        try:
            validated = validate_email(email, **syntax_kwargs)
        except Exception as e:
            # A syntax (or other per-row) failure keeps its own error.
            row.result = e
        else:
            if do_dns and getattr(validated, "domain_address", None) is None:
                key = (validated.ascii_domain, policy)
                group = groups.get(key)
                if group is None:
                    group = _DnsGroup(submit_dns(validated.ascii_domain, validated.domain))
                    groups[key] = group
                group.refs += 1
                row.validated = validated
                row.group = group
                row.key = key
            else:
                row.result = validated
        inflight.append(row)
        return True

    def finish(row: _Row) -> Union[ValidatedEmail, Exception]:
        # Wait for the row's result, blocking only on the row at the
        # head of the window so results are yielded in input order.
        if row.group is None:
            return row.result  # type: ignore[return-value]
        group = row.group
        try:
            deliverability_info = group.future.result()
            ret = row.validated
            assert ret is not None
            mx = deliverability_info.get("mx")
            if mx is not None:
                ret.mx = mx
            ret.mx_fallback_type = deliverability_info.get("mx_fallback_type")
            return ret
        except Exception as e:
            return e
        finally:
            # Once every row sharing this check has been consumed, drop
            # the group so later windows perform a fresh DNS check.
            group.refs -= 1
            if group.refs == 0 and groups.get(row.key) is group:
                del groups[row.key]

    try:
        for _ in range(max_lookahead):
            if not pull():
                break
        while inflight:
            row = inflight.popleft()
            result = finish(row)
            yield (row.email, result)
            pull()
    finally:
        # If the consumer stops early, stop pulling from the source and
        # release the DNS workers without waiting for slow checks.
        close = getattr(source, "close", None)
        if callable(close):
            close()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
