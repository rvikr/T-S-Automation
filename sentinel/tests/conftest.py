import pytest


@pytest.fixture(autouse=True)
def _no_external_ticketing(monkeypatch):
    """Keep tests hermetic: never let a configured Jira environment leak into test runs.

    Set to empty string (not delete): load_dotenv() does not override existing
    variables, so this also blocks re-import from .env.local mid-test.
    """
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
        monkeypatch.setenv(var, "")
