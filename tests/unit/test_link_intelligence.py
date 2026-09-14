"""Unit tests for link intelligence (offline by default, fetch opt-in)."""
from __future__ import annotations

import pytest

from otto import paths
from otto.intelligence import link_intelligence as li
from otto.intelligence.link_intelligence import analyze_url, extract_urls


@pytest.fixture(autouse=True)
def _fresh_cache():
    li.clear_link_cache()
    yield
    li.clear_link_cache()


def test_extract_urls():
    text = "Check out this repo: https://github.com/acme/audio-kit and test it."
    assert extract_urls(text) == ["https://github.com/acme/audio-kit"]


def test_extract_urls_filters_internal_and_dedupes():
    text = ("Link: https://app.slack.com/client/T1/C1 and https://example.com/docs "
            "and again https://example.com/docs plus http://localhost:7077/")
    assert extract_urls(text) == ["https://example.com/docs"]


def test_extract_urls_bare_www():
    assert extract_urls("see www.example.org/page.") == ["https://www.example.org/page"]


def test_github_repo_is_described_offline(monkeypatch):
    monkeypatch.setenv("OTTO_LINK_FETCH", "0")
    intel = analyze_url("https://github.com/acme/audio-kit")
    assert intel["title"] == "acme/audio-kit"
    assert intel["kind"] == "repository"
    assert intel["source"] == "url"
    assert "audio-kit" in intel["summary"]
    assert len(intel["why_useful"]) >= 1


def test_general_url_is_described_offline_without_network(monkeypatch):
    monkeypatch.setenv("OTTO_LINK_FETCH", "0")

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("network access attempted with fetching disabled")

    monkeypatch.setattr(li, "urlopen", boom)
    intel = analyze_url("https://hai.example.edu/definitions/what-are-weights")
    assert intel["url"] == "https://hai.example.edu/definitions/what-are-weights"
    assert intel["kind"] == "paper"
    assert "What are weights" in intel["title"]
    assert intel["source"] == "url"


def test_no_hardcoded_knowledge_base():
    # The module describes links from their shape (host class, path words,
    # optional page metadata) — never from a table of particular sites, repos
    # or articles. The only host literals allowed are the generic platform
    # names it classifies by.
    import inspect
    import re
    src = inspect.getsource(li)
    hosts = set(re.findall(r"""["']([a-z-]+(?:\.[a-z-]+)+)["']""", src)) - {"links.fetch"}   # a config key, not a host
    allowed = {"slack.com", "slack-edge.com", "arxiv.org", "paperswithcode.com", "youtube.com", "youtu.be", "vimeo.com"}
    assert hosts <= allowed, sorted(hosts - allowed)
    assert not re.search(r"github\.com/[\w.-]+/[\w.-]+", src)     # no repo names baked in
    assert not re.search(r"""["'][^"'\s]*[a-z]\.edu[/"']""", src)     # the ".edu" TLD, yes; a particular university, no


def test_fetch_opt_in_uses_page_metadata(monkeypatch):
    monkeypatch.setenv("OTTO_LINK_FETCH", "1")
    monkeypatch.setattr(li, "_host_is_public", lambda host: True)

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self, n=-1):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    html = b"<html><head><title> Great  Tool </title>" \
           b"<meta name='description' content='Does great things.'></head></html>"
    monkeypatch.setattr(li, "urlopen", lambda req, timeout=0: _Resp(html))

    intel = analyze_url("https://tools.example.com/great")
    assert intel["title"] == "Great Tool"
    assert intel["summary"] == "Does great things."
    assert intel["source"] == "fetched"


def test_fetch_refuses_private_hosts(monkeypatch):
    monkeypatch.setenv("OTTO_LINK_FETCH", "1")
    called = []
    monkeypatch.setattr(li, "urlopen", lambda *a, **k: called.append(1))
    assert li._fetch_text("http://localhost:8080/admin") == ""
    assert li._fetch_text("http://127.0.0.1/") == ""
    assert li._fetch_text("ftp://example.com/x") == ""
    assert not called


def test_results_are_cached_on_disk(monkeypatch):
    monkeypatch.setenv("OTTO_LINK_FETCH", "0")
    analyze_url("https://github.com/acme/audio-kit")
    assert paths.link_cache_file().exists()
    assert str(paths.link_cache_file()).startswith(str(paths.data_dir()))
