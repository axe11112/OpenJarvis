"""The acceptance contract: what stops Claude's opinion from being the verdict."""

from __future__ import annotations

import pytest

from openjarvis.wiz.features.acceptance import (
    CONSOLE,
    CONTENT,
    DESKTOP,
    GATE,
    INTERACTION,
    MANUAL,
    MOBILE,
    NETWORK,
    PERFORMANCE,
    AcceptanceContract,
    Criterion,
    contract_for,
    criteria_from_mapping,
)


class TestCriterion:
    def test_an_unknown_kind_is_refused(self):
        # The whole design rests on every criterion having a checker. A kind
        # nobody checks must not be constructible.
        with pytest.raises(ValueError, match="unknown acceptance criterion kind"):
            Criterion(kind="VIBES", description="it feels good")

    def test_a_criterion_needs_a_description(self):
        with pytest.raises(ValueError, match="needs a description"):
            Criterion(kind=CONTENT, description="  ")

    def test_a_performance_criterion_needs_a_number(self):
        # "Make it faster" is the request. It is not the contract.
        with pytest.raises(ValueError, match="measurable budget"):
            Criterion(
                kind=PERFORMANCE, description="the page should feel faster", name="lcp"
            )

    def test_a_performance_criterion_with_a_budget_is_fine(self):
        criterion = Criterion(
            kind=PERFORMANCE,
            description="largest contentful paint stays under 2.5s",
            name="lcp",
            budget=2.5,
            baseline=3.1,
        )
        assert criterion.checkable

    def test_a_manual_criterion_is_not_checkable(self):
        criterion = Criterion(kind=MANUAL, description="a person looks at the design")
        assert not criterion.checkable

    def test_round_trips_through_a_dict(self):
        original = Criterion(
            kind=INTERACTION,
            description="clicking export opens the dialog",
            route="/reports",
            selector="[data-testid=export]",
            then_selector="[role=dialog]",
            viewports=("desktop",),
        )
        assert Criterion.from_dict(original.to_dict()) == original


class TestSelfVerification:
    def test_a_contract_with_a_manual_criterion_cannot_self_verify(self):
        # The property that stops "we could not check it" from becoming
        # "it passed". A feature carrying this contract must not reach READY
        # on machine evidence alone.
        contract = AcceptanceContract(
            feature_id="FEAT-00001",
            criteria=(
                Criterion(kind=GATE, name="tests", description="tests pass"),
                Criterion(kind=MANUAL, description="a person checks the wording"),
            ),
        )
        assert not contract.self_verifiable
        assert contract.unmet_without_a_person() == ("a person checks the wording",)

    def test_a_fully_checkable_contract_self_verifies(self):
        contract = AcceptanceContract(
            feature_id="FEAT-00001",
            criteria=(Criterion(kind=GATE, name="tests", description="tests pass"),),
        )
        assert contract.self_verifiable
        assert contract.unmet_without_a_person() == ()


