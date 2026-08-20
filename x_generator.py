# -*- coding: utf-8 -*-
"""
x_generator.py  —  Gerador de posts prontos para o X (em PT-BR).

Cria 1-2 posts virais a partir do CRUZAMENTO de múltiplas fontes:
  - Noticias cripto (RSS gringo: Cointelegraph, The Defiant, CryptoSlate, CryptoPotato)
  - YouTube (feed oficial dos canais gringos curados)
  - Influencers do X (via RSSHub, best-effort)
  - Sinais de mercado ja usados no bot (CoinGecko trending, Binance funding, preco)

Sem postagem automatica: grava os posts em textos prontos e, opcionalmente,
envia cada um como mensagem separada no Telegram para o usuario revisar e publicar.

Requisitos: apenas stdlib (mesmo padrao do crypto_daily_agent.py).

Uso:
  python x_generator.py                       # gera posts, grava em x_posts/ e imprime
  python x_generator.py --send-telegram       # alem disso, envia cada post no Telegram
  python x_generator.py --dry-run             # nao grava state nem envia Telegram
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ----------------------------------------------------------------------------
# Configuracoes / fontes
# ----------------------------------------------------------------------------

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_FAPI = "https://fapi.binance.com"

NEWS_RSS: list[tuple[str, str]] = [
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("The Defiant", "https://thedefiant.io/feed/"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("CryptoPotato", "https://cryptopotato.com/feed/"),
]

# Canais gringos curados (usuario: galera EUA/gringa com info solida)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

YT_CHANNELS: list[tuple[str, str]] = [
    ("Coin Bureau", "UCqK_GSMbpiV8spgD3ZGloSw"),
    ("Benjamin Cowen", "UCRvqjQPSeaWn-uEx-w0XOIg"),
    ("Glassnode", "UCDq7GjSes-8kQn_Vcg35jfA"),  # on-chain / whale / ETF flows
]

# Influencers do X (best-effort via RSSHub; se falhar, seguem sem eles)
INFLUENCER_X: list[str] = [
    "LynAldenContact",   # Lyn Alden — macro/bitcoin
    "APompliano",        # Anthony Pompliano — macro
    "DefiIgnas",         # DeFi on-chain
    "MilesDeutscher",    # pesquisa/narrativas
    "RyanWatkins_",      # Messari research
    "aeyakovenko",       # Solana co-founder
    "FarsideUK",         # fluxo diario de ETF institucional
    "lookonchain",       # baleias / smart money on-chain
    "glassnode",         # analise on-chain / whale
]

# Lookonchain: feed proprio de baleias/smart money (parseavel sem API)
LOOKONCHAIN_URL = "https://www.lookonchain.com/feeds"
RSSHUB_BASE = "https://rsshub.app"

# Alias para detectar moedas mencionadas em titulos/trechos (canonico -> palavras)
COIN_ALIASES: dict[str, list[str]] = {
    "Bitcoin": ["bitcoin", "btc ", "#btc"],
    "Ethereum": ["ethereum", "eth ", "#eth"],
    "Solana": ["solana", "sol ", "#sol"],
    "Dogecoin": ["dogecoin", "doge"],
    "Shiba Inu": ["shiba", "shib"],
    "Pepe": ["pepe", "$pepe"],
    "XRP": ["xrp"],
    "Cardano": ["cardano", "ada"],
    "BNB": ["bnb"],
    "Chainlink": ["chainlink", "link"],
    "Polygon": ["polygon", "matic"],
    "Avalanche": ["avalanche", "avax"],
    "Bonk": ["bonk", "$bonk"],
    "Dogwifhat": ["wif", "dogwifhat"],
    # Stablecoins / pecas centrais do mundo real
    "USDC": ["usdc", "circle", "usd coin"],
    "Tether": ["tether", "usdt"],
    # Protocolos em destaque (DeFi / infra)
    "Aave": ["aave"],
    "Uniswap": ["uniswap"],
    "Lido": ["lido"],
    "Ethena": ["ethena", "usde"],
    "MakerDAO": ["makerdao", "sky ecosystem"],
    "Pendle": ["pendle"],
    "Hyperliquid": ["hyperliquid", "hype"],
    "EigenLayer": ["eigenlayer", "einstein"],
    "LayerZero": ["layerzero"],
    "Jupiter": ["jupiter", "jup"],
    "Ondo": ["ondo"],  # RWA / real-world assets
    "Sui": ["sui"],
    "TON": ["ton "],
    "Base": ["base chain", "base network"],  # L2
    "Arbitrum": ["arbitrum", "arb "],
}

# Padrao para detectar noticias de captacao / VC funding (onde VCs poe grana)
FUNDING_RE = re.compile(
    r"\b(raises?|raised|funding|fundraise|round|seed|series [ab]|led by|vc\b|venture|"
    r"backed by|secures|invests?|investment|tranche|valuation)\b",
    re.I,
)

# Temas do "mundo cripto" alem de moedas: stablecoins/RWA e captacao/VC
STABLE_RWA_RE = re.compile(
    r"\b(stablecoin|stable coin|usdc|usdt|tether|circle|rwa|real[- ]?world|tokeniz|"
    r"payment|stablecoins|regulation|sec\b|etf|institutional)\b",
    re.I,
)
THEME_LABELS: dict[str, str] = {
    "VC Funding": r"\b(raises?|raised|funding|fundraise|round|seed|series|led by|backed by|vc\b|venture|investment)\b",
    "Stablecoins & RWA": r"\b(stablecoin|stable coin|usdc|usdt|tether|circle|rwa|real[- ]?world|tokeniz|payment)\b",
    "Regulação & Institucional": r"\b(sec\b|etf|regulation|congress|institutional|approval|cftc|court|ruling)\b",
}

# Formulas de gancho (virais). A IA reescreve; isso e fallback/derivacao.
HOOK_FALLBACKS: list[str] = [
    "\U0001f525 {coin} nao esta apenas subindo \u2014 ha um motivo que ninguem esta batendo o olho.",
    "\U0001f4c8 {coin} chamou atencao hoje. Mas o que os dados dizem antes de voce entrar?",
    "\u26a0\ufe0f Todo mundo fala de {coin}, mas ninguem aponta isso:",
    "\U0001f3af {coin}: 3 sinais que se cruzaram hoje apontando o mesmo lado.",
]

DEFAULT_ASSETS = ["bitcoin", "ethereum", "solana"]
NO_DATA = "\u2014 sem dados \u2014"


# ----------------------------------------------------------------------------
# Helpers HTTP / state / parse (mesmo estilo do bot original)
# ----------------------------------------------------------------------------

@dataclass
class ApiResult:
    ok: bool
    status: int | None
    data: Any
    error: str | None = None


def _error_message(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        msg = data.get("error") or data.get("message")
        if isinstance(msg, str) and msg:
            return msg
        detail = data.get("errors")
        if isinstance(detail, list) and detail:
            return str(detail[0])
    return fallback


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 45,
) -> ApiResult:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json", "User-Agent": "XPostGenerator/1.0"}
    req_headers.update(headers or {})
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=req_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return ApiResult(True, response.status, _json_or_text(raw))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return ApiResult(False, exc.code, _json_or_text(raw), _error_message(_json_or_text(raw), str(exc.reason)))
    except URLError as exc:
        return ApiResult(False, None, None, str(exc.reason))
    except TimeoutError:
        return ApiResult(False, None, None, "Request timed out")


def _json_or_text(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    published: str
    kind: str  # news | youtube | influencer


class StateManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            self.state = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"state save failed: {exc}", file=sys.stderr)


def clean_text(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"<!\[CDATA\[|\]\]>", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rss_feed(url: str, source: str, kind: str, limit: int = 15) -> list[NewsItem]:
    result = http_request("GET", url, timeout=30, headers={"Accept": "application/rss+xml,application/xml,text/xml,*/*"})
    if not result.ok or not isinstance(result.data, str):
        return []
    try:
        root = ET.fromstring(result.data)
    except ET.ParseError:
        return []
    items: list[NewsItem] = []
    entries = list(root.iter("item")) + list(root.iter("entry"))
    for entry in entries:
        title = clean_text(next((c.text or "" for c in entry if c.tag.endswith("title")), ""))
        link = next((c.text or "" for c in entry if c.tag in ("link",) ), "")
        if not link:
            for c in entry:
                if c.tag.endswith("link") and c.get("href"):
                    link = c.get("href")
                    break
        pub = next((c.text or "" for c in entry if c.tag.endswith(("pubDate", "published", "updated"))), "")
        items.append(NewsItem(source, title, link, pub, kind))
        if len(items) >= limit:
            break
    return items


def parse_youtube_feed(channel_id: str, channel_name: str, limit: int = 8) -> list[NewsItem]:
    browser_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    attempts: list[tuple[str, str]] = [
        ("oficial", f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"),
        ("rsshub", f"{RSSHUB_BASE}/youtube/channel/{channel_id}"),
    ]
    last_err: str | None = None
    for label, url in attempts:
        result = http_request("GET", url, timeout=30, headers={"Accept": "application/atom+xml,*/*", "User-Agent": browser_ua})
        if not result.ok:
            last_err = f"{label}: status {result.status}"
            continue
        raw = result.data
        if not isinstance(raw, str) or raw.lstrip().startswith(("<html", "<!DOCTYPE")):
            last_err = f"{label}: resposta nao-RSS (bloqueado/renderizado)"
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            last_err = f"{label}: parse falhou"
            continue
        items: list[NewsItem] = []
        for entry in root.iter():
            if not entry.tag.endswith("entry"):
                continue
            title = clean_text(next((c.text or "" for c in entry if c.tag.endswith("title")), ""))
            vid = next((c.text or "" for c in entry if c.tag.endswith("videoId")), "")
            pub = next((c.text or "" for c in entry if c.tag.endswith("published")), "")
            url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
            items.append(NewsItem(channel_name, title, url, pub, "youtube"))
            if len(items) >= limit:
                break
        if items:
            return items
        last_err = f"{label}: feed vazio"
    if last_err:
        print(f"  [yt:{channel_name}] sem videos ({last_err})", file=sys.stderr)
    return []


def parse_twitter_feed(handle: str, limit: int = 8) -> list[NewsItem]:
    url = f"{RSSHUB_BASE}/twitter/user/{handle}"
    result = http_request("GET", url, timeout=25, headers={"Accept": "application/rss+xml,*/*"}, )
    if not result.ok or not isinstance(result.data, str):
        return []
    try:
        root = ET.fromstring(result.data)
    except ET.ParseError:
        return []
    items: list[NewsItem] = []
    for entry in root.iter("item"):
        title = clean_text(next((c.text or "" for c in entry if c.tag.endswith("title")), ""))
        link = next((c.text or "" for c in entry if c.tag.endswith("link") and c.text), "")
        if not link:
            for c in entry:
                if c.tag.endswith("link") and c.get("href"):
                    link = c.get("href")
                    break
        pub = next((c.text or "" for c in entry if c.tag.endswith(("pubDate", "published"))), "")
        # remove o @handle que o RSSHub prefixa no titulo
        title = re.sub(rf"^{re.escape(handle)}:\s*", "", title, flags=re.IGNORECASE)
        items.append(NewsItem(f"@{handle}", title, link, pub, "influencer"))
        if len(items) >= limit:
            break
    return items


def yt_video_id(url: str) -> str | None:
    m = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", url or "")
    return m.group(1) if m else None


def flow_magnitude_usdm(text: str) -> float:
    """Maior valor em USD presente no texto (para ponderar momentum de fluxo/baleia)."""
    t = text.replace("US$", "$")
    mult = {
        "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12,
        "thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12,
    }
    best = 0.0
    for mt in re.finditer(r"(\d+(?:\.\d+)?)\s*(k|m|b|t|million|billion|thousand)", t, re.I):
        try:
            val = float(mt.group(1)) * mult[mt.group(2).lower()]
            if val > best:
                best = val
        except (ValueError, KeyError):
            pass
    for mt in re.finditer(r"\$\s?(\d+(?:\.\d+)?)", t):
        try:
            v = float(mt.group(1))
            if v > best:
                best = v
        except ValueError:
            pass
    return best


def parse_lookonchain(limit: int = 12) -> list[NewsItem]:
    """Feed proprio da Lookonchain (baleias / smart money / fluxos). Parse direto, sem API."""
    result = http_request("GET", LOOKONCHAIN_URL, timeout=30, headers={"User-Agent": BROWSER_UA})
    if not result.ok or not isinstance(result.data, str):
        return []
    txt = unescape(result.data)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = txt.replace("&quot;", '"').replace("&#39;", "'")
    sentences = re.split(r"(?<=[.!])\s+", txt)
    seen: set[str] = set()
    items: list[NewsItem] = []
    keyword = re.compile(r"\b(whale|funds\s+(?:flowed|flow|have flown|into)|inflow|outflow|ETF|liquidat|"
                         r"transferred|short|long|profit|bought|sold|burn|mint)\b", re.I)
    has_money = re.compile(r"(?:US)?\$\s?\d|million|billion|^[0-9.]+ ?[KMBT]")
    for s in sentences:
        s = re.sub(r"\s+", " ", s).strip()
        # remove prefixo de data/hora que vem em alguns feeds da Lookonchain
        s = re.sub(r"^\d{4}\.\d{2}\.\d{2}[ T]\d{2}:\d{2}(:\d{2})?\s*", "", s)
        if not (40 < len(s) < 240):
            continue
        if not keyword.search(s) or not has_money.search(s):
            continue
        key = s.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        items.append(NewsItem("Lookonchain", s, LOOKONCHAIN_URL, "", "flow"))
        if len(items) >= limit:
            break
    return items


def fetch_youtube_transcript(vid: str) -> str | None:
    """Transcricao de um video do YouTube (requer pacote youtube-transcript-api)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        tr = api.fetch(vid)
        return " ".join(s.text for s in tr if s.text)
    except Exception:
        return None


