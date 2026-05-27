#!/usr/bin/env python3
"""
CryptoBot Multi-API daily/weekly briefing agent.

Collects market, sentiment, trend, fundraising, airdrop, and AI narrative data
from free API tiers where available, writes a Markdown report, and optionally
sends it to Telegram.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    BeautifulSoup = None
    HAS_BS4 = False


MESSARI_BASE = "https://api.messari.io"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
CMC_BASE = "https://pro-api.coinmarketcap.com"
CRYPTORANK_BASE = "https://api.cryptorank.io/v2"
ALTERNATIVE_FNG_URL = "https://api.alternative.me/fng/?limit=1"
BINANCE_BASE = "https://fapi.binance.com"
TELEGRAM_BASE = "https://api.telegram.org"
TELEGRAM_LIMIT = 4096
SAFE_TELEGRAM_LIMIT = 3900
DEFAULT_ASSETS = ["bitcoin", "ethereum", "solana", "hyperliquid"]
ASSET_SLUG_ALIASES: dict[str, str] = {
    "hyperliquid": "hyperliquid",
    "hype": "hyperliquid",
}

COINGECKO_ID_MAP: dict[str, str] = {
    "hyperliquid": "hyperliquid",
    "bitcoin": "bitcoin",
    "ethereum": "ethereum",
    "solana": "solana",
}
NO_DATA = "\u26a0\ufe0f Sem dados novos dispon\u00edveis neste momento."
SEPARATOR = "\u2501" * 25
PLACEHOLDER_MARKERS = ("sua_chave", "sua-chave", "seu_token", "seu-token", "seu_chat", "seu-chat", "aqui")
BINANCE_SYMBOL_MAP: dict[str, str | None] = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "binancecoin": "BNBUSDT",
    "ripple": "XRPUSDT",
    "cardano": "ADAUSDT",
    "avalanche-2": "AVAXUSDT",
    "chainlink": "LINKUSDT",
    "polkadot": "DOTUSDT",
    "hyperliquid": None,
}

PROMPT_NARRATIVE = """
Você é um analista cripto sênior. Com base apenas em dados verificáveis disponíveis na Messari,
responda em português do Brasil:

1. Quais foram os 3 eventos mais relevantes no mercado cripto nos últimos 7 dias?
   (Inclua: nome do evento, ativo relacionado, impacto observado)

2. Qual narrativa está dominando o mercado esta semana?
   (Ex: stablecoins, RWA, L2, IA+cripto, BTC como reserva etc.)

3. Quais riscos macro ou on-chain merecem atenção nos próximos 7 dias?

REGRAS:
- Se não houver dados novos verificáveis, diga "Sem eventos novos verificáveis neste período."
- Nunca invente eventos. Nunca cite datas ou valores sem fonte.
- Responda em Markdown, máximo 400 palavras no total.
- Nenhuma recomendação financeira.

Ativos de foco: {assets}
"""

PROMPT_FUNDING = """
Com base nos dados de fundraising cripto desta semana fornecidos abaixo,
responda em português do Brasil:

1. Qual o deal mais relevante e por quê?
2. Qual setor recebeu mais capital (DeFi, Infraestrutura, CeFi, IA+Cripto etc.)?
3. O que esse fluxo de capital sinaliza sobre o sentimento dos VCs esta semana?

Dados:
{funding_data}

