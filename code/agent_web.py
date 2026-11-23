# -*- coding: utf-8 -*-
"""Optional web search for OpenECRS.

Web results are deliberately kept separate from the evidence base: they are
useful for *current* news / competitors / regulation, but they are not part of
the audited dataset and must be labelled as external in answers.

Provider: Bing HTML results (no API key required). The query is sent to Bing.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.parse
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SETLANG = {"en": "en", "zh": "zh-Hans", "ja": "ja", "es": "es"}
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8889").rstrip("/")
SEARXNG_FALLBACK = ("http://127.0.0.1:8888",)

NEWS_WORDS = ("latest", "news", "today", "current", "regulation", "policy", "2026",
              "最新", "新闻", "今天", "近期", "监管", "政策", "最新情報", "ニュース", "規制",
              "actualidad", "noticias", "regulación", "hoy")


def _wants_news(query: str) -> bool:
    lowered = query.lower()
    return any(word in lowered for word in NEWS_WORDS)


def _news_rss(query: str, max_results: int, lang: str, timeout: int) -> list[dict]:
    import xml.etree.ElementTree as ET
    locale = {"zh": ("zh-CN", "CN", "CN:zh-Hans"), "ja": ("ja", "JP", "JP:ja"),
              "es": ("es", "ES", "ES:es")}.get(lang, ("en-US", "US", "US:en"))
    urls = [
        "https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
        + "&hl=" + locale[0] + "&gl=" + locale[1] + "&ceid=" + locale[2],
        "https://www.bing.com/news/search?q=" + urllib.parse.quote(query) + "&format=rss",
    ]
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "ignore")
            root = ET.fromstring(raw)
            results, seen = [], set()
            for item in root.iter("item"):
                title = re.sub(r"<[^>]+>", "", (item.findtext("title") or "")).strip()
                link = (item.findtext("link") or "").strip()
                if not title or not link.startswith(("http://", "https://")):
                    continue
                key = (title[:120], link[:200])
                if key in seen:
                    continue
                seen.add(key)
                description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
                source = item.findtext("{http://news.google.com/rss}source") or item.findtext("source") or ""
                results.append({
                    "title": html.unescape(title)[:240],
                    "url": link[:500],
                    "snippet": html.unescape(re.sub(r"\s+", " ", description)).strip()[:360],
                    "published": (item.findtext("pubDate") or "")[:40],
                    "engine": source or ("google-news" if "news.google" in url else "bing-news"),
                    "source": "web",
                })
                if len(results) >= max_results:
                    return results
            if results:
                return results
        except Exception:
            continue
    return []


class WebSearchError(RuntimeError):
    pass


def _clean(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(re.sub(r"\s+", " ", value)).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def _real_url(href: str) -> str:
    href = html.unescape(href or "")
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        encoded = (query.get("u") or [""])[0]
        if encoded.startswith("a1"):
            padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
    except Exception:
        pass
    return href


def parse_results(page: str, max_results: int = 5) -> list[dict]:
    """Parse Bing's HTML result list (kept small and deterministic)."""
    results = []
    seen = set()
    for block in re.split(r'<li class="b_algo"', page or "")[1:]:
        title_match = re.search(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
        if not title_match:
            continue
        title = _clean(title_match.group(2))
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _clean(snippet_match.group(1)) if snippet_match else ""
        url = _real_url(title_match.group(1))
        if not title or not url.startswith(("http://", "https://")):
            continue
        key = (title[:120], url[:200])
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": title[:240], "url": url[:500], "snippet": snippet[:360], "source": "web"})
        if len(results) >= max_results:
            break
    return results


def fetch_page(url: str, timeout: int = 15) -> str:
    """Fetch a page and return readable text (trafilatura if installed, else stdlib)."""
    if not url.startswith(("http://", "https://")):
        return ""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                  "Accept-Language": "en-US,en;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_500_000)
    except Exception:
        return ""
    try:
        import trafilatura  # optional, much cleaner
        text = trafilatura.extract(raw.decode("utf-8", "ignore")) or ""
        if text:
            return re.sub(r"\s+", " ", text).strip()[:6000]
    except Exception:
        pass
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip = 0
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
                self.skip += 1
            if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "tr"):
                self.parts.append(" ")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip and data.strip():
                self.parts.append(data.strip())

    parser = _Text()
    try:
        parser.feed(raw.decode("utf-8", "ignore"))
    except Exception:
        return ""
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:6000]


