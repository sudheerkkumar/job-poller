"""Careers-page job-board detection on real-world-shaped HTML snippets."""
from jobpoller.sources.careers import detect_providers


def test_greenhouse_embed_and_eu_board():
    html = ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=aircallioinc"></script>'
            '<a href="https://job-boards.eu.greenhouse.io/feverup/jobs/123">job</a>')
    d = detect_providers(html)
    assert ("aircallioinc", None) in d["greenhouse"]
    assert ("feverup", "eu") in d["greenhouse"]


def test_ashby_slug_with_dot_and_lever_eu():
    html = ('<a href="https://jobs.ashbyhq.com/taxfix.com/abc">Open roles</a>'
            '<a href="https://jobs.eu.lever.co/acme/1">x</a>')
    d = detect_providers(html)
    assert d["ashby"] == [("taxfix.com", None)]
    assert d["lever"] == [("acme", "api.eu.lever.co")]


def test_unreadable_providers_are_reported():
    d = detect_providers('<a href="https://acme.wd3.myworkdayjobs.com/External">jobs</a>'
                         '<iframe src="https://career5.successfactors.eu/career?company=acme"></iframe>')
    assert "workday" in d and "successfactors" in d


def test_junk_slugs_ignored():
    assert "greenhouse" not in detect_providers('<a href="https://boards.greenhouse.io/embed">x</a>')
