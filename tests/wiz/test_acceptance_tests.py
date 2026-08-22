"""Tests for acceptance test generation."""

from __future__ import annotations

from openjarvis.wiz.acceptance_tests import AcceptanceTestGenerator


def test_generate_ui_tests():
    """Test generating UI acceptance tests."""
    generator = AcceptanceTestGenerator()
    tests = generator.generate_tests("Add a refresh button to the dashboard", feature_type="ui")

    assert len(tests) > 0
    # Should generate visibility, interaction, and mobile tests
    assert any("visible" in t.name.lower() for t in tests)
    assert any("interact" in t.name.lower() for t in tests)


def test_generate_api_tests():
    """Test generating API acceptance tests."""
    generator = AcceptanceTestGenerator()
    tests = generator.generate_tests("Create a new user endpoint", feature_type="api")

    assert len(tests) > 0
    # Should generate endpoint, structure, and error handling tests
    assert any("endpoint" in t.name.lower() for t in tests)
    assert any("error" in t.name.lower() for t in tests)


def test_extract_action():
    """Test action extraction from description."""
    generator = AcceptanceTestGenerator()

    tests = [
        ("Add a refresh button", "refresh button"),
        ("Create user endpoint", "user endpoint"),
        ("Update dashboard layout", "dashboard layout"),
        ("Remove old feature", "old feature"),
        ("Fix login timeout", "login timeout"),
    ]

    for description, expected_action in tests:
        action = generator._extract_action(description)
        assert action is not None
        assert expected_action.lower() in action.lower()


def test_basic_tests_fallback():
    """Test generating basic tests when type is unknown."""
    generator = AcceptanceTestGenerator()
    tests = generator.generate_tests("Do something cool")

    assert len(tests) > 0
    assert len(tests[0].steps) > 0
    assert len(tests[0].assertions) > 0


def test_acceptance_test_structure():
    """Test AcceptanceTest data structure."""
    generator = AcceptanceTestGenerator()
    tests = generator.generate_tests("Add a button")

    test = tests[0]
    assert test.name
    assert test.description
    assert test.steps
    assert test.assertions
    assert test.expected_outcome


def test_mobile_responsiveness_test():
    """Test that UI tests include mobile responsiveness."""
    generator = AcceptanceTestGenerator()
    tests = generator.generate_tests("Add a dashboard widget", feature_type="ui")

    mobile_tests = [t for t in tests if "mobile" in t.name.lower()]
    assert len(mobile_tests) > 0
    assert any("viewport" in " ".join(t.steps).lower() for t in mobile_tests)