def transcript_excerpt(vid: str, coin: str, max_chars: int = 1800) -> str | None:
    """Trecho relevante da transcricao (foca onde menciona a moeda; senao, abre o video)."""
    full = fetch_youtube_transcript(vid)
    if not full:
        return None
    words = [w.lower() for w in COIN_ALIASES.get(coin, [coin])]
    sentences = re.split(r"(?<=[.!?])\s+", full)
    hits = [s for s in sentences if any(w in s.lower() for w in words)]
    if hits:
        excerpt = " ".join(hits)
    else:
        excerpt = full
    return excerpt[:max_chars]


# ----------------------------------------------------------------------------
# Sinais de mercado (reuso leve das APIs que o bot ja usa)
# ----------------------------------------------------------------------------

def coingecko_trending() -> list[dict[str, Any]]:
    result = http_request("GET", f"{COINGECKO_BASE}/search/trending", timeout=30)
    if not result.ok or not isinstance(result.data, dict):
        return []
    coins = result.data.get("coins") or []
    out: list[dict[str, Any]] = []
    for c in coins:
        item = c.get("item") or {}
        if isinstance(item, dict):
            out.append(
                {
                    "id": item.get("id"),
                    "symbol": str(item.get("symbol", "")).lower(),
                    "name": item.get("name"),
                    "market_cap_rank": item.get("market_cap_rank"),
                }
            )
    time.sleep(1)
    return out