class TestDerivation:
    def test_gates_come_from_the_target_not_from_this_module(self):
        # A repository with no typecheck script gets no typecheck criterion.
        # Hardcoding the four Wize commands here is exactly what §13 forbids.
        contract = contract_for(
            feature_id="FEAT-00001",
            request="tidy the readme",
            gates=["lint", "tests"],
        )
        assert contract.gates == ("lint", "tests")

    def test_a_quoted_label_becomes_a_content_criterion(self):
        contract = contract_for(
            feature_id="FEAT-00007",
            request='Add a "Download report" button to /coach/summary',
        )
        content = [c for c in contract.criteria if c.kind == CONTENT]
        assert len(content) == 1
        assert content[0].text == "Download report"
        assert content[0].route == "/coach/summary"

    def test_a_user_interface_change_is_always_checked_on_a_phone(self):
        contract = contract_for(
            feature_id="FEAT-00007", request="add a coach dashboard page"
        )
        mobile_only = [c for c in contract.criteria if c.viewports == (MOBILE.name,)]
        assert mobile_only, "a UI feature must carry a mobile criterion"

    def test_a_user_interface_change_forbids_console_and_network_errors(self):
        contract = contract_for(
            feature_id="FEAT-00007", request="add a coach dashboard page"
        )
        kinds = {c.kind for c in contract.criteria}
        assert CONSOLE in kinds
        assert NETWORK in kinds

    def test_a_change_with_no_interface_gets_no_browser_criteria(self):
        contract = contract_for(
            feature_id="FEAT-00008",
            request="bump the retry backoff constant",
            gates=["tests"],
        )
        assert contract.browser_criteria == ()

    def test_an_api_change_with_no_route_is_admitted_as_manual(self):
        # The failure this prevents: inventing an endpoint path so there is
        # something to check, then passing against the 404 handler.
        contract = contract_for(
            feature_id="FEAT-00009",
            request="add an endpoint that returns the weekly totals",
        )
        assert not contract.self_verifiable
        assert "no route was named" in contract.unmet_without_a_person()[0]

    def test_a_model_may_add_criteria_but_the_derived_ones_remain(self):
        # Extra criteria are additive. Nothing a model proposes can remove or
        # relax what the deterministic reading already required.
        derived = contract_for(
            feature_id="FEAT-00010",
            request='add a "Save" button',
            gates=["tests"],
        )
        with_extra = contract_for(
            feature_id="FEAT-00010",
            request='add a "Save" button',
            gates=["tests"],
            extra=[
                Criterion(
                    kind=CONTENT,
                    route="/settings",
                    selector="[data-testid=saved-toast]",
                    description="a confirmation appears after saving",
                )
            ],
        )
        assert set(derived.describe()).issubset(set(with_extra.describe()))
        assert len(with_extra.criteria) == len(derived.criteria) + 1

    def test_duplicate_criteria_collapse(self):
        contract = contract_for(
            feature_id="FEAT-00011",
            request='add a "Save" button',
            extra=[
                Criterion(
                    kind=CONTENT,
                    route="/",
                    text="Save",
                    description="the save button is on the page",
                )
            ],
        )
        content = [c for c in contract.criteria if c.kind == CONTENT]
        assert len(content) == 1


class TestCompilation:
    def test_browser_criteria_compile_to_one_probe_per_viewport(self):
        contract = contract_for(
            feature_id="FEAT-00012",
            request='Add a "Weekly summary" heading to /coach',
        )
        specs = contract.probe_specs()
        viewports = {viewport.name for viewport, _ in specs}
        assert viewports == {DESKTOP.name, MOBILE.name}

    def test_a_compiled_probe_asserts_the_content_that_was_asked_for(self):
        contract = contract_for(
            feature_id="FEAT-00012",
            request='Add a "Weekly summary" heading to /coach',
        )
        _, spec = contract.probe_specs()[0]
        assert any(e.value == "Weekly summary" for e in spec.expect)
        assert spec.steps[0].action == "goto"
        assert spec.steps[0].url == "/coach"

    def test_a_compiled_probe_is_never_mutating(self):
        # A preview usually shares production's database. A check that creates
        # data is a check that writes to production.
        contract = contract_for(
            feature_id="FEAT-00013", request='add a "Go" button to /x'
        )
        for _, spec in contract.probe_specs():
            assert spec.mutating is False

    def test_a_compiled_probe_is_marked_temporary(self):
        # §15: a feature check must not silently become a production probe.
        contract = contract_for(
            feature_id="FEAT-00013", request='add a "Go" button to /x'
        )
        for _, spec in contract.probe_specs():
            assert spec.metadata["temporary"] is True
            assert spec.component == "feature-verification"

    def test_a_compiled_probe_does_not_wait_for_a_second_opinion(self):
        # Confirmation runs exist to stop a flake opening an incident. Here a
        # failure is evidence handed back to Claude, so repeating it only costs
        # the operator time.
        contract = contract_for(
            feature_id="FEAT-00013", request='add a "Go" button to /x'
        )
        for _, spec in contract.probe_specs():
            assert spec.retry.confirm_runs == 1

    def test_a_contract_with_nothing_to_assert_compiles_to_no_probes(self):
        # Better than an empty probe that passes: a check that asserts nothing
        # reads in the evidence exactly like a check that passed.
        contract = AcceptanceContract(
            feature_id="FEAT-00014",
            criteria=(Criterion(kind=GATE, name="tests", description="tests pass"),),
        )
        assert contract.probe_specs() == []

    def test_a_mobile_only_criterion_does_not_produce_a_desktop_probe(self):
        contract = AcceptanceContract(
            feature_id="FEAT-00015",
            criteria=(
                Criterion(
                    kind=CONTENT,
                    route="/m",
                    selector="[data-testid=drawer]",
                    viewports=(MOBILE.name,),
                    description="the drawer appears on a phone",
                ),
            ),
        )
        names = {viewport.name for viewport, _ in contract.probe_specs()}
        assert names == {MOBILE.name}

    def test_an_interaction_becomes_a_click_and_a_consequence(self):
        contract = AcceptanceContract(
            feature_id="FEAT-00016",
            criteria=(
                Criterion(
                    kind=INTERACTION,
                    route="/reports",
                    selector="[data-testid=export]",
                    then_selector="[role=dialog]",
                    description="clicking export opens the dialog",
                ),
            ),
        )
        _, spec = contract.probe_specs()[0]
        assert [s.action for s in spec.steps] == ["goto", "click", "screenshot"]
        assert spec.expect[0].selector == "[role=dialog]"

    def test_every_probe_ends_by_taking_a_picture(self):
        # The operator approving a feature and the reviewer reading the pull
        # request both want to see it working, and there is nowhere later to
        # get that picture from.
        contract = contract_for(
            feature_id="FEAT-00018", request='add a "Go" button to /x'
        )
        for _, spec in contract.probe_specs():
            assert spec.steps[-1].action == "screenshot"


