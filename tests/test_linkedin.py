"""LinkedIn guest-page parsing and block handling - offline, synthetic HTML, no requests to LinkedIn."""
import httpx
import pytest

from jobpoller.sources.linkedin import Blocked, LinkedInClient, parse_cards

CARD = """
<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4100000001">
  <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/x-4100000001"></a>
  <h3 class="base-search-card__title"> Senior Backend Engineer </h3>
  <h4 class="base-search-card__subtitle"><a href="https://es.linkedin.com/company/acme?trk=x">Acme</a></h4>
  <span class="job-search-card__location">Madrid, Community of Madrid, Spain</span>
  <time class="job-search-card__listdate--new" datetime="2026-11-15">1 hour ago</time>
</div></li>"""


def test_parse_cards():
    jobs = parse_cards(CARD)
    assert len(jobs) == 1
    j = jobs[0]
    assert (j.source, j.source_id, j.title, j.company) == ("linkedin", "4100000001", "Senior Backend Engineer", "Acme")
    assert j.location.startswith("Madrid")


def test_429_raises_blocked_with_retry_after():
    t = httpx.MockTransport(lambda req: httpx.Response(429, headers={"retry-after": "120", "set-cookie": "x=1"}))
    c = LinkedInClient(jitter=(15, 25), transport=t)
    with pytest.raises(Blocked) as e:
        c.search("backend", "Spain", 3600, max_pages=1)
    assert e.value.retry_after_seconds == 120
    assert "set-cookie" not in {k.lower() for k in e.value.headers}   # cookies are never kept


def test_login_redirect_is_a_block():
    t = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "https://www.linkedin.com/authwall?x"}))
    c = LinkedInClient(jitter=(15, 25), transport=t)
    with pytest.raises(Blocked):
        c.search("backend", "Spain", 3600, max_pages=1)


def test_client_identifies_itself_honestly():
    c = LinkedInClient(jitter=(15, 25))
    assert c.http.headers["user-agent"].startswith("job-poller/")
