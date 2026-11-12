"""Tests for the SemVer parser and precedence comparison."""

from __future__ import annotations

import pytest

from natsort import SemVer, SemVerParseError, parse_semver


class TestParseSemVer:
    @pytest.mark.parametrize(
        "text",
        [
            "0.0.0",
            "1.2.3",
            "10.20.30",
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-0.3.7",
            "1.0.0-x.7.z.92",
            "1.0.0-alpha+001",
            "1.0.0+20130313144700",
            "1.0.0-beta+exp.sha.5114f85",
            "1.0.0+21AF26D3----117B344092BD",
        ],
    )
    def test_strict_parser_accepts_spec_examples(self, text: str) -> None:
        parsed = SemVer.parse(text, strict=True)
        assert str(parsed) == text

    @pytest.mark.parametrize(
        "text",
        [
            "1",
            "1.2",
            "v1.2.3",
            "V1.2.3-alpha",
            "01.02.03",
            "1.2.3-rc.1+build.9",
        ],
    )
    def test_lenient_parser_accepts_shortened_forms(self, text: str) -> None:
        assert parse_semver(text) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "1",
            "1.2",
            "v1.2.3",
            "01.2.3",
            "1.2.3-01",
            "1.2.3-",
            "1.2.3+",
            "1.2.3_alpha",
            "",
            "not-a-version",
            "1.2.3.4",
        ],
    )
    def test_strict_parser_rejects_non_compliant_strings(self, text: str) -> None:
        assert parse_semver(text, strict=True) is None
        with pytest.raises(SemVerParseError):
            SemVer.parse(text, strict=True)

    def test_lenient_parser_defaults_missing_components_to_zero(self) -> None:
        parsed = SemVer.parse("v1.2")
        assert (parsed.major, parsed.minor, parsed.patch) == (1, 2, 0)
        assert parsed.build is None
        assert parsed.prerelease == ()

    def test_build_metadata_is_stored_verbatim(self) -> None:
        parsed = SemVer.parse("1.2.3-rc.1+Exp.SHA.5114f85")
        assert parsed.build == "Exp.SHA.5114f85"
        assert parsed.prerelease == ("rc", "1")

    def test_parse_semver_returns_none_for_garbage_and_non_strings(self) -> None:
        assert parse_semver("nonsense") is None
        assert parse_semver(None) is None  # type: ignore[arg-type]
        assert parse_semver(123) is None  # type: ignore[arg-type]

    def test_parse_raises_type_error_for_non_strings(self) -> None:
        with pytest.raises(TypeError):
            SemVer.parse(1.0)  # type: ignore[arg-type]

    def test_raw_string_is_retained(self) -> None:
        assert SemVer.parse("v1.2").raw == "v1.2"


class TestSemVerPrecedence:
    # The canonical precedence chain from the SemVer 2.0.0 specification.
    SPEC_ORDER = (
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    )

    def test_spec_precedence_chain(self) -> None:
        shuffled = list(reversed(self.SPEC_ORDER))
        assert sorted(shuffled, key=SemVer.parse) == list(self.SPEC_ORDER)

    def test_core_components_order_numerically(self) -> None:
        ordered = ["1.0.0", "1.0.1", "1.1.0", "1.1.1", "2.0.0", "10.0.0"]
        shuffled = ordered.copy()
        shuffled.reverse()
        assert sorted(shuffled, key=SemVer.parse) == ordered

    def test_prerelease_precedes_release(self) -> None:
        assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0")

    def test_numeric_identifier_precedes_alphanumeric(self) -> None:
        assert SemVer.parse("1.0.0-alpha.1") < SemVer.parse("1.0.0-alpha.beta")

    def test_long_numeric_prerelease_fields_compare_exactly(self) -> None:
        # 10**300 - 1 versus 10**300: a float conversion would lose the
        # distinction or overflow, arbitrary precision integers keep it exact.
        smaller = "1.0.0-alpha." + "9" * 300
        larger = "1.0.0-alpha." + "1" + "0" * 300
        assert SemVer.parse(smaller) < SemVer.parse(larger)

    def test_long_version_core_components_compare_exactly(self) -> None:
        smaller = "1." + "9" * 200 + ".0"
        larger = "2." + "0" * 200 + ".0"
        assert SemVer.parse(smaller) < SemVer.parse(larger)

    def test_shorter_prerelease_prefix_sorts_first(self) -> None:
        assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0-alpha.1")

    def test_build_metadata_does_not_affect_precedence(self) -> None:
        first = SemVer.parse("1.0.0+build.1")
        second = SemVer.parse("1.0.0+build.2")
        assert first == second
        assert not (first < second)
        assert hash(first) == hash(second)

    def test_build_ignored_across_full_ordering(self) -> None:
        versions = ["2.0.0+b", "1.0.0+z", "1.0.0+a"]
        assert sorted(versions, key=lambda v: SemVer.parse(v).precedence_key()) == [
            "1.0.0+z",
            "1.0.0+a",
            "2.0.0+b",
        ]

    def test_comparison_returns_not_implemented_for_foreign_types(self) -> None:
        version = SemVer.parse("1.0.0")
        assert version.__eq__("1.0.0") is NotImplemented
        assert version.__lt__("1.0.0") is NotImplemented

    def test_round_trip_display(self) -> None:
        text = "1.2.3-rc.1+build.5"
        assert str(SemVer.parse(text)) == text
        assert repr(SemVer.parse(text)) == "SemVer.parse('1.2.3-rc.1+build.5')"
