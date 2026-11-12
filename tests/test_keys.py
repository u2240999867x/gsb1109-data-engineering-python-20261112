import functools

import pytest

from mimesis import keys
from mimesis.exceptions import LocaleError, PipeCheckpointError
from mimesis.locales import Locale
from mimesis.random import Random, random


ROMANIZE_INPUT_PARAMETERS = [
    (Locale.RU, "Ликид", "Likid"),
    (Locale.RU, "Что-то там_4352-!@", "Chto-to tam_4352-!@"),
    (
        Locale.RU,
        " ".join("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"),
        (
            "A B V G D E Yo Zh Z I Ye K L M N O P R S T U F Kh Ts "
            "Ch Sh Shch  Y  E Yu Ja a b v g d e yo zh z i ye k l m n"
            " o p r s t u f kh ts ch sh shch  y  e yu ja"
        ),
    ),
    (Locale.UK, "Українська мова!", "Ukrayins’ka mova!"),
    (Locale.UK, "Щось там_4352-!@", "Shchos’ tam_4352-!@"),
    (
        Locale.KK,
        "Python - ең жақсы бағдарламалау тілі!",
        "Python - eñ zhaqsy bağdarlamalau tili!",
    ),
]


def test_maybe():
    key = keys.maybe(None, probability=1)
    assert key("foo", random) is None

    key = keys.maybe(None, probability=0.0)
    assert key("foo", random) is not None


@pytest.mark.parametrize(
    "locale, string, expected",
    ROMANIZE_INPUT_PARAMETERS,
)
def test_romanize_cyrillic_string(locale, string, expected):
    assert keys.romanize(locale)(string) == expected


def test_romanize_invalid_locale():
    with pytest.raises(LocaleError):
        keys.romanize(locale="sindarin")


def test_romanize_unsupported_locale():
    with pytest.raises(ValueError):
        keys.romanize(locale=Locale.DE)


def test_romanize_missing_positional_arguments():
    with pytest.raises(TypeError):
        keys.romanize()

    with pytest.raises(TypeError):
        keys.romanize(locale=Locale.RU)()


def test_romanize_raises_type_error():
    key = keys.romanize(Locale.RU)
    with pytest.raises(TypeError, match="romanize\\(\\) requires a string"):
        key(12345)


@pytest.mark.parametrize(
    "before, after, value, expected",
    [
        ("<", ">", "word", "<word>"),
        ("{", "}", "data", "{data}"),
        ("(", ")", "123", "(123)"),
    ],
)
def test_wrap(before, after, value, expected):
    key = keys.wrap(before, after)
    assert key(value) == expected


def test_wrap_type_error():
    key = keys.wrap("[", "]")
    with pytest.raises(TypeError, match="wrap\\(\\) requires a string"):
        key(12345)
    with pytest.raises(TypeError, match="wrap\\(\\) requires a string"):
        key(["list"])
    with pytest.raises(TypeError, match="wrap\\(\\) requires a string"):
        key(None)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("word", "drow"),
        ("Mimesis", "sisemiM"),
        ("", ""),
        ("a", "a"),
    ],
)
def test_reverse(value, expected):
    result = keys.reverse(value)
    assert result == expected
    assert isinstance(result, str)


def test_reverse_type_error():
    with pytest.raises(TypeError, match="reverse\\(\\) requires a string"):
        keys.reverse(12345)
    with pytest.raises(TypeError, match="reverse\\(\\) requires a string"):
        keys.reverse(["list"])
    with pytest.raises(TypeError, match="reverse\\(\\) requires a string"):
        keys.reverse(None)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("test sentence 123!", "test-sentence-123"),
        ("   Leading and trailing spaces   ", "leading-and-trailing-spaces"),
        ("Special characters!*&^%$", "special-characters"),
        ("", ""),
        ("---multiple---dashes---", "multiple-dashes"),
    ],
)
def test_slugify(value, expected):
    result = keys.slugify(value)
    assert result == expected
    assert isinstance(result, str)


def test_slugify_type_error():
    with pytest.raises(TypeError, match="slugify\\(\\) requires a string"):
        keys.slugify(12345)
    with pytest.raises(TypeError, match="slugify\\(\\) requires a string"):
        keys.slugify(["list"])


