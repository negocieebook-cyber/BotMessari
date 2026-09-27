# -*- coding: utf-8 -*-
"""
x_generator.py  —  Gerador de posts prontos para o X (inglês, voz autoral, com discernimento editorial).

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
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any

from lib.common import (
    ApiResult,
    StateManager,
    clean_text,
    env_status,
    env_value,
    http_request,
    load_env_file,
    single_instance_lock,
)

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

# desativa a OpenRouter dentro da execucao se a chave responder 401/403
_OPENROUTER_DISABLED = False


# ----------------------------------------------------------------------------
# Helpers HTTP / state / parse (mesmo estilo do bot original)
# ----------------------------------------------------------------------------

# Executavel do Hermes Agent (agente local com modelo proprio). Pode ser
# sobrescrito com HERMES_BIN no .env. Se nao existir, o adaptador e pulado.
_HERMES_CANDIDATES = [
    shutil.which("hermes"),
    Path.home() / "AppData/Local/hermes/hermes-agent/.hermes/bin/hermes.exe",
    Path.home() / ".hermes/bin/hermes",
]
_HERMES_BIN = next((c for c in _HERMES_CANDIDATES if c and Path(c).exists()), None)


def hermes_chat(env: dict[str, str], system: str, user: str) -> str | None:
    """Usa o Hermes Agent local (hermes --cli -z) como LLM. Grátis, sem chave externa.

    Retorna None se o binario nao existir, se HERMES_DISABLED=1 ou em caso de falha.
    """
    global _HERMES_BIN
    if os.environ.get("HERMES_DISABLED", "").strip() == "1":
        return None
    if _HERMES_BIN is None:
        override = (env.get("HERMES_BIN") or "").strip()
        if override and Path(override).exists():
            _HERMES_BIN = override
        else:
            return None
    prompt = f"{system}\n\n---\n\n{user}"
    try:
        completed = subprocess.run(
            [str(_HERMES_BIN), "--cli", "-z", prompt],
            capture_output=True,
            text=True,
            timeout=240,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [hermes] falhou: {type(exc).__name__}", file=sys.stderr)
        return None
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        print(f"  [hermes] exit {completed.returncode}: {detail[-1] if detail else 'sem detalhe'}", file=sys.stderr)
        return None
    text = (completed.stdout or "").strip()
    if not text:
        return None
    return text


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    published: str
    kind: str  # news | youtube | influencer


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
                items=sort_items_by_recency(items_by_coin[coin]),
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
    """Tenta Hermes local, depois OpenRouter, depois Messari AI; None se nada responder."""
    global _OPENROUTER_DISABLED
    hermes_text = hermes_chat(env, system, user)
    if hermes_text:
        return hermes_text

    key = env.get("OPENROUTER_API_KEY")
    if key and not _OPENROUTER_DISABLED:
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
            if result.status in (401, 403):
                # chave invalida: nao tenta de novo nesta rodada (nem nos proximos posts)
                _OPENROUTER_DISABLED = True
                print(f"  [openrouter] chave invalida ({result.status}); desativada nesta execucao", file=sys.stderr)
                break
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
    "You are the editor and writer of a personal crypto X account (@bpweb33), global audience, "
    "native English. You are a person who trades and watches the tape every day, writing for "
    "people who follow you because you share real data, not hype. Your job is discernment: from "
    "the real facts below, find the one worth sharing and write it like a human being.\n\n"
    "WHAT THIS ACCOUNT OFFERS:\n"
    "- A real, specific data point and the honest reading of it. That is the whole product.\n"
    "- Share the data like a peer, not a guru: here is what I saw, here is what it does and does "
    "not tell us, here is what I am watching next.\n"
    "- Say clearly when something is unknowable from the data. 'This could be rotation or it "
    "could be exit' is a strong, honest line. Uncertainty is not weakness here.\n"
    "- If two facts in the context pull in different directions, that tension is the post.\n\n"
    "NEVER PROMISE. NEVER PUMP. Specifically, never write:\n"
    "- Any price prediction, target, or implied direction of return ('will rally', 'set to pump', "
    "'next leg', 'going to explode', 'price target').\n"
    "- FOMO or urgency ('before it's too late', 'don't miss', 'last chance', 'getting in early', "
    "'if you're not stacking').\n"
    "- Investment advice or sizing ('buy the dip', 'I'm all in', 'loaded up', 'back up the truck').\n"
    "- Certainty you do not have. If the data does not tell you why, say so.\n"
    "- Engagement bait ('what do you think?', 'are you bullish?', 'follow for more', 'link in bio', "
    "'drop your take'). End with substance, not a hook begging for replies.\n\n"
    "CONTENT RULES:\n"
    "- Anchor the post in ONE real, specific fact from the context (a USD amount, a whale move, "
    "a funding round, a ruling, a price level). Use exact figures only from the context. No number "
    "in the context -> no number in the post.\n"
    "- The first sentence must do real work: either the sharpest fact or a straight reaction to it. "
    "Never open with a formal or generic line.\n"
    "- Never mention internal scoring, source counts, 'confluence', or how many feeds mentioned something.\n"
    "- THE GENERIC-SENTENCE TEST: if a sentence would still make sense under a different story about "
    "a different coin, delete it and write something that only fits THIS story.\n"
    "- Plain words, short sentences, contractions welcome. Write the way you would text a friend "
    "who also trades. It is fine to end with what you are watching or a real open question.\n\n"
    "BANNED filler (never write these or their close siblings):\n"
    "'all over the feed', 'worth a closer look', 'gets my attention', 'before I trust any take', "
    "'markets price', 'real money tends to', 'rarely noise', 'worth noticing', 'worth noting', "
    "'worth asking', 'the question now', \"I'll be watching\", \"I'll keep checking\", "
    "'only time will tell', 'what everyone is missing', 'trust the numbers over the chatter', "
    "'this never happens by chance', 'I'd rather follow the flow', 'before the narrative does', "
    "'decides what comes next', 'carry a reason before', 'show up before', 'the trail decides', "
    "'the signal for what's next', 'not a big narrative', 'is where the real'.\n\n"
    "VOICE to mirror (from the author's real posts):\n"
    "It's interesting how @federalreserve meetings become so significant during a bull market or a heated market...\n"
    "\n"
    "We had a meeting today, yet there wasn't even a ripple of movement regarding that.\n"
    "\n"
    "I absolutely love BTC, but this level of speculation still worries me\n"
    "Traits: observant, honest about doubt, a little skeptical, concise, no hype words.\n\n"
    "VARY THE SHAPE (never the same skeleton twice in one run):\n"
    "a) fact first, then what it does and does not tell you\n"
    "b) short reaction first ('Honestly...', 'Not sure how to read this'), then the fact\n"
    "c) the fact, then the honest fork: the two things it could mean\n"
    "d) two facts from the context, then the tension between them\n\n"
    "DISCUSSION HOOK (only when the user prompt asks for one, roughly every other post):\n"
    "- End with a real invitation to disagree: a specific question or a mildly provocative take "
    "you are willing to defend about THIS story.\n"
    "- The hook must be story-specific ('Where does this land next - exchange, or cold storage?', "
    "'I don't buy the panic read here. Tell me what I'm missing.') -- never generic ('thoughts?', "
    "'what do you think?', 'are you bullish?').\n"
    "- Provocative is fine. Reckless is not: the hook can challenge a narrative, never promise a "
    "direction.\n\n"
    "FORMAT: ~270 characters total (free X plan), up to 3 short paragraphs separated by BLANK lines, "
    "every paragraph a complete thought with a clean ending (no dangling '...'). English only. "
    "No emojis, no hashtags, no ALL-CAPS.\n\n"
    "EXAMPLE of the right tone:\n"
    "Almost every on-chain tracker logged the same thing an hour ago.\n\n"
    "A whale that sat still for 600 days just moved $52M in PEPE to a fresh address.\n\n"
    "No exchange deposit yet. I can't tell you why from here, but the trail is public if you want to follow it.\n"
)


# Frases-coringa que matam a voz humana. Usadas para (a) proibir o modelo de repetir
# e (b) gate de qualidade pós-geração com 1 re-geração.
FILLER_PATTERNS = [
    r"all over the feed",
    r"worth a closer look",
    r"i'?ll be watching",
    r"trust the numbers over",
    r"never happens by chance",
    r"everyone is missing",
    r"only time will tell",
    r"worth asking",
    r"caught my attention",
    r"worth noticing",
    r"worth noting",
    r"gets? my attention",
    r"before i trust any take",
    r"real money tends to",
    r"rarely noise",
    r"decides what comes next",
    r"the question now",
    r"the part that matters",
    r"i'?d rather follow the flow",
    r"before the narrative does",
    r"carry a reason before",
    r"show up before the narrative",
    r"gets? my attention before",
    r"i watch moves like this",
    # promessa / pump / conselho financeiro / engagement bait
    r"price target",
    r"to the moon",
    r"before it'?s too late",
    r"don'?t miss",
    r"last chance",
    r"getting in early",
    r"next (leg|stop)",
    r"set to (pump|rally|explode)",
    r"going to (explode|moon|pump|rally)",
    r"will (explode|moon|pump)",
    r"i'?m all in",
    r"loaded up",
    r"back up the truck",
    r"buy the dip",
    r"guaranteed",
    r"are you (bullish|bearish|in\b)",
    r"what do you think",
    r"follow for more",
    r"link in bio",
    r"drop your",
    r"insiders? are (loading|accumulating|buying)",
    r"you'?re not stacking",
    r"opportunity of a lifetime",
    r"the trail decides",
    r"signal for what'?s next",
    r"not a big narrative",
    # vazamento de metadados internos (score/confluencia/fontes) que nunca podem ir no post
    r"\d+\s*pts?\b",
    r"\d+\s*sources?\b",
    r"independent sources",
    r"confluenc",
    r"\bfontes\b",
    r"fontes independentes",
    # resto de marcador/junk que o modelo as vezes cospe
    r"\[/?\w+\]",
    r"\b##\b",
    r"\bpost\s*\d\b",
    r"fontes/fatos",
]


def has_filler(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(p, low) for p in FILLER_PATTERNS)


def _dangling_end(text: str) -> bool:
    """Post terminando em reticencia no vacuo ('If...', 'keeping an...') = frase cortada."""
    t = (text or "").rstrip()
    return t.endswith("…") or t.endswith("...")


def _strip_dangling_ellipsis(text: str) -> str:
    t = text.rstrip()
    for sep in ("…", "..."):
        if t.endswith(sep):
            t = t[: -len(sep)].rstrip()
            break
    return t


def _human_name(topic: Topic) -> str:
    if topic.trend_rank is None and topic.coin in THEME_LABELS:
        return {
            "VC Funding": "capital flows",
            "Stablecoins & RWA": "the stablecoin economy",
            "Regulação & Institucional": "the institutional shift",
        }.get(topic.coin, "the space")
    return topic.coin


def _best_flow_item(topic: Topic) -> NewsItem | None:
    best = None
    bm = -1.0
    for it in topic.items:
        mag = flow_magnitude_usdm(it.title)
        if mag > bm:
            best, bm = it, mag
    if best is not None and bm > 0:
        return best
    return topic.items[0] if topic.items else None


_TIME_TAIL = re.compile(r"(?:\s*\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?|days?|weeks?)\s*ago|\s*\bago|\s*\d+)$", re.I)


def _compact_fact(item: NewsItem) -> str:
    """Titular limpa e curta: corta em elemento completo (parenteses/clausula),
    remove fragmentos de tempo soltos e fecha aspas penduradas."""
    t = clean_text(item.title)
    t = re.sub(r"\s+", " ", t).strip().strip('"').strip()
    t = t.rstrip(".!?…")
    if len(t) > 150:
        window = t[:150]
        cut = -1
        for sep in (") ", ", ", " - ", "; ", ". "):
            c = window.rfind(sep)
            if c >= 60 and c > cut:
                cut = c
        if cut >= 0:
            t = window[: cut + 1]  # mantem o separador completo (")", ",", "-")
        else:
            t = window.rsplit(" ", 1)[0]
    # fragmentos de tempo soltos (ex: "... million) 9 hours ago" cortado no limite)
    while True:
        m = _TIME_TAIL.search(t)
        if not m or not m.group(0):
            break
        match_text = m.group(0).strip()
        if match_text.isdigit() and m.start() > 0 and t[m.start() - 1] in (",", "."):
            break  # numero legitimo de milhar/decimal no fim, nao fragmento
        t = t[: m.start()].rstrip(" ,;-(")
    # parenteses/aspas abertas e nunca fechadas = fragmento inutil, remove
    if t.count("(") > t.count(")"):
        t = t[: t.rfind("(")].rstrip(" ,;-")
    if t.count('"') % 2 == 1:
        t = t[: t.rfind('"')].rstrip(" ,;-")
    t = t.rstrip(",;-: ")
    if t:
        t = t[0].upper() + t[1:]
        t += "."
    return t


# Observacoes de acompanhamento por tipo de fonte. Honestas sobre incerteza,
# sem promessa, especificas o suficiente para sobreviver ao teste de genericidade.
_FOLLOWUPS_BY_KIND: dict[str, list[str]] = {
    "flow": [
        "Doesn't tell you why yet. The deposit address will, when it moves again.",
        "Could be rotation, could be exit. I can't tell you which from here.",
        "No exchange deposit so far. The trail is public if you want to follow it.",
    ],
    "news": [
        "Second read on this will matter more than the first.",
        "Counterparties react slower than headlines. Watch what they actually do.",
        "The text is one thing. Who has to comply with it is the other.",
    ],
    "youtube": [
        "Video takes move faster than data confirms.",
        "Thumbnails argue. Charts settle.",
    ],
    "influencer": [
        "Big accounts talk. Wallets tell.",
        "Take it as sentiment data, not as a signal.",
    ],
}


# Ganchos de discussao por tipo de fonte: pergunta/take provocador porem
# especifico da historia, sem promessa. Usados alternadamente, nao em todo post.
_HOOKS_BY_KIND: dict[str, list[str]] = {
    "flow": [
        "Where does this land next - exchange, or back to cold storage?",
        "If this is a plan, it's a patient one. Anyone reading it differently?",
        "I don't see panic in an address like this. Tell me what I'm missing.",
    ],
    "news": [
        "Who actually gets squeezed if this holds?",
        "The size is the headline. The counterparty is the part nobody is watching yet.",
        "I read this as slow-motion, not breaking news. Disagree?",
    ],
    "youtube": [
        "The tape disagrees with the timeline. Who do you trust more?",
        "Bold claims from big channels. The chart will say who was right.",
    ],
    "influencer": [
        "Big accounts talk. Wallets tell. I know which one I watch.",
        "Louder than the data right now. That usually evens out.",
    ],
}


def template_post(
    topic: Topic,
    used_followups: set[str] | None = None,
    with_hook: bool = False,
) -> str:
    """Fallback sem IA: fato real do topico + observacao honesta (sem promessa) por tipo de fonte.

    `used_followups` evita repetir observacao na mesma rodada; `with_hook` troca a
    observacao por um gancho de discussao (alternado, nao em todo post).
    """
    used_followups = used_followups if used_followups is not None else set()
    best = _best_flow_item(topic)
    name = _human_name(topic)
    if best is None:
        return (
            f"{name} keeps moving without a clean reason on the tape.\n\n"
            "I'd rather wait for the numbers than guess the story."
        )
    h = 0
    for ch in topic.coin:
        h = (h * 31 + ord(ch)) % 100000
    pool = _HOOKS_BY_KIND if with_hook else _FOLLOWUPS_BY_KIND
    followups = pool.get(best.kind, pool["news"])
    ordered = followups[h % len(followups):] + followups[: h % len(followups)]
    followup = next((f for f in ordered if f not in used_followups), ordered[0])
    used_followups.add(followup)
    post = f"{_compact_fact(best)}\n\n{followup}"
    return truncate_post(post)


def _best_fact_line(topic: Topic) -> str:
    """Primeira headline concreta do topico, p/ o menu do editor."""
    for it in topic.items[:6]:
        if it.title.strip():
            return it.title.strip()[:160]
    return "no concrete headline captured"


def pick_topics_via_llm(env: dict[str, str], topics: list[Topic], want: int = 2, k: int = 6) -> list[int]:
    """
    Camada de DISCERNIMENTO: em vez de postar so o mais-citado (score de confluencia),
    deixa o LLM escolher QUAIS historias valem post, por substancia e interesse.
    Se nao houver chave de LLM ou vier resposta invalida, retorna [] (fallback = ordem de score).
    """
    if not (env.get("OPENROUTER_API_KEY") or env.get("MESSARI_API_KEY")):
        return []
    if not topics:
        return []
    k = max(1, min(k, len(topics)))
    want = max(1, want)
    menu = []
    for i, t in enumerate(topics[:k], 1):
        menu.append(f"{i}. {t.coin} ({t.total_score}pts, {len(t.scores)} fontes) -- {_best_fact_line(t)}")
    editor_system = (
        "You are the editor for a crypto X account. Below is a ranked list of candidate stories "
        "from today's independent feeds (news, on-chain whale/flows, YouTube). The rank reflects "
        "how often each coin was mentioned -- IGNORE rank as a quality signal. Pick the candidates "
        "that are GENUINELY worth a post: specific, concrete, surprising, with a real fact behind "
        "them. Thin or generic candidates are not publishable just because they were mentioned a lot.\n\n"
        f"CANDIDATES:\n{chr(10).join(menu)}\n\n"
        f"Reply with ONLY the numbers you want, comma-separated, most interesting first (e.g. '3, 5'). "
        "Pick no more than " + str(want) + ". If fewer than one candidate is solid and specific, reply exactly: SKIP"
    )
    resp = llm_chat(env, editor_system, "Choose which candidates deserve a post.")
    if not resp:
        return []
    if resp.strip().upper() == "SKIP":
        return []
    nums: list[int] = []
    for tok in re.split(r"[,\s;/]+", resp):
        tok = tok.strip()
        if tok.isdigit() and 1 <= int(tok) <= k:
            cand = int(tok) - 1
            if cand not in nums:
                nums.append(cand)
    return nums[:want]


def truncate_post(text: str, hard: int = 297) -> str:
    """Ultimo recurso p/ post longo: corta no fim de uma frase COMPLETA (ponto),
    sem injetar reticencia pendurada nem cortar palavra no meio."""
    t = text.rstrip()
    if len(t) <= hard:
        return t
    window = t[:hard]
    for sep in (". ", ".\n", "? ", "! "):
        cut = window.rfind(sep)
        if cut >= int(hard * 0.5):
            return t[: cut + 1].rstrip()
    sp = window.rfind(" ")
    return (window[:sp] if sp > 0 else window).rstrip()


def generate_posts(topics: list[Topic], env: dict[str, str], limit: int = 2) -> list[tuple[Topic, str]]:
    posts: list[tuple[Topic, str]] = []
    previous: list[str] = []

    def build_user_prompt(ctx: str, retry_note: bool) -> str:
        vary = ""
        if previous:
            vary = (
                "\n\nPosts already written this run (do NOT reuse their skeleton, opener, "
                "or phrasing):\n" + "\n".join(f"- {p}" for p in previous)
            )
        if retry_note:
            return (
                "Contexto (so dados reais):\n" + ctx + vary
                + "\n\nYour previous draft had a problem: too long, empty filler, dangling ending, "
                  "a sentence that would fit any other story, or (worst of all) a promise/prediction/urgency "
                  "line that this account never makes. Rewrite the SAME idea tighter: UNDER 270 characters, "
                  "no sentence that could apply to a different coin, real numbers from the context only, "
                  "no price predictions, no FOMO, no advice, no engagement-bait questions -- just the data "
                  "and the honest reading. Every paragraph a complete thought with a clean ending, shape "
                  "different from any previous post. Post text only. If you still cannot, reply exactly: SKIP"
            )
        return (
            "Contexto (so dados reais):\n" + ctx + vary
            + "\n\nWrite 1 finished post now. Output the post text only -- no intro, no quotes, no labels."
        )

    # Escolha editorial (discernimento): LLM elege os temas por interesse/substancia.
    order = pick_topics_via_llm(env, topics, want=limit, k=min(6, len(topics)))
    if not order:
        # sem chave/LLM indisponivel -> ordem de confluencia (fallback original)
        order = list(range(min(len(topics), limit * 3)))

    used_followups: set[str] = set()
    post_position = 0
    for idx in order:
        if idx >= len(topics) or len(posts) >= limit:
            continue
        topic = topics[idx]
        with_hook = post_position % 2 == 1  # provocacao alternada: nem todo post
        hook_note = (
            "\n\nEnd this post with the DISCUSSION HOOK: one specific question or mildly provocative "
            "take about THIS story, per the system rules. No generic hooks."
            if with_hook
            else ""
        )
        ctx = build_context(topic, {})
        text = llm_chat(env, SYSTEM_PROMPT, build_user_prompt(ctx, retry_note=False) + hook_note)
        if text is None:
            text = template_post(topic, used_followups, with_hook=with_hook)
        elif text.strip().upper() == "SKIP":
            continue
        text = text.strip().strip('"').strip()

        if has_filler(text) or _dangling_end(text) or len(text) > 285:
            # gate de qualidade: 1 re-geracao com regras mais duras
            retry = llm_chat(env, SYSTEM_PROMPT, build_user_prompt(ctx, retry_note=True) + hook_note)
            if retry and retry.strip().upper() != "SKIP":
                text = retry.strip().strip('"').strip()

        # rede de seguranca final: nada de reticencia pendurada, e corte sem dano (sem "...")
        text = _strip_dangling_ellipsis(text)
        text = truncate_post(text)
        text = _strip_dangling_ellipsis(text)
        previous.append(text.replace("\n\n", " / ")[:140])
        posts.append((topic, text))
        post_position += 1
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
    parser = argparse.ArgumentParser(description="Gera posts prontos para o X (inglês, voz autoral) cruzando fontes.")
    parser.add_argument("--send-telegram", action="store_true", help="Envia cada post como mensagem separada no Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="Nao grava state nem envia Telegram.")
    parser.add_argument("--limit", type=int, default=2, help="Quantos posts gerar (padrao 2).")
    parser.add_argument("--output-dir", default="x_posts", help="Onde salvar os posts (.md e .json).")
    parser.add_argument("--state-file", default="state/x_generator_state.json", help="State p/ deduplicacao.")
    parser.add_argument("--include-x", action="store_true", help="Inclui influencers do X (via RSSHub, pode falhar).")
    return parser.parse_args()



# ----------------------------------------------------------------------------
# Deduplicacao por historia (evita repostar o mesmo fato/coin dias a fio).
# A comparacao e por SIMILARIDADE de tokens (sem considerar a fonte), entao a
# mesma historia contada por outlets diferentes tambem e detectada.

STOP_TOKENS = frozenset(
    """the and for with from this that will has have its are was were after over into
    out new now just get got all one two amid via more most than then them they
    whats today report reports news says said as at by on in of to a an is be it""".split()
)

_SIMILARITY_THRESHOLD = 0.65


def _title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9$#]+", (title or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOP_TOKENS}


def _story_fingerprint(item: NewsItem) -> str:
    return " ".join(sorted(_title_tokens(item.title)))


def _title_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    if tokens_a == tokens_b:
        return 1.0
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))


def _within_hours(ts: str, now: datetime, hours: int) -> bool:
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return False
    return (now - t) < timedelta(hours=hours)


def _recent_stories(state: StateManager, hours: int = 36) -> list[set[str]]:
    now = datetime.now(timezone.utc)
    recent = state.state.get("posted_fps", {})
    tokens_list = []
    for fp, ts in recent.items():
        if _within_hours(ts, now, hours):
            tokens_list.append(set(fp.split()))
    return tokens_list


def _is_repeat(recent_tokens: list[set[str]], item: NewsItem) -> bool:
    tokens = _title_tokens(item.title)
    if not tokens:
        return False
    return any(_title_similarity(tokens, other) >= _SIMILARITY_THRESHOLD for other in recent_tokens)


def _prune_stories(state: StateManager, hours: int = 48) -> None:
    now = datetime.now(timezone.utc)
    recent = state.state.get("posted_fps", {})
    state.state["posted_fps"] = {fp: ts for fp, ts in recent.items() if _within_hours(ts, now, hours)}


def parse_pub_date(raw: str) -> datetime | None:
    """Converte datas de RSS/Atom (RFC822, ISO, etc.) em datetime UTC. None se falhar."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def item_age_hours(item: NewsItem, now: datetime | None = None) -> float | None:
    parsed = parse_pub_date(item.published)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - parsed).total_seconds() / 3600.0


