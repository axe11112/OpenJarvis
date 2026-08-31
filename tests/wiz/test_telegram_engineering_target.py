"""Regression tests: TelegramOwnerDoor has canonical engineering target.

Proves that TelegramOwnerDoor's Wiz runtime is wired to the same
engineering target as the canonical autonomous feature pipeline.
"""

from __future__ import annotations

import pytest


class TestTelegramOwnerDoorEngineeringTarget:
    """Verify TelegramOwnerDoor runtime has engineering target configured."""

    def test_telegram_owner_door_has_engineering_target(self):
        """TelegramOwnerDoor wiz_runtime includes product with pipeline."""
        from openjarvis.core.config import load_config
        from openjarvis.wiz.assemble import assemble
        from openjarvis.wiz.runtime import build_wiz

        config = load_config()
        product = assemble(config=config)

        # Engineering target must be configured
        assert product is not None, "Engineering target not configured"
        assert hasattr(product, "pipeline"), "Product missing pipeline"
        assert product.pipeline is not None, "Pipeline not configured"

    def test_canonical_target_is_wize_performance(self):
        """Engineering target points to Wize-Performance repository."""
        from openjarvis.core.config import load_config
        from openjarvis.wiz.assemble import assemble

        config = load_config()
        product = assemble(config=config)

        assert product is not None
        profile = product.pipeline.profile
        assert profile.repository == "axe11112/Wize-Performance"
        assert profile.base_branch == "main"

    def test_feature_request_no_longer_returns_no_target_error(self, tmp_path):
        """Feature requests no longer get 'no engineering target' error.

        A real target and a real ``assemble()`` — that is the point of this
        test — but entirely under ``tmp_path``: this must never open the
        operator's actual FeatureStore, journal or authority file. It
        previously called ``load_config()``/``assemble(config=config)``
        against the real ``~/.openjarvis``, which really did submit "Build a
        feature to make the form submit button green" as a genuine feature
        request on every test run — the source of a dozen identical
        FEAT-000xx rows found live in production during a later hardening
        pass.
        """
        from openjarvis.wiz.assemble import assemble
        from openjarvis.wiz.authority import Authority, AuthorityPolicy, Channel
        from openjarvis.wiz.features.profile import EngineeringProfile
        from openjarvis.wiz.intake import TelegramIntake
        from openjarvis.wiz.runtime import build_wiz
        from openjarvis.wiz.settings import WizSettings

        profile = EngineeringProfile(
            name="test-target",
            checkout=str(tmp_path / "checkout"),
            test_command="npm test",
        )
        settings = WizSettings(
            targets={"test-target": profile},
            default_target="test-target",
            worktree_root=str(tmp_path / "worktrees"),
            evidence_root=str(tmp_path / "evidence"),
        )
        product = assemble(home=tmp_path, settings=settings)
        assert product is not None, "a complete profile should assemble"

        wiz_runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={Channel.TELEGRAM: frozenset({Authority.SAFE_ACTION})}
            ),
            product=product,
        )

        intake = TelegramIntake(wiz=wiz_runtime.wiz, owner_chat_ids=["123"])

        # The exact message that previously failed
        result = intake.receive(
            chat_id="123",
            text="Build a feature to make the form submit button green",
            sender="owner",
        )

        reply = str(getattr(result, "reply", ""))
        assert "no engineering target is configured" not in reply.lower()
        assert getattr(result, "accepted", False), "Feature request should be accepted"

    def test_telegram_runtime_has_same_pipeline_as_canonical(self):
        """TelegramOwnerDoor runtime and canonical runtime share pipeline."""
        from openjarvis.core.config import load_config
        from openjarvis.wiz.assemble import assemble
        from openjarvis.wiz.runtime import build_wiz

        config = load_config()
        product = assemble(config=config)

        # Both runtimes should have the same product/pipeline
        telegram_runtime = build_wiz(config=config, product=product)
        canonical_runtime = build_wiz(config=config, product=product)

        assert telegram_runtime.wiz is not canonical_runtime.wiz
        # But both should have the same product — literally the same object,
        # since the same `product` was passed into both build_wiz() calls.
        assert telegram_runtime.product is canonical_runtime.product
        # And the same capability surface. Not CapabilitySpec equality: each
        # build_wiz() call constructs fresh closures (browser_capabilities()
        # and friends), so two specs are never `==` across separate calls no
        # matter how identically configured — the registry lives on
        # WizRuntime itself, not on the inner Wiz dispatcher, and the name is
        # the stable, meaningful thing to compare between two builds of it.
        telegram_names = {spec.name for spec in telegram_runtime.registry.all()}
        canonical_names = {spec.name for spec in canonical_runtime.registry.all()}
        assert telegram_names == canonical_names
        assert telegram_names  # not vacuously true

    def test_missing_target_still_fails_closed(self):
        """If engineering target config missing, returns None (fail closed)."""
        from openjarvis.wiz.assemble import assemble
        from openjarvis.wiz.settings import WizSettings

        # Create settings with no targets
        empty_settings = WizSettings(targets={})

        product = assemble(settings=empty_settings)
        assert product is None, "Should return None when no target configured"

    def test_feature_store_is_canonical(self):
        """FeatureStore in product is shared (not a parallel instance)."""
        from openjarvis.core.config import load_config
        from openjarvis.wiz.assemble import assemble

        config = load_config()
        product = assemble(config=config)

        assert product is not None
        assert hasattr(product, "pipeline")
        pipeline = product.pipeline
        assert pipeline.store is not None
        # Store path should be in .openjarvis/wiz/features.db
        assert "features.db" in str(pipeline.store.path)