@pytest.mark.parametrize(
    "value, expected",
    [
        ("John Doe", "john_doe"),
        ("This is a Test!", "this_is_a_test"),
        ("   Leading and trailing spaces   ", "leading_and_trailing_spaces"),
        ("Special Characters!*&^%$", "special_characters"),
        ("", ""),
    ],
)
def test_snake_case(value, expected):
    result = keys.snake_case(value)
    assert result == expected
    assert isinstance(result, str)


def test_snake_case_type_error():
    with pytest.raises(TypeError, match="snake_case\\(\\) requires a string"):
        keys.snake_case(12345)
    with pytest.raises(TypeError, match="snake_case\\(\\) requires a string"):
        keys.snake_case(None)


@pytest.mark.parametrize(
    "value, expected",
    (
        ("John Doe", "johnDoe"),
        ("This is a Test", "thisIsATest"),
        ("   Leading and trailing spaces   ", "leadingAndTrailingSpaces"),
        ("single", "single"),
        ("", ""),
    ),
)
def test_camel_case(value, expected):
    result = keys.camel_case(value)
    assert result == expected
    assert isinstance(result, str)


def test_camel_case_type_error():
    with pytest.raises(TypeError, match="camel_case\\(\\) requires a string"):
        keys.camel_case(12345)
    with pytest.raises(TypeError, match="camel_case\\(\\) requires a string"):
        keys.camel_case([])


def test_kebab_case():
    assert keys.kebab_case("Hello World") == "hello-world"
    assert keys.kebab_case("Test Case 123") == "test-case-123"


def test_kebab_case_type_error():
    with pytest.raises(TypeError, match="slugify\\(\\) requires a string"):
        keys.kebab_case(12345)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Hello, World!", "He---"),
        ("Short", "Short"),
        ("", ""),
    ],
)
def test_truncate(value, expected):
    key = keys.truncate(5, suffix="---")
    assert key(value) == expected


def test_truncate_type_error():
    key = keys.truncate(10)
    with pytest.raises(TypeError, match="truncate\\(\\) requires a string"):
        key(12345)
    with pytest.raises(TypeError, match="truncate\\(\\) requires a string"):
        key(None)


def test_truncate_value_error():
    with pytest.raises(ValueError, match="max_length must be positive"):
        keys.truncate(0)
    with pytest.raises(ValueError, match="max_length must be positive"):
        keys.truncate(-5)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("This is a test.", "Thisisatest."),
        (" NoSpaces ", "NoSpaces"),
        ("", ""),
        ("   ", ""),
        ("\t\n\r", ""),
    ],
)
def test_remove_whitespace(value, expected):
    assert keys.remove_whitespace(value) == expected


def test_remove_whitespace_type_error():
    with pytest.raises(TypeError, match="remove_whitespace\\(\\) requires a string"):
        keys.remove_whitespace(None)


@pytest.mark.parametrize(
    "prefix_text, value, expected",
    [
        ("user_", "john", "user_john"),
        ("pre@", "data", "pre@data"),
        ("", "value", "value"),
    ],
)
def test_prefix(prefix_text, value, expected):
    key = keys.prefix(prefix_text)
    assert key(value) == expected


def test_prefix_type_error():
    key = keys.prefix("pre-")
    with pytest.raises(TypeError, match="prefix\\(\\) requires a string"):
        key(12345)


@pytest.mark.parametrize(
    "suffix_text, value, expected",
    [
        (".com", "example", "example.com"),
        ("_suffix", "test", "test_suffix"),
        ("", "value", "value"),
    ],
)
def test_suffix(suffix_text, value, expected):
    key = keys.suffix(suffix_text)
    assert key(value) == expected


def test_suffix_type_error():
    key = keys.suffix(".com")
    with pytest.raises(TypeError, match="suffix\\(\\) requires a string"):
        key(12345)
    with pytest.raises(TypeError, match="suffix\\(\\) requires a string"):
        key([])


@pytest.mark.parametrize(
    "algorithm, value, length",
    [
        ("md5", "password", 32),
        ("sha1", "password", 40),
        ("sha224", "password", 56),
        ("sha256", "password", 64),
        ("sha384", "password", 96),
        ("sha512", "password", 128),
        ("blake2b", "password", 128),
    ],
)
def test_hash_with(algorithm, value, length):
    key = keys.hash_with(algorithm)
    result = key(value)
    assert len(result) == length
    assert isinstance(result, str)
    assert key(value) == key(value)