def coingecko_trending_by_id() -> dict[str, str]:
    """Mapa id -> nome para os trenders atuais."""
    return {c["id"]: c["name"] for c in coingecko_trending() if c.get("id")}


def binance_funding(symbol: str) -> float | None:
    url = f"{BINANCE_FAPI}/fapi/v1/fundingRate?symbol={symbol}&limit=3"
    result = http_request("GET", url, timeout=20)
    if not result.ok or not isinstance(result.data, list) or not result.data:
        return None
    rates = [float(r["fundingRate"]) for r in result.data if isinstance(r, dict) and "fundingRate" in r]
    return (sum(rates) / len(rates) * 100) if rates else None


# ----------------------------------------------------------------------------
# Cruzamento de informacoes
# ----------------------------------------------------------------------------

def detect_coins(text: str) -> list[str]:
    """Retorna os nomes canonicos das moedas mencionadas em um texto."""
    low = text.lower()
    found = []
    for canonical, words in COIN_ALIASES.items():
        if any(w.lower() in low for w in words):
            found.append(canonical)
    return found


@dataclass
class Topic:
    coin: str
    scores: dict[str, int]         # fontes -> qtd mencoes
    items: list[NewsItem]          # itens que corroboram o tema
    total_score: int
    trend_rank: int | None = None