class TestSerialisation:
    def test_a_contract_round_trips(self):
        contract = contract_for(
            feature_id="FEAT-00017",
            request='Add a "Download" button to /reports',
            gates=["lint", "tests"],
        )
        restored = AcceptanceContract.from_dict(contract.to_dict())
        assert restored.describe() == contract.describe()
        assert [v.name for v in restored.viewports] == [
            v.name for v in contract.viewports
        ]

    def test_an_unusable_proposal_is_dropped_not_fatal(self):
        parsed = criteria_from_mapping(
            [
                {
                    "kind": "CONTENT",
                    "description": "the heading is there",
                    "text": "Hi",
                },
                {"kind": "TELEPATHY", "description": "it feels right"},
                "not even a table",
            ]
        )
        assert len(parsed) == 1
        assert parsed[0].text == "Hi"


class TestWhatThePilotFound:
    """Cases from the first real run against a real repository."""

    def test_a_python_backend_change_gets_no_browser_criteria(self):
        # The pilot request. The first version of the word list read "render"
        # and gave this three browser criteria against a route that does not
        # exist — which no amount of correct code could ever satisfy, so the
        # feature could never reach READY and would look broken.
        contract = contract_for(
            feature_id="FEAT-00001",
            request=(
                "Add a render_footer function to the report module that "
                "returns a dash rule, with a test"
            ),
            plan=(
                "I would add render_footer to src/report.py next to "
                "render_header, which renders the header text, and display "
                'the result. The docstring says "The top line of a report".'
            ),
            gates=["test"],
        )
        assert contract.browser_criteria == ()
        assert contract.gates == ("test",)

    def test_the_plan_cannot_turn_a_backend_change_into_a_ui_change(self):
        # A plan is prose a model wrote about a codebase: it mentions
        # rendering, it quotes identifiers, and it talks about the page a
        # change is near. Reading it here is how the above happened.
        contract = contract_for(
            feature_id="FEAT-2",
            request="bump the retry backoff constant",
            plan="This affects the dashboard page and the button that renders it.",
        )
        assert contract.browser_criteria == ()

    def test_an_apostrophe_is_not_a_quoted_label(self):
        # "the operator's phrasing (see below)" became a criterion demanding
        # the page contain "s phrasing (".
        contract = contract_for(
            feature_id="FEAT-3",
            request="Fix the dashboard so it uses the operator's phrasing (see below)",
        )
        for criterion in contract.criteria:
            assert "s phrasing" not in criterion.text

    def test_a_genuinely_quoted_label_still_works(self):
        contract = contract_for(
            feature_id="FEAT-4",
            request='Add a "Download report" button to /coach/summary',
        )
        assert any(c.text == "Download report" for c in contract.criteria)
