"""Board readers against faked provider APIs (no network)."""
import httpx

from jobpoller.sources import boards as ats
from jobpoller.sources.boards import Board


def client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        key = f"{req.url.host}{req.url.path}"
        return routes.get(key, httpx.Response(404))
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_greenhouse_list_us_and_eu_host():
    body = {"jobs": [{"id": 1, "title": "Backend Engineer", "location": {"name": "Madrid, Spain"},
                      "absolute_url": "https://x/1", "first_published": "2026-11-01T10:00:00Z"}]}
    c = client({"boards-api.greenhouse.io/v1/boards/acme/jobs": httpx.Response(200, json=body),
                "boards-api.eu.greenhouse.io/v1/boards/feverup/jobs": httpx.Response(200, json=body)})
    us = ats.fetch_board(c, Board(ats="greenhouse", slug="acme", company="Acme"))
    eu = ats.fetch_board(c, Board(ats="greenhouse", slug="feverup", company="Fever", host="eu"))
    assert [j.title for j in us] == ["Backend Engineer"] and us[0].location == "Madrid, Spain"
    assert [j.id for j in eu] == ["1"]
    assert Board(ats="greenhouse", slug="feverup", company="F", host="eu").url == \
        "https://job-boards.eu.greenhouse.io/feverup"


def test_lever_eu_host_and_remote_flag():
    body = [{"id": "a1", "text": "Senior Kotlin Engineer", "hostedUrl": "https://jobs.eu.lever.co/acme/a1",
             "categories": {"location": "Barcelona", "allLocations": ["Barcelona"]}, "workplaceType": "remote",
             "createdAt": 1762000000000, "descriptionPlain": "Kotlin", "lists": []}]
    c = client({"api.eu.lever.co/v0/postings/acme": httpx.Response(200, json=body)})
    jobs = ats.fetch_board(c, Board(ats="lever", slug="acme", company="Acme", host="api.eu.lever.co"))
    assert jobs[0].title == "Senior Kotlin Engineer" and jobs[0].remote is True


def test_missing_board_raises():
    c = client({})
    try:
        ats.fetch_board(c, Board(ats="ashby", slug="nope", company="Nope"))
    except ats.AtsError as e:
        assert e.status == 404
    else:
        raise AssertionError("expected AtsError")


def test_rate_limit_is_its_own_error():
    c = client({"api.ashbyhq.com/posting-api/job-board/busy": httpx.Response(429)})
    try:
        ats.fetch_board(c, Board(ats="ashby", slug="busy", company="Busy"))
    except ats.RateLimited:
        pass
    else:
        raise AssertionError("expected RateLimited")


def test_region_tagging():
    from jobpoller.seed_refresh import regions_for
    regions = {"spain": {"locations": ["spain", "madrid"]},
               "europe-remote": {"remote": True, "locations": ["europe", "emea"]}}
    jobs = [ats.AtsJob(id="1", title="x", location="Madrid", url=""),
            ats.AtsJob(id="2", title="y", location="Europe", url="", remote=False),
            ats.AtsJob(id="3", title="z", location="Remote - EMEA", url="")]
    assert regions_for(jobs, regions) == ["spain", "europe-remote"]
    assert regions_for(jobs[1:2], regions) == []      # "Europe" office, not remote -> not europe-remote