def cross_reference(all_items: list[NewsItem], trend_map: dict[str, str]) -> list[Topic]:
    """
    Cruza noticias + youtube + influencers + fluxos (Lookonchain) + trending.
    O score e por MOMENTUM: pesa a magnitude em $ dos fluxos de baleias/institucional,
    a diversidade de fontes e o peso de cada tipo de sinal.
    """
    by_coin: dict[str, dict[str, int]] = {}
    items_by_coin: dict[str, list[NewsItem]] = {}
    momentum: dict[str, float] = {}
    for item in all_items:
        coins = detect_coins(item.title)
        if not coins:
            continue
        w = 1.0
        if item.kind == "flow":
            mag = flow_magnitude_usdm(item.title)
            w = 3.0 if mag < 10_000_000 else (6.0 if mag < 100_000_000 else 10.0)
        elif item.kind == "youtube":
            w = 1.5
        elif item.kind == "influencer":
            w = 1.2
        elif item.kind == "news":
            # noticias de funding/VC (onde os VCs estao colocando grana) ganham peso
            if FUNDING_RE.search(item.title):
                mag = flow_magnitude_usdm(item.title)
                w = 2.5 if mag < 10_000_000 else (3.5 if mag < 100_000_000 else 5.0)
        for coin in coins:
            by_coin.setdefault(coin, {}).setdefault(item.source, 0)
            by_coin[coin][item.source] += 1
            momentum[coin] = momentum.get(coin, 0.0) + w
            items_by_coin.setdefault(coin, []).append(item)

    topics: list[Topic] = []
    for coin, mom in momentum.items():
        scores = by_coin[coin]
        trend_rank = None
        for i, (tid, tname) in enumerate(trend_map.items()):
            if coin.lower() in (str(tname or "").lower(), tid.lower()):
                trend_rank = i + 1
                scores.setdefault("CoinGecko Trending", 1)
                # bonus maior quanto mais topo estiver no trending
                mom += 2.5 / max(1, min(trend_rank, 10))
                break
        if int(mom) <= 0:
            continue
        topics.append(
            Topic(
                coin=coin,
                scores=scores,
                items=sorted(items_by_coin[coin], key=lambda it: it.published, reverse=True),
                total_score=int(mom),
                trend_rank=trend_rank,
            )
        )
    topics.sort(key=lambda t: t.total_score, reverse=True)
    return topics


