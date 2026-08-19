"""Risk classification, and the fact that a model cannot argue its way out of it."""

from __future__ import annotations

import pytest

from openjarvis.wiz.capabilities import Risk
from openjarvis.wiz.features.risk import classify, classify_paths, classify_text


class TestPathsDecide:
    @pytest.mark.parametrize(
        "path",
        [
            "src/lib/auth/session.ts",
            "src/middleware.ts",
            "app/api/login/route.ts",
            "supabase/migrations/0007_add_rls.sql",
            "db/schema.sql",
            "src/lib/payments/stripe.ts",
            ".env.production",
            ".github/workflows/deploy.yml",
            "src/security/permissions.ts",
            "vercel.json",
        ],
    )
    def test_sensitive_paths_are_high(self, path):
        assert classify_paths([path]).risk is Risk.HIGH

    @pytest.mark.parametrize(
        "path",
        [
            "app/api/athletes/route.ts",
            "src/lib/format.ts",
            "package.json",
            "src/components/Dashboard.tsx",
        ],
    )
    def test_behavioural_paths_are_medium(self, path):
        assert classify_paths([path]).risk is Risk.MEDIUM

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "public/logo.svg",
            "src/styles/globals.css",
            "docs/onboarding.md",
        ],
    )
    def test_presentational_paths_are_low(self, path):
        assert classify_paths([path]).risk is Risk.LOW

    def test_one_sensitive_file_makes_the_whole_change_high(self):
        assessment = classify_paths(
            ["README.md", "src/styles/globals.css", "src/lib/auth/session.ts"]
        )
        assert assessment.risk is Risk.HIGH
        assert any("auth" in reason for reason in assessment.reasons)

    def test_the_reason_names_the_file(self):
        assessment = classify_paths(["supabase/migrations/1_init.sql"])
        assert any("migrations" in reason for reason in assessment.reasons)


class TestWordsDecide:
    @pytest.mark.parametrize(
        "text",
        [
            "Add a new role system",
            "Make the login page nicer",
            "Add Stripe checkout",
            "Add a migration for the athletes table",
            "Delete inactive users",
        ],
    )
    def test_dangerous_requests_are_high(self, text):
        assert classify_text(text).risk is Risk.HIGH

    @pytest.mark.parametrize(
        "text",
        [
            "Change the heading on the landing page to say Welcome",
            "Make the footer text smaller",
        ],
    )
    def test_copy_changes_are_low(self, text):
        assert classify_text(text).risk is Risk.LOW

    def test_building_a_page_is_medium(self):
        assert classify_text("Add a new coach dashboard page").risk is Risk.MEDIUM

    @pytest.mark.parametrize(
        "text",
        [
            # Nobody asks Wiz to "modify the RBAC policy". They ask this, and
            # every one of these is an authorisation change wearing the clothes
            # of an ordinary feature request.
            "Change who can see a swimmer's results",
            "Let coaches see other swimmers' data",
            "Make the athlete profile visible to the whole club",
            "Give parents access to the training log",
            "Let swimmers share their results with a coach",
            "Add a public link for a private profile",
            "Change who is allowed to download the report",
        ],
    )
    def test_authorisation_asked_for_in_plain_english_is_high(self, text):
        assert classify_text(text).risk is Risk.HIGH, text

    @pytest.mark.parametrize(
        "text",
        [
            # The neighbouring phrasings that must *not* trip it, or every
            # request becomes HIGH and the approval step stops meaning anything.
            "Make the share button bigger",
            "Show the swimmer's name in the header",
            "Add an access key illustration to the empty state",
        ],
    )
    def test_ordinary_requests_near_those_words_stay_below_high(self, text):
        assert classify_text(text).risk is not Risk.HIGH, text


class TestTheAgentCannotTalkItsWayDown:
    def test_an_agent_calling_an_auth_change_low_is_overruled(self):
        assessment = classify(
            text="tidy up a helper",
            paths=["src/lib/auth/session.ts"],
            agent_opinion=Risk.LOW,
        )
        assert assessment.risk is Risk.HIGH

    def test_an_agent_calling_a_copy_change_high_is_believed(self):
        # Being too careful is not a failure mode worth defending against.
        assessment = classify(
            text="change the footer text",
            paths=["src/components/Footer.tsx"],
            agent_opinion=Risk.HIGH,
        )
        assert assessment.risk is Risk.HIGH
        assert any("raised the risk itself" in r for r in assessment.reasons)

    def test_the_result_is_the_maximum_of_every_signal(self):
        assert (
            classify(text="change the footer text", paths=["README.md"]).risk
            is Risk.LOW
        )
        assert classify(text="add a login page", paths=["README.md"]).risk is Risk.HIGH
        assert (
            classify(text="change the footer", paths=["app/api/login/route.ts"]).risk
            is Risk.HIGH
        )

    def test_no_signals_at_all_is_low_not_an_error(self):
        assert classify().risk is Risk.LOW


class TestApprovalGate:
    def test_high_risk_requires_approval(self):
        assert classify(paths=["src/lib/auth/x.ts"]).requires_approval

    def test_medium_risk_does_not(self):
        assert not classify(paths=["src/lib/format.ts"]).requires_approval


class TestDomainWordsAreNotAuthWords:
    """A false HIGH on every request is a false HIGH nobody reads.

    Found by running the pilot: in a swim-training product almost every
    request mentions "sessions", and bare ``session`` in the word list made
    almost everything need an approval. An approval that appears on everything
    is one the operator learns to click through without reading, which is worse
    than not asking.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Make weekly_distance skip sessions that are marked as skipped",
            "Show the number of training sessions this week",
            "Let a coach add a session to the plan",
        ],
    )
    def test_a_training_session_is_not_an_authentication_session(self, text):
        assert classify_text(text).risk is not Risk.HIGH, text

    @pytest.mark.parametrize(
        "text",
        [
            "Make the user session expire after an hour",
            "Store the session token in a cookie",
            "Change how the auth session is refreshed",
            "Increase the session timeout",
            "Fix session_id handling",
        ],
    )
    def test_an_authentication_session_is_still_high(self, text):
        assert classify_text(text).risk is Risk.HIGH, text

    def test_the_path_still_decides_whatever_the_request_called_it(self):
        # The word list is the weaker signal and always was. A change to the
        # session module is HIGH because of where it lands, not because of how
        # it was described.
        assessment = classify(
            text="tidy up the training session helper",
            paths=["src/lib/auth/session.ts"],
        )
        assert assessment.risk is Risk.HIGH
