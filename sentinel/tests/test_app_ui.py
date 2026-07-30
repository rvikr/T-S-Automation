"""Page-flow tests for the Streamlit app, via streamlit.testing.AppTest.

These execute the real ``sentinel/app.py`` script headlessly — the same code
path a browser session runs — covering what the backend tests cannot: view
wiring, the password gate, and the Tier-1 → ticket → human-resolution loop as
it renders.

Hermeticity: the app module reads ``DEFAULT_DB_PATH`` from ``sentinel.config``
at script (re-)execution time, so patching the config attribute redirects every
run into a temp database. Quarantine is stubbed at the orchestrator seam so UI
runs never write markers into the repo's data directory.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    import sentinel.agents.orchestrator as orchestrator
    import sentinel.config as config

    monkeypatch.setattr(config, "DEFAULT_DB_PATH", tmp_path / "audit.sqlite")
    monkeypatch.setattr(orchestrator, "quarantine", lambda case: True)
    monkeypatch.setenv("SENTINEL_UI_PASSWORD", "")
    return tmp_path


def _boot() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=120)
    at.run()
    return at


def _view_options(at: AppTest) -> list[str]:
    return list(at.sidebar.radio[0].options)


def test_all_views_render_without_exceptions(app_env):
    at = _boot()
    assert not at.exception
    views = _view_options(at)
    assert views[0] == "Moderation"
    assert any(v.startswith("Review queue") for v in views)
    assert "Logs" in views and "Metrics" in views

    for view in views[1:]:
        at.sidebar.radio[0].set_value(view).run()
        assert not at.exception, f"view {view!r} raised"


def test_tier1_case_creates_ticket_then_human_resolution_closes_it(app_env):
    at = _boot()
    box = next(s for s in at.selectbox if any("tier1-child-standin-001" in str(o) for o in s.options))
    box.set_value(next(o for o in box.options if "tier1-child-standin-001" in o))
    next(b for b in at.button if b.label == "Run synthetic case").click().run()

    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Decision"] == "ambiguous"
    assert metrics["Severity tier"] == "1"
    assert metrics["Reviewer"] == "human"
    assert any("Human review ticket created" in e.value for e in at.error)

    # The sidebar badge is computed at the top of the script run, so the count
    # from a ticket created mid-run appears on the *next* rerun — same as a
    # real browser session.
    at.run()
    queue_view = next(v for v in _view_options(at) if v.startswith("Review queue"))
    assert queue_view.endswith("(1)")
    at.sidebar.radio[0].set_value(queue_view).run()
    ticket_box = next(s for s in at.selectbox if any("TKT-" in str(o) for o in s.options))
    assert "tier1-child-standin-001" in ticket_box.options[0]

    decision = next(r for r in at.radio if r.options == ["allow", "reject"])
    decision.set_value("reject")
    at.text_area[0].set_value("Confirmed on human review.")
    next(b for b in at.button if b.label == "Resolve ticket").click().run()

    assert not at.exception
    assert not any(v.endswith("(1)") for v in _view_options(at))


def test_resolution_without_rationale_is_refused(app_env):
    at = _boot()
    box = next(s for s in at.selectbox if any("tier1-child-standin-001" in str(o) for o in s.options))
    box.set_value(next(o for o in box.options if "tier1-child-standin-001" in o))
    next(b for b in at.button if b.label == "Run synthetic case").click().run()
    at.run()  # badge updates on the rerun after ticket creation

    queue_view = next(v for v in _view_options(at) if v.startswith("Review queue"))
    at.sidebar.radio[0].set_value(queue_view).run()
    next(b for b in at.button if b.label == "Resolve ticket").click().run()

    assert any("rationale is required" in e.value for e in at.error)
    # Ticket stays open: the badge still shows it.
    assert any(v.endswith("(1)") for v in _view_options(at))


def test_password_gate_locks_production_tab(app_env, monkeypatch):
    monkeypatch.setenv("SENTINEL_UI_PASSWORD", "demo-pass")
    at = _boot()

    labels = [b.label for b in at.button]
    assert "Unlock" in labels
    assert "Run the Tier-1 guardrail demo" not in labels

    # Wrong password stays locked.
    at.text_input[0].set_value("wrong")
    next(b for b in at.button if b.label == "Unlock").click().run()
    assert any("Incorrect password" in e.value for e in at.error)

    # Correct password unlocks the production surface.
    at.text_input[0].set_value("demo-pass")
    next(b for b in at.button if b.label == "Unlock").click().run()
    assert "Run the Tier-1 guardrail demo" in [b.label for b in at.button]