def build_themes(all_items: list[NewsItem]) -> list[Topic]:
    """
    Temas do 'mundo cripto' que extrapolam moedas: captacao/VC, stablecoins/RWA,
    regulacao/institucional. Entram no ranking de momentum junto com os assuntos por token.
    """
    buckets: dict[str, list[NewsItem]] = {label: [] for label in THEME_LABELS}
    compiled = {label: re.compile(pat, re.I) for label, pat in THEME_LABELS.items()}
    for it in all_items:
        for label, rx in compiled.items():
            if rx.search(it.title):
                buckets[label].append(it)

    out: list[Topic] = []
    for label, its in buckets.items():
        if not its:
            continue
        wsum = 0.0
        srcs: dict[str, int] = {}
        for it in its[:10]:
            base = 1.0
            if it.kind == "flow":
                base = 4.0
            elif it.kind == "news":
                base = 2.0
            mag = flow_magnitude_usdm(it.title)
            if mag >= 100_000_000:
                base += 3.0
            elif mag >= 10_000_000:
                base += 1.5
            wsum += base
            srcs[it.source] = srcs.get(it.source, 0) + 1
        out.append(
            Topic(
                coin=label,
                scores=srcs,
                items=its[:8],
                total_score=min(int(wsum), 40),  # cap p/ nao dominar so por agregacao
                trend_rank=None,
            )
        )
    out.sort(key=lambda t: t.total_score, reverse=True)
    return out


def merge_and_sort(*lists: list[Topic]) -> list[Topic]:
    merged: list[Topic] = []
    for lst in lists:
        merged.extend(lst)
    merged.sort(key=lambda t: t.total_score, reverse=True)
    return merged


# ----------------------------------------------------------------------------
# Geracao de post (IA com fallback por template)
# ----------------------------------------------------------------------------

