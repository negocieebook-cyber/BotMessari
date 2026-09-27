from datetime import timezone

import messari_daily_agent as agent


# ---------------------------------------------------------------------------
# Dedup de research
# ---------------------------------------------------------------------------

def test_report_fingerprint_uses_title_and_date():
    report = {"title": "BTC Outlook", "createdAt": "2026-01-01"}
    assert agent.report_fingerprint(report) == "btc outlook|2026-01-01"
    # campos alternativos
    assert agent.report_fingerprint({"name": "Alt", "publishedAt": "d"}) == "alt|d"


def test_filter_new_research_dedups_by_id_and_fingerprint():
    state = {"sent_research_ids": ["r1"], "sent_research_fingerprints": ["old title|x"]}
    reports = [
        {"id": "r1", "title": "A"},
        {"id": "r2", "title": "New", "createdAt": "now"},
        {"id": "r3", "title": "old title", "createdAt": "x"},
    ]
    new, ids, fps = agent.filter_new_research(reports, state)
    assert [r["id"] for r in new] == ["r2"]
    assert ids == ["r2"]
    assert fps == ["new|now"]


def test_remember_research_caps_lists():
    state = {"sent_research_ids": [f"id{i}" for i in range(1200)]}
    agent.remember_research(state, ["extra"], [])
    assert len(state["sent_research_ids"]) == 1000
    assert state["sent_research_ids"][-1] == "extra"


def test_filter_new_public_items():
    state = {"sent_public_item_ids": ["p1"]}
    items = [{"id": "p1"}, {"id": "p2"}]
    new, ids = agent.filter_new_public_items(items, state)
    assert [i["id"] for i in new] == ["p2"]
    assert ids == ["p2"]


# ---------------------------------------------------------------------------
# Formatacao
# ---------------------------------------------------------------------------

def test_build_market_section_rows():
    assets = [
        {
            "symbol": "BTC",
            "name": "Bitcoin",
            "marketData": {"priceUsd": 100000, "volume24Hour": 5_000_000_000, "marketcap": {"circulatingUsd": 2_000_000_000_000}},
            "returnOnInvestment": {"priceChange24h": 1.5, "priceChange7d": -2.0, "priceChange30d": 10.0},
        }
    ]
    section, ai_lines = agent.build_market_section(assets)
    assert "| BTC | Bitcoin | $100,000.00 | +1.50% | -2.00% | +10.00% | $5.00B | $2.00T |" in section
    assert "BTC" in ai_lines[0]


def test_build_research_section_fields():
    reports = [
        {
            "title": "Report A",
            "createdAt": "2026-01-01",
            "authors": [{"name": "Ana"}],
            "assets": [{"symbol": "BTC"}],
            "content": "Insight **forte** [link](https://x.com)",
            "id": "r1",
        }
    ]
    section, ai_lines = agent.build_research_section(reports)
    assert "### Report A" in section
    assert "- Autoria: Ana" in section
    assert "- Ativos citados: BTC" in section
    assert "https://x.com" not in section  # links limpos do trecho
    assert ai_lines


def test_clean_markdown_is_flat():
    out = agent.clean_markdown("## Titulo\n\nTexto com *marcador* e [link](https://x.com)")
    assert "\n" not in out and "#" not in out and "*" not in out


# ---------------------------------------------------------------------------
# Telegram chunking herdado da lib
# ---------------------------------------------------------------------------

def test_split_for_telegram_imported():
    assert agent.split_for_telegram("ok") == ["ok"]


def test_unavailable_line_format():
    from lib.common import ApiResult
    line = agent.unavailable_line("Research", ApiResult(False, 401, None, "unauthorized"))
    assert "HTTP 401" in line and "Research" in line


def test_write_report_name(tmp_path):
    path = agent.write_report("conteudo", tmp_path)
    assert path.name.startswith("messari-daily-") and path.suffix == ".md"
    assert path.read_text(encoding="utf-8") == "conteudo"


def test_unique_keep_order():
    assert agent.unique_keep_order(["a", "A", "b", "", " b "]) == ["a", "b"]
