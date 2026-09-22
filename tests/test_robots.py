import pytest

from fundscout.fetch.robots import RobotsChecker, RobotsRules
from tests.fakeweb import fixture_bytes

UA = "FundscoutBot/0.1.0 (+mailto:ops@example.org)"
EDCTP = "https://www.global-health-edctp3.europa.eu"


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/funding/calls-proposals_en", True),
        ("/funding/calls-proposals_en?page=2", True),
        ("/document/download/d3d297d7-6fe2-442c-bd25-917f2b6b9dbf_en", True),
        ("/node/93/rss_en", True),
        ("/search/site?q=x", False),
        ("/user/login", False),
        ("/fr/media/oembed", False),  # Disallow: /*/media/oembed
        ("/core/misc/drupal.js", True),  # Allow: /core/*.js$ beats Disallow: /core/
        ("/core/misc/drupal.js.map", False),  # "$" anchors the Allow rule
        ("/core/install.php", False),
    ],
)
def test_edctp3_robots(path: str, allowed: bool) -> None:
    rules = RobotsRules.parse(fixture_bytes("robots/global-health-edctp3.europa.eu.txt").decode())
    assert rules.can_fetch(EDCTP + path, UA) is allowed


def test_grants_gov_allows_all() -> None:
    rules = RobotsRules.parse(fixture_bytes("robots/www.grants.gov.txt").decode())
    assert rules.can_fetch("https://www.grants.gov/search-results-detail/1", UA)


def test_specific_group_and_crawl_delay() -> None:
    rules = RobotsRules.parse(
        "User-agent: *\nDisallow: /\n\nUser-agent: fundscoutbot\nDisallow: /private\n"
        "Crawl-delay: 7\n"
    )
    assert rules.can_fetch("https://x.org/public", UA)
    assert not rules.can_fetch("https://x.org/private/a", UA)
    assert rules.crawl_delay(UA) == 7
    assert not rules.can_fetch("https://x.org/public", "OtherBot")


def test_longest_match_and_allow_wins_tie() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /a\nAllow: /a/b\nAllow: /c\nDisallow: /c")
    assert not rules.can_fetch("https://x.org/a/x", UA)
    assert rules.can_fetch("https://x.org/a/b/c", UA)
    assert rules.can_fetch("https://x.org/c", UA)


@pytest.mark.parametrize(
    ("status", "allowed"),
    [(200, False), (404, True), (403, True), (401, True), (500, False), (503, False)],
)
async def test_checker_status_handling(status: int, allowed: bool) -> None:
    async def fetcher(url: str) -> tuple[int, str]:
        return status, "User-agent: *\nDisallow: /"

    checker = RobotsChecker(fetcher, UA)
    assert await checker.can_fetch("https://x.org/page") is allowed


async def test_checker_network_error_disallows_and_caches() -> None:
    calls: list[str] = []

    async def fetcher(url: str) -> tuple[int, str]:
        calls.append(url)
        raise OSError("boom")

    checker = RobotsChecker(fetcher, UA)
    assert not await checker.can_fetch("https://x.org/a")
    assert not await checker.can_fetch("https://x.org/b")
    assert calls == ["https://x.org/robots.txt"]