def build_context(topic: Topic, trend_map: dict[str, str]) -> str:
    lines = [f"MOEDA: {topic.coin}"]
    lines.append(f"CONFLUENCIA: {topic.total_score} mencoes em {len(topic.scores)} fontes independentes.")
    if topic.trend_rank:
        lines.append(f"ESTA NO TRENDING do CoinGecko (posicao #{topic.trend_rank}).")
    src = ", ".join(f"{s} ({n}x)" for s, n in sorted(topic.scores.items(), key=lambda kv: -kv[1]))
    lines.append(f"FONTES QUE FALAM DISSO: {src}")
    lines.append("HEADLINES / TWEETS / VIDEOS DE HOJE:")
    for it in topic.items[:6]:
        tag = {"news": "NOTICIA", "youtube": "VIDEO", "influencer": "TWEET", "flow": "FLUXO"}[it.kind]
        lines.append(f"[{tag}/{it.source}] {it.title}  ({it.url})")

    # Transcricao real dos videos do YouTube que falam da moeda
    yt_done = 0
    for it in topic.items:
        if it.kind != "youtube":
            continue
        vid = yt_video_id(it.url)
        if not vid:
            continue
        excerpt = transcript_excerpt(vid, topic.coin)
        if excerpt:
            lines.append(f"\nTRANSCRICAO DE [{it.source}] '{it.title}':")
            lines.append(excerpt)
            yt_done += 1
            if yt_done >= 2:
                break

    lines.append("\nRegra de ouro: NAO inventar numeros. Se nao tiver numero, nao poe numero. Cite a fonte.")
    return "\n".join(lines)


def llm_chat(env: dict[str, str], system: str, user: str) -> str | None:
    """Tenta LLM OpenAI-compatible (OPENROUTER, varios modelos), depois Messari AI, senao None."""
    key = env.get("OPENROUTER_API_KEY")
    if key:
        models = [
            env.get("X_POST_MODEL"),
            "openai/gpt-4o-mini",
            "meta-llama/llama-3.3-70b-instruct:free",
            "openai/gpt-3.5-turbo",
        ]
        models = [m for m in models if m]
        for model in models:
            result = http_request(
                "POST",
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                payload={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.85,
                },
                timeout=60,
            )
            if result.ok and isinstance(result.data, dict):
                content = (result.data.get("choices") or [{}])[0].get("message", {}).get("content")
                if content and str(content).strip():
                    return str(content).strip()
                # sucesso mas vazio -> tenta proximo
                print(f"  [openrouter:{model}] resposta vazia", file=sys.stderr)
                continue
            detail = result.error or f"status {result.status}"
            # tenta extrair mensagem do corpo de erro
            if isinstance(result.data, dict):
                err = result.data.get("error") or {}
                if isinstance(err, dict):
                    detail = err.get("message") or detail
            print(f"  [openrouter:{model}] falhou: {detail}", file=sys.stderr)

    messari_key = env.get("MESSARI_API_KEY")
    if messari_key:
        result = http_request(
            "POST",
            "https://api.messari.io/ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {messari_key}"},
            payload={
                "messages": [{"role": "user", "content": f"{system}\n\n{user}"}],
                "verbosity": "balanced",
                "response_format": "markdown",
                "stream": False,
            },
        )
        if result.ok and isinstance(result.data, dict):
            content = (result.data.get("choices") or [{}])[0].get("message", {}).get("content")
            if content and str(content).strip():
                return str(content).strip()
        print(f"  [messari ai] falhou: {result.error}", file=sys.stderr)
    return None


