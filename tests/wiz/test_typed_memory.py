"""Typed memory with provenance tests.

The critical rule: an inference must NEVER silently become a fact.
Operator corrections must supersede old information rather than destroying
the audit trail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from openjarvis.wiz.typed_memory import (
    MemoryCategory,
    MemorySource,
    TypedMemory,
    TypedMemoryStore,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> TypedMemoryStore:
    """Temporary in-memory database for testing."""
    db_path = tmp_path / "test_memory.db"
    return TypedMemoryStore(db_path)


class TestBasicOperations:
    """Core remember/retrieve/search operations."""

    def test_remember_and_retrieve(self, temp_db: TypedMemoryStore) -> None:
        memory = TypedMemory(
            id="test:001",
            category=MemoryCategory.FACT,
            content="The application uses SQLite",
            source=MemorySource.OBSERVATION,
            source_id="code-review:2025-08-22",
        )
        temp_db.remember(memory)

        retrieved = temp_db.retrieve("test:001")
        assert retrieved is not None
        assert retrieved.content == "The application uses SQLite"
        assert retrieved.category == MemoryCategory.FACT

    def test_auto_generates_id_if_missing(
        self, temp_db: TypedMemoryStore
    ) -> None:
        memory = TypedMemory(
            id="",
            category=MemoryCategory.FACT,
            content="Auto ID test",
            source=MemorySource.OBSERVATION,
        )
        temp_db.remember(memory)

        # After remember(), id should be set
        assert memory.id
        assert memory.id.startswith("fact:")

        # Should be retrievable
        retrieved = temp_db.retrieve(memory.id)
        assert retrieved is not None
        assert retrieved.content == "Auto ID test"

    def test_retrieve_nonexistent_returns_none(
        self, temp_db: TypedMemoryStore
    ) -> None:
        assert temp_db.retrieve("nonexistent") is None

    def test_timestamps_set_on_remember(self, temp_db: TypedMemoryStore) -> None:
        memory = TypedMemory(
            id="test:001",
            category=MemoryCategory.FACT,
            content="Test",
            source=MemorySource.OBSERVATION,
        )
        assert not memory.created_at
        temp_db.remember(memory)
        assert memory.created_at
        assert memory.updated_at


class TestCategoryIsolation:
    """Categories do not interfere with each other."""

    def test_retrieve_active_filters_by_category(
        self, temp_db: TypedMemoryStore
    ) -> None:
        temp_db.remember(
            TypedMemory(
                id="fact:001",
                category=MemoryCategory.FACT,
                content="Fact 1",
                source=MemorySource.OBSERVATION,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="decision:001",
                category=MemoryCategory.DECISION,
                content="Decision 1",
                source=MemorySource.OPERATOR,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="inference:001",
                category=MemoryCategory.INFERENCE,
                content="Inference 1",
                source=MemorySource.INFERENCE,
                confidence=0.7,
            )
        )

        facts = temp_db.retrieve_active(MemoryCategory.FACT)
        assert len(facts) == 1
        assert facts[0].id == "fact:001"

        decisions = temp_db.retrieve_active(MemoryCategory.DECISION)
        assert len(decisions) == 1

        inferences = temp_db.retrieve_active(MemoryCategory.INFERENCE)
        assert len(inferences) == 1

    def test_clear_category_only_affects_target_category(
        self, temp_db: TypedMemoryStore
    ) -> None:
        temp_db.remember(
            TypedMemory(
                id="fact:001",
                category=MemoryCategory.FACT,
                content="Fact",
                source=MemorySource.OBSERVATION,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="decision:001",
                category=MemoryCategory.DECISION,
                content="Decision",
                source=MemorySource.OPERATOR,
            )
        )

        temp_db.clear_category(MemoryCategory.DECISION)

        facts = temp_db.retrieve_active(MemoryCategory.FACT)
        assert len(facts) == 1

        decisions = temp_db.retrieve_active(MemoryCategory.DECISION)
        assert len(decisions) == 0


class TestProvenanceTracking:
    """Full audit trail is preserved."""

    def test_provenance_stored_and_retrieved(
        self, temp_db: TypedMemoryStore
    ) -> None:
        memory = TypedMemory(
            id="test:001",
            category=MemoryCategory.INCIDENT_LESSON,
            content="Rate limiting was the root cause",
            source=MemorySource.INCIDENT,
            source_id="INC-12345",
            provenance={
                "incident_id": "INC-12345",
                "extracted_at": "2025-08-22T10:00:00Z",
                "analyzer": "pattern_detector",
            },
        )
        temp_db.remember(memory)

        retrieved = temp_db.retrieve("test:001")
        assert retrieved is not None
        assert retrieved.provenance["incident_id"] == "INC-12345"
        assert retrieved.provenance["analyzer"] == "pattern_detector"

    def test_inspect_provenance_shows_chain(
        self, temp_db: TypedMemoryStore
    ) -> None:
        # Create an inference
        inference = TypedMemory(
            id="inf:001",
            category=MemoryCategory.INFERENCE,
            content="Database connection is flaky",
            source=MemorySource.INFERENCE,
            confidence=0.6,
            provenance={"deduced_from": "incident_pattern"},
        )
        temp_db.remember(inference)

        # Operator corrects it
        fact_id = temp_db.correct(
            "inf:001",
            "Database connection pool is undersized",
            "Confirmed by database team",
            operator_id="owner@example.com",
        )
        assert fact_id

        # Inspect provenance of the new fact
        provenance = temp_db.inspect_provenance(fact_id)
        assert provenance is not None
        assert len(provenance["chain"]) > 0
        assert provenance["provenance"]["reason"] == "Confirmed by database team"
        assert provenance["provenance"]["operator"] == "owner@example.com"


class TestCriticalRule:
    """Inference never silently becomes fact."""

    def test_inference_has_low_confidence(
        self, temp_db: TypedMemoryStore
    ) -> None:
        inference = TypedMemory(
            id="inf:001",
            category=MemoryCategory.INFERENCE,
            content="User prefers dark mode",
            source=MemorySource.INFERENCE,
            confidence=0.7,
        )
        temp_db.remember(inference)

        retrieved = temp_db.retrieve("inf:001")
        assert retrieved is not None
        assert retrieved.confidence < 1.0
        assert retrieved.source == MemorySource.INFERENCE

    def test_operator_correction_creates_new_fact_entry(
        self, temp_db: TypedMemoryStore
    ) -> None:
        # Start with inference
        inference = TypedMemory(
            id="inf:001",
            category=MemoryCategory.INFERENCE,
            content="User is in Europe",
            source=MemorySource.INFERENCE,
            confidence=0.5,
        )
        temp_db.remember(inference)

        # Operator corrects
        fact_id = temp_db.correct(
            "inf:001",
            "User is in Germany",
            "User confirmed location",
            operator_id="owner",
        )

        # Original inference still exists but is superseded
        inference_retrieved = temp_db.retrieve("inf:001")
        assert inference_retrieved is not None
        assert inference_retrieved.superseded_by == fact_id
        assert inference_retrieved.active  # Still active, not deleted

        # New fact exists with high confidence
        fact = temp_db.retrieve(fact_id)
        assert fact is not None
        assert fact.source == MemorySource.CORRECTION
        assert fact.confidence == 1.0
        assert fact.category == MemoryCategory.FACT
        assert fact.supersedes == "inf:001"

    def test_correction_preserves_inference_in_provenance(
        self, temp_db: TypedMemoryStore
    ) -> None:
        inference = TypedMemory(
            id="inf:001",
            category=MemoryCategory.INFERENCE,
            content="Original inference",
            source=MemorySource.INFERENCE,
            confidence=0.6,
        )
        temp_db.remember(inference)

        fact_id = temp_db.correct(
            "inf:001",
            "Corrected content",
            "Because reasons",
            operator_id="op",
        )

        fact = temp_db.retrieve(fact_id)
        assert fact is not None
        corrected_from = fact.provenance.get("corrected_from", {})
        assert corrected_from["content"] == "Original inference"
        assert corrected_from["source"] == "inference"
        assert corrected_from["confidence"] == 0.6


class TestSupersession:
    """Supersession links are maintained; old entries are not deleted."""

    def test_supersede_marks_both_entries(
        self, temp_db: TypedMemoryStore
    ) -> None:
        old = TypedMemory(
            id="decision:v1",
            category=MemoryCategory.DECISION,
            content="Use PostgreSQL",
            source=MemorySource.OPERATOR,
        )
        temp_db.remember(old)

        new = TypedMemory(
            id="decision:v2",
            category=MemoryCategory.DECISION,
            content="Use SQLite",
            source=MemorySource.OPERATOR,
        )
        temp_db.supersede("decision:v1", new)

        old_retrieved = temp_db.retrieve("decision:v1")
        assert old_retrieved is not None
        assert old_retrieved.superseded_by == "decision:v2"

        new_retrieved = temp_db.retrieve("decision:v2")
        assert new_retrieved is not None
        assert new_retrieved.supersedes == "decision:v1"

    def test_provenance_chain_follows_supersession(
        self, temp_db: TypedMemoryStore
    ) -> None:
        # v1
        v1 = TypedMemory(
            id="pref:v1",
            category=MemoryCategory.PREFERENCE,
            content="Use Python",
            source=MemorySource.OPERATOR,
        )
        temp_db.remember(v1)

        # v2 supersedes v1
        v2 = TypedMemory(
            id="pref:v2",
            category=MemoryCategory.PREFERENCE,
            content="Prefer Python 3.11+",
            source=MemorySource.OPERATOR,
        )
        temp_db.supersede("pref:v1", v2)

        # v3 supersedes v2
        v3 = TypedMemory(
            id="pref:v3",
            category=MemoryCategory.PREFERENCE,
            content="Prefer Python 3.12+",
            source=MemorySource.OPERATOR,
        )
        temp_db.supersede("pref:v2", v3)

        # Inspect chain from v3
        provenance = temp_db.inspect_provenance("pref:v3")
        assert provenance is not None
        assert len(provenance["chain"]) == 2  # v2 and v1


class TestSoftDelete:
    """forget() and clear_category() are soft-deletes."""

    def test_forget_marks_inactive(self, temp_db: TypedMemoryStore) -> None:
        memory = TypedMemory(
            id="test:001",
            category=MemoryCategory.FACT,
            content="To be forgotten",
            source=MemorySource.OBSERVATION,
        )
        temp_db.remember(memory)

        temp_db.forget("test:001")

        # Entry still exists but is inactive
        retrieved = temp_db.retrieve("test:001")
        assert retrieved is not None
        assert not retrieved.active

        # Not returned by retrieve_active
        active = temp_db.retrieve_active()
        assert "test:001" not in [m.id for m in active]

    def test_clear_category_soft_deletes_all(
        self, temp_db: TypedMemoryStore
    ) -> None:
        for i in range(3):
            temp_db.remember(
                TypedMemory(
                    id=f"temp:00{i}",
                    category=MemoryCategory.TEMPORARY,
                    content=f"Temporary {i}",
                    source=MemorySource.OBSERVATION,
                )
            )

        temp_db.clear_category(MemoryCategory.TEMPORARY)

        # Still retrievable but inactive
        for i in range(3):
            retrieved = temp_db.retrieve(f"temp:00{i}")
            assert retrieved is not None
            assert not retrieved.active

        # Not in active results
        active = temp_db.retrieve_active(MemoryCategory.TEMPORARY)
        assert len(active) == 0


class TestSearch:
    """Full-text search over memory content."""

    def test_search_returns_matching_entries(
        self, temp_db: TypedMemoryStore
    ) -> None:
        temp_db.remember(
            TypedMemory(
                id="eng:001",
                category=MemoryCategory.ENGINEERING_LESSON,
                content="Caching layer improved latency by 40%",
                source=MemorySource.OBSERVATION,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="eng:002",
                category=MemoryCategory.ENGINEERING_LESSON,
                content="Database indexing is critical for performance",
                source=MemorySource.OBSERVATION,
            )
        )

        results = temp_db.search("caching")
        assert len(results) >= 1
        assert any(r.id == "eng:001" for r in results)

    def test_search_filters_by_category(
        self, temp_db: TypedMemoryStore
    ) -> None:
        temp_db.remember(
            TypedMemory(
                id="fact:001",
                category=MemoryCategory.FACT,
                content="Python is fast",
                source=MemorySource.OBSERVATION,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="inf:001",
                category=MemoryCategory.INFERENCE,
                content="Python is fast because of C extensions",
                source=MemorySource.INFERENCE,
            )
        )

        fact_results = temp_db.search("fast", category=MemoryCategory.FACT)
        inference_results = temp_db.search("fast", category=MemoryCategory.INFERENCE)

        assert any(r.id == "fact:001" for r in fact_results)
        assert not any(r.id == "inf:001" for r in fact_results)
        assert any(r.id == "inf:001" for r in inference_results)

    def test_search_empty_query_returns_active(
        self, temp_db: TypedMemoryStore
    ) -> None:
        temp_db.remember(
            TypedMemory(
                id="test:001",
                category=MemoryCategory.FACT,
                content="Test memory",
                source=MemorySource.OBSERVATION,
            )
        )

        results = temp_db.search("")
        assert len(results) >= 1

    def test_search_excludes_inactive(self, temp_db: TypedMemoryStore) -> None:
        temp_db.remember(
            TypedMemory(
                id="test:001",
                category=MemoryCategory.FACT,
                content="Active memory",
                source=MemorySource.OBSERVATION,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="test:002",
                category=MemoryCategory.FACT,
                content="Inactive memory",
                source=MemorySource.OBSERVATION,
            )
        )

        temp_db.forget("test:002")

        results = temp_db.search("memory")
        active_ids = [r.id for r in results]
        assert "test:001" in active_ids
        assert "test:002" not in active_ids


class TestConfidenceAndSource:
    """Confidence and source tracking."""

    def test_confidence_defaults_to_1_0(self, temp_db: TypedMemoryStore) -> None:
        memory = TypedMemory(
            id="test:001",
            category=MemoryCategory.FACT,
            content="Test",
            source=MemorySource.OBSERVATION,
        )
        temp_db.remember(memory)

        retrieved = temp_db.retrieve("test:001")
        assert retrieved is not None
        assert retrieved.confidence == 1.0

    def test_confidence_can_be_set_for_inferences(
        self, temp_db: TypedMemoryStore
    ) -> None:
        memory = TypedMemory(
            id="inf:001",
            category=MemoryCategory.INFERENCE,
            content="User might prefer X",
            source=MemorySource.INFERENCE,
            confidence=0.45,
        )
        temp_db.remember(memory)

        retrieved = temp_db.retrieve("inf:001")
        assert retrieved is not None
        assert retrieved.confidence == 0.45

    def test_source_values_are_preserved(
        self, temp_db: TypedMemoryStore
    ) -> None:
        for source in MemorySource:
            memory = TypedMemory(
                id=f"src:{source.value}",
                category=MemoryCategory.FACT,
                content=f"From {source.value}",
                source=source,
            )
            temp_db.remember(memory)

            retrieved = temp_db.retrieve(f"src:{source.value}")
            assert retrieved is not None
            assert retrieved.source == source


class TestUtilities:
    """count() and count_by_category()."""

    def test_count(self, temp_db: TypedMemoryStore) -> None:
        assert temp_db.count() == 0

        for i in range(3):
            temp_db.remember(
                TypedMemory(
                    id=f"test:{i:02d}",
                    category=MemoryCategory.FACT,
                    content=f"Entry {i}",
                    source=MemorySource.OBSERVATION,
                )
            )

        assert temp_db.count() == 3

        temp_db.forget("test:00")
        assert temp_db.count(active_only=True) == 2
        assert temp_db.count(active_only=False) == 3

    def test_count_by_category(self, temp_db: TypedMemoryStore) -> None:
        temp_db.remember(
            TypedMemory(
                id="f:1",
                category=MemoryCategory.FACT,
                content="Fact",
                source=MemorySource.OBSERVATION,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="f:2",
                category=MemoryCategory.FACT,
                content="Fact 2",
                source=MemorySource.OBSERVATION,
            )
        )
        temp_db.remember(
            TypedMemory(
                id="d:1",
                category=MemoryCategory.DECISION,
                content="Decision",
                source=MemorySource.OPERATOR,
            )
        )

        counts = temp_db.count_by_category()
        assert counts[MemoryCategory.FACT.value] == 2
        assert counts[MemoryCategory.DECISION.value] == 1


class TestDeserialization:
    """TypedMemory.from_dict() handles all fields."""

    def test_from_dict_reconstructs_object(self) -> None:
        original = TypedMemory(
            id="test:001",
            category=MemoryCategory.INFERENCE,
            content="Test content",
            source=MemorySource.INFERENCE,
            source_id="ref:123",
            created_at="2025-08-22T10:00:00Z",
            updated_at="2025-08-22T11:00:00Z",
            confidence=0.7,
            supersedes="old:001",
            superseded_by="new:001",
            active=True,
            expires_at="2025-09-22T10:00:00Z",
            provenance={"key": "value"},
        )

        data = original.to_dict()
        reconstructed = TypedMemory.from_dict(data)

        assert reconstructed.id == original.id
        assert reconstructed.category == original.category
        assert reconstructed.content == original.content
        assert reconstructed.source == original.source
        assert reconstructed.confidence == original.confidence
        assert reconstructed.provenance == original.provenance