REGRAS:
- Use apenas os dados fornecidos acima. Não acrescente deals inventados.
- Se os dados estiverem vazios, diga: "Sem dados de funding verificáveis esta semana."
- Máximo 300 palavras. Sem recomendação financeira.
"""


@dataclass
class ApiResult:
    ok: bool
    status: int | None
    data: Any = None
    error: str | None = None


def _json_or_text(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _error_message(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        for key in ("error", "message", "description", "status"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("error_message") or value.get("message")
                if nested:
                    return str(nested)
    if isinstance(data, str) and data.strip():
        return data.strip()[:300]
    return fallback


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 45,
) -> ApiResult:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", "User-Agent": "CryptoDailyAgent/1.0"}
    request_headers.update(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return ApiResult(True, response.status, _json_or_text(raw))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = _json_or_text(raw)
        return ApiResult(False, exc.code, parsed, _error_message(parsed, str(exc.reason)))
    except URLError as exc:
        return ApiResult(False, None, None, str(exc.reason))
    except TimeoutError:
        return ApiResult(False, None, None, "Request timed out")


def fetch_binance_funding(asset: str) -> tuple[float | None, str | None]:
    """
    Funding rate atual via Binance Futures API.
    100% gratuito, sem chave, sem cadastro.
    Endpoint: GET /fapi/v1/fundingRate
    """
    symbol = BINANCE_SYMBOL_MAP.get(asset.lower())
    if symbol is None:
        return None, f"Binance funding: {asset} n\u00e3o listado em Futures"
    url = f"{BINANCE_BASE}/fapi/v1/fundingRate?symbol={symbol}&limit=3"
    result = http_request("GET", url, timeout=20)
    if not result.ok:
        return None, f"Binance funding {symbol}: status {result.status} \u2014 {result.error}"
    rows = result.data if isinstance(result.data, list) else []
    rates = [
        float(row["fundingRate"])
        for row in rows
        if isinstance(row, dict) and "fundingRate" in row
    ]
    if not rates:
        return None, f"Binance funding {symbol}: sem dados retornados"
    avg_rate = sum(rates) / len(rates) * 100
    return avg_rate, None


def fetch_binance_oi(asset: str) -> tuple[float | None, str | None]:
    """
    Open Interest em USD via Binance Futures API.
    100% gratuito, sem chave, sem cadastro.
    Endpoint: GET /fapi/v1/openInterest
    """
    symbol = BINANCE_SYMBOL_MAP.get(asset.lower())
    if symbol is None:
        return None, f"Binance OI: {asset} n\u00e3o listado em Futures"
    url = f"{BINANCE_BASE}/fapi/v1/openInterest?symbol={symbol}"
    result = http_request("GET", url, timeout=20)
    if not result.ok:
        return None, f"Binance OI {symbol}: status {result.status} \u2014 {result.error}"
    data = result.data
    if isinstance(data, dict) and "openInterest" in data:
        price_url = f"{BINANCE_BASE}/fapi/v1/ticker/price?symbol={symbol}"
        price_result = http_request("GET", price_url, timeout=20)
        oi_qty = first_number(data.get("openInterest"))
        if oi_qty is not None and price_result.ok and isinstance(price_result.data, dict):
            price = first_number(price_result.data.get("price"))
            if price is not None:
                return oi_qty * price, None
        return oi_qty, None
    return None, f"Binance OI {symbol}: formato de resposta inesperado"


def unwrap_data(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def env_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if looks_like_placeholder(value):
        return ""
    return value


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_fetch(func: Callable[[], ApiResult], label: str) -> tuple[Any, str | None]:
    """
    Wrapper para chamadas de API. Nunca levanta exceção.
    Retorna (data, error_message).
    """
    try:
        result = func()
        if result.ok:
            return result.data, None
        return None, f"\u274c {label}: status {result.status} \u2014 {result.error}"
    except Exception as exc:
        return None, f"\u274c {label}: {type(exc).__name__} \u2014 {exc}"


class MessariClient:
    def __init__(self, api_key: str, timeout: int = 45) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"X-Messari-API-Key": self.api_key}

    def get(self, path: str, params: dict[str, Any] | None = None) -> ApiResult:
        if not self.api_key:
            return ApiResult(False, None, None, "MESSARI_API_KEY ausente")
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        result = http_request("GET", f"{MESSARI_BASE}{path}{query}", self._headers(), timeout=self.timeout)
        time.sleep(0.5)
        return result

    def post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        if not self.api_key:
            return ApiResult(False, None, None, "MESSARI_API_KEY ausente")
        result = http_request("POST", f"{MESSARI_BASE}{path}", self._headers(), payload, self.timeout)
        time.sleep(0.5)
        return result

    def asset_details(self, assets: list[str]) -> ApiResult:
        return self.get("/metrics/v2/assets/details", {"assetIDs": ",".join(assets)})

    def price_timeseries(self, asset: str, start: str, end: str) -> ApiResult:
        return self.get(f"/metrics/v2/assets/{asset}/metrics/price/time-series/1d", {"start": start, "end": end})

    def funding_rate(self, asset: str, start: str, end: str) -> ApiResult:
        return ApiResult(False, 401, None, "endpoint requer Messari Enterprise")

    def open_interest(self, asset: str) -> ApiResult:
        return ApiResult(False, 401, None, "endpoint requer Messari Enterprise")

    def volatility(self, asset: str, start: str, end: str) -> ApiResult:
        return ApiResult(False, 401, None, "endpoint requer Messari Enterprise")

    def ai_chat(self, prompt: str) -> ApiResult:
        return self.post(
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


class CoinGeckoClient:
    def __init__(self, api_key: str, timeout: int = 45) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"x-cg-demo-api-key": self.api_key} if self.api_key else {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> ApiResult:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        result = http_request("GET", f"{COINGECKO_BASE}{path}{query}", self._headers(), timeout=self.timeout)
        time.sleep(1)
        return result

    def trending(self) -> ApiResult:
        return self.get("/search/trending")

    def gainers_losers(self) -> ApiResult:
        return self.get(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 50,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
        )

    def global_data(self) -> ApiResult:
        return self.get("/global")

    def top_markets(self) -> ApiResult:
        return self.get(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h,7d,30d",
            },
        )


class CoinMarketCapClient:
    def __init__(self, api_key: str, timeout: int = 45) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"X-CMC_PRO_API_KEY": self.api_key} if self.api_key else {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> ApiResult:
        if not self.api_key:
            return ApiResult(False, None, None, "COINMARKETCAP_API_KEY ausente")
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        result = http_request("GET", f"{CMC_BASE}{path}{query}", self._headers(), timeout=self.timeout)
        time.sleep(1)
        return result

    def fear_greed_latest(self) -> ApiResult:
        return self.get("/v3/fear-and-greed/latest")

    def fear_greed_historical(self) -> ApiResult:
        return self.get("/v3/fear-and-greed/historical", {"limit": 7})


class CryptoRankClient:
    def __init__(self, api_key: str, timeout: int = 45) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> ApiResult:
        if not self.api_key:
            return ApiResult(False, None, None, "CRYPTORANK_API_KEY ausente")
        query_params = dict(params or {})
        query_params["api_key"] = self.api_key
        query = f"?{urlencode(query_params, doseq=True)}"
        url = f"https://api.cryptorank.io/v1{path}{query}"
        result = http_request("GET", url, timeout=self.timeout)
        time.sleep(0.6)
        return result

    def funding_rounds(self) -> ApiResult:
        """
        Busca funding rounds recentes.
        Endpoint v1 compatível com plano sandbox.
        """
        result = self.get(
            "/currencies/funding-rounds",
            {"limit": 10, "offset": 0}
        )
        if result.status in (401, 403, 404):
            result = self.get(
                "/ieo",
                {"limit": 10, "offset": 0, "status": "active,upcoming"}
            )
        return result

    def drophunting(self) -> ApiResult:
        """
        Busca airdrops com potencial.
        Endpoint v1 compatível com plano sandbox.
        """
        result = self.get(
            "/currencies/drophunting",
            {"limit": 10, "offset": 0}
        )
        if result.status in (401, 403, 404):
            result = self.get(
                "/currencies",
                {
                    "limit": 10,
                    "offset": 0,
                    "category": "airdrop",
                }
            )
        return result


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 45) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def send_text(self, text: str) -> ApiResult:
        chunks = split_for_telegram(text)
        last_result = ApiResult(True, 200, {})
        for chunk in chunks:
            last_result = self._post("sendMessage", {"chat_id": self.chat_id, "text": chunk, "disable_web_page_preview": True})
            if not last_result.ok:
                return last_result
        return last_result

    def _post(self, method: str, payload: dict[str, Any]) -> ApiResult:
        return http_request(
            "POST",
            f"{TELEGRAM_BASE}/bot{self.bot_token}/{method}",
            {"Content-Type": "application/json"},
            payload,
            self.timeout,
        )


class StateManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        default = {
            "sent_funding_ids": [],
            "sent_airdrop_ids": [],
            "sent_research_fps": [],
            "sent_trending_ids": [],
            "last_run_at": "",
            "last_fear_greed": None,
        }
        if not self.path.exists():
            return default
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
        if not isinstance(loaded, dict):
            return default
        for key, value in default.items():
            loaded.setdefault(key, value)
        return loaded

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def is_new(self, bucket: str, item_id: str, ttl: timedelta) -> bool:
        now = datetime.now(timezone.utc)
        entries = self._fresh_entries(bucket, ttl, now)
        self.state[bucket] = entries
        for entry in entries:
            if self._entry_id(entry) == item_id:
                return False
        return True

    def remember(self, bucket: str, item_ids: list[str], ttl: timedelta) -> None:
        now = datetime.now(timezone.utc)
        entries = self._fresh_entries(bucket, ttl, now)
        known = {self._entry_id(entry) for entry in entries}
        for item_id in item_ids:
            if item_id and item_id not in known:
                entries.append({"id": item_id, "sent_at": now.isoformat()})
                known.add(item_id)
        self.state[bucket] = entries[-1000:]

    def _fresh_entries(self, bucket: str, ttl: timedelta, now: datetime) -> list[Any]:
        fresh = []
        for entry in self.state.get(bucket, []):
            sent_at = self._entry_time(entry)
            if sent_at is None or now - sent_at <= ttl:
                fresh.append(entry)
        return fresh

    @staticmethod
    def _entry_id(entry: Any) -> str:
        if isinstance(entry, dict):
            return str(entry.get("id") or "")
        return str(entry)

    @staticmethod
    def _entry_time(entry: Any) -> datetime | None:
        if not isinstance(entry, dict):
            return None
        raw = entry.get("sent_at")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_markdown(text: str, max_chars: int = 2600) -> str:
    if not text:
        return ""
    # Remove footnotes: [^1], [1], [^12] etc
    text = re.sub(r'\[\^?\d+\]', '', text)
    # Remove links markdown [texto](url) mantendo só o texto
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove headers ## Header -> linha vazia (não joga o texto fora)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Converte **negrito** -> texto em maiúsculas para destacar sem markdown
    text = re.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1).upper(), text)
    # Remove * e _ e ` soltos
    text = re.sub(r'[*_`]', '', text)
    # Normaliza bullets para o marcador visual do Telegram.
    text = re.sub(r'^\*\s+', '\u2022 ', text, flags=re.MULTILINE)
    text = re.sub(r'^\-\s+', '\u2022 ', text, flags=re.MULTILINE)
    # Remove linhas em branco excessivas
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Remove espaços extras
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()[:max_chars]


def first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", ""))
            except ValueError:
                continue
    return None


def pick(data: Any, *paths: str) -> Any:
    for path in paths:
        current = data
        ok = True
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break
        if ok and current is not None:
            return current
    return None


def money(value: Any) -> str:
    number = first_number(value)
    if number is None:
        return "n/d"
    abs_value = abs(number)
    if abs_value >= 1_000_000_000_000:
        return f"${number / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"
    if abs_value >= 1:
        return f"${number:,.2f}"
    return f"${number:.6f}"


def pct(value: Any) -> str:
    number = first_number(value)
    if number is None:
        return "n/d"
    return f"{number:+.2f}%"


def avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def normalize_timeseries_values(data: Any) -> list[float]:
    payload = unwrap_data(data)
    if isinstance(payload, dict):
        for key in ("values", "points", "series", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []
    values = []
    for item in payload:
        if isinstance(item, dict):
            number = first_number(
                item.get("value"),
                item.get("close"),
                item.get("price"),
                item.get("rate"),
                item.get("openInterest"),
                item.get("volatility"),
            )
        elif isinstance(item, list):
            number = first_number(*reversed(item))
        else:
            number = first_number(item)
        if number is not None:
            values.append(number)
    return values


def normalize_list(data: Any) -> list[Any]:
    payload = unwrap_data(data)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "coins", "data", "result", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if isinstance(payload.get("cryptoCurrencyList"), list):
            return payload["cryptoCurrencyList"]
    return []


def fear_greed_label(value: Any) -> str:
    number = first_number(value)
    if number is None:
        return "n/d"
    if number <= 24:
        return "Extreme Fear \U0001f631"
    if number <= 44:
        return "Fear \U0001f628"
    if number <= 55:
        return "Neutral \U0001f610"
    if number <= 74:
        return "Greed \U0001f60f"
    return "Extreme Greed \U0001f911"


def trend_label(old: Any, new: Any) -> str:
    old_number = first_number(old)
    new_number = first_number(new)
    if old_number is None or new_number is None:
        return "n/d"
    diff = new_number - old_number
    if abs(diff) < 2:
        return "estável"
    return "subindo" if diff > 0 else "caindo"


def calcular_potencial_airdrop(funding_usd: float | None, twitter_score: int | None, status: str) -> str:
    """
    Classifica o potencial de um airdrop com base em critérios objetivos.
    Nunca retorna "ALTO" se não houver dados de funding.
    """
    if status == "snapshot":
        return "ENCERRADO \u2014 Snapshot j\u00e1 feito"

    score = 0

    if funding_usd is not None:
        if funding_usd >= 100_000_000:
            score += 3
        elif funding_usd >= 50_000_000:
            score += 2
        elif funding_usd >= 10_000_000:
            score += 1

    if twitter_score is not None:
        if twitter_score >= 80:
            score += 2
        elif twitter_score >= 50:
            score += 1

    if status == "confirmed":
        score += 1

    if score >= 4:
        return "\U0001f534 ALTO"
    if score >= 2:
        return "\U0001f7e1 M\u00c9DIO"
    return "\u26aa BAIXO"


def extract_ai_content(data: Any) -> str | None:
    payload = unwrap_data(data)
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) and content.strip():
                return clean_markdown(content.strip(), 2600)
        messages = payload.get("messages")
        if isinstance(messages, list) and messages:
            content = messages[-1].get("content") if isinstance(messages[-1], dict) else None
            if isinstance(content, str) and content.strip():
                return clean_markdown(content.strip(), 2600)
    return None


def asset_symbol(asset: dict[str, Any], fallback: str) -> str:
    return str(asset.get("symbol") or asset.get("ticker") or fallback[:4]).upper()


def normalize_asset_detail(raw_assets: Any, requested_assets: list[str], market_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    rows = normalize_list(raw_assets)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        key = str(row.get("slug") or row.get("id") or row.get("assetKey") or row.get("name") or "").lower()
        if not key and index < len(requested_assets):
            key = requested_assets[index]
        details[key] = row

    for row in market_rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or row.get("symbol") or "").lower()
        if key:
            details.setdefault(key, row)
    return details


def build_asset_rows(
    assets: list[str],
    messari_details: Any,
    market_rows: list[dict[str, Any]],
    timeseries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    details_by_key = normalize_asset_detail(messari_details, assets, market_rows)
    rows = []
    for asset in assets:
        detail = details_by_key.get(asset) or next(
            (value for key, value in details_by_key.items() if key == asset.lower() or asset.lower() in key),
            {},
        )
        if (
            not detail
            or not isinstance(detail, dict)
            or pick(detail, "marketData.priceUsd", "priceUsd", "current_price") is None
        ):
            cg_id = COINGECKO_ID_MAP.get(asset.lower(), asset.lower())
            cg_detail = next(
                (row for row in market_rows if isinstance(row, dict) and (
                    str(row.get("id") or "").lower() == cg_id or
                    str(row.get("symbol") or "").lower() == asset.lower()
                )),
                {},
            )
            if cg_detail:
                detail = cg_detail
        market_data = detail.get("marketData") if isinstance(detail, dict) else {}
        roi = detail.get("returnOnInvestment") if isinstance(detail, dict) else {}
        cap = market_data.get("marketcap") if isinstance(market_data, dict) else {}
        ts = timeseries.get(asset, {})
        funding_avg = ts.get("funding_direct")
        oi_direct = ts.get("oi_direct")
        rows.append(
            {
                "asset": asset,
                "symbol": asset_symbol(detail if isinstance(detail, dict) else {}, asset),
                "price": pick(detail, "marketData.priceUsd", "priceUsd", "current_price"),
                "change_24h": pick(detail, "returnOnInvestment.priceChange24h", "price_change_percentage_24h"),
                "change_7d": pick(detail, "returnOnInvestment.priceChange7d", "price_change_percentage_7d_in_currency"),
                "volume": pick(detail, "marketData.volume24Hour", "total_volume"),
                "ath": pick(
                    detail,
                    "allTimeHigh.allTimeHigh",
                    "allTimeHigh.price",
                    "marketData.allTimeHigh",
                    "ath",
                ),
                "ath_change": pick(
                    detail,
                    "allTimeHigh.allTimeHighPercentDown",
                    "allTimeHigh.percentDown",
                    "ath_change_percentage",
                ),
                "mcap": pick(detail, "marketData.marketcap.circulatingUsd", "market_cap") or (cap.get("circulatingUsd") if isinstance(cap, dict) else None),
                "funding_rate": funding_avg,
                "open_interest": oi_direct,
                "volatility": None,
            }
        )
    return rows


def parse_global(data: Any) -> dict[str, Any]:
    payload = unwrap_data(data)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        return {}
    total_mcap = pick(payload, "total_market_cap.usd")
    total_volume = pick(payload, "total_volume.usd")
    dominance = payload.get("market_cap_percentage") if isinstance(payload.get("market_cap_percentage"), dict) else {}
    return {
        "market_cap": total_mcap,
        "volume": total_volume,
        "btc_dominance": dominance.get("btc") if isinstance(dominance, dict) else None,
        "eth_dominance": dominance.get("eth") if isinstance(dominance, dict) else None,
        "mcap_change_24h": payload.get("market_cap_change_percentage_24h_usd"),
    }


def parse_cmc_fear_latest(data: Any) -> int | None:
    payload = unwrap_data(data)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload, dict):
        return int(number) if (number := first_number(payload.get("value"), payload.get("score"))) is not None else None
    return None


def parse_cmc_fear_history(data: Any) -> list[int]:
    values = []
    for item in normalize_list(data):
        if isinstance(item, dict):
            number = first_number(item.get("value"), item.get("score"))
            if number is not None:
                values.append(int(number))
    return values


def parse_alternative_fear(data: Any) -> int | None:
    payload = data
    if isinstance(payload, dict) and isinstance(payload.get("data"), list) and payload["data"]:
        return int(number) if (number := first_number(payload["data"][0].get("value"))) is not None else None
    return None


def parse_trending(data: Any) -> list[dict[str, Any]]:
    items = []
    for row in normalize_list(data):
        item = row.get("item") if isinstance(row, dict) and isinstance(row.get("item"), dict) else row
        if not isinstance(item, dict):
            continue
        coin_id = str(item.get("id") or item.get("coin_id") or item.get("symbol") or item.get("name") or "")
        items.append(
            {
                "id": coin_id,
                "name": item.get("name") or coin_id or "n/d",
                "symbol": str(item.get("symbol") or "").upper(),
                "change_24h": pick(item, "data.price_change_percentage_24h.usd", "price_change_percentage_24h"),
            }
        )
    return items


def parse_gainers_losers(data: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = unwrap_data(data)
    gainers_raw = payload.get("top_gainers") if isinstance(payload, dict) else None
    losers_raw = payload.get("top_losers") if isinstance(payload, dict) else None
    if not isinstance(gainers_raw, list) and not isinstance(losers_raw, list):
        rows = normalize_list(data)
        rows = [row for row in rows if isinstance(row, dict)]
        rows.sort(key=lambda row: first_number(row.get("price_change_percentage_24h")) or 0, reverse=True)
        gainers_raw = rows[:3]
        losers_raw = rows[-3:]
    return parse_coin_rows(gainers_raw or []), parse_coin_rows(losers_raw or [])


def parse_coin_rows(rows: list[Any]) -> list[dict[str, Any]]:
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed.append(
            {
                "id": str(row.get("id") or row.get("symbol") or row.get("name") or ""),
                "name": row.get("name") or row.get("id") or "n/d",
                "symbol": str(row.get("symbol") or "").upper(),
                "change_24h": row.get("price_change_percentage_24h") or row.get("usd_24h_change"),
            }
        )
    return parsed


def parse_funding_rounds(data: Any) -> tuple[list[dict[str, Any]], bool]:
    if data is None:
        return [], False

    rows = normalize_list(data)
    parsed = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        project = (
            pick(row, "currency.name") or
            pick(row, "project.name") or
            pick(row, "name") or
            pick(row, "title") or
            pick(row, "key") or
            "n/d"
        )

        funding_usd = first_number(
            row.get("funding_usd"),
            row.get("moneyRaised"),
            row.get("amount"),
            row.get("raised"),
            row.get("totalRaise"),
            pick(row, "raise.amount"),
            pick(row, "usdAmount"),
        )

        stage = (
            row.get("stage") or
            row.get("round") or
            row.get("type") or
            row.get("status") or
            "n/d"
        )

        lead = (
            pick(row, "leadInvestor.name") or
            pick(row, "lead.name") or
            pick(row, "funds[0].name") or
            row.get("leadInvestor") or
            row.get("lead") or
            "n/d"
        )

        description = clean_text(
            str(row.get("description") or row.get("whatItDoes") or "")
        )[:220]

        item_id = str(
            row.get("id") or
            row.get("key") or
            f"{project}|{row.get('date') or row.get('announcementDate') or ''}"
        )

        if project and project != "n/d":
            parsed.append({
                "id": item_id,
                "project": project,
                "amount": funding_usd,
                "stage": stage,
                "lead": lead,
                "description": description,
            })

    return parsed, False


def parse_airdrops(data: Any) -> tuple[list[dict[str, Any]], bool]:
    if data is None:
        return [], False

    rows = normalize_list(data)
    parsed = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        project = (
            pick(row, "currency.name") or
            pick(row, "project.name") or
            pick(row, "name") or
            pick(row, "title") or
            "n/d"
        )

        funding_usd = first_number(
            row.get("funding_usd"),
            row.get("funding"),
            row.get("totalFunding"),
            row.get("totalRaise"),
            pick(row, "funding.total"),
        )

        twitter_score = first_number(
            row.get("twitterScore"),
            row.get("twitter_score"),
            row.get("score"),
        )

        status = str(
            row.get("status") or
            row.get("state") or
            "potential"
        ).lower()

        network = (
            row.get("blockchain") or
            row.get("network") or
            row.get("chain") or
            pick(row, "currency.network") or
            "n/d"
        )

        reward = (
            row.get("reward") or
            row.get("rewardType") or
            row.get("type") or
            "Token"
        )

        item_id = str(
            row.get("id") or
            row.get("key") or
            f"{project}|{status}"
        )

        if project and project != "n/d":
            parsed.append({
                "id": item_id,
                "project": project,
                "status": status,
                "funding": funding_usd,
                "twitter_score": int(twitter_score) if twitter_score is not None else None,
                "network": network,
                "reward": reward,
            })

    return parsed, False


def format_endpoint_status(statuses: dict[str, str]) -> str:
    if not statuses:
        return NO_DATA
    enterprise_keywords = (
        "requer messari enterprise",
        "enterprise membership",
        "enterprise required",
    )
    lines = []
    for label, status in statuses.items():
        if any(kw in status.lower() for kw in enterprise_keywords):
            continue
        lines.append(f"\u2022 {label}: {status}")
    return "\n".join(lines) if lines else "\u2705 Todos os endpoints ativos responderam."


def section(title: str, body: str) -> str:
    return f"{title}\n{body.strip() if body.strip() else NO_DATA}"


def build_report(context: dict[str, Any]) -> str:
    now_label = datetime.now().strftime("%d/%m/%Y")
    global_data = context.get("global") or {}
    fear_today = context.get("fear_today")
    fear_old = context.get("fear_old")
    asset_rows = context.get("asset_rows") or []
    trending = context.get("trending") or []
    gainers = context.get("gainers") or []
    losers = context.get("losers") or []
    narrative = context.get("narrative") or NO_DATA
    funding = context.get("funding") or []
    airdrops = context.get("airdrops") or []
    endpoints = context.get("endpoints") or {}
    weekly = bool(context.get("weekly"))
    funding_fallback = bool(context.get("funding_fallback"))
    airdrop_fallback = bool(context.get("airdrop_fallback"))

    lines = [
        SEPARATOR,
        f"\U0001f52e CRYPTO BRIEFING \u2014 {now_label}",
        SEPARATOR,
        "",
        "\U0001f4ca MERCADO GLOBAL",
    ]
    if global_data:
        lines.extend(
            [
                f"\u2022 Market Cap total: {money(global_data.get('market_cap'))} ({pct(global_data.get('mcap_change_24h'))})",
                f"\u2022 Volume 24h: {money(global_data.get('volume'))}",
                f"\u2022 Domin\u00e2ncia BTC: {pct(global_data.get('btc_dominance')).replace('+', '')} | ETH: {pct(global_data.get('eth_dominance')).replace('+', '')}",
            ]
        )
    else:
        lines.append(NO_DATA)

    lines.extend(["", "\U0001f631 SENTIMENTO \u2014 Fear & Greed"])
    if fear_today is not None:
        old_value = fear_old if fear_old is not None else fear_today
        lines.append(f"\u2022 Hoje: {int(fear_today)}/100 \u2014 {fear_greed_label(fear_today)}")
        lines.append(f"\u2022 Tend\u00eancia 7d: {trend_label(old_value, fear_today)} ({int(old_value)} \u2192 {int(fear_today)})")
    else:
        lines.append(NO_DATA)

    lines.extend(["", "\U0001f4b9 ATIVOS MONITORADOS"])
    if asset_rows:
        for row in asset_rows:
            funding_str = pct(row.get('funding_rate')) if row.get('funding_rate') is not None else "n/d"
            oi_str = money(row.get('open_interest')) if row.get('open_interest') is not None else "n/d"
            ath_str = money(row.get('ath')) if row.get('ath') is not None else "n/d"
            ath_pct = f" ({pct(row.get('ath_change'))} abaixo)" if row.get('ath_change') is not None else ""

            lines.append(
                f"{row['symbol']} | {money(row.get('price'))} | "
                f"24h: {pct(row.get('change_24h'))} | 7d: {pct(row.get('change_7d'))}"
            )
            lines.append(f"  \u21b3 Mcap: {money(row.get('mcap'))} | Vol: {money(row.get('volume'))}")
            lines.append(f"  \u21b3 ATH: {ath_str}{ath_pct}")
            if funding_str != "n/d" or oi_str != "n/d":
                lines.append(f"  \u21b3 Funding: {funding_str} | OI: {oi_str}")
    else:
        lines.append(NO_DATA)

    lines.extend(["", "\U0001f525 TRENDING \u2014 CoinGecko"])
    if trending:
        for index, item in enumerate(trending[:5], start=1):
            symbol = f" ({item['symbol']})" if item.get("symbol") else ""
            lines.append(f"{index}. {item.get('name')}{symbol} \u2014 24h: {pct(item.get('change_24h'))}")
    else:
        lines.append(NO_DATA)

    lines.extend(["", "\U0001f4c8 TOP GAINERS 24H"])
    if gainers:
        for item in gainers[:3]:
            lines.append(f"\u2022 {item.get('name')} ({item.get('symbol') or 'n/d'}) \u2014 {pct(item.get('change_24h'))}")
    else:
        lines.append(NO_DATA)

    lines.extend(["", "\U0001f4c9 TOP LOSERS 24H"])
    if losers:
        for item in losers[:3]:
            lines.append(f"\u2022 {item.get('name')} ({item.get('symbol') or 'n/d'}) \u2014 {pct(item.get('change_24h'))}")
    else:
        lines.append(NO_DATA)

    lines.extend(["", "\U0001f9e0 NARRATIVA DA SEMANA \u2014 Messari AI", narrative.strip() or NO_DATA])

    if weekly:
        lines.extend(["", "\U0001f4b0 FUNDING ROUNDS DA SEMANA \u2014 CryptoRank"])
        if funding:
            for deal in funding[:5]:
                amount_str = money(deal.get('amount')) if deal.get('amount') else "valor n/d"
                lines.append(
                    f"\u2022 {deal.get('project')} \u2014 {amount_str} | {deal.get('stage')}"
                )
                if deal.get('lead') and deal.get('lead') != 'n/d':
                    lines.append(f"  \u21b3 Lead: {deal.get('lead')}")
                if deal.get('description'):
                    lines.append(f"  \u21b3 {deal.get('description')[:150]}")
        else:
            lines.append("\u26a0\ufe0f API CryptoRank n\u00e3o retornou deals esta semana.")
            lines.append("Acesse: https://cryptorank.io/funding-rounds")

        lines.extend(["", "\U0001fa82 AIRDROPS COM POTENCIAL \u2014 CryptoRank"])
        if airdrops:
            for drop in airdrops[:5]:
                potential = calcular_potencial_airdrop(drop.get("funding"), drop.get("twitter_score"), str(drop.get("status") or ""))
                score = drop.get("twitter_score")
                score_text = f"{score}/100" if score is not None else "n/d"
                status_emoji = "\u2705" if drop.get("status") == "confirmed" else "\u23f3"
                lines.append(
                    f"\u2022 {drop.get('project')} [{status_emoji} {drop.get('status')}]"
                )
                lines.append(
                    f"  \u21b3 Funding: {money(drop.get('funding'))} | "
                    f"Twitter: {score_text} | Rede: {drop.get('network')}"
                )
                lines.append(f"  \u21b3 Potencial: {potential}")
        else:
            lines.append("\u26a0\ufe0f API CryptoRank n\u00e3o retornou airdrops esta semana.")
            lines.append("Acesse: https://cryptorank.io/drophunting")

        funding_ai = context.get("funding_ai")
        if funding_ai:
            lines.extend(["", "\U0001f9e0 LEITURA DE FUNDING \u2014 Messari AI", funding_ai])

    lines.extend(
        [
            "",
            SEPARATOR,
            "\u26a0\ufe0f STATUS DOS ENDPOINTS",
            format_endpoint_status(endpoints),
            SEPARATOR,
            "Aviso: informativo. N\u00e3o \u00e9 recomenda\u00e7\u00e3o financeira.",
        ]
    )
    return "\n".join(lines)


def split_for_telegram(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n")
    if len(normalized) <= TELEGRAM_LIMIT:
        return [normalized]

    blocks = re.split(r"(?=\n?[\U0001f4ca\U0001f631\U0001f4b9\U0001f525\U0001f4c8\U0001f4c9\U0001f9e0\U0001f4b0\U0001fa82\u26a0])", normalized)
    chunks = []
    current = ""
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) <= SAFE_TELEGRAM_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(block) <= SAFE_TELEGRAM_LIMIT:
            current = block
            continue
        for line in block.splitlines():
            candidate = f"{current}\n{line}".strip() if current else line
            if len(candidate) <= SAFE_TELEGRAM_LIMIT:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line[:SAFE_TELEGRAM_LIMIT]
    if current:
        chunks.append(current)
    return chunks


def write_report(content: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"crypto-daily-{datetime.now().date().isoformat()}.md"
    path = output_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def run_parallel(tasks: dict[str, Callable[[], tuple[Any, str | None]]]) -> dict[str, tuple[Any, str | None]]:
    results: dict[str, tuple[Any, str | None]] = {}
    lock = threading.Lock()

    def runner(name: str, func: Callable[[], tuple[Any, str | None]]) -> None:
        value = func()
        with lock:
            results[name] = value

    threads = [threading.Thread(target=runner, args=(name, func), daemon=True) for name, func in tasks.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def endpoint_status(error: str | None, data: Any = None, fallback: bool = False) -> str:
    if fallback:
        return "\u26a0\ufe0f fallback usado"
    if error:
        return error
    if data is None:
        return "\u274c falhou \u2014 resposta vazia"
    return "\u2705 ok"


def collect_messari_timeseries(
    client: MessariClient,
    assets: list[str],
    endpoints: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """
    Coleta timeseries por ativo.
    - Funding rate: Binance Futures API (gratuito, sem chave)
    - Open Interest: Binance Futures API (gratuito, sem chave)
    - Preço timeseries: removido (requer Messari Enterprise)
    """
    results: dict[str, dict[str, Any]] = {}

    for asset in assets:
        results[asset] = {}
        results[asset]["price"] = None

        funding_val, funding_error = fetch_binance_funding(asset)
        endpoints[f"Binance funding/{asset}"] = (
            "\u2705 ok" if funding_error is None and funding_val is not None
            else f"\u274c {funding_error or 'sem dado'}"
        )
        results[asset]["funding_direct"] = funding_val

        oi_val, oi_error = fetch_binance_oi(asset)
        endpoints[f"Binance OI/{asset}"] = (
            "\u2705 ok" if oi_error is None and oi_val is not None
            else f"\u274c {oi_error or 'sem dado'}"
        )
        results[asset]["oi_direct"] = oi_val

        time.sleep(0.3)

    return results


def filter_new_items(items: list[dict[str, Any]], state: StateManager, bucket: str, ttl: timedelta) -> tuple[list[dict[str, Any]], list[str]]:
    fresh = []
    remembered = []
    for item in items:
        item_id = str(item.get("id") or item.get("project") or item.get("name") or "")
        if not item_id:
            continue
        if state.is_new(bucket, item_id, ttl):
            fresh.append(item)
            remembered.append(item_id)
    return fresh, remembered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a multi-API crypto daily briefing.")
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS), help="Comma-separated Messari asset slugs.")
    parser.add_argument("--send-telegram", action="store_true", help="Send report to Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="Do not save state or send Telegram.")
    parser.add_argument("--no-ai", action="store_true", help="Skip Messari AI calls.")
    parser.add_argument("--output-dir", default="reports", help="Directory where the Markdown report is saved.")
    parser.add_argument("--state-file", default="state/crypto_agent_state.json", help="State file used for deduplication.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--weekly", action="store_true", help="Weekly mode: includes funding rounds and airdrops.")
    mode.add_argument("--daily", action="store_true", help="Daily mode: market, sentiment, trending, narrative.")
    return parser.parse_args()


def main() -> int:
    load_env_file(Path(".env"))
    args = parse_args()
    assets = [item.strip() for item in args.assets.split(",") if item.strip()]
    if not assets:
        assets = DEFAULT_ASSETS
    weekly = bool(args.weekly)

    state = StateManager(Path(args.state_file))
    messari = MessariClient(env_value("MESSARI_API_KEY"))
    coingecko = CoinGeckoClient(env_value("COINGECKO_API_KEY"))
    cmc = CoinMarketCapClient(env_value("COINMARKETCAP_API_KEY"))
    cryptorank = CryptoRankClient(env_value("CRYPTORANK_API_KEY"))

    endpoints: dict[str, str] = {}
    tasks: dict[str, Callable[[], tuple[Any, str | None]]] = {
        "messari_details": lambda: safe_fetch(lambda: messari.asset_details(assets), "Messari asset details"),
        "coingecko_trending": lambda: safe_fetch(coingecko.trending, "CoinGecko trending"),
        "coingecko_gainers_losers": lambda: safe_fetch(coingecko.gainers_losers, "CoinGecko gainers/losers"),
        "coingecko_global": lambda: safe_fetch(coingecko.global_data, "CoinGecko global"),
        "coingecko_markets": lambda: safe_fetch(coingecko.top_markets, "CoinGecko top markets"),
        "CMC Fear & Greed latest": lambda: safe_fetch(cmc.fear_greed_latest, "CoinMarketCap Fear & Greed latest"),
        "CMC Fear & Greed history": lambda: safe_fetch(cmc.fear_greed_historical, "CoinMarketCap Fear & Greed historical"),
        "alternative_fng": lambda: safe_fetch(lambda: http_request("GET", ALTERNATIVE_FNG_URL), "Alternative.me Fear & Greed"),
    }
    if weekly:
        tasks["cryptorank_funding"] = lambda: safe_fetch(cryptorank.funding_rounds, "CryptoRank funding rounds")
        tasks["cryptorank_airdrops"] = lambda: safe_fetch(cryptorank.drophunting, "CryptoRank drophunting")

    raw = run_parallel(tasks)
    for key, (data, error) in raw.items():
        endpoints[key.replace("_", " ")] = endpoint_status(error, data)

    timeseries = collect_messari_timeseries(messari, assets, endpoints)
    market_rows = [
        *normalize_list(raw.get("coingecko_markets", (None, None))[0]),
        *normalize_list(raw.get("coingecko_gainers_losers", (None, None))[0]),
    ]
    asset_rows = build_asset_rows(assets, raw.get("messari_details", (None, None))[0], market_rows, timeseries)
    global_data = parse_global(raw.get("coingecko_global", (None, None))[0])

    cmc_today = parse_cmc_fear_latest(raw.get("CMC Fear & Greed latest", (None, None))[0])
    cmc_history = parse_cmc_fear_history(raw.get("CMC Fear & Greed history", (None, None))[0])
    alt_today = parse_alternative_fear(raw.get("alternative_fng", (None, None))[0])
    fear_today = cmc_today if cmc_today is not None else alt_today
    fear_old = cmc_history[-1] if cmc_history else state.state.get("last_fear_greed")

    trending_all = parse_trending(raw.get("coingecko_trending", (None, None))[0])
    trending, trending_ids = filter_new_items(trending_all, state, "sent_trending_ids", timedelta(hours=6))
    gainers, losers = parse_gainers_losers(raw.get("coingecko_gainers_losers", (None, None))[0])

    funding: list[dict[str, Any]] = []
    airdrops: list[dict[str, Any]] = []
    funding_ids: list[str] = []
    airdrop_ids: list[str] = []
    funding_fallback = False
    airdrop_fallback = False
    if weekly:
        funding_all, funding_fallback = parse_funding_rounds(raw.get("cryptorank_funding", (None, None))[0])
        airdrop_all, airdrop_fallback = parse_airdrops(raw.get("cryptorank_airdrops", (None, None))[0])
        funding, funding_ids = filter_new_items(funding_all, state, "sent_funding_ids", timedelta(days=7))
        airdrops, airdrop_ids = filter_new_items(airdrop_all, state, "sent_airdrop_ids", timedelta(days=7))
        if funding_fallback:
            endpoints["CryptoRank funding rounds"] = "\u26a0\ufe0f fallback usado"
        if airdrop_fallback:
            endpoints["CryptoRank drophunting"] = "\u26a0\ufe0f fallback usado"

    narrative = NO_DATA
    funding_ai = ""
    if args.no_ai:
        endpoints["Messari AI narrative"] = "\u26a0\ufe0f pulado por --no-ai"
        if weekly:
            endpoints["Messari AI funding"] = "\u26a0\ufe0f pulado por --no-ai"
    else:
        narrative_data, narrative_error = safe_fetch(
            lambda: messari.ai_chat(PROMPT_NARRATIVE.format(assets=", ".join(assets))),
            "Messari AI narrative",
        )
        _narrative_raw = extract_ai_content(narrative_data) or ""
        narrative = clean_markdown(_narrative_raw, max_chars=2600) or NO_DATA
        endpoints["Messari AI narrative"] = endpoint_status(narrative_error, narrative_data)
        if weekly:
            funding_context = json.dumps(funding, ensure_ascii=False, default=str)
            funding_data, funding_error = safe_fetch(
                lambda: messari.ai_chat(PROMPT_FUNDING.format(funding_data=funding_context)),
                "Messari AI funding",
            )
            funding_ai = clean_markdown(extract_ai_content(funding_data) or "", max_chars=1800)
            endpoints["Messari AI funding"] = endpoint_status(funding_error, funding_data)

    report = build_report(
        {
            "weekly": weekly,
            "global": global_data,
            "fear_today": fear_today,
            "fear_old": fear_old,
            "asset_rows": asset_rows,
            "trending": trending,
            "gainers": gainers,
            "losers": losers,
            "narrative": narrative,
            "funding": funding,
            "airdrops": airdrops,
            "funding_fallback": funding_fallback,
            "airdrop_fallback": airdrop_fallback,
            "funding_ai": funding_ai,
            "endpoints": endpoints,
        }
    )

    if all(status.startswith("\u274c") for status in endpoints.values()):
        report = build_report(
            {
                "weekly": weekly,
                "narrative": "Todos os blocos falharam. Veja o status dos endpoints abaixo.",
                "endpoints": endpoints,
            }
        )

    output_path = write_report(report, Path(args.output_dir))
    print(f"Report written to {output_path}")

    if args.send_telegram:
        bot_token = env_value("TELEGRAM_BOT_TOKEN")
        chat_id = env_value("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausente/placeholder.", file=sys.stderr)
            return 3
        if args.dry_run:
            print("Dry run enabled; Telegram delivery skipped.")
        else:
            sent = TelegramClient(bot_token, chat_id).send_text(report)
            if not sent.ok:
                print(f"Telegram delivery failed: {sent.error}", file=sys.stderr)
                return 4
            print("Telegram message sent.")

    if not args.dry_run:
        state.remember("sent_trending_ids", trending_ids, timedelta(hours=6))
        state.remember("sent_funding_ids", funding_ids, timedelta(days=7))
        state.remember("sent_airdrop_ids", airdrop_ids, timedelta(days=7))
        research_fp = f"narrative|{datetime.now(timezone.utc).date().isoformat()}|{','.join(assets)}"
        if narrative and narrative != NO_DATA:
            state.remember("sent_research_fps", [research_fp], timedelta(days=30))
        state.state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        if fear_today is not None:
            state.state["last_fear_greed"] = int(fear_today)
        state.save()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