SYSTEM_PROMPT = (
    "You write short, human, native-English X posts for a crypto account (@bpweb33), global audience.\n\n"
    "CONTENT RULES (most important):\n"
    "- LEAD WITH A REAL, SPECIFIC FACT from the context: a price, a USD amount, a whale/flows move, "
    "a funding round, a protocol launch, a number. Be concrete.\n"
    "- NEVER write meta-filler such as 'X is all over the feed today', 'it shows up in N sources', "
    "'this never happens by chance', 'worth asking what everyone is missing', 'I like the coin'.\n"
    "- Sound like a person who noticed ONE concrete thing and shares it with a light, genuine opinion.\n"
    "- Use only facts that appear in the context. If there's no solid fact, reply exactly: SKIP\n\n"
    "AUTHOR VOICE (imitate this person's writing, from their real posts):\n"
    "It's interesting how @federalreserve meetings become so significant during a bull market or a heated market...\n"
    "\n"
    "We had a meeting today, yet there wasn't even a ripple of movement regarding that.\n"
    "\n"
    "I absolutely love BTC, but this level of speculation still worries me\n"
    "-> Traits to mirror: curious and observational, honest ('I absolutely love BTC, but...'), a touch of "
    "worry/caution, plain words, concise. Write like a thoughtful trader talking to a friend, not a news anchor.\n\n"
    "EXAMPLE of the right tone (facts first):\n"
    "Almost every on-chain tracker logged the same thing an hour ago...\n"
    "\n"
    "A whale that was dormant for 600 days just moved $52M in PEPE to a new address.\n"
    "\n"
    "I'll be watching whether it hits an exchange\n\n"
    "FORMAT (follow precisely, the author's style):\n"
    "- Exactly 3 short paragraphs, each separated by a BLANK line.\n"
    "- Para 1: a hook / open-ended observation ENDING WITH '...'\n"
    "- Para 2: the concrete fact in one clean line ENDING WITH '.'\n"
    "- Para 3: a short personal take ENDING WITH NO punctuation.\n"
    "- Max ~270 characters (free X plan). No emojis, no hashtags, no ALL-CAPS."
)


_MID_VARIANTS = [
    "That kind of move is worth a closer look.",
    "This is the kind of thing that moves a market before the headlines do.",
    "Numbers like this are why I watch the flow and not the noise.",
    "The data is the real story here, more than the narrative.",
    "A move this size gets my attention before I trust any take.",
]
_TAIL_VARIANTS = [
    "I'd rather follow the flow than the hype for {name}",
    "I watch moves like this before I ever buy the story for {name}",
    "For {name}, I trust the numbers over the chatter",
    "I'll be watching whether {name} can hold this",
]


def _human_name(topic: Topic) -> str:
    if topic.trend_rank is None and topic.coin in THEME_LABELS:
        return {
            "VC Funding": "capital flows",
            "Stablecoins & RWA": "the stablecoin economy",
            "Regulação & Institucional": "the institutional shift",
        }.get(topic.coin, "the space")
    return topic.coin


