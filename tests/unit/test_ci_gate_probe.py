def test_deliberately_failing_to_prove_the_ci_gate() -> None:
    """Temporary (#356 AC 5): `test` fails here, so `ci` must fail even though `e2e` passes."""
    assert False  # noqa: B011, PT015
