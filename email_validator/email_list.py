"""
Parsing and validation of a field that contains a list of email addresses.

A single customer-record field may hold several recipients separated by
commas, and a legal display name can itself contain a comma when it is
quoted. The :class:`EmailListParser` in this module receives the field
incrementally, one input block (chunk) at a time, and emits each mailbox as
soon as it is complete.

The parser keeps track of quoted strings, backslash escaping, and angle
brackets across chunks: a quote, escape sequence, or angle-bracket address
that starts in one chunk can finish in a later chunk. A chunk is consumed
once and is never requested again.

An unclosed quoted string or angle bracket would swallow every following
comma and therefore every following recipient. The caller can mark a hard
record boundary (``record_boundary=True`` on :meth:`EmailListParser.feed`),
which is the only point at which the parser resynchronizes: the malformed
record is rejected on its own and parsing of the next customer starts in a
clean state.
"""

from dataclasses import dataclass
from typing import Iterable, Optional, TYPE_CHECKING, Union

from .exceptions import EmailNotValidError

if TYPE_CHECKING:
    from .types import ValidatedEmail


# Structural characters of the RFC 5322 mailbox-list grammar.
_QUOTE = '"'
_BACKSLASH = "\\"
_ANGLE_OPEN = "<"
_ANGLE_CLOSE = ">"
_COMMA = ","
_SPACE = " "

# Reasons for rejections that are detected structurally, before an
# individual address is handed to validate_email.
REASON_EMPTY_ADDRESS = "An empty email address is not valid."
REASON_UNCLOSED_QUOTE = "A quoted string in the email address list was not closed before the end of the record."
REASON_UNCLOSED_ANGLE = "An angle bracket in the email address list was not closed before the end of the record."


@dataclass(frozen=True)
class EmailListResult:
    """Base class for one element of a parsed email address list.

    ``start`` and ``end`` are character offsets into the concatenation of
    the chunks that were fed to the parser, using the usual half-open
    Python convention (``end`` is exclusive).
    """

    start: int
    end: int


@dataclass(frozen=True)
class EmailListItem(EmailListResult):
    """A successfully validated email address."""

    # The normalized address and the other information returned by
    # validate_email.
    email: "ValidatedEmail"

    # The substring of the original field that was validated, with any
    # surrounding whitespace removed.
    original: str


@dataclass(frozen=True)
class EmailListRejection(EmailListResult):
    """An element of the list that could not be validated.

    Each rejection carries its own reason and the character range of the
    offending element, so one bad recipient never prevents the other
    recipients from being delivered.
    """

    reason: str


@dataclass(frozen=True)
class _Token:
    # One input character, its global offset in the concatenated field,
    # and the structural state the parser was in when it was read.
    char: str
    index: int
    quoted: bool
    in_angle: bool


