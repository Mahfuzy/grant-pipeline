import json
from pathlib import Path

from fundscout.fetch.hashing import HashSpec, content_hash, sha256_hex
from fundscout.fetch.ratelimit import DomainRateLimiter
from fundscout.fetch.storage import LocalRawStorage
from tests.fakeweb import fixture_bytes

GRANTS_GOV_HASH = HashSpec(mode="json", exclude_keys=["token"], sort_lists=True)


def test_grants_gov_canonical_hash_ignores_token_and_list_order() -> None:
    # Two real fetches of the same, unchanged opportunity.
    a = fixture_bytes("grants_gov/fetch_355824_a.json")
    b = fixture_bytes("grants_gov/fetch_355824_b.json")
    assert sha256_hex(a) != sha256_hex(b)
    assert content_hash(a, GRANTS_GOV_HASH) == content_hash(b, GRANTS_GOV_HASH)


def test_canonical_hash_detects_real_changes() -> None:
    data = json.loads(fixture_bytes("grants_gov/fetch_355824_a.json"))
    before = content_hash(json.dumps(data).encode(), GRANTS_GOV_HASH)
    data["data"]["forecast"]["awardCeiling"] = "700000"
    assert content_hash(json.dumps(data).encode(), GRANTS_GOV_HASH) != before


def test_raw_mode_and_invalid_json_fall_back_to_bytes() -> None:
    assert content_hash(b"abc", HashSpec()) == sha256_hex(b"abc")
    assert content_hash(b"not json", GRANTS_GOV_HASH) == sha256_hex(b"not json")


def test_html_hash_scoped_to_selector() -> None:
    spec = HashSpec(mode="html", selector="main", exclude_selectors=["form"])
    page = "<html><body><header>{}</header><main><p>Grant</p><form>{}</form></main></body></html>"
    assert content_hash(page.format("a", "t1").encode(), spec) == content_hash(
        page.format("b", "t2").encode(), spec
    )
    changed = page.replace("Grant", "Grant v2")
    assert content_hash(changed.format("a", "t1").encode(), spec) != content_hash(
        page.format("a", "t1").encode(), spec
    )


def test_local_storage_is_content_addressed(tmp_path: Path) -> None:
    storage = LocalRawStorage(tmp_path)
    key = storage.save(b"hello")
    assert key == storage.save(b"hello")
    assert key.endswith(sha256_hex(b"hello"))
    assert storage.load(key) == b"hello"
    assert storage.save(b"other") != key


async def test_rate_limiter_spaces_requests_per_host() -> None:
    now = [100.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = DomainRateLimiter(clock=lambda: now[0], sleep=sleep)
    await limiter.wait("a.org", 3)
    await limiter.wait("b.org", 3)  # other host: no wait
    await limiter.wait("a.org", 3)
    assert slept == [3.0]
    limiter.defer("a.org", 60)
    await limiter.wait("a.org", 3)
    assert slept[-1] == 60.0