def test_hash_with_invalid_algorithm():
    with pytest.raises(ValueError, match="Unsupported hash algorithm"):
        keys.hash_with("invalid_algorithm")


def test_hash_with_type_error():
    key = keys.hash_with("sha256")
    with pytest.raises(TypeError, match="hash_with\\(\\) requires a string"):
        key(12345)


def test_base64_encode():
    assert keys.base64_encode("test") == "dGVzdA=="
    assert keys.base64_encode("") == ""


def test_base64_encode_type_error():
    with pytest.raises(TypeError, match="base64_encode\\(\\) requires a string"):
        keys.base64_encode(12345)


def test_urlsafe_base64_encode():
    result = keys.urlsafe_base64_encode("test data")
    assert isinstance(result, str)
    assert "+" not in result
    assert "/" not in result


def test_urlsafe_base64_encode_type_error():
    with pytest.raises(
        TypeError, match="urlsafe_base64_encode\\(\\) requires a string"
    ):
        keys.urlsafe_base64_encode([123])


def test_redact():
    key = keys.redact("[CLASSIFIED]")
    assert key("Any input string") == "[CLASSIFIED]"
    assert key(12345) == "[CLASSIFIED]"
    assert key(None) == "[CLASSIFIED]"
    assert key([1, 2, 3]) == "[CLASSIFIED]"


def test_redact_default():
    key = keys.redact()
    assert key("sensitive") == "[REDACTED]"


class CustomSeq:
    def __init__(self, items):
        self.items = items

    def __iter__(self):
        return iter(["a", "b", "c"])


@pytest.mark.parametrize(
    "seq, sep, expected",
    (
        [("a", "b", "c"), " | ", "a | b | c"],
        [["a", "b", "c"], " | ", "a | b | c"],
        [range(3), " | ", "0 | 1 | 2"],
        ["abc", ",", "a,b,c"],
        [CustomSeq(["a", "b", "c"]), " | ", "a | b | c"],
        [[], " | ", ""],
    ),
)
def test_join(seq, sep, expected):
    assert keys.join(sep)(seq) == expected


def test_join_default_separator():
    assert keys.join()(["a", "b", "c"]) == "a, b, c"


def test_join_type_error():
    key = keys.join(" | ")
    with pytest.raises(TypeError, match="join\\(\\) requires an iterable"):
        key(None)


@pytest.mark.parametrize(
    "condition, transform, otherwise, input_value, expected",
    [
        (lambda x: x > 10, lambda x: x * 2, None, 15, 30),
        (lambda x: x > 10, lambda x: x * 2, None, 5, 5),
        (lambda x: isinstance(x, str), lambda x: x.upper(), None, "hello", "HELLO"),
        (lambda x: isinstance(x, str), lambda x: x.upper(), None, 123, 123),
        (lambda x: x % 2 == 0, lambda x: "even", lambda x: "odd", 4, "even"),
        (lambda x: x % 2 == 0, lambda x: "even", lambda x: "odd", 5, "odd"),
    ],
)
def test_apply_if(condition, transform, otherwise, input_value, expected):
    key = keys.apply_if(condition, transform, otherwise)
    assert key(input_value) == expected


def test_apply_if_no_otherwise():
    key = keys.apply_if(
        lambda x: x > 10,
        lambda x: x * 2,
    )
    assert key(15) == 30
    assert key(5) == 5


def test_pipe():
    key = keys.pipe(
        str.upper,
        keys.prefix("PREFIX-"),
        keys.slugify,
    )
    result = key("  Hello World  ", random)
    assert result == "prefix-hello-world"


def test_pipe_single_function():
    key = keys.pipe(str.upper)
    assert key("hello") == "HELLO"


def test_pipe_empty():
    key = keys.pipe()
    assert key("test") == "test"
    assert key(123) == 123


def test_pipe_with_random():
    key = keys.pipe(keys.maybe("N/A", probability=1.0))
    result = key("original", random)
    assert result == "N/A"


