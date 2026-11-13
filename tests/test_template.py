import pytest

from faker import Faker, SlotCycleError, TemplateError


class SequentialCustomerProvider:
    """Customer ids handed out in order, with a way to fail on demand."""

    def __init__(self):
        self.issued = []
        self._counter = 0
        self.fail_on = None

    def customer_id(self):
        self._counter += 1
        value = f"CUST-{self._counter:04d}"
        self.issued.append(value)
        if self.fail_on == value:
            raise RuntimeError(f"generation failed for {value}")
        return value

    def sku(self):
        self._counter += 1
        return f"SKU-{self._counter:04d}"


class TrackingProvider:
    def __init__(self):
        self.calls = 0

    def tracked(self):
        self.calls += 1
        return "TRACKED"


class FailingProvider:
    def boom(self):
        raise RuntimeError("boom")


@pytest.fixture
def fake():
    fake = Faker()
    fake.seed_instance(2026)
    return fake


@pytest.fixture
def customers():
    return SequentialCustomerProvider()


@pytest.fixture
def tracking():
    return TrackingProvider()


class TestSharedSlots:
    def test_header_and_details_share_same_customer_id(self, fake, customers):
        fake.add_provider(customers)
        template = {
            "header": "{{ let customer_id: customer_id }}customer={{ customer_id }}",
            "details": [
                {"line": 1, "buyer": "{{ customer_id }}"},
                {"line": 2, "buyer": "{{ customer_id }}"},
                {"line": 3, "buyer": "{{ customer_id }}"},
            ],
        }

        rendered = fake.render_template(template)

        assert rendered["header"] == "customer=CUST-0001"
        assert [line["buyer"] for line in rendered["details"]] == ["CUST-0001"] * 3
        # The slot is generated once and reused, not regenerated per reference.
        assert customers.issued == ["CUST-0001"]

    def test_let_token_renders_empty_and_leaves_no_markers(self, fake, customers):
        fake.add_provider(customers)
        rendered = fake.render_template("{{ let customer_id: customer_id }}[{{ customer_id }}]")
        assert rendered == "[CUST-0001]"

    def test_detail_can_shadow_outer_slot_with_same_name(self, fake, customers):
        fake.add_provider(customers)
        template = {
            "header": "{{ let customer_id: customer_id }}{{ customer_id }}",
            "details": [
                {"buyer": "{{ customer_id }}"},
                {
                    "buyer": "{{ let customer_id: customer_id }}{{ customer_id }}",
                },
                {"buyer": "{{ customer_id }}"},
            ],
        }

        rendered = fake.render_template(template)

        outer = "CUST-0001"
        assert rendered["header"] == outer
        assert rendered["details"][0]["buyer"] == outer
        # The detail-local slot shadows the outer one for this detail only.
        assert rendered["details"][1]["buyer"] == "CUST-0002"
        assert rendered["details"][2]["buyer"] == outer

    def test_sibling_details_have_independent_scopes(self, fake, customers):
        fake.add_provider(customers)
        template = {
            "details": [
                {"local": "{{ let line_ref: sku }}{{ line_ref }}"},
                {"local": "{{ let line_ref: sku }}{{ line_ref }}"},
            ],
        }

        rendered = fake.render_template(template)

        assert rendered["details"][0]["local"] != rendered["details"][1]["local"]

    def test_outer_slot_visible_through_nested_lists(self, fake, customers):
        fake.add_provider(customers)
        template = {
            "declare": "{{ let customer_id: customer_id }}",
            "groups": [[{"buyer": "{{ customer_id }}"}], [{"buyer": "{{ customer_id }}"}]],
        }

        rendered = fake.render_template(template)

        assert rendered["groups"][0][0]["buyer"] == "CUST-0001"
        assert rendered["groups"][1][0]["buyer"] == "CUST-0001"


class TestForwardReferences:
    def test_forward_reference_matches_forward_declaration(self, fake):
        declared_first = "{{ let code: bothify:CODE-#### }}use={{ code }}"
        referenced_first = "use={{ code }}{{ let code: bothify:CODE-#### }}"

        one = Faker()
        one.seed_instance(99)
        other = Faker()
        other.seed_instance(99)

        assert one.render_template(declared_first) == other.render_template(referenced_first)

    def test_slot_declared_later_in_dict_is_resolvable_earlier(self, fake, customers):
        fake.add_provider(customers)
        template = {
            "first": "{{ customer_id }}",
            "later": "{{ let customer_id: customer_id }}ref={{ customer_id }}",
        }

        rendered = fake.render_template(template)

        assert rendered["first"] == "CUST-0001"
        assert rendered["later"] == "ref=CUST-0001"