def _enrich(results: list[dict], max_pages: int = 2) -> list[dict]:
    for item in results[:max_pages]:
        try:
            content = fetch_page(item["url"])
            if content and len(content) > len(item.get("snippet", "")):
                item["content"] = content[:4000]
        except Exception:
            continue
    return results


def _searxng(query: str, max_results: int, lang: str, timeout: int) -> list[dict]:
    bases = [SEARXNG_URL] if SEARXNG_URL else []
    bases += [base for base in SEARXNG_FALLBACK if base not in bases]
    payload = {}
    for base in bases:
        url = (base + "/search?q=" + urllib.parse.quote(query)
               + "&format=json&count=" + str(max(1, min(10, int(max_results))))
               + "&language=" + SETLANG.get(lang, "en"))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore") or "{}")
            break
        except Exception:
            continue
    results, seen = [], set()
    for item in payload.get("results") or []:
        title = (item.get("title") or "").strip()
        link = (item.get("url") or "").strip()
        if not title or not link.startswith(("http://", "https://")):
            continue
        key = (title[:120], link[:200])
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "title": html.unescape(title)[:240],
            "url": link[:500],
            "snippet": html.unescape((item.get("content") or "").strip())[:360],
            "engine": item.get("engine") or "",
            "source": "web",
        })
        if len(results) >= max_results:
            break
    return results


def _bing_html(query: str, max_results: int, lang: str, timeout: int) -> list[dict]:
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(query)
           + "&count=" + str(max(1, min(10, int(max_results))))
           + "&setlang=" + SETLANG.get(lang, "en") + "&cc=US")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": {"zh": "zh-CN,zh;q=0.9,en;q=0.8", "ja": "ja,en;q=0.8",
                            "es": "es,en;q=0.8"}.get(lang, "en-US,en;q=0.9"),
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        page = response.read().decode("utf-8", "ignore")
    return parse_results(page, max_results)


def _relevance(item: dict, query: str) -> float:
    terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return 0.0
    haystack = " ".join([item.get("title", ""), item.get("snippet", ""), item.get("content", "")]).lower()
    return sum(haystack.count(term) for term in terms)


STOP_TERMS = {"ai", "agent", "agents", "latest", "news", "current", "today", "2026", "the", "and",
              "最新", "新闻", "今天", "近期", "关于", "什么", "怎么", "如何", "ai", "agent", "监管新闻"}


def _topic_terms(query: str) -> list[str]:
    return [t for t in re.split(r"\W+", (query or "").lower())
            if len(t) > 1 and t not in STOP_TERMS and not t.isdigit()]


def _dedupe(results: list[dict]) -> list[dict]:
    out, seen = [], set()
    for item in results:
        key = (item.get("title", "")[:120].lower(), item.get("url", "")[:200])
        if not item.get("title") or not item.get("url") or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def search(query: str, max_results: int = 5, lang: str = "en", timeout: int = 20) -> dict:
    query = (query or "").strip()
    if not query:
        raise WebSearchError("Empty query")
    pooled, providers = [], []
    # 1) news RSS for current-events questions
    if _wants_news(query):
        try:
            news = _news_rss(query, max_results, lang, timeout)
            if news:
                pooled.extend(news)
                providers.append(news[0].get("engine") or "news")
        except Exception:
            pass
    # 2) local SearXNG
    try:
        searx = _searxng(query, max_results, lang, timeout)
        if searx:
            pooled.extend(searx)
            providers.append("searxng")
    except Exception:
        pass
    # 3) Bing HTML fallback / extra coverage
    if len(pooled) < max_results:
        try:
            bing = _bing_html(query, max_results, lang, timeout)
            if bing:
                pooled.extend(bing)
                providers.append("bing")
        except Exception:
            pass
    results = _dedupe(pooled)
    if not results:
        raise WebSearchError("Web search returned no results (the provider may be rate-limiting).")
    # prefer results that hit the topic terms (e.g. "regulation") over generic AI/agent pages
    topics = _topic_terms(query)
    if topics:
        focused = []
        for item in results:
            haystack = " ".join([item.get("title", ""), item.get("snippet", ""), item.get("content", "")]).lower()
            if any(term in haystack for term in topics):
                focused.append(item)
        if len(focused) >= 2:
            results = focused
    results.sort(key=lambda item: _relevance(item, query), reverse=True)
    results = _enrich(results[:max_results], max_pages=2)
    provider = "mixed" if len(set(providers)) > 1 else (providers[0] if providers else "bing")
    return {
        "query": query,
        "provider": provider,
        "providers": sorted(set(providers)),
        "results": results,
        "note": "External web results, not part of the audited evidence base. Verify before citing.",
    }
