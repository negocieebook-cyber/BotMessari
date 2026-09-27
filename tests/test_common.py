import io
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from lib import common


# ---------------------------------------------------------------------------
# Helpers de valor
# ---------------------------------------------------------------------------

def test_money_variants():
    assert common.money(1_500_000_000) == "$1.50B"
    assert common.money(2_500_000) == "$2.50M"
    assert common.money(123.45) == "$123.45"
    assert common.money(0.5) == "$0.500000"
    assert common.money(None) == "n/d"
    assert common.money("abc") == "n/d"
    assert common.money("1,234.5") == "$1,234.50"


def test_money_trillion():
    assert common.money(2_000_000_000_000) == "$2.00T"


def test_pct():
    assert common.pct(1.23) == "+1.23%"
    assert common.pct(-4.5) == "-4.50%"
    assert common.pct(None) == "n/d"


def test_first_number():
    assert common.first_number(1) == 1.0
    assert common.first_number("1,234.5") == 1234.5  # separador de milhar
    assert common.first_number(True) is None  # bool nao e numero aqui
    assert common.first_number(None, "x", 3) == 3.0


def test_pick_paths():
    data = {"a": {"b": [{"c": 1}]}, "d": None}
    assert common.pick(data, "a.b.0.c") is None  # nao navega listas
    assert common.pick(data, "a.b", "d", "x.y") == data["a"]["b"]
    assert common.pick(data, "missing") is None


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def test_clean_markdown_strips_links_and_marks():
    raw = "## Titulo\n**negrito** e [link](https://x.com) com `code` e *it*"
    out = common.clean_markdown(raw)
    assert "##" not in out
    assert "**" not in out
    assert "https://x.com" not in out
    assert "NEGRITO" in out  # negrito vira caixa alta

def test_clean_markdown_flat_single_line_and_truncate():
    raw = "# Titulo\n\nTexto com [link](https://x.com) e *pontos* _under_"
    out = common.clean_markdown_flat(raw, max_chars=900)
    assert "\n" not in out
    assert "#" not in out and "*" not in out and "_" not in out
    assert "https://x.com" not in out
    out_short = common.clean_markdown_flat("a" * 2000, max_chars=50)
    assert len(out_short) == 50 and out_short.endswith("...")


def test_split_for_telegram_respects_limit():
    text = "\n\n".join(f"bloco {i} " + "x" * 500 for i in range(10))
    chunks = common.split_for_telegram(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert len(chunks) >= 5
    assert "".join(c.replace("\n\n", "") for c in chunks).startswith("bloco 0")
    # texto curto nao e dividido
    assert common.split_for_telegram("curto", limit=1000) == ["curto"]


# ---------------------------------------------------------------------------
# env / placeholders
# ---------------------------------------------------------------------------

def test_load_env_file_bom_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes("MESSARI_API_KEY=valor-com-aspas\r\n\r\nTELEGRAM_CHAT_ID=\"123\"\n".encode("utf-8-sig"))
    saved = {}
    with mock.patch.dict(os.environ, {}, clear=True):
        common.load_env_file(env_file)
        saved["key"] = os.environ.get("MESSARI_API_KEY")
        saved["chat"] = os.environ.get("TELEGRAM_CHAT_ID")
    assert saved["key"] == "valor-com-aspas"
    assert saved["chat"] == "123"


def test_env_value_filters_placeholder(monkeypatch):
    monkeypatch.setenv("X_KEY", "sua-chave-da-messari-aqui")
    assert common.env_value("X_KEY") == ""
    monkeypatch.setenv("X_KEY", "real-key")
    assert common.env_value("X_KEY") == "real-key"


def test_require_env_telegram_token_validation(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert common.require_env("TELEGRAM_BOT_TOKEN") == "123:abc"
    # com espacos -> None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123 abc")
    assert common.require_env("TELEGRAM_BOT_TOKEN") is None
    # sem dois pontos -> None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abcdef")
    assert common.require_env("TELEGRAM_BOT_TOKEN") is None
    # comecando com 'bot' -> None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot123:abc")
    assert common.require_env("TELEGRAM_BOT_TOKEN") is None


# ---------------------------------------------------------------------------
# HTTP com retry (GET) e sem retry (POST)
# ---------------------------------------------------------------------------

class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, headers=None):
        super().__init__("http://x", code, "err", headers or {}, io.BytesIO(b"{}"))


def test_http_request_get_retries_on_503(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=45):
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeHTTPError(503)
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = mock.MagicMock(
            status=200, read=lambda: b'{"ok": true}'
        )
        return ctx

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    result = common.http_request("GET", "http://x")
    assert result.ok is True and calls["n"] == 3


def test_http_request_post_never_retries(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=45):
        calls["n"] += 1
        raise FakeHTTPError(500)

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    result = common.http_request("POST", "http://x", payload={"a": 1})
    assert result.ok is False and calls["n"] == 1


def test_http_request_get_no_retry_on_404(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=45):
        calls["n"] += 1
        raise FakeHTTPError(404)

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    result = common.http_request("GET", "http://x")
    assert result.ok is False and calls["n"] == 1


def test_http_request_respects_retry_after(monkeypatch):
    sleeps = []

    def fake_urlopen(request, timeout=45):
        raise FakeHTTPError(429, headers={"Retry-After": "7"})

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    monkeypatch.setattr(common.time, "sleep", lambda s: sleeps.append(s))
    result = common.http_request("GET", "http://x", retries=1)
    assert result.ok is False
    assert 7.0 in sleeps


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------

def test_single_instance_lock_blocks_and_stales(tmp_path):
    with common.single_instance_lock("t1", state_dir=tmp_path):
        with pytest.raises(RuntimeError):
            with common.single_instance_lock("t1", state_dir=tmp_path, stale_minutes=30):
                pass
    # libera apos o with
    with common.single_instance_lock("t1", state_dir=tmp_path):
        pass
    # lock antigo e roubado (cria o arquivo com mtime velho)
    lock_file = tmp_path / ".lock_t1"
    lock_file.write_text("123", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock_file, (old, old))
    with common.single_instance_lock("t1", state_dir=tmp_path, stale_minutes=30):
        pass


# ---------------------------------------------------------------------------
# StateManager com TTL
# ---------------------------------------------------------------------------

def test_state_manager_ttl_and_forget(tmp_path):
    sm = common.StateManager(tmp_path / "state.json")
    assert sm.is_new("bucket", "a", timedelta(hours=1))
    sm.remember("bucket", ["a", "b"], timedelta(hours=1))
    assert not sm.is_new("bucket", "a", timedelta(hours=1))
    assert sm.is_new("bucket", "c", timedelta(hours=1))
    sm.forget("bucket", ["a"])
    assert sm.is_new("bucket", "a", timedelta(hours=1))

    # expira com o tempo
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    sm.state["bucket"] = [{"id": "old", "sent_at": past}]
    assert sm.is_new("bucket", "old", timedelta(hours=1))
    # save + reload
    sm.remember("bucket", ["new"], timedelta(hours=1))
    sm.save()
    sm2 = common.StateManager(tmp_path / "state.json")
    assert not sm2.is_new("bucket", "new", timedelta(hours=1))


def test_state_manager_handles_corrupt_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{{{not json", encoding="utf-8")
    sm = common.StateManager(p)
    assert sm.state == {}
