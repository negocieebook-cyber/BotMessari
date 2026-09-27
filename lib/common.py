# -*- coding: utf-8 -*-
"""Primitivas compartilhadas pelos agentes do BotMessari.

Inclui: HTTP com retry/backoff, clientes de API, Telegram, state com TTL,
lockfile de concorrencia, logging em arquivo e helpers de texto/valores.
Apenas stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TELEGRAM_BASE = "https://api.telegram.org"
TELEGRAM_LIMIT = 4096
SAFE_TELEGRAM_LIMIT = 3900

PLACEHOLDER_MARKERS = (
    "sua_chave", "sua-chave", "seu_token", "seu-token", "seu_chat", "seu-chat",
    "token-do-seu", "sk-or-v1-sua", "xxxx", "***", "aqui",
)

STATUS_NOT_FOUND: set[int | None] = {401, 403, 404}
STATUS_RETRYABLE: set[int] = {429, 500, 502, 503, 504}


@dataclass
class ApiResult:
    ok: bool
    status: int | None
    data: Any = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(name: str, log_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logger.addHandler(console)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / f"{name}.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError:
            pass
    return logger


# ---------------------------------------------------------------------------
# Lockfile (evita dois agentes rodando em paralelo e enviando duplicado)
# ---------------------------------------------------------------------------

@contextmanager
def single_instance_lock(name: str, state_dir: Path | None = None, stale_minutes: int = 30) -> Iterator[None]:
    if state_dir is None:
        state_dir = Path("state")
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f".lock_{name}"
    stale_after = timedelta(minutes=stale_minutes)

    def acquire() -> bool:
        try:
            if lock_path.exists():
                age = datetime.now() - datetime.fromtimestamp(lock_path.stat().st_mtime)
                if age < stale_after:
                    return False
                lock_path.unlink(missing_ok=True)
            lock_path.write_text(str(os.getpid()), encoding="utf-8")
            return True
        except OSError:
            return False

    if not acquire():
        raise RuntimeError(
            f"Outra execucao do agente '{name}' esta em andamento "
            f"(lock em {lock_path}). Se for um travamento antigo, delete o arquivo."
        )
    try:
        yield
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HTTP com retry/backoff
# ---------------------------------------------------------------------------

def _json_or_text(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
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
    retries: int = 2,
    user_agent: str = "BotMessari/1.0",
) -> ApiResult:
    """HTTP com retry automatico em GET (429/5xx/rede), com backoff exponencial.

    POST nunca e reenviado automaticamente (evita mensagem duplicada no Telegram).
    """
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", "User-Agent": user_agent}
    request_headers.update(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"

    attempts = 0
    last: ApiResult | None = None
    while attempts <= retries:
        attempts += 1
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return ApiResult(True, response.status, _json_or_text(raw))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = _json_or_text(raw)
            last = ApiResult(False, exc.code, parsed, _error_message(parsed, str(exc.reason)))
            retryable = (
                method.upper() == "GET"
                and exc.code in STATUS_RETRYABLE
                and attempts <= retries
            )
            if not retryable:
                return last
            delay = _retry_delay(exc, attempts)
        except URLError as exc:
            last = ApiResult(False, None, None, str(exc.reason))
            retryable = method.upper() == "GET" and attempts <= retries
            if not retryable:
                return last
            delay = min(30.0, 1.5 ** attempts) + random.uniform(0, 0.5)
        except TimeoutError:
            last = ApiResult(False, None, None, "Request timed out")
            retryable = method.upper() == "GET" and attempts <= retries
            if not retryable:
                return last
            delay = min(30.0, 1.5 ** attempts) + random.uniform(0, 0.5)
        time.sleep(delay)
    return last or ApiResult(False, None, None, "Request failed")


def _retry_delay(exc: Exception, attempts: int) -> float:
    retry_after = None
    try:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
    except AttributeError:
        pass
    try:
        return min(30.0, float(retry_after)) if retry_after else min(30.0, 1.5 ** attempts) + random.uniform(0, 0.5)
    except (TypeError, ValueError):
        return min(30.0, 1.5 ** attempts) + random.uniform(0, 0.5)


def fetch_public_url(url: str, timeout: int = 45) -> ApiResult:
    return http_request(
        "GET",
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=timeout,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    )


# ---------------------------------------------------------------------------
# Clientes de API
# ---------------------------------------------------------------------------

class MessariClient:
    def __init__(self, api_key: str, timeout: int = 45, pace_seconds: float = 0.5) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.pace_seconds = pace_seconds

    def _headers(self) -> dict[str, str]:
        return {"X-Messari-API-Key": self.api_key} if self.api_key else {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> ApiResult:
        if not self.api_key:
            return ApiResult(False, None, None, "MESSARI_API_KEY ausente")
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        result = http_request("GET", f"https://api.messari.io{path}{query}", self._headers(), timeout=self.timeout)
        if self.pace_seconds:
            time.sleep(self.pace_seconds)
        return result

    def post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        if not self.api_key:
            return ApiResult(False, None, None, "MESSARI_API_KEY ausente")
        result = http_request("POST", f"https://api.messari.io{path}", self._headers(), payload, self.timeout)
        if self.pace_seconds:
            time.sleep(self.pace_seconds)
        return result

    def asset_details(self, assets: list[str]) -> ApiResult:
        return self.get("/metrics/v2/assets/details", {"assetIDs": ",".join(assets)})

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


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 45) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def send_text(self, text: str, disable_web_page_preview: bool = True) -> ApiResult:
        chunks = split_for_telegram(text)
        last_result = ApiResult(True, 200, {})
        for chunk in chunks:
            last_result = self._post(
                "sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": disable_web_page_preview,
                },
            )
            if not last_result.ok:
                return last_result
        return last_result

    def send_alert(self, text: str) -> ApiResult:
        return self.send_text(text)

    def _post(self, method: str, payload: dict[str, Any]) -> ApiResult:
        return http_request(
            "POST",
            f"{TELEGRAM_BASE}/bot{self.bot_token}/{method}",
            {"Content-Type": "application/json"},
            payload,
            self.timeout,
        )


# ---------------------------------------------------------------------------
# State com TTL
# ---------------------------------------------------------------------------

class StateManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            print(f"state save failed: {exc}", file=sys.stderr)

    def is_new(self, bucket: str, item_id: str, ttl: timedelta) -> bool:
        now = datetime.now(timezone.utc)
        entries = self._fresh_entries(bucket, ttl, now)
        self.state[bucket] = entries
        return all(self._entry_id(entry) != item_id for entry in entries)

    def remember(self, bucket: str, item_ids: list[str], ttl: timedelta) -> None:
        now = datetime.now(timezone.utc)
        entries = self._fresh_entries(bucket, ttl, now)
        known = {self._entry_id(entry) for entry in entries}
        for item_id in item_ids:
            if item_id and item_id not in known:
                entries.append({"id": item_id, "sent_at": now.isoformat()})
                known.add(item_id)
        self.state[bucket] = entries[-1000:]

    def forget(self, bucket: str, item_ids: list[str]) -> None:
        drop = {str(item_id) for item_id in item_ids}
        entries = self.state.get(bucket, [])
        self.state[bucket] = [entry for entry in entries if self._entry_id(entry) not in drop]

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


# ---------------------------------------------------------------------------
# Env / validacao
# ---------------------------------------------------------------------------

def looks_like_placeholder(value: str) -> bool:
    lowered = (value or "").lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def env_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if looks_like_placeholder(value):
        return ""
    return value


def env_status(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        return "AUSENTE (nao encontrada no .env)"
    if looks_like_placeholder(value):
        return "PLACEHOLDER (parece texto de exemplo, nao chave real)"
    return "ok"


def require_env(name: str) -> str | None:
    value = env_value(name)
    if not value:
        print(f"Missing {name}. Add it to .env.", file=sys.stderr)
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
    if name == "TELEGRAM_CHAT_ID" and any(char.isspace() for char in value):
        print("TELEGRAM_CHAT_ID contains spaces or line breaks. Remove all spaces from the chat id in .env.", file=sys.stderr)
        return None
    return value


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Helpers genericos
# ---------------------------------------------------------------------------

def unwrap_data(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def safe_fetch(func: Callable[[], ApiResult], label: str) -> tuple[Any, str | None]:
    """Chamada de API que nunca levanta excecao. Retorna (data, error_message)."""
    try:
        result = func()
        if result.ok:
            return result.data, None
        return None, f"{label}: status {result.status} - {result.error}"
    except Exception as exc:
        return None, f"{label}: {type(exc).__name__} - {exc}"


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


def clean_text(text: str) -> str:
    text = re.sub(r"<!\[CDATA\[|\]\]>", "", text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_html_text(text: str) -> str:
    return clean_text(text)


def clean_markdown_flat(text: str, max_chars: int = 900) -> str:
    """Converte markdown em texto puro de uma linha (estilo messari_daily_agent)."""
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text or "")
    text = re.sub(r"[*_`>#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rsplit(" ", 1)[0] + "..."


def clean_markdown(text: str, max_chars: int = 2600) -> str:
    """Limpa markdown para texto plano do Telegram (estilo crypto_daily_agent)."""
    if not text:
        return ""
    text = re.sub(r"\[\^?\d+\]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: m.group(1).upper(), text)
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"^\*\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"^-\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()[:max_chars]


def split_for_telegram(text: str, limit: int = SAFE_TELEGRAM_LIMIT) -> list[str]:
    normalized = text.replace("\r\n", "\n")
    if len(normalized) <= limit:
        return [normalized]

    chunks = []
    current = ""
    for block in re.split(r"\n\n", normalized):
        block = block.strip()
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(block) <= limit:
            current = block
            continue
        for line in block.splitlines():
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line[:limit]
    if current:
        chunks.append(current)
    return chunks


def unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item.strip())
    return unique
