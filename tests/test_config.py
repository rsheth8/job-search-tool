

def test_the_suite_does_not_read_a_developers_dotenv():
    """A .env in the repo root must not reach the tests.

    This is not hypothetical: APNS_USE_SANDBOX=true in a local .env flipped
    `push_check`'s expected APNs host, so one test failed on the main checkout
    and passed in a worktree and in CI — neither of which has a .env. A suite
    whose result depends on whose machine it runs on is worse than a failing one.
    """
    from app.config import Settings

    assert Settings.model_config.get("env_file") is None
