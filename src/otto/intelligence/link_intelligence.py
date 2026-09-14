"""
Link intelligence: what is this URL someone shared, and why might it matter?

Two tiers:

* **Offline (default)** — derive everything from the URL itself: host,
  GitHub owner/repo, path words. No network request is ever made, so the
  fact that *you* were sent a link is never leaked to a third party.
* **Fetch (opt-in)** — when ``[links] fetch = true`` in config or
  ``OTTO_LINK_FETCH=1`` is set, Otto additionally downloads the page
  ``<title>`` / description (or a GitHub README) with short timeouts and a
  small size cap. Only ``http(s)`` URLs to public hosts are fetched.

Results are cached on disk (``link_cache.json`` in the data dir).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import threading
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from otto import paths

logger = logging.getLogger("otto.intelligence.link_intelligence")

_LOCK = threading.Lock()
_MEMORY_CACHE: dict[str, dict[str, Any]] | None = None
_MAX_CACHE_ENTRIES = 500
_FETCH_TIMEOUT = 4
_FETCH_MAX_BYTES = 256 * 1024

_INTERNAL_HOST_FRAGMENTS = ("slack.com", "slack-edge.com", "localhost", "127.0.0.1", "[::1]")


def fetch_enabled() -> bool:
    """External fetching is opt-in (privacy-by-default)."""
    env = os.environ.get("OTTO_LINK_FETCH")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from otto.config import ConfigManager
        return bool(ConfigManager().get_or("links.fetch", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, dict[str, Any]]:
    global _MEMORY_CACHE
    if _MEMORY_CACHE is not None:
        return _MEMORY_CACHE
    data: dict[str, dict[str, Any]] = {}
    cache_file = paths.link_cache_file()
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as e:
            logger.debug("Failed to load link cache: %s", e)
    _MEMORY_CACHE = data
    return data


def _save_cache() -> None:
    if _MEMORY_CACHE is None:
        return
    try:
        cache_file = paths.link_cache_file()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if len(_MEMORY_CACHE) > _MAX_CACHE_ENTRIES:
            for key in list(_MEMORY_CACHE)[: len(_MEMORY_CACHE) - _MAX_CACHE_ENTRIES]:
                _MEMORY_CACHE.pop(key, None)
        tmp = cache_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_MEMORY_CACHE, f, indent=1)
        os.replace(tmp, cache_file)
    except Exception as e:
        logger.debug("Failed to save link cache: %s", e)


def clear_link_cache() -> None:
    global _MEMORY_CACHE
    with _LOCK:
        _MEMORY_CACHE = {}
        try:
            paths.link_cache_file().unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://[^\s<>"\']+|www\.[^\s<>"\']+')


def extract_urls(text: str) -> list[str]:
    """Extract external URLs from text, ignoring Slack-internal / local links."""
    if not text:
        return []
    out: list[str] = []
    for u in _URL_RE.findall(text):
        u = u.rstrip('.,);:!?\'"')
        if not u.lower().startswith('http'):
            u = f'https://{u}'
        if any(frag in u.lower() for frag in _INTERNAL_HOST_FRAGMENTS):
            continue
        if u not in out:
            out.append(u)
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_url(url: str) -> dict[str, Any]:
    """
    Describe a shared link: ``{title, url, summary, key_features, why_useful, source}``.

    ``source`` is ``"url"`` (derived offline) or ``"fetched"``.
    """
    with _LOCK:
        cache = _load_cache()
        cached = cache.get(url)
        if cached is not None:
            return cached

    intel = _analyze_offline(url)
    if fetch_enabled():
        try:
            fetched = _fetch_details(url, intel)
            if fetched:
                intel.update(fetched)
                intel["source"] = "fetched"
        except Exception as e:
            logger.debug("Link fetch failed for %s: %s", url, e)

    with _LOCK:
        cache[url] = intel
        _save_cache()
    return intel


_GITHUB_RE = re.compile(r'github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)')


def _humanise(slug: str) -> str:
    words = re.sub(r'[-_.]+', ' ', slug).strip()
    return words[:1].upper() + words[1:] if words else slug


def _analyze_offline(url: str) -> dict[str, Any]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path_words = [p for p in parts.path.split('/') if p]

    gh = _GITHUB_RE.search(url)
    if gh:
        owner, repo = gh.group(1), re.sub(r'\.git$', '', gh.group(2))
        return {
            "title": f"{owner}/{repo}",
            "url": url,
            "summary": f"GitHub repository {owner}/{repo} ({_humanise(repo)}).",
            "key_features": [],
            "why_useful": [
                f"Open-source project on GitHub — evaluate {repo} for upcoming work.",
                "Check the README, license, recent activity and open issues before adopting.",
            ],
            "kind": "repository",
            "source": "url",
        }

    kind = "article"
    if host.endswith(".edu") or "arxiv.org" in host or "paperswithcode.com" in host:
        kind = "paper"
    elif any(host.endswith(d) for d in ("youtube.com", "youtu.be", "vimeo.com")):
        kind = "video"
    elif any(host.startswith(d) for d in ("docs.", "developer.", "developers.")):
        kind = "documentation"

    topic = _humanise(path_words[-1]) if path_words else host
    title = f"{topic} — {host}" if path_words else host
    return {
        "title": title[:80],
        "url": url,
        "summary": f"{kind.capitalize()} on {host}" + (f": {topic}" if path_words else "") + ".",
        "key_features": [],
        "why_useful": [f"Shared for review — {kind} from {host}."],
        "kind": kind,
        "source": "url",
    }


def _host_is_public(host: str) -> bool:
    """Refuse to fetch anything that resolves to a private/loopback address (SSRF guard)."""
    if not host or host in ("localhost",) or host.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _fetch_text(url: str, user_agent: str = "Otto/1.0 (+read-only briefing)") -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not _host_is_public(parts.hostname or ""):
        return ""
    req = Request(url, headers={"User-Agent": user_agent, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
    with urlopen(req, timeout=_FETCH_TIMEOUT) as resp:  # noqa: S310 - scheme + host validated above
        return resp.read(_FETCH_MAX_BYTES).decode("utf-8", errors="ignore")


def _fetch_details(url: str, base: dict[str, Any]) -> dict[str, Any]:
    gh = _GITHUB_RE.search(url)
    if gh:
        owner, repo = gh.group(1), re.sub(r'\.git$', '', gh.group(2))
        for branch in ("main", "master"):
            try:
                readme = _fetch_text(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md")
            except Exception:
                continue
            if readme:
                return _summarise_readme(readme)
        return {}

    html = _fetch_text(url)
    if not html:
        return {}
    out: dict[str, Any] = {}
    t_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if t_match:
        out["title"] = re.sub(r'\s+', ' ', t_match.group(1)).strip()[:80]
    desc = re.search(
        r'<meta[^>]*(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*(?:name|property)=["\'](?:description|og:description)["\']',
        html, re.IGNORECASE,
    )
    if desc:
        out["summary"] = re.sub(r'\s+', ' ', desc.group(1)).strip()[:200]
    return out


def _summarise_readme(readme: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    feat = re.search(r'##\s*(?:Features|Why|Capabilities|Highlights)[\s\S]*?(?=\n##|\Z)', readme, re.IGNORECASE)
    if feat:
        bullets = [
            l.strip().lstrip('-*•').strip()
            for l in feat.group(0).split('\n')
            if l.strip().startswith(('-', '*', '•'))
        ]
        bullets = [re.sub(r'[*_`]', '', b)[:120] for b in bullets if len(b) > 10]
        if bullets:
            out["key_features"] = bullets[:6]
            out["why_useful"] = bullets[:4]
    desc = re.search(r'#[^\n]+\n+([^\n#\[!<][^\n]{20,})', readme)
    if desc:
        out["summary"] = re.sub(r'[*_`]', '', desc.group(1)).strip()[:200]
    return out
