"""End-to-end against a real PostgreSQL (docker compose / CI service). Skipped when TEST_DATABASE_URL is unset.

seed sync -> first poll = baseline (no alerts) -> a new job appears -> Stage 1 -> stored pending -> scored by a
fake LLM -> notified once (console) -> a repost with a new id is a duplicate, not a second alert.
"""
import json
import os

import psycopg
import pytest

from jobpoller import app, boards_runner, scorer
from jobpoller.config import Config
from jobpoller.db import Store
from jobpoller.sources import boards as ats
from jobpoller.sources.boards import AtsJob

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def db():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    s = Store(URL)
    yield s
    s.close()


@pytest.fixture
def cfg():
    return Config.model_validate({
        "regions": ["spain"],
        "filters": {"title_include_any": ["backend", "engineer"], "title_exclude_any": ["junior"],
                    "locations_accepted_any": ["spain", "madrid"]},
        "scorer": {"profile": "backend dev", "criteria": "spain", "min_seconds_between_calls": 0},
        "job_boards": {"workers": 2},
    })


def test_migrations_are_idempotent(db):
    assert db.migrate() == []


def test_seed_sync_filters_by_region(db, cfg, tmp_path):
    seed = tmp_path / "boards.json"
    seed.write_text(json.dumps({"boards": [
        {"id": "ashby:a", "ats": "ashby", "slug": "a", "company": "A", "regions": ["spain"]},
        {"id": "ashby:b", "ats": "ashby", "slug": "b", "company": "B", "regions": ["europe-remote"]}]}))
    assert app.sync_seed(cfg, db, seed) == 1
    assert app.sync_seed(cfg, db, seed) == 0          # idempotent
    assert [b["id"] for b in db.list_boards()] == ["ashby:a"]


class FakeScorer:
    def score(self, system, prompt):
        return scorer.ScoreResult(score=scorer.JobScore(score=8, matched=["backend"], gaps=[], red_flags=[],
                                                        one_liner="good fit"), model="fake", prompt_tokens=1,
                                  output_tokens=1, thoughts_tokens=None)


def test_poll_baseline_then_new_job_scored_and_notified_once(db, cfg, monkeypatch, capsys):
    db.add_boards([{"id": "ashby:acme", "ats": "ashby", "slug": "acme", "company": "Acme"}])
    listing = [AtsJob(id="1", title="Backend Engineer", location="Madrid, Spain", url="https://x/1", description="d")]
    monkeypatch.setattr(ats, "fetch_board", lambda c, b: [AtsJob(**vars(j)) for j in listing])
    monkeypatch.setattr(scorer, "make_scorer", lambda cfg, key=None: FakeScorer())

    first = boards_runner.poll_boards(cfg, db)
    assert first["baseline"] == 1 and first["passed"] == 0          # existing jobs are never alerted

    listing += [AtsJob(id="2", title="Senior Backend Engineer", location="Madrid, Spain", url="https://x/2",
                       description="Kotlin backend"),
                AtsJob(id="3", title="Junior Backend Engineer", location="Madrid", url="https://x/3", description="d"),
                AtsJob(id="4", title="Backend Engineer", location="Berlin, Germany", url="https://x/4", description="d")]
    second = boards_runner.poll_boards(cfg, db)
    assert second["passed"] == 1 and second["rejected"] == 2

    st = app.score_and_notify(cfg, db)
    assert st["scored"] == 1 and st["matches"] == 1
    assert "8/10 · Senior Backend Engineer · Acme" in capsys.readouterr().out
    assert db.counts()["notified"] == 1

    # a repost of the same role under a new id: duplicate, no second alert
    listing.append(AtsJob(id="5", title="Senior Backend Engineer", location="Madrid, Spain", url="https://x/5",
                          description="Kotlin backend"))
    third = boards_runner.poll_boards(cfg, db)
    assert third["duplicate"] == 1 and third["passed"] == 0
    assert app.score_and_notify(cfg, db)["matches"] == 0


def test_failing_board_is_marked_dead(db, cfg, monkeypatch):
    cfg.job_boards.dead_after_failures = 2
    db.add_boards([{"id": "ashby:gone", "ats": "ashby", "slug": "gone", "company": "Gone"}])

    def boom(c, b):
        raise ats.AtsError(404, "HTTP 404")
    monkeypatch.setattr(ats, "fetch_board", boom)
    boards_runner.poll_boards(cfg, db)
    assert db.list_boards()[0]["status"] == "active"
    boards_runner.poll_boards(cfg, db)
    assert db.list_boards()[0]["status"] == "dead"


def test_discovery_adds_found_board_and_records_none(db, cfg, monkeypatch):
    db.add_config_company("acme", "Acme", None)
    db.add_config_company("nobody", "Nobody", None)

    def probe(c, provider, name):
        if name == "Acme" and provider == "greenhouse":
            return ats.Found(ats.Board(ats="greenhouse", slug="acme", company="Acme"), 12)
        return None
    monkeypatch.setattr(ats, "probe_company", probe)
    out = boards_runner.discover(cfg, db)
    assert out["found"] == 1 and out["added"] == ["greenhouse:acme (Acme)"]
    disc = db.discovery()
    assert disc["acme"]["status"] == "found" and disc["nobody"]["status"] == "none"
    assert boards_runner.discover(cfg, db)["due"] == 0              # nothing due again until the re-check


def test_only_one_process_scores_at_a_time(db, cfg):
    other = Store(URL)                       # e.g. the linkedin container
    try:
        assert other.try_lock("scoring")
        assert app.score_and_notify(cfg, db) == {"skipped": "another process is scoring"}
        other.unlock("scoring")
        assert "skipped" not in app.score_and_notify(cfg, db)
        assert other.try_lock("scoring")     # released again after scoring
    finally:
        other.close()


def test_concurrent_migrations_take_turns(db):
    a, b = Store(URL), Store(URL)            # poller + linkedin starting at once: no error, applied once
    try:
        assert a.migrate() == [] and b.migrate() == []
    finally:
        a.close()
        b.close()
