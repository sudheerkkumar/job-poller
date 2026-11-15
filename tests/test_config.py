"""Config defaults and the LinkedIn pacing floors that config cannot lower."""
import pytest

from jobpoller.app import parse_board
from jobpoller.config import Config, LinkedInConfig


def test_linkedin_is_opt_in_by_default():
    assert Config().linkedin.enabled is False


def test_linkedin_floors_cannot_be_lowered():
    li = LinkedInConfig(enabled=True, interval_minutes=5, jitter_seconds=(1, 2), max_requests_per_hour=1000)
    assert li.interval_minutes == 30
    assert li.jitter_seconds == (15.0, 15.0)
    assert li.max_requests_per_hour == 60
    assert LinkedInConfig(max_requests_per_hour=0).max_requests_per_hour == 60


def test_parse_board_specs():
    assert parse_board("greenhouse:stripe")["id"] == "greenhouse:stripe"
    assert parse_board("greenhouse:feverup@eu")["host"] == "eu"
    assert parse_board("lever:acme@eu")["host"] == "api.eu.lever.co"
    assert parse_board("lever:acme")["host"] == "api.lever.co"
    with pytest.raises(ValueError):
        parse_board("workday:acme")
