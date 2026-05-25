#!/usr/bin/env python3
"""
Daily Messari crypto briefing agent.

Reads MESSARI_API_KEY from the environment, fetches the Messari endpoints that are
available to the key, and writes a Markdown briefing under ./reports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html import unescape
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


API_BASE = "https://api.messari.io"
DEFAULT_ASSETS = ["bitcoin", "ethereum", "solana", "hyperliquid"]
TELEGRAM_BASE = "https://api.telegram.org"
TELEGRAM_LIMIT = 3900
PLACEHOLDER_MARKERS = ("sua-chave", "token-do", "seu-chat", "aqui")
PUBLIC_RESEARCH_URLS = [
    "https://messari.io/research/research-reports?page=1",
    "https://messari.io/research/valuations",
]
PUBLIC_NEWS_URL = "https://messari.io/news"
PUBLIC_NEWSLETTER_PODCAST_URL = "https://messari.io/research/newsletter-and-podcast"
MESSARI_PODCAST_RSS_URL = "https://anchor.fm/s/fb66e238/podcast/rss"


@dataclass
class ApiResult:
    ok: bool
    status: int | None
    data: Any = None
    error: str | None = None


class MessariClient:
    def __init__(self, api_key: str, timeout: int = 45) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> ApiResult:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        return self._request("GET", f"{API_BASE}{path}{query}")

    def post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        return self._request("POST", f"{API_BASE}{path}", payload)

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> ApiResult:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "X-Messari-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "BotMessariDailyAgent/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return ApiResult(True, response.status, _json_or_text(raw))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = _json_or_text(raw)
            message = parsed.get("error") if isinstance(parsed, dict) else raw
            return ApiResult(False, exc.code, parsed, str(message or exc.reason))
        except URLError as exc:
            return ApiResult(False, None, None, str(exc.reason))
        except TimeoutError:
            return ApiResult(False, None, None, "Request timed out")


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 45) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def send_text(self, text: str) -> ApiResult:
        chunks = split_for_telegram(text)
        last_result = ApiResult(True, 200, {})
        for chunk in chunks:
            last_result = self._post("sendMessage", {"chat_id": self.chat_id, "text": chunk})
            if not last_result.ok:
                return last_result
        return last_result

    def _post(self, method: str, payload: dict[str, Any]) -> ApiResult:
        url = f"{TELEGRAM_BASE}/bot{self.bot_token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return ApiResult(True, response.status, _json_or_text(raw))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = _json_or_text(raw)
            message = parsed.get("description") if isinstance(parsed, dict) else raw
            return ApiResult(False, exc.code, parsed, str(message or exc.reason))
        except URLError as exc:
            return ApiResult(False, None, None, str(exc.reason))
        except TimeoutError:
            return ApiResult(False, None, None, "Request timed out")


def _json_or_text(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def unique_keep_order(items: list[str]) -> list[str]:
    seen = set()
    unique = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item.strip())
    return unique


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sent_research_ids": [], "sent_research_fingerprints": [], "sent_public_item_ids": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sent_research_ids": [], "sent_research_fingerprints": [], "sent_public_item_ids": []}
    if not isinstance(state, dict):
        return {"sent_research_ids": [], "sent_research_fingerprints": [], "sent_public_item_ids": []}
    state.setdefault("sent_research_ids", [])
    state.setdefault("sent_research_fingerprints", [])
    state.setdefault("sent_public_item_ids", [])
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def report_fingerprint(report: dict[str, Any]) -> str:
    title = report.get("title") or report.get("name") or ""
    created = report.get("createdAt") or report.get("publishedAt") or ""
    return f"{title}|{created}".strip().lower()


def filter_new_research(reports: list[dict[str, Any]], state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    sent_ids = set(state.get("sent_research_ids", []))
    sent_fingerprints = set(state.get("sent_research_fingerprints", []))
    new_reports = []
    new_ids = []
    new_fingerprints = []

    for report in reports:
        report_id = report.get("id")
        fingerprint = report_fingerprint(report)
        if report_id and report_id in sent_ids:
            continue
        if fingerprint and fingerprint in sent_fingerprints:
            continue
        new_reports.append(report)
        if report_id:
            new_ids.append(report_id)
        if fingerprint:
            new_fingerprints.append(fingerprint)

    return new_reports, new_ids, new_fingerprints


def remember_research(state: dict[str, Any], report_ids: list[str], fingerprints: list[str]) -> None:
    ids = list(dict.fromkeys([*state.get("sent_research_ids", []), *report_ids]))
    fps = list(dict.fromkeys([*state.get("sent_research_fingerprints", []), *fingerprints]))
    state["sent_research_ids"] = ids[-1000:]
    state["sent_research_fingerprints"] = fps[-1000:]
    state["last_sent_at"] = datetime.now(timezone.utc).isoformat()


def filter_new_public_items(items: list[dict[str, Any]], state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    sent = set(state.get("sent_public_item_ids", []))
    new_items = []
    new_ids = []
    for item in items:
        item_id = str(item.get("id") or report_fingerprint(item))
        if not item_id or item_id in sent:
            continue
        new_items.append(item)
        new_ids.append(item_id)
    return new_items, new_ids


def remember_public_items(state: dict[str, Any], item_ids: list[str]) -> None:
    ids = list(dict.fromkeys([*state.get("sent_public_item_ids", []), *item_ids]))
    state["sent_public_item_ids"] = ids[-2000:]
    state["last_sent_at"] = datetime.now(timezone.utc).isoformat()


def split_for_telegram(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n")
    if len(normalized) <= TELEGRAM_LIMIT:
        return [normalized]

    chunks = []
    current = ""
    for block in normalized.split("\n\n"):
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) <= TELEGRAM_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > TELEGRAM_LIMIT:
            chunks.append(block[:TELEGRAM_LIMIT])
            block = block[TELEGRAM_LIMIT:]
        current = block
    if current:
        chunks.append(current)
    return chunks


def money(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/d"
    if abs(value) >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if abs(value) >= 1:
        return f"${value:,.2f}"
    return f"${value:,.6f}"


def pct(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/d"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def clean_markdown(text: str, max_chars: int = 900) -> str:
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text or "")
    text = re.sub(r"[*_`>#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rsplit(" ", 1)[0] + "..."


def clean_html_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def unwrap_data(result: ApiResult) -> Any:
    if not result.ok:
        return None
    if isinstance(result.data, dict) and "data" in result.data:
        return result.data["data"]
    return result.data


def unavailable_line(name: str, result: ApiResult) -> str:
    status = f"HTTP {result.status}" if result.status else "erro de conexao"
    return f"- {name}: indisponivel para esta chave agora ({status}: {result.error})."


def fetch_asset_details(client: MessariClient, assets: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    result = client.get(
        "/metrics/v2/assets/details",
        {"assetIDs": ",".join(assets), "limit": min(len(assets), 20), "sort": "rank", "order": "asc"},
    )
    if result.ok:
        return list(unwrap_data(result) or []), []

    legacy_assets: list[dict[str, Any]] = []
    errors = [unavailable_line("Market Data v2", result)]
    for asset in assets:
        item = client.get(f"/metrics/v1/assets/{asset}")
        if item.ok:
            data = unwrap_data(item)
            if isinstance(data, dict):
                legacy_assets.append(data)
        else:
            errors.append(unavailable_line(f"Market Data v1/{asset}", item))
    return legacy_assets, errors


def fetch_research(client: MessariClient, limit: int, tags: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    params: dict[str, Any] = {"limit": limit, "page": 0}
    if tags:
        params["tags"] = tags

    result = client.get("/research/v1/reports", params)
    if not result.ok:
        return [], [unavailable_line("Research Reports", result)]

    reports = list(unwrap_data(result) or [])
    detailed: list[dict[str, Any]] = []
    errors: list[str] = []
    for report in reports[:limit]:
        report_id = report.get("id")
        if not report_id:
            continue
        detail = client.get(f"/research/v1/reports/{report_id}")
        if detail.ok and isinstance(unwrap_data(detail), dict):
            detailed.append(unwrap_data(detail))
        else:
            detailed.append(report)
            errors.append(unavailable_line(f"Research detail {report_id}", detail))
    return detailed, errors


def fetch_public_url(url: str, timeout: int = 45) -> ApiResult:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "BotMessariDailyAgent/1.0 public-research-check",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return ApiResult(True, response.status, raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return ApiResult(False, exc.code, raw, str(exc.reason))
    except URLError as exc:
        return ApiResult(False, None, None, str(exc.reason))
    except TimeoutError:
        return ApiResult(False, None, None, "Request timed out")


def child_text(element: ElementTree.Element, name: str) -> str:
    child = element.find(name)
    if child is not None and child.text:
        return clean_html_text(child.text)
    return ""


def fetch_podcast_rss(limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    result = fetch_public_url(MESSARI_PODCAST_RSS_URL)
    if not result.ok:
        return [], [unavailable_line("Messari Podcast RSS", result)]
    try:
        root = ElementTree.fromstring(str(result.data))
    except ElementTree.ParseError as exc:
        return [], [f"- Messari Podcast RSS: XML invalido ({exc})."]

    items = []
    for item in root.findall("./channel/item"):
        title = child_text(item, "title") or "Sem titulo"
        published = child_text(item, "pubDate") or "n/d"
        link = child_text(item, "link") or MESSARI_PODCAST_RSS_URL
        description = child_text(item, "description")
        items.append(
            {
                "id": f"podcast-rss|{title}|{published}",
                "title": title,
                "publishedAt": published,
                "source": "Messari Podcast RSS",
                "summary": clean_markdown(description, max_chars=700),
                "url": link,
            }
        )
        if len(items) >= limit:
            break
    return items, []


def parse_public_research_index(html: str, source_url: str, limit: int) -> list[dict[str, Any]]:
    reports = []
    seen = set()
    pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(html):
        href = unescape(match.group(1))
        title = clean_html_text(match.group(2))
        if not title or title.startswith("Image:"):
            continue
        if href.startswith("/"):
            href = urljoin("https://messari.io", href)
        if "messari.io" not in href:
            continue
        if not any(part in href for part in ("/research/", "/report/", "/project/")):
            continue
        if title.lower() in {"research", "research reports", "all research", "api", "explore", "protocol reporting", "diligence"}:
            continue
        key = f"{title}|{href}"
        if key in seen:
            continue
        seen.add(key)
        window = clean_html_text(html[match.end() : match.end() + 650])
        date_match = re.search(r"(\d+\s+(?:minutes?|hours?|days?)\s+ago|[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})", window)
        tier = "Enterprise" if re.search(r"\bEnterprise\b", window[:250]) else "Public/unknown"
        reports.append(
            {
                "id": key,
                "title": title,
                "publishedAt": date_match.group(1) if date_match else "n/d",
                "summary": f"Fonte publica Messari. Tier visivel: {tier}.",
                "url": href,
                "authors": [],
                "assets": [],
            }
        )
        if len(reports) >= limit:
            break
    return reports


def fetch_public_research(limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    reports = []
    errors = []
    for url in PUBLIC_RESEARCH_URLS:
        result = fetch_public_url(url)
        if not result.ok:
            errors.append(unavailable_line(f"Public Research page {url}", result))
            continue
        parsed = parse_public_research_index(str(result.data), url, limit)
        reports.extend(parsed)
        if len(reports) >= limit:
            break

    unique = []
    seen = set()
    for report in reports:
        key = report_fingerprint(report)
        if key in seen:
            continue
        seen.add(key)
        unique.append(report)
    return unique[:limit], errors


def parse_anchor_items(html: str, source_url: str, limit: int, allow_external: bool) -> list[dict[str, Any]]:
    items = []
    seen = set()
    ignored = {
        "api",
        "home",
        "research",
        "news",
        "intel",
        "fundraising",
        "watchlists",
        "screener",
        "subscribe",
        "pricing",
        "plans",
        "documentation",
        "privacy policy",
        "terms of service",
        "ask messariai",
    }
    pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(html):
        href = unescape(match.group(1))
        title = clean_html_text(match.group(2))
        if not title or title.startswith("Image:"):
            continue
        normalized_title = title.lower().strip()
        if normalized_title in ignored or len(title) < 12:
            continue
        if href.startswith("/"):
            href = urljoin("https://messari.io", href)
        if not allow_external and "messari.io" not in href:
            continue
        if href.startswith("#") or href.startswith("mailto:"):
            continue
        key = f"{title}|{href}"
        if key in seen:
            continue
        seen.add(key)
        window = clean_html_text(html[match.end() : match.end() + 500])
        date_match = re.search(r"(\d+\s+(?:minutes?|hours?|days?)\s+ago|Today,\s+[A-Z][a-z]{2}\s+\d{1,2}|[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}|[A-Z][a-z]{2}\s+\d{1,2})", window)
        source_match = re.search(r"\b(The Block|CoinDesk|CoinTelegraph|Decrypt|The Defiant|Bankless|Messari)\b", window)
        items.append(
            {
                "id": key,
                "title": title,
                "publishedAt": date_match.group(1) if date_match else "n/d",
                "source": source_match.group(1) if source_match else ("Messari" if "messari.io" in href else "External"),
                "summary": "Item publico listado pela Messari.",
                "url": href,
            }
        )
        if len(items) >= limit:
            break
    return items


def parse_daily_recaps(html: str, limit: int) -> list[dict[str, Any]]:
    text = clean_html_text(html)
    section_match = re.search(r"Daily Recaps(.*?)(?:Asset All|News Date|Upgrade to Enterprise)", text)
    if not section_match:
        return []
    section = section_match.group(1)
    date_pattern = r"(Today,\s+[A-Z][a-z]{2}\s+\d{1,2}|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Z][a-z]{2}\s+\d{1,2})"
    matches = list(re.finditer(date_pattern, section))
    recaps = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        date = match.group(1)
        body = section[start:end].strip()
        body = re.sub(r"Upgrade to Enterprise.*", "", body).strip()
        if not body:
            continue
        recaps.append(
            {
                "id": f"daily-recap|{date}|{body[:80]}",
                "title": f"Daily Recap - {date}",
                "publishedAt": date,
                "source": "Messari News",
                "summary": clean_markdown(body, max_chars=650),
                "url": PUBLIC_NEWS_URL,
            }
        )
        if len(recaps) >= limit:
            break
    return recaps


def fetch_public_news(limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    result = fetch_public_url(PUBLIC_NEWS_URL)
    if not result.ok:
        return [], [unavailable_line("Public News page", result)]
    html = str(result.data)
    recaps = parse_daily_recaps(html, max(1, min(limit, 4)))
    headlines = parse_anchor_items(html, PUBLIC_NEWS_URL, limit, allow_external=True)
    combined = [*recaps, *headlines]
    unique = []
    seen = set()
    for item in combined:
        key = item.get("id") or report_fingerprint(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:limit], []


def fetch_public_newsletter_podcast(limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    result = fetch_public_url(PUBLIC_NEWSLETTER_PODCAST_URL)
    if not result.ok:
        rss_items, rss_errors = fetch_podcast_rss(limit)
        return rss_items, [unavailable_line("Public Newsletter/Podcast page", result), *rss_errors]
    html = str(result.data)
    items = parse_anchor_items(html, PUBLIC_NEWSLETTER_PODCAST_URL, limit, allow_external=True)
    if len(items) < limit:
        rss_items, rss_errors = fetch_podcast_rss(limit - len(items))
        items.extend(rss_items)
        return items[:limit], rss_errors
    return items[:limit], []


def extract_ai_content(result: ApiResult) -> str | None:
    if not result.ok:
        return None
    data = unwrap_data(result)
    messages = data.get("messages") if isinstance(data, dict) else None
    if isinstance(messages, list) and messages:
        content = messages[-1].get("content")
        return content if isinstance(content, str) and content.strip() else None
    return None


def fetch_ai_research_fallback(client: MessariClient, assets: list[str]) -> tuple[str | None, list[str]]:
    prompt = (
        "Voce e um analista cripto. Gere uma secao chamada Research Messari usando apenas "
        "informacoes publicas/free que estejam disponiveis via Messari ou sejam citadas pela Messari AI. "
        "Foque nos ativos: " + ", ".join(assets) + ". "
        "Traga de 3 a 6 pontos com: tema, ativo relacionado, tese/insight, risco e o que monitorar. "
        "Se nao houver research publico/free acessivel, diga isso claramente. "
        "Nao invente links, nao repita o mesmo ponto e nao de recomendacao financeira. "
        "Responda em portugues do Brasil em Markdown curto."
    )
    result = client.post(
        "/ai/v1/chat/completions",
        {
            "messages": [{"role": "user", "content": prompt}],
            "verbosity": "balanced",
            "response_format": "markdown",
            "inline_citations": True,
            "stream": False,
            "generate_related_questions": 0,
        },
    )
    if not result.ok:
        return None, [unavailable_line("Messari AI Research fallback", result)]
    content = extract_ai_content(result)
    if content:
        return content, []
    return None, ["- Messari AI Research fallback: resposta recebida, mas sem conteudo de mensagem."]


def fetch_ai_summary(client: MessariClient, market_lines: list[str], research_lines: list[str], no_new_research: bool) -> tuple[str | None, list[str]]:
    research_context = "\n".join(research_lines[:10]) if research_lines else "Nenhum report novo de Research foi encontrado sem repetir envios anteriores."
    no_new_instruction = "Se nao houver Research novo, diga apenas que nao houve Research novo sem repetir reports antigos." if no_new_research else ""
    prompt = (
        "Gere um resumo cripto diario em portugues do Brasil, objetivo e acionavel, "
        "usando apenas os dados abaixo. Separe em: Leitura de mercado, Temas de pesquisa, "
        "Riscos e O que monitorar. Nao de recomendacao financeira. "
        "Nao repita nenhum item de research que ja tenha sido enviado antes. "
        f"{no_new_instruction}\n\n"
        "Mercado:\n" + "\n".join(market_lines[:12]) + "\n\n"
        "Research:\n" + research_context
    )
    result = client.post(
        "/ai/v1/chat/completions",
        {
            "messages": [{"role": "user", "content": prompt}],
            "verbosity": "balanced",
            "response_format": "markdown",
            "inline_citations": True,
            "stream": False,
            "generate_related_questions": 0,
        },
    )
    if not result.ok:
        return None, [unavailable_line("Messari AI", result)]

    content = extract_ai_content(result)
    if content:
        return content, []
    return None, ["- Messari AI: resposta recebida, mas sem conteudo de mensagem."]


def build_market_section(assets: list[dict[str, Any]]) -> tuple[str, list[str]]:
    lines = []
    ai_lines = []
    for asset in assets:
        market = asset.get("marketData") or {}
        roi = asset.get("returnOnInvestment") or {}
        cap = market.get("marketcap") or {}
        row = (
            f"| {asset.get('symbol', 'n/d')} | {asset.get('name', 'n/d')} | "
            f"{money(market.get('priceUsd'))} | {pct(roi.get('priceChange24h'))} | "
            f"{pct(roi.get('priceChange7d'))} | {pct(roi.get('priceChange30d'))} | "
            f"{money(market.get('volume24Hour'))} | {money(cap.get('circulatingUsd'))} |"
        )
        lines.append(row)
        ai_lines.append(
            f"{asset.get('symbol')}: preco {money(market.get('priceUsd'))}, "
            f"24h {pct(roi.get('priceChange24h'))}, 7d {pct(roi.get('priceChange7d'))}, "
            f"30d {pct(roi.get('priceChange30d'))}, volume 24h {money(market.get('volume24Hour'))}."
        )

    section = [
        "## Mercado",
        "",
        "| Ativo | Nome | Preco | 24h | 7d | 30d | Volume 24h | Market cap |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
        *lines,
    ]
    return "\n".join(section), ai_lines


def build_basic_summary_section(assets: list[dict[str, Any]], has_research: bool) -> str:
    ranked = []
    for asset in assets:
        market = asset.get("marketData") or {}
        roi = asset.get("returnOnInvestment") or {}
        symbol = asset.get("symbol") or "n/d"
        ranked.append(
            {
                "symbol": symbol,
                "price": market.get("priceUsd"),
                "day": roi.get("priceChange24h"),
                "week": roi.get("priceChange7d"),
                "month": roi.get("priceChange30d"),
                "volume": market.get("volume24Hour"),
            }
        )

    lines = ["## Sintese do agente", ""]
    if not ranked:
        lines.append("Sem dados de mercado acessiveis para gerar leitura.")
    else:
        leaders = sorted(ranked, key=lambda item: item["day"] if isinstance(item["day"], (int, float)) else -999, reverse=True)
        top = leaders[0]
        lines.append(
            f"Leitura de mercado: {top['symbol']} lidera no recorte de 24h com {pct(top['day'])}, "
            f"preco em {money(top['price'])} e volume 24h de {money(top['volume'])}."
        )
        lines.append("")
        lines.append("O que monitorar:")
        for item in ranked[:4]:
            lines.append(
                f"- {item['symbol']}: preco {money(item['price'])}, 24h {pct(item['day'])}, "
                f"7d {pct(item['week'])}, 30d {pct(item['month'])}."
            )
    lines.append("")
    if has_research:
        lines.append("Research: bloco preenchido abaixo com os dados disponiveis sem repetir reports ja enviados.")
    else:
        lines.append("Research: sem report novo acessivel nesta execucao.")
    lines.append("")
    lines.append("Aviso: informativo, nao e recomendacao financeira.")
    return "\n".join(lines)


def build_research_section(reports: list[dict[str, Any]]) -> tuple[str, list[str]]:
    lines = ["## Research Messari", ""]
    ai_lines: list[str] = []

    if not reports:
        return "## Research Messari\n\nNenhum report acessivel foi retornado.", []

    for report in reports:
        title = report.get("title") or report.get("name") or "Sem titulo"
        created = report.get("createdAt") or report.get("publishedAt") or "n/d"
        authors = ", ".join(a.get("name", "") for a in report.get("authors", []) if isinstance(a, dict))
        assets = ", ".join(a.get("symbol", "") for a in report.get("assets", []) if isinstance(a, dict))
        content = clean_markdown(report.get("content") or report.get("description") or report.get("summary") or "")
        fallback_url = f"https://messari.io/report/{report.get('id')}" if report.get("id") else ""
        url = report.get("url") or fallback_url

        lines.append(f"### {title}")
        lines.append(f"- Data: {created}")
        if authors:
            lines.append(f"- Autoria: {authors}")
        if assets:
            lines.append(f"- Ativos citados: {assets}")
        if content:
            lines.append(f"- Trecho/insight: {content}")
        if url:
            lines.append(f"- Link: {url}")
        lines.append("")

        ai_lines.append(f"{title} ({created}, ativos: {assets or 'n/d'}): {content}")

    return "\n".join(lines), ai_lines


def build_ai_research_section(ai_research: str) -> tuple[str, list[str]]:
    content = ai_research.strip()
    section = "## Research Messari\n\n" + content
    ai_lines = [clean_markdown(content, max_chars=1800)]
    return section, ai_lines


def build_public_items_section(title: str, items: list[dict[str, Any]], empty_message: str) -> tuple[str, list[str]]:
    if not items:
        return f"## {title}\n\n{empty_message}", []
    lines = [f"## {title}", ""]
    ai_lines = []
    for item in items:
        item_title = item.get("title") or "Sem titulo"
        date = item.get("publishedAt") or "n/d"
        source = item.get("source") or "Messari"
        summary = clean_markdown(item.get("summary") or "", max_chars=700)
        url = item.get("url") or ""
        lines.append(f"### {item_title}")
        lines.append(f"- Data: {date}")
        lines.append(f"- Fonte: {source}")
        if summary:
            lines.append(f"- Resumo: {summary}")
        if url:
            lines.append(f"- Link: {url}")
        lines.append("")
        ai_lines.append(f"{item_title} ({date}, {source}): {summary}")
    return "\n".join(lines), ai_lines


def write_report(content: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"messari-daily-{datetime.now(timezone.utc).date().isoformat()}.md"
    path = output_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily Messari crypto briefing.")
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS), help="Comma-separated asset slugs or IDs.")
    parser.add_argument("--research-limit", type=int, default=5, help="Number of research reports to include.")
    parser.add_argument("--news-limit", type=int, default=8, help="Number of public Messari news items to include.")
    parser.add_argument("--podcast-limit", type=int, default=6, help="Number of public Messari newsletter/podcast items to include.")
    parser.add_argument("--tags", default="", help="Comma-separated Messari research tags, e.g. defi,stablecoins.")
    parser.add_argument("--no-ai", action="store_true", help="Skip Messari AI synthesis.")
    parser.add_argument("--no-ai-research-fallback", action="store_true", help="Do not use Messari AI when direct Research API is unavailable.")
    parser.add_argument("--no-public-research-fallback", action="store_true", help="Do not read public Messari research pages when the API is unavailable.")
    parser.add_argument("--no-public-news", action="store_true", help="Do not read public Messari news pages.")
    parser.add_argument("--no-public-podcast", action="store_true", help="Do not read public Messari newsletter/podcast pages.")
    parser.add_argument("--output-dir", default="reports", help="Directory where the Markdown report is saved.")
    parser.add_argument("--state-file", default="state/messari_agent_state.json", help="File used to avoid repeated research reports.")
    parser.add_argument("--send-telegram", action="store_true", help="Send the report to Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write state or send Telegram.")
    return parser.parse_args()


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def require_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"Missing {name}. Add it to .env.", file=sys.stderr)
        return None
    if looks_like_placeholder(value):
        print(f"{name} still looks like a placeholder in .env.", file=sys.stderr)
        return None
    if name == "TELEGRAM_BOT_TOKEN":
        if any(char.isspace() for char in value):
            print("TELEGRAM_BOT_TOKEN contains spaces or line breaks. Remove all spaces from the token in .env.", file=sys.stderr)
            return None
        if ":" not in value:
            print("TELEGRAM_BOT_TOKEN must look like 123456789:ABCDEF... and contain a colon.", file=sys.stderr)
            return None
        if value.lower().startswith("bot"):
            print("TELEGRAM_BOT_TOKEN must not start with 'bot'. Paste only the token from BotFather.", file=sys.stderr)
            return None
    if name == "TELEGRAM_CHAT_ID":
        if any(char.isspace() for char in value):
            print("TELEGRAM_CHAT_ID contains spaces or line breaks. Remove all spaces from the chat id in .env.", file=sys.stderr)
            return None
    return value


def main() -> int:
    load_env_file(Path(".env"))
    args = parse_args()

    api_key = require_env("MESSARI_API_KEY")
    if not api_key:
        return 2

    client = MessariClient(api_key)
    assets = unique_keep_order([item.strip() for item in args.assets.split(",") if item.strip()])
    tags = unique_keep_order([item.strip() for item in args.tags.split(",") if item.strip()])
    state_path = Path(args.state_file)
    state = load_state(state_path)

    asset_details, market_errors = fetch_asset_details(client, assets)
    research_reports, research_errors = fetch_research(client, args.research_limit, tags)
    public_research_errors: list[str] = []
    if not research_reports and not args.no_public_research_fallback:
        public_research, public_research_errors = fetch_public_research(args.research_limit)
        if public_research:
            research_reports = public_research
            research_errors.append("- Research API bloqueada; usando indice publico da Messari sem acessar conteudo fechado.")
    new_research_reports, new_research_ids, new_research_fingerprints = filter_new_research(research_reports, state)
    public_news_errors: list[str] = []
    public_podcast_errors: list[str] = []
    news_items: list[dict[str, Any]] = []
    podcast_items: list[dict[str, Any]] = []
    if not args.no_public_news:
        news_items, public_news_errors = fetch_public_news(args.news_limit)
    if not args.no_public_podcast:
        podcast_items, public_podcast_errors = fetch_public_newsletter_podcast(args.podcast_limit)
    new_news_items, new_news_ids = filter_new_public_items(news_items, state)
    new_podcast_items, new_podcast_ids = filter_new_public_items(podcast_items, state)

    market_section, market_ai_lines = build_market_section(asset_details)
    research_section, research_ai_lines = build_research_section(new_research_reports)
    news_section, news_ai_lines = build_public_items_section("News Messari", new_news_items, "Nenhuma noticia publica nova foi retornada.")
    podcast_section, podcast_ai_lines = build_public_items_section("Newsletter e Podcasts Messari", new_podcast_items, "Nenhum item publico novo de newsletter/podcast foi retornado.")
    ai_research_errors: list[str] = []
    if not new_research_reports and not args.no_ai and not args.no_ai_research_fallback:
        ai_research, ai_research_errors = fetch_ai_research_fallback(client, assets)
        if ai_research:
            research_section, research_ai_lines = build_ai_research_section(ai_research)

    ai_section = "## Sintese do agente\n\nMessari AI foi pulado por configuracao."
    ai_errors: list[str] = []
    ai_used_for_research = bool(ai_research_errors) or (not new_research_reports and research_ai_lines and not args.no_ai_research_fallback)
    if ai_used_for_research:
        ai_section = build_basic_summary_section(asset_details, bool(research_ai_lines))
    elif not args.no_ai:
        ai_text, ai_errors = fetch_ai_summary(client, market_ai_lines, research_ai_lines, not bool(new_research_reports))
        if ai_text:
            ai_section = f"## Sintese do agente\n\n{ai_text.strip()}"
        else:
            ai_section = "## Sintese do agente\n\nNao foi possivel gerar sintese via Messari AI."

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    errors = market_errors + research_errors + public_research_errors + public_news_errors + public_podcast_errors + ai_research_errors + ai_errors
    dedupe_line = f"- Research novo incluido: {len(new_research_reports)} de {len(research_reports)} reports retornados. Reports ja enviados foram pulados."
    public_dedupe_line = f"- News/podcast novos incluidos: {len(new_news_items) + len(new_podcast_items)} de {len(news_items) + len(podcast_items)} itens publicos retornados."
    availability = "\n".join([dedupe_line, *errors]) if errors else "\n".join([dedupe_line, "- Todos os blocos solicitados responderam com sucesso."])
    availability = "\n".join([public_dedupe_line, availability])

    report = "\n\n".join(
        [
            f"# Resumo cripto diario - Messari\n\nGerado em: {generated_at}\n\nAviso: informativo, nao e recomendacao financeira.",
            ai_section,
            market_section,
            research_section,
            news_section,
            podcast_section,
            f"## Disponibilidade dos endpoints\n\n{availability}",
        ]
    )

    path = write_report(report, Path(args.output_dir))
    print(f"Report written to {path}")

    delivered = False
    if args.send_telegram:
        bot_token = require_env("TELEGRAM_BOT_TOKEN")
        chat_id = require_env("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            return 3
        if args.dry_run:
            print("Dry run enabled; Telegram delivery skipped.")
        else:
            telegram = TelegramClient(bot_token, chat_id)
            sent = telegram.send_text(report)
            if not sent.ok:
                print(f"Telegram delivery failed: {sent.error}", file=sys.stderr)
                return 4
            print("Telegram message sent.")
            delivered = True
    else:
        delivered = True

    if delivered and not args.dry_run:
        remember_research(state, new_research_ids, new_research_fingerprints)
        remember_public_items(state, [*new_news_ids, *new_podcast_ids])
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