class TestOptionalDetails:
    def test_unselected_optional_detail_generates_no_data(self, fake, tracking):
        fake.add_provider(tracking)
        template = {
            "details": [
                {"value": "{{ tracked }}", "optional": True},
                {"value": "{{ tracked }}", "optional": False},
            ],
        }

        rendered = fake.render_template(template)

        assert rendered == {"details": [{"value": "TRACKED"}]}
        assert tracking.calls == 1

    def test_string_optional_markers(self, fake, tracking):
        fake.add_provider(tracking)
        template = {
            "details": [
                {"value": "off", "optional": "false"},
                {"value": "on", "optional": "yes"},
            ],
        }

        rendered = fake.render_template(template)

        assert rendered == {"details": [{"value": "on"}]}

    def test_probability_zero_keeps_probability_one(self, fake, tracking):
        fake.add_provider(tracking)
        rendered = fake.render_template(
            {
                "details": [
                    {"value": "never", "optional": 0},
                    {"value": "always", "optional": 1},
                ],
            }
        )
        assert rendered == {"details": [{"value": "always"}]}

    def test_invalid_probability_rejected(self, fake):
        with pytest.raises(TemplateError):
            fake.render_template({"details": [{"value": "x", "optional": 1.5}]})

    def test_invalid_marker_rejected(self, fake):
        with pytest.raises(TemplateError):
            fake.render_template({"details": [{"value": "x", "optional": "maybe"}]})


class TestCycleDetection:
    def test_same_scope_cycle_names_involved_slots(self, fake):
        template = {
            "a": "{{ let x: y }}",
            "b": "{{ let y: x }}",
            "use": "{{ x }}",
        }

        with pytest.raises(SlotCycleError) as excinfo:
            fake.render_template(template)

        message = str(excinfo.value)
        assert "x" in message
        assert "y" in message

    def test_cross_layer_cycle_names_involved_slots_and_scopes(self, fake):
        template = {
            "header": "{{ let outer: inner }}",
            "details": [
                {"line": "{{ let inner: outer }}{{ outer }}"},
            ],
        }

        with pytest.raises(SlotCycleError) as excinfo:
            fake.render_template(template)

        message = str(excinfo.value)
        assert "outer" in message
        assert "inner" in message
        assert "root" in message
        assert "details[0]" in message

    def test_mutual_cycle_through_three_slots(self, fake):
        template = {
            "a": "{{ let x: y }}",
            "b": "{{ let y: z }}",
            "c": "{{ let z: x }}",
            "use": "{{ x }}",
        }
        with pytest.raises(SlotCycleError) as excinfo:
            fake.render_template(template)
        message = str(excinfo.value)
        for slot in ("x", "y", "z"):
            assert slot in message

    def test_slot_named_like_its_provider_binds_to_provider(self, fake, customers):
        fake.add_provider(customers)
        rendered = fake.render_template("{{ let customer_id: customer_id }}{{ customer_id }}")
        assert rendered == "CUST-0001"

    def test_unresolvable_self_reference_raises_template_error(self, fake):
        with pytest.raises(TemplateError):
            fake.render_template("{{ let x: x }}{{ x }}")

    def test_renderer_remains_usable_after_cycle(self, fake, customers):
        fake.add_provider(customers)
        with pytest.raises(SlotCycleError):
            fake.render_template("{{ let x: y }}{{ let y: x }}{{ x }}")

        rendered = fake.render_template("{{ let customer_id: customer_id }}{{ customer_id }}")
        assert rendered == "CUST-0001"


