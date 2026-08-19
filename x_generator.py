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
YT_CHANNELS: list[tuple[str, str]] = [
    ("Coin Bureau", "UCqK_GSMbpiV8spgD3ZGloSw"),
    ("Benjamin Cowen", "UCRvqjQPSeaWn-uEx-w0XOIg"),
]

# Influencers do X (best-effort via RSSHub; se falhar, seguem sem eles)
INFLUENCER_X: list[str] = [
    "LynAldenContact",   # Lyn Alden — macro/bitcoin
    "APompliano",        # Anthony Pompliano — macro
    "DefiIgnas",         # DeFi on-chain
    "MilesDeutscher",    # pesquisa/narrativas
    "RyanWatkins_",      # Messari research
    "aeyakovenko",       # Solana co-founder
]

RSSHUB_BASE = "https://rsshub.app"

# Alias para detectar moedas mencionadas em titulos/trechos (canonico -> palavras)
COIN_ALIASES: dict[str, list[str]] = {
    "Bitcoin": ["bitcoin", "btc", "#btc"],
    "Ethereum": ["ethereum", "eth", "#eth"],
    "Solana": ["solana", "sol", "#sol"],
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
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    result = http_request("GET", url, timeout=30, headers={"Accept": "application/atom+xml,*/*"})
    if not result.ok or not isinstance(result.data, str):
        return []
    try:
        root = ET.fromstring(result.data)
    except ET.ParseError:
        return []
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
    return items


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
    Cruza: noticias + youtube + influencers + trending do CoinGecko.
    Um tema so sobe se bater em multiplas fontes independentes (score de confluencia).
    """
    by_coin: dict[str, dict[str, int]] = {}
    items_by_coin: dict[str, list[NewsItem]] = {}
    for item in all_items:
        for coin in detect_coins(item.title):
            by_coin.setdefault(coin, {}).setdefault(item.source, 0)
            by_coin[coin][item.source] += 1
            items_by_coin.setdefault(coin, []).append(item)

    topics: list[Topic] = []
    for coin, scores in by_coin.items():
        # bonus: tambem esta no top trending do CoinGecko?
        trend_rank = None
        for i, (tid, tname) in enumerate(trend_map.items()):
            if coin.lower() in (str(tname or "").lower(), tid.lower()):
                trend_rank = i + 1
                scores["CoinGecko Trending"] = max(scores.get("CoinGecko Trending", 0), 1)
                break
        total = sum(scores.values())
        if total <= 0:
            continue
        topics.append(
            Topic(
                coin=coin,
                scores=scores,
                items=sorted(items_by_coin[coin], key=lambda it: it.published, reverse=True),
                total_score=total,
                trend_rank=trend_rank,
            )
        )
    topics.sort(key=lambda t: (t.total_score, t.trend_rank is not None), reverse=True)
    return topics


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
        tag = {"news": "NOTICIA", "youtube": "VIDEO", "influencer": "TWEET"}[it.kind]
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
    """Tenta LLM OpenAI-compatible (OPENROUTER), depois Messari AI, senao None."""
    key = env.get("OPENROUTER_API_KEY")
    if key:
        model = env.get("X_POST_MODEL") or "openai/gpt-4o-mini"
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
                "temperature": 0.8,
            },
        )
        if result.ok and isinstance(result.data, dict):
            content = (result.data.get("choices") or [{}])[0].get("message", {}).get("content")
            if content:
                return str(content).strip()
        print(f"  [openrouter] falhou: {result.error}", file=sys.stderr)

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
            if content:
                return str(content).strip()
        print(f"  [messari ai] falhou: {result.error}", file=sys.stderr)
    return None


SYSTEM_PROMPT = (
    "You write short X posts in ENGLISH for the crypto-focused account @bpweb33 "
    "(global audience). Replicate the author's exact preferred format:\n"
    "EXAMPLE POST:\n"
    "It\u2019s interesting how @federalreserve meetings become so significant during a bull market or a heated market...\n"
    "\n"
    "We had a meeting today, yet there wasn\u2019t even a ripple of movement regarding that.\n"
    "\n"
    "I absolutely love BTC, but this level of speculation still worries me\n\n"
    "FORMAT RULES (follow precisely):\n"
    "- English, conversational and observational, personal voice.\n"
    "- Exactly 3 short paragraphs, each separated by a BLANK line.\n"
    "- Paragraph 1: a hook / open-ended thought ENDING WITH '...'\n"
    "- Paragraph 2: the concrete fact or development ENDING WITH '.'\n"
    "- Paragraph 3: a personal take / closing thought ENDING WITH NO punctuation.\n"
    "- Max ~270 characters total (free X plan). No emojis, no hashtags, no ALL-CAPS.\n"
    "- Never invent figures: use ONLY real facts/numbers from the context; cite them naturally.\n"
    "- If there is no solid fact to turn into a post, reply exactly: SKIP"
)


def template_post(topic: Topic) -> str:
    srcs = ", ".join(sorted({it.source for it in topic.items})[:2])
    return (
        f"{topic.coin} is all over the feed today, and it\u2019s worth asking what everyone is missing...\n"
        f"\n"
        f"It shows up in {topic.total_score} independent sources right now, from {srcs}, which almost never happens by chance.\n"
        f"\n"
        f"I like the coin, but watching this kind of heat still makes me pause"
    )


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

    topics = cross_reference(all_items, trend_map)

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
