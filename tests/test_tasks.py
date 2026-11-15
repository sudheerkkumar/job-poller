"""Task selection for the two containers: poller (--skip linkedin) and the opt-in linkedin container."""
from jobpoller import app
from jobpoller.config import Config

LI = {"enabled": True, "searches": [{"keywords": "backend", "location": "Spain"}]}


def names(cfg, **kw):
    return [t.name for t in app.tasks(cfg, db=None, **kw)]


def test_default_runs_everything_enabled():
    assert names(Config()) == ["boards", "discovery"]                  # LinkedIn is off by default
    assert names(Config(linkedin=LI)) == ["boards", "discovery", "linkedin"]


def test_poller_container_skips_linkedin():
    assert names(Config(linkedin=LI), skip={"linkedin"}) == ["boards", "discovery"]


def test_linkedin_container_runs_only_linkedin():
    assert names(Config(linkedin=LI), only={"linkedin"}) == ["linkedin"]


def test_linkedin_container_idles_when_not_enabled(caplog):
    assert names(Config(), only={"linkedin"}) == []
    assert "linkedin.enabled is false" in caplog.text


def test_linkedin_without_searches_is_skipped():
    assert names(Config(linkedin={"enabled": True}), only={"linkedin"}) == []