class TestDocumentIsolation:
    def _invoice(self, suffix=""):
        return {
            "header": f"{{{{ let customer_id: customer_id }}}}{{{{ customer_id }}}}{suffix}",
            "details": [{"buyer": "{{ customer_id }}"}],
        }

    def test_interleaved_documents_reconcile_independently(self, fake, customers):
        fake.add_provider(customers)

        first = fake.render_template(self._invoice())
        second = fake.render_template(self._invoice())

        assert first["header"] == first["details"][0]["buyer"]
        assert second["header"] == second["details"][0]["buyer"]
        assert first["header"] != second["header"]

    def test_failed_detail_does_not_leak_into_other_document(self, fake, customers):
        fake.add_provider(customers)
        good = self._invoice()
        poisoned = {
            "header": "{{ let customer_id: customer_id }}{{ customer_id }}",
            "details": [{"bad": "{{ boom }}"}],
        }
        fake.add_provider(FailingProvider())

        before = fake.render_template(good)
        with pytest.raises(RuntimeError):
            fake.render_template(poisoned)
        after = fake.render_template(good)

        assert before["header"] == before["details"][0]["buyer"]
        assert after["header"] == after["details"][0]["buyer"]
        assert before["header"] != after["header"]
        # No customer id produced during the failed document is reused later.
        earlier = customers.issued[: customers.issued.index(after["header"])]
        assert after["header"] not in earlier

    def test_render_templates_uses_isolated_root_scopes(self, fake, customers):
        fake.add_provider(customers)
        rendered = fake.render_templates([self._invoice(), self._invoice()])

        assert rendered[0]["header"] == rendered[0]["details"][0]["buyer"]
        assert rendered[1]["header"] == rendered[1]["details"][0]["buyer"]
        assert rendered[0]["header"] != rendered[1]["header"]

    def test_failure_in_batch_leaves_other_documents_intact(self, fake, customers):
        fake.add_provider(customers)
        fake.add_provider(FailingProvider())
        templates = [
            self._invoice(),
            {
                "header": "{{ let customer_id: customer_id }}",
                "details": [{"bad": "{{ boom }}"}],
            },
            self._invoice(),
        ]

        with pytest.raises(RuntimeError):
            fake.render_templates(templates)

        # The renderer is not left with half-resolved slot state.
        rendered = fake.render_template(self._invoice())
        assert rendered["header"] == rendered["details"][0]["buyer"]


class TestProviderTemplates:
    def test_plain_string_tokens_still_work(self, fake):
        rendered = fake.render_template("name={{ name }}; id={{ pyint: min_value=1, max_value=1 }}")
        assert rendered.startswith("name=")
        assert rendered.endswith("id=1")

    def test_positional_and_keyword_arguments_pass_through(self, fake):
        rendered = fake.render_template("{{ bothify: ABC-## }}")
        assert rendered.startswith("ABC-")
        assert fake.render_template("{{ pyint: min_value=5, max_value=5 }}") == "5"

    def test_scalars_and_non_template_values_pass_through(self, fake):
        assert fake.render_template([1, "plain", True, None]) == [1, "plain", True, None]
        assert fake.render_template({"n": 3, "flag": False}) == {"n": 3, "flag": False}

    def test_parse_is_unchanged(self, fake):
        assert "{{" not in fake.parse("hello {{ name }}")

    def test_bare_token_falls_back_to_provider_method(self, fake):
        assert "{{" not in fake.render_template("{{ name }}")
        assert "{{" not in fake.render_template("{{ email }}")


class TestSlotValidation:
    def test_unknown_slot_raises_template_error(self, fake):
        with pytest.raises(TemplateError) as excinfo:
            fake.render_template("{{ no_such_slot_or_formatter }}")
        assert "no_such_slot_or_formatter" in str(excinfo.value)

    @pytest.mark.parametrize(
        "token",
        [
            "{{ let: customer_id }}",
            "{{ let x }}",
            "{{ let 1bad: customer_id }}",
            "{{ let }}",
        ],
    )
    def test_malformed_declaration_rejected(self, fake, token):
        with pytest.raises(TemplateError):
            fake.render_template(token)

    def test_conflicting_duplicate_declaration_rejected(self, fake):
        with pytest.raises(TemplateError):
            fake.render_template("{{ let x: bothify:A-# }}{{ let x: bothify:B-# }}")

    def test_variables_are_available_as_slots(self, fake):
        rendered = fake.render_template("id={{ customer_id }}", variables={"customer_id": "FIXED"})
        assert rendered == "id=FIXED"

    def test_slot_is_evaluated_only_once_per_scope(self, fake, tracking):
        fake.add_provider(tracking)
        rendered = fake.render_template(
            "{{ let value: tracked }}{{ value }}-{{ value }}-{{ value }}"
        )
        assert rendered == "TRACKED-TRACKED-TRACKED"
        assert tracking.calls == 1