class EmailListParser:
    """Incrementally parse and validate a mailbox-list field.

    Feed the field one block at a time with :meth:`feed`. Every call
    returns the list elements (``EmailListItem`` or
    ``EmailListRejection`` instances) that became complete while reading
    that block; call :meth:`close` once after the final block to flush the
    last element.

    The remaining keyword arguments are the same options accepted by
    :func:`email_validator.validate_email` and are applied independently
    to each address. ``allow_display_name=True`` is typically needed for
    fields containing ``Display Name <address@example.com>`` entries.
    """

    def __init__(
        self,
        *,
        allow_smtputf8: Optional[bool] = None,
        allow_empty_local: Optional[bool] = None,
        allow_quoted_local: Optional[bool] = None,
        allow_domain_literal: Optional[bool] = None,
        allow_display_name: Optional[bool] = None,
        strict: Optional[bool] = None,
        check_deliverability: Optional[bool] = None,
        test_environment: Optional[bool] = None,
        globally_deliverable: Optional[bool] = None,
        timeout: Optional[int] = None,
        dns_resolver: Optional[object] = None,
    ) -> None:
        # Options are forwarded verbatim; validate_email fills in its own
        # defaults for any value that is left as None.
        self._options: dict[str, object] = {
            "allow_smtputf8": allow_smtputf8,
            "allow_empty_local": allow_empty_local,
            "allow_quoted_local": allow_quoted_local,
            "allow_domain_literal": allow_domain_literal,
            "allow_display_name": allow_display_name,
            "strict": strict,
            "check_deliverability": check_deliverability,
            "test_environment": test_environment,
            "globally_deliverable": globally_deliverable,
            "timeout": timeout,
            "dns_resolver": dns_resolver,
        }

        # Structural parser state, which persists across chunks.
        self._inside_quote = False
        self._escaped = False
        self._angle_depth = 0

        # Tokens of the element that is currently being read (since the
        # previous comma or record boundary). The buffer only grows to the
        # length of a single element, so memory does not depend on the
        # total size of the field.
        self._window: list[_Token] = []

        # Total number of characters consumed so far and whether close()
        # has been called. A consumed chunk can never be re-read.
        self._total_length = 0
        self._closed = False

    def feed(self, chunk: str, *, record_boundary: bool = False) -> list[EmailListResult]:
        """Consume one input block and return any elements completed by it.

        When ``record_boundary`` is True, the block ends a hard record
        boundary. A pending element is delivered if the record is in a
        clean state. If a quoted string or angle bracket is still open,
        the pending element is rejected and the parser resynchronizes so
        that the next record cannot be swallowed by the malformed element.
        """
        if self._closed:
            raise RuntimeError("The email list parser has already been closed.")
        if not isinstance(chunk, str):
            raise TypeError("chunk must be a str")

        chunk_start = self._total_length
        self._total_length += len(chunk)

        results: list[EmailListResult] = []
        for offset, char in enumerate(chunk):
            token = self._read_character(char, chunk_start + offset)

            # A comma only separates recipients when it is not quoted and
            # not inside angle brackets. Every other comma is part of an
            # element (e.g. a comma in a quoted display name), and the
            # separator itself is never included in the element.
            if char == _COMMA and not token.quoted and not token.in_angle:
                results.extend(self._validate_pending(token.index))
            else:
                self._window.append(token)

        if record_boundary:
            results.extend(self._end_record())
        return results

    def close(self) -> list[EmailListResult]:
        """Flush the final element after the last block.

        After calling this method the parser cannot be fed any more
        blocks. Calling it more than once returns an empty list. An
        unclosed quoted string or angle bracket makes the final element a
        rejection.
        """
        if self._closed:
            return []
        self._closed = True

        if self._inside_quote or self._escaped or self._angle_depth > 0:
            return self._reject_pending(self._structural_reason())
        return self._flush_pending()

    def _read_character(self, char: str, index: int) -> _Token:
        # Update the structural state machine for one character and
        # return a token describing the state the character was read in.
        quoted = self._inside_quote
        in_angle = self._angle_depth > 0

        if self._inside_quote:
            if self._escaped:
                # The character completes a backslash escape.
                self._escaped = False
            elif char == _BACKSLASH:
                self._escaped = True
            elif char == _QUOTE:
                # The only way out of a quoted string is an unescaped quote.
                self._inside_quote = False
        elif char == _QUOTE:
            self._inside_quote = True
        elif char == _ANGLE_OPEN:
            self._angle_depth += 1
        elif char == _ANGLE_CLOSE and self._angle_depth > 0:
            self._angle_depth -= 1

        return _Token(char=char, index=index, quoted=quoted, in_angle=in_angle)

    def _end_record(self) -> list[EmailListResult]:
        # Handle a caller-declared hard record boundary.
        if self._inside_quote or self._escaped or self._angle_depth > 0:
            # The record ends inside a quoted string or angle bracket.
            # Reject the malformed element and resynchronize here rather
            # than carrying the open structure into the next record.
            results = self._reject_pending(self._structural_reason())
            self._inside_quote = False
            self._escaped = False
            self._angle_depth = 0
            return results

        # In a clean state the boundary simply terminates a pending
        # mailbox, just as an end of input would.
        return self._flush_pending()

    def _structural_reason(self) -> str:
        if self._angle_depth > 0 and not self._inside_quote and not self._escaped:
            return REASON_UNCLOSED_ANGLE
        return REASON_UNCLOSED_QUOTE

    def _trimmed_bounds(self, window: list[_Token]) -> tuple[int, int]:
        # Return the indices of the first and last non-whitespace tokens.
        first = 0
        last = len(window) - 1
        while first <= last and window[first].char == _SPACE:
            first += 1
        while last >= first and window[last].char == _SPACE:
            last -= 1
        return first, last

    def _validate_pending(self, separator_index: int) -> list[EmailListResult]:
        # Validate everything buffered since the previous comma. Called
        # when an unquoted comma outside angle brackets is read, so an
        # empty (or whitespace-only) buffer is a genuine empty element.
        window = self._window
        self._window = []

        first, last = self._trimmed_bounds(window)
        if first > last:
            return [
                EmailListRejection(
                    start=separator_index,
                    end=separator_index,
                    reason=REASON_EMPTY_ADDRESS,
                )
            ]

        start = window[first].index
        end = window[last].index + 1
        original = "".join(token.char for token in window[first:last + 1])

        from .validate_email import validate_email
        try:
            validated = validate_email(original, **self._options)  # type: ignore[arg-type]
        except EmailNotValidError as error:
            return [EmailListRejection(start=start, end=end, reason=str(error))]
        return [EmailListItem(start=start, end=end, email=validated, original=original)]

    def _flush_pending(self) -> list[EmailListResult]:
        # Flush at end of input or at a clean hard record boundary. No
        # separator follows, so an empty/whitespace-only buffer means
        # there is simply no element here (e.g. a trailing comma) rather
        # than an empty address.
        window = self._window
        self._window = []
        if not window:
            return []

        first, last = self._trimmed_bounds(window)
        if first > last:
            return []

        start = window[first].index
        end = window[last].index + 1
        original = "".join(token.char for token in window[first:last + 1])

        from .validate_email import validate_email
        try:
            validated = validate_email(original, **self._options)  # type: ignore[arg-type]
        except EmailNotValidError as error:
            return [EmailListRejection(start=start, end=end, reason=str(error))]
        return [EmailListItem(start=start, end=end, email=validated, original=original)]

    def _reject_pending(self, reason: str) -> list[EmailListResult]:
        # Reject the buffered element for a structural (unclosed quote or
        # angle bracket) failure without consulting validate_email.
        window = self._window
        self._window = []

        first, last = self._trimmed_bounds(window)
        if first > last:
            # Only whitespace (or nothing) was buffered. The rejection is
            # reported at the position where the structure ended.
            end_position = self._total_length
            return [EmailListRejection(start=end_position, end=end_position, reason=reason)]

        start = window[first].index
        end = window[last].index + 1
        return [EmailListRejection(start=start, end=end, reason=reason)]


def validate_email_list(
    chunks: Iterable[Union[str, tuple[str, bool]]],
    /,
    **options: object,
) -> list[EmailListResult]:
    """Parse an entire mailbox-list field delivered as a sequence of blocks.

    Each element of ``chunks`` is either a string block, or a
    ``(block, record_boundary)`` tuple in which a true second element
    marks a hard record boundary at the end of that block. This is a
    convenience wrapper around :class:`EmailListParser` for callers that
    already hold all of the blocks; streaming callers should use the
    class directly so that complete elements are delivered immediately.

    The remaining keyword arguments are passed through to
    :func:`email_validator.validate_email` for each address.
    """
    parser = EmailListParser(**options)  # type: ignore[arg-type]
    results: list[EmailListResult] = []
    for chunk in chunks:
        if isinstance(chunk, tuple):
            block, record_boundary = chunk
            results.extend(parser.feed(block, record_boundary=record_boundary))
        else:
            results.extend(parser.feed(chunk))
    results.extend(parser.close())
    return results