def sort_items_by_recency(items: list[NewsItem]) -> list[NewsItem]:
    """Ordena por data real parseada (mais nova primeiro); sem data, mantem ordem no fim."""
    def sort_key(index_item: tuple[int, NewsItem]):
        index, item = index_item
        parsed = parse_pub_date(item.published)
        if parsed is None:
            return (1, 0.0, index)
        return (0, -parsed.timestamp(), index)
    return [item for _, item in sorted(enumerate(items), key=sort_key)]


def main() -> int:
    load_env_file(Path(".env"))
    print("  [env] MESSARI_API_KEY:", env_status("MESSARI_API_KEY"))
    print("  [env] OPENROUTER_API_KEY:", env_status("OPENROUTER_API_KEY"))
    print("  [env] TELEGRAM_BOT_TOKEN:", env_status("TELEGRAM_BOT_TOKEN"))
    print("  [env] TELEGRAM_CHAT_ID:", env_status("TELEGRAM_CHAT_ID"))
    args = parse_args()
    # env com placeholders filtrados: evita usar chave de exemplo como real
    env = dict(os.environ)
    for name in ("OPENROUTER_API_KEY", "MESSARI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env[name] = env_value(name)

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

    # Descarta itens com data parseavel muito antiga (news velha nao merece post)
    max_age_hours = 72
    before_count = len(all_items)
    all_items = [it for it in all_items if item_age_hours(it) is None or item_age_hours(it) <= max_age_hours]
    dropped = before_count - len(all_items)
    if dropped:
        print(f"  [idade] {dropped} itens com mais de {max_age_hours}h descartados")

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

    # Nao repostar historias ja entregues nas ultimas ~36h (comparacao por
    # similaridade de tokens, entre fontes): o editor escolhe algo fresco.
    recent = _recent_stories(state)
    if recent:
        fresh = [t for t in topics if not any(_is_repeat(recent, it) for it in t.items[:4])]
        if fresh:
            topics = fresh

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
        md_lines.append(f"**Ângulo (por que este post):** {topic.coin} — {topic.total_score}pts em {len(topic.scores)} fonte{'s' if len(topic.scores) != 1 else ''} independente{'s' if len(topic.scores) != 1 else ''}.")
        md_lines.append("**Fontes/fatos:**")
        for it in topic.items[:4]:
            md_lines.append(f"- [{it.source}] {it.title} — {it.url}")
        md_lines.append("")
        blocks.append(text)
        payload_saved.append({"coin": topic.coin, "post": text, "sources": [it.url for it in topic.items[:4]]})

    # registra o que foi postado para nao repetir nas proximas rodadas
    for (_topic, _text) in posts:
        state.state.setdefault("posted_fps", {})
        for it in _topic.items[:2]:
            fp = _story_fingerprint(it)
            if fp:
                state.state["posted_fps"][fp] = datetime.now(timezone.utc).isoformat()
    _prune_stories(state)
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
    try:
        with single_instance_lock("x_generator"):
            raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(5)