def template_post(topic: Topic) -> str:
    """Fallback sem IA: usa FATO real do topico, com frases variadas (sem repetir entre posts)."""
    best = None
    bm = -1.0
    for it in topic.items:
        mag = flow_magnitude_usdm(it.title)
        if mag > bm:
            best, bm = it, mag
    name = _human_name(topic)
    h = 0
    for ch in topic.coin:
        h = (h * 31 + ord(ch)) % 100000
    mid = _MID_VARIANTS[h % len(_MID_VARIANTS)]
    tail = _TAIL_VARIANTS[(h // 7) % len(_TAIL_VARIANTS)].format(name=name)
    if best is not None and bm > 0:
        return f"{best.title.rstrip().rstrip('.')}...\n\n{mid}\n\n{tail}"
    item = topic.items[0] if topic.items else None
    if item:
        return f"{item.title.rstrip().rstrip('.')}...\n\nCaught my attention around {name}.\n\n{tail}"
    return f"{name} just made a move worth noticing...\n\nChecking the data before I say anything clever.\n\nI'll wait for confirmation"


def generate_posts(topics: list[Topic], env: dict[str, str], limit: int = 2) -> list[tuple[Topic, str]]:
    posts: list[tuple[Topic, str]] = []
    for topic in topics[:limit * 3]:  # tenta um pouquinho alem, por causa de SKIPs
        ctx = build_context(topic, {})
        text = llm_chat(
            env,
            SYSTEM_PROMPT,
            "Contexto (so dados reais):\n" + ctx
            + "\n\nEscreva 1 post pronto (apenas o texto do post, sem introducao, sem aspas, sem hashtags excessivas).",
        )
        if text is None:
            text = template_post(topic)
        elif text.strip().upper() == "SKIP":
            continue
        text = text.strip().strip('"').strip()
        if len(text) > 300:
            text = text[:297].rstrip() + "…"
        posts.append((topic, text))
        if len(posts) >= limit:
            break
    return posts


# ----------------------------------------------------------------------------
# Entrega / main
# ----------------------------------------------------------------------------

def send_telegram_blocks(env: dict[str, str], blocks: list[str]) -> bool:
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN/CHAT_ID ausente.", file=sys.stderr)
        return False
    ok_all = True
    for i, block in enumerate(blocks, 1):
        header = f"\U0001f4ac POST {i} para o X @bpweb33 — revisa e publica:\n\n"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": header + block, "disable_web_page_preview": False}
        result = http_request("POST", url, payload=payload)
        if not result.ok:
            ok_all = False
            print(f"  [telegram] post {i} falhou: {result.error}", file=sys.stderr)
        time.sleep(1)
    return ok_all


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera posts prontos para o X (PT-BR) cruzando fontes.")
    parser.add_argument("--send-telegram", action="store_true", help="Envia cada post como mensagem separada no Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="Nao grava state nem envia Telegram.")
    parser.add_argument("--limit", type=int, default=2, help="Quantos posts gerar (padrao 2).")
    parser.add_argument("--output-dir", default="x_posts", help="Onde salvar os posts (.md e .json).")
    parser.add_argument("--state-file", default="state/x_generator_state.json", help="State p/ deduplicacao.")
    parser.add_argument("--include-x", action="store_true", help="Inclui influencers do X (via RSSHub, pode falhar).")
    return parser.parse_args()


def main() -> int:
    load_env_file(Path(".env"))
    args = parse_args()
    env = dict(os.environ)

    state = StateManager(Path(args.state_file))

    print("Coletando fontes...")
    all_items: list[NewsItem] = []
    for name, url in NEWS_RSS:
        items = parse_rss_feed(url, name, "news")
        print(f"  [news] {name}: {len(items)} titulos")
        all_items.extend(items)
    for cname, cid in YT_CHANNELS:
        items = parse_youtube_feed(cid, cname)
        print(f"  [yt] {cname}: {len(items)} videos")
        all_items.extend(items)
    flow_items = parse_lookonchain()
    print(f"  [flow] Lookonchain: {len(flow_items)} movimentos de baleia/fluxo")
    all_items.extend(flow_items)
    if args.include_x:
        for handle in INFLUENCER_X:
            items = parse_twitter_feed(handle)
            print(f"  [x] @{handle}: {len(items)} tweets")
            all_items.extend(items)

    print("Sinais de mercado (CoinGecko trending)...")
    trend_map = coingecko_trending_by_id()
    print(f"  [cg] {len(trend_map)} moedas em trending")

    if not all_items:
        print("Nenhuma fonte retornou dados. Verifique a rede.", file=sys.stderr)
        return 1

    topics = merge_and_sort(cross_reference(all_items, trend_map), build_themes(all_items))

    if not topics:
        print("Nenhum tema com confluencia suficiente hoje.", file=sys.stderr)
        return 2

    print("\nTop temas (confluencia):")
    for t in topics[:8]:
        rank = f" | trending #{t.trend_rank}" if t.trend_rank else ""
        print(f"  {t.total_score} pts {t.coin}{rank}: {', '.join(f'{k} {v}x' for k, v in sorted(t.scores.items(), key=lambda kv: -kv[1]))}")

    posts = generate_posts(topics, env, args.limit)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    md_path = out_dir / f"posts_{stamp}.md"
    json_path = out_dir / f"posts_{stamp}.json"

    md_lines = [f"# Posts para X (@bpweb33) — {stamp}\n"]
    payload_saved = []
    blocks: list[str] = []
    for i, (topic, text) in enumerate(posts, 1):
        md_lines.append(f"## POST {i} — {topic.coin}\n")
        md_lines.append(text + "\n")
        md_lines.append("**Fontes/fatos:**")
        for it in topic.items[:4]:
            md_lines.append(f"- [{it.source}] {it.title} — {it.url}")
        md_lines.append("")
        blocks.append(text)
        payload_saved.append({"coin": topic.coin, "post": text, "sources": [it.url for it in topic.items[:4]]})

    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    json_path.write_text(json.dumps(payload_saved, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nPosts salvos em {md_path}")

    if args.send_telegram:
        if args.dry_run:
            print("Dry run: Telegram nao enviado.")
        else:
            ok = send_telegram_blocks(env, blocks)
            if not ok:
                print("Entrega parcial/falha no Telegram.", file=sys.stderr)
                return 4

    if not args.dry_run:
        state.state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        state.state["last_topics"] = [{"coin": t.coin, "score": t.total_score} for t in topics[:8]]
        state.save()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
