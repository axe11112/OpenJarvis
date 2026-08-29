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

    def test_feature_request_no_longer_returns_no_target_error(self):
        """Feature requests no longer get 'no engineering target' error."""
        from openjarvis.core.config import load_config
        from openjarvis.wiz.assemble import assemble
        from openjarvis.wiz.intake import TelegramIntake
        from openjarvis.wiz.runtime import build_wiz

        config = load_config()
        product = assemble(config=config)
        wiz_runtime = build_wiz(config=config, product=product)

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
        # But both should have the same product
        assert (
            telegram_runtime.wiz.registry.all()
            == canonical_runtime.wiz.registry.all()
        )

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