def test_pipe_string_transformation_chain():
    key1 = keys.pipe(
        str.lower,
        keys.remove_whitespace,
        keys.snake_case,
        keys.prefix("user_"),
        keys.suffix("_id"),
        keys.hash_with("sha1"),
    )
    result1 = key1("John Doe 123", random)
    assert isinstance(result1, str)
    assert len(result1) == 40


def test_pipe_with_conditional_transformation():
    key = keys.pipe(
        keys.apply_if(
            lambda x: len(x) > 10,
            keys.truncate(10),
            lambda x: x,
        ),
        keys.base64_encode,
        keys.prefix("encoded_"),
    )
    result = key("This is a very long string", random)
    assert result.startswith("encoded_")
    assert isinstance(result, str)


def test_pipe_with_wrapping_and_case_conversion():
    key = keys.pipe(
        str.strip,
        keys.camel_case,
        keys.wrap("[", "]"),
        keys.reverse,
    )
    result = key("  hello world  ", random)
    assert result == "]dlroWolleh["


def test_pipe_with_maybe():
    key = keys.pipe(
        keys.maybe("N/A", probability=1.0),
        str.upper,
    )
    result = key("original", random)
    assert result == "N/A"


def test_pipe_with_multiple_hash_transformations():
    key = keys.pipe(
        keys.slugify,
        keys.prefix("item-"),
        keys.hash_with("md5"),
        lambda x: x[:8],
    )
    result = key("Test Item #123", random)
    assert isinstance(result, str)
    assert len(result) == 8


def test_pipe_with_conditional_redact():
    key = keys.pipe(
        keys.apply_if(
            lambda x: "secret" in x.lower(),
            keys.redact("[CLASSIFIED]"),
            str.upper,
        ),
    )
    assert key("This is secret", random) == "[CLASSIFIED]"
    assert key("This is public", random) == "THIS IS PUBLIC"


def test_pipe_with_case_conversion_and_transformation():
    key = keys.pipe(
        str.strip,
        str.lower,
        keys.snake_case,
        keys.prefix("api_"),
        keys.suffix("_endpoint"),
        str.upper,
    )
    result = key("  Get User Data  ", random)
    assert result == "API_GET_USER_DATA_ENDPOINT"


def test_call_key_func_propagates_type_error_from_inside_key():
    def key(result, random):
        raise TypeError("boom")

    with pytest.raises(TypeError, match="boom"):
        keys._call_key_func(key, "value", random)


def test_call_key_func_falls_back_to_single_argument():
    assert keys._call_key_func(str.strip, "  value  ", random) == "value"
    assert keys._call_key_func(lambda v: v.upper(), "value", random) == "VALUE"


def test_call_key_func_with_keyword_only_random():
    def key(value, *, random):
        return f"{value}-{random.randint(1, 1)}"

    assert keys._call_key_func(key, "value", random) == "value-1"


def test_call_key_func_with_partial():
    def key(value, random, suffix):
        return f"{value}{suffix}{random.randint(1, 1)}"

    partial_key = functools.partial(key, suffix="!")
    assert keys._call_key_func(partial_key, "value", random) == "value!1"

    partial_random = functools.partial(key, random=random, suffix="?")
    assert keys._call_key_func(partial_random, "value", random) == "value?1"


def test_call_key_func_with_decorated_function():
    def key(value, *, random):
        return f"{value}={random.randint(1, 1)}"

    @functools.wraps(key)
    def wrapper(*args, **kwargs):
        return key(*args, **kwargs)

    assert keys._call_key_func(wrapper, "value", random) == "value=1"


def test_call_key_func_with_bound_method():
    class Helper:
        def key(self, value, *, random):
            return f"{value}:{random.randint(1, 1)}"

    assert keys._call_key_func(Helper().key, "value", random) == "value:1"


def test_call_key_func_two_argument_type_error_raised_once():
    calls = []

    def key(value, random):
        calls.append(value)
        raise TypeError("boom")

    with pytest.raises(TypeError, match="boom"):
        keys._call_key_func(key, "value", random)

    assert calls == ["value"]


