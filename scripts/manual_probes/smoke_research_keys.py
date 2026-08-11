#!/usr/bin/env python3
"""Smoke-test optional research provider API keys without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


QUERY = "Donald Trump Federal Reserve"


@dataclass
class Result:
    provider: str
    env_var: str
    status: str
    http_status: int | None = None
    detail: str = ""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    data = None
    merged_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
        body = response.read().decode("utf-8", errors="replace")
        try:
            parsed: dict[str, Any] | list[Any] | str = json.loads(body)
        except json.JSONDecodeError:
            parsed = body[:500]
        return int(response.status), parsed


def http_error_detail(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace")
    body = " ".join(body.split())
    if len(body) > 240:
        body = body[:237] + "..."
    return body or exc.reason


def count_path(payload: Any, *path: str) -> int | None:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, list):
        return len(current)
    return None


def env_key(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def run_provider(provider: str, env_var: str, callback: Any) -> Result:
    key = env_key(env_var)
    if not key:
        return Result(provider, env_var, "SKIP", detail=f"{env_var} is empty")
    try:
        status, payload = callback(key)
        detail = summarize_payload(provider, payload)
        if isinstance(payload, dict) and payload.get("_smoke_status") == "LIMITED":
            detail = str(payload.get("_smoke_detail") or detail)
            return Result(provider, env_var, "LIMITED", http_status=status, detail=detail)
        return Result(provider, env_var, "OK", http_status=status, detail=detail)
    except urllib.error.HTTPError as exc:
        return Result(provider, env_var, "FAIL", http_status=exc.code, detail=http_error_detail(exc))
    except Exception as exc:
        detail = " ".join(str(exc).split())
        if len(detail) > 240:
            detail = detail[:237] + "..."
        return Result(provider, env_var, "FAIL", detail=detail)


def summarize_payload(provider: str, payload: Any) -> str:
    if provider == "Tavily":
        return f"results={count_path(payload, 'results')}"
    if provider == "Exa":
        return f"results={count_path(payload, 'results')}"
    if provider == "Firecrawl":
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return f"data={len(data)}"
            if isinstance(data, dict):
                web = data.get("web")
                if isinstance(web, list):
                    return f"web={len(web)}"
            if payload.get("success") is True:
                return "success=true"
        return f"data={count_path(payload, 'data')}"
    if provider == "Brave Search":
        count = count_path(payload, "web", "results")
        return f"web_results={count}"
    if provider == "NewsAPI":
        count = count_path(payload, "articles")
        total = payload.get("totalResults") if isinstance(payload, dict) else None
        return f"articles={count} totalResults={total}"
    if provider == "Guardian":
        count = count_path(payload, "response", "results")
        return f"results={count}"
    if provider == "NYTimes":
        count = count_path(payload, "response", "docs")
        return f"docs={count}"
    if provider == "Event Registry":
        count = count_path(payload, "articles", "results")
        return f"articles={count}"
    if provider == "Perigon":
        count = count_path(payload, "articles")
        return f"articles={count}"
    if provider == "Diffbot":
        count = count_path(payload, "objects")
        return f"objects={count}"
    if provider == "SerpAPI":
        organic = count_path(payload, "organic_results")
        news = count_path(payload, "news_results")
        return f"organic_results={organic} news_results={news}"
    if provider == "GDELT":
        count = count_path(payload, "articles")
        return f"articles={count}"
    if provider == "Wayback CDX":
        return "reachable"
    return "request succeeded"


def tavily(key: str) -> tuple[int, Any]:
    return request_json(
        "https://api.tavily.com/search",
        method="POST",
        payload={
            "api_key": key,
            "query": QUERY,
            "topic": "news",
            "search_depth": "basic",
            "max_results": 1,
            "include_answer": False,
            "include_raw_content": False,
        },
    )


def exa(key: str) -> tuple[int, Any]:
    return request_json(
        "https://api.exa.ai/search",
        method="POST",
        headers={"x-api-key": key},
        payload={
            "query": QUERY,
            "numResults": 1,
            "startPublishedDate": "2024-12-01T00:00:00.000Z",
            "endPublishedDate": "2024-12-18T23:59:59.999Z",
        },
    )


def firecrawl(key: str) -> tuple[int, Any]:
    return request_json(
        "https://api.firecrawl.dev/v2/search",
        method="POST",
        headers={"Authorization": f"Bearer {key}"},
        payload={"query": QUERY, "limit": 1, "sources": [{"type": "web"}]},
    )


def brave(key: str) -> tuple[int, Any]:
    params = urllib.parse.urlencode({"q": QUERY, "count": 1, "freshness": "py"})
    return request_json(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )


def newsapi(key: str) -> tuple[int, Any]:
    def run(start: str, end: str) -> tuple[int, Any]:
        params = urllib.parse.urlencode(
            {
                "q": '"Donald Trump"',
                "from": start,
                "to": end,
                "language": "en",
                "pageSize": 1,
                "sortBy": "publishedAt",
            }
        )
        return request_json(f"https://newsapi.org/v2/everything?{params}", headers={"X-Api-Key": key})

    try:
        return run("2024-12-01", "2024-12-18")
    except urllib.error.HTTPError as exc:
        if exc.code != 426:
            raise
        historical_detail = http_error_detail(exc)
        today = date.today()
        recent_start = (today - timedelta(days=7)).isoformat()
        status, payload = run(recent_start, today.isoformat())
        if isinstance(payload, dict):
            payload["_smoke_status"] = "LIMITED"
            payload["_smoke_detail"] = (
                f"key works on recent search; historical Dec 2024 request rejected by plan: {historical_detail}"
            )
        return status, payload


def guardian(key: str) -> tuple[int, Any]:
    params = urllib.parse.urlencode(
        {
            "q": '"Donald Trump"',
            "from-date": "2024-12-01",
            "to-date": "2024-12-18",
            "page-size": 1,
            "api-key": key,
        }
    )
    return request_json(f"https://content.guardianapis.com/search?{params}")


def nytimes(key: str) -> tuple[int, Any]:
    params = urllib.parse.urlencode(
        {
            "q": '"Donald Trump"',
            "begin_date": "20241201",
            "end_date": "20241218",
            "page": 0,
            "api-key": key,
        }
    )
    return request_json(f"https://api.nytimes.com/svc/search/v2/articlesearch.json?{params}")


def event_registry(key: str) -> tuple[int, Any]:
    return request_json(
        "https://eventregistry.org/api/v1/article/getArticles",
        method="POST",
        payload={
            "apiKey": key,
            "action": "getArticles",
            "keyword": "Donald Trump",
            "dateStart": "2024-12-01",
            "dateEnd": "2024-12-18",
            "lang": "eng",
            "articlesPage": 1,
            "articlesCount": 1,
            "articlesSortBy": "date",
            "articlesSortByAsc": False,
        },
    )


def perigon(key: str) -> tuple[int, Any]:
    params = urllib.parse.urlencode(
        {
            "q": '"Donald Trump"',
            "from": "2024-12-01",
            "to": "2024-12-18",
            "language": "en",
            "size": 1,
            "apiKey": key,
        }
    )
    return request_json(f"https://api.goperigon.com/v1/all?{params}")


def diffbot(key: str) -> tuple[int, Any]:
    params = urllib.parse.urlencode(
        {
            "token": key,
            "url": "https://en.wikipedia.org/wiki/Kevin_Warsh",
            "discussion": "false",
        }
    )
    return request_json(f"https://api.diffbot.com/v3/article?{params}", timeout=60)


def serpapi(key: str) -> tuple[int, Any]:
    params = urllib.parse.urlencode(
        {
            "engine": "google",
            "q": QUERY,
            "num": 1,
            "api_key": key,
        }
    )
    return request_json(f"https://serpapi.com/search.json?{params}")


def gdelt(_: str = "") -> tuple[int, Any]:
    params = urllib.parse.urlencode(
        {
            "query": '"Donald Trump"',
            "mode": "artlist",
            "format": "json",
            "maxrecords": 1,
            "startdatetime": "20241201000000",
            "enddatetime": "20241218235959",
        }
    )
    return request_json(f"https://api.gdeltproject.org/api/v2/doc/doc?{params}")


def wayback(_: str = "") -> tuple[int, Any]:
    params = urllib.parse.urlencode(
        {
            "url": "nytimes.com",
            "from": "20241201",
            "to": "20241218",
            "limit": 1,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype",
        }
    )
    return request_json(f"https://web.archive.org/cdx?{params}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--include-free", action="store_true", help="also test no-key sources")
    args = parser.parse_args()

    load_dotenv(Path(args.env_file))

    checks: list[tuple[str, str, Any]] = [
        ("Tavily", "TAVILY_API_KEY", tavily),
        ("Exa", "EXA_API_KEY", exa),
        ("Firecrawl", "FIRECRAWL_API_KEY", firecrawl),
        ("Brave Search", "BRAVE_SEARCH_API_KEY", brave),
        ("NewsAPI", "NEWSAPI_KEY", newsapi),
        ("Guardian", "GUARDIAN_API_KEY", guardian),
        ("NYTimes", "NYTIMES_API_KEY", nytimes),
        ("Event Registry", "EVENT_REGISTRY_API_KEY", event_registry),
        ("Perigon", "PERIGON_API_KEY", perigon),
        ("Diffbot", "DIFFBOT_TOKEN", diffbot),
        ("SerpAPI", "SERPAPI_API_KEY", serpapi),
    ]
    results = [run_provider(provider, env_var, callback) for provider, env_var, callback in checks]

    if args.include_free:
        for provider, callback in [("GDELT", gdelt), ("Wayback CDX", wayback)]:
            try:
                status, payload = callback()
                results.append(Result(provider, "(no key)", "OK", status, summarize_payload(provider, payload)))
            except urllib.error.HTTPError as exc:
                results.append(Result(provider, "(no key)", "FAIL", exc.code, http_error_detail(exc)))
            except Exception as exc:
                results.append(Result(provider, "(no key)", "FAIL", detail=" ".join(str(exc).split())[:240]))

    print("provider\tkey\tstatus\thttp\tdetail")
    for result in results:
        http_status = "" if result.http_status is None else str(result.http_status)
        print(f"{result.provider}\t{result.env_var}\t{result.status}\t{http_status}\t{result.detail}")

    failed = [result for result in results if result.status == "FAIL"]
    skipped = [result for result in results if result.status == "SKIP"]
    limited = [result for result in results if result.status == "LIMITED"]
    ok = len(results) - len(failed) - len(skipped) - len(limited)
    print(f"\nsummary: ok={ok} limited={len(limited)} fail={len(failed)} skip={len(skipped)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
