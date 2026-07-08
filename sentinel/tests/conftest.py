import pytest


@pytest.fixture(autouse=True)
def _no_external_ticketing(monkeypatch):
    """Keep tests hermetic: never let a configured Jira environment leak into test runs.

    Set to empty string (not delete): load_dotenv() does not override existing
    variables, so this also blocks re-import from .env.local mid-test.

    OPENAI_API_KEY is scrubbed for the same reason: no test may reach the live
    agent runtime or spawn the SDK's background trace exporter.
    """
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(var, "")