def test_pipe_resume_from_failed_stage():
    log = []
    should_fail = True

    def stage_a(value, random):
        log.append(("a", value))
        return value + 1

    def stage_b(value, random):
        log.append(("b", value, random.randint(1, 1000)))
        if should_fail:
            raise RuntimeError("boom")
        return value * 10

    def stage_c(value):
        log.append(("c", value))
        return value

    key = keys.pipe(stage_a, stage_b, stage_c)
    rnd = Random(42)

    with pytest.raises(RuntimeError, match="boom"):
        key(5, rnd)

    checkpoint = key.checkpoint
    assert checkpoint is not None
    assert checkpoint.stage_index == 1
    assert checkpoint.value == 6
    assert checkpoint.random_state is not None

    should_fail = False
    result = key.resume(rnd)

    assert result == 60
    # The completed stage was not re-run, and the failed stage was retried
    # with the value and the random state of its first entry.
    assert log[0] == ("a", 5)
    assert log[1] == log[2]
    assert log[1][0] == "b" and log[1][1] == 6
    assert log[3] == ("c", 60)
    assert key.checkpoint is None


def test_pipe_resume_through_new_call():
    should_fail = True

    def stage_a(value):
        return value + 1

    def stage_b(value):
        if should_fail:
            raise RuntimeError("boom")
        return value * 10

    key = keys.pipe(stage_a, stage_b)

    with pytest.raises(RuntimeError, match="boom"):
        key(5, random)

    should_fail = False
    # The next call resumes the recorded run instead of starting over.
    assert key(999, random) == 60
    assert key.checkpoint is None


def test_pipe_resume_without_checkpoint():
    key = keys.pipe(str.upper)

    with pytest.raises(PipeCheckpointError):
        key.resume(random)


def test_pipe_checkpoint_version_mismatch():
    def stage_a(value, random):
        return value + 1

    def stage_b(value, random):
        raise RuntimeError("boom")

    key = keys.pipe(stage_a, stage_b)

    with pytest.raises(RuntimeError, match="boom"):
        key(5, random)

    checkpoint = key.checkpoint
    assert checkpoint is not None

    # The same chain accepts the checkpoint.
    same_chain = keys.pipe(stage_a, stage_b, checkpoint=checkpoint)
    assert same_chain.checkpoint is checkpoint

    # A changed chain refuses it.
    with pytest.raises(PipeCheckpointError):
        keys.pipe(stage_a, str.upper, checkpoint=checkpoint)

    with pytest.raises(PipeCheckpointError):
        keys.pipe(stage_a, str.upper).attach(checkpoint)

    with pytest.raises(PipeCheckpointError):
        keys.pipe(stage_a, str.upper).resume(random, checkpoint=checkpoint)


def test_nested_pipe_resume():
    log = []
    should_fail = True

    def inner_stage_a(value, random):
        log.append(("inner_a", value))
        return value + 100

    def inner_stage_b(value, random):
        log.append(("inner_b", value, random.randint(1, 1000)))
        if should_fail:
            raise ValueError("inner boom")
        return value * 2

    def outer_stage_a(value, random):
        log.append(("outer_a", value))
        return value + 1

    def outer_stage_c(value):
        log.append(("outer_c", value))
        return value

    inner = keys.pipe(inner_stage_a, inner_stage_b)
    outer = keys.pipe(outer_stage_a, inner, outer_stage_c)
    rnd = Random(7)

    with pytest.raises(ValueError, match="inner boom"):
        outer(1, rnd)

    assert outer.checkpoint is not None
    assert outer.checkpoint.stage_index == 1
    assert inner.checkpoint is not None
    assert inner.checkpoint.stage_index == 1

    should_fail = False
    result = outer.resume(rnd)

    assert result == 204
    # Inner stages that completed were not re-run, and the failed inner
    # stage was retried with the value and random state of first entry.
    assert log[0] == ("outer_a", 1)
    assert log[1] == ("inner_a", 2)
    assert log[2] == log[3]
    assert log[2][0] == "inner_b" and log[2][1] == 102
    assert log[4] == ("outer_c", 204)
    assert outer.checkpoint is None
    assert inner.checkpoint is None


def test_pipe_checkpoint_attributes():
    def stage_a(value):
        raise RuntimeError("boom")

    key = keys.pipe(str.upper, stage_a)

    with pytest.raises(RuntimeError, match="boom"):
        key("value", random)

    checkpoint = key.checkpoint
    assert checkpoint is not None
    assert checkpoint.version == key.version
    assert checkpoint.stage_index == 1
    assert checkpoint.value == "VALUE"
    assert repr(checkpoint).startswith("PipeCheckpoint(")
