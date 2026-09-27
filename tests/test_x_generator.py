import x_generator as xg
from x_generator import NewsItem, Topic


# ---------------------------------------------------------------------------
# Parsing de datas / idade
# ---------------------------------------------------------------------------

def test_parse_pub_date_rfc822():
    dt = xg.parse_pub_date("Mon, 22 Sep 2025 10:00:00 GMT")
    assert dt is not None and dt.tzinfo is not None
    assert dt.year == 2025 and dt.month == 9


def test_parse_pub_date_iso():
    assert xg.parse_pub_date("2025-09-22T10:00:00Z") is not None
    assert xg.parse_pub_date("2025-09-22") is not None
    assert xg.parse_pub_date("") is None
    assert xg.parse_pub_date("garbage") is None


def test_item_age_hours():
    item = NewsItem("s", "t", "u", "2025-09-22T10:00:00Z", "news")
    hours = xg.item_age_hours(item)
    assert hours is not None and hours > 0


def test_sort_items_by_recency_newest_first():
    now = "2025-09-22T12:00:00Z"
    a = NewsItem("s", "a", "u", "2025-09-22T10:00:00Z", "news")
    b = NewsItem("s", "b", "u", "2025-09-22T11:00:00Z", "news")
    c = NewsItem("s", "c", "u", "", "news")  # sem data
    ordered = xg.sort_items_by_recency([a, c, b])
    assert ordered[0].title == "b"  # mais novo primeiro
    assert ordered[-1].title == "c"  # sem data por ultimo


# ---------------------------------------------------------------------------
# Dedup por similaridade
# ---------------------------------------------------------------------------

def test_title_tokens_stops_and_strips():
    tokens = xg._title_tokens("Bitcoin ETF sees $100M inflow in one day")
    assert "bitcoin" in tokens and "etf" in tokens
    assert "the" not in tokens and "in" not in tokens


def test_is_repeat_catches_same_story_other_source():
    a = NewsItem("A", "Bitcoin ETF sees 100M inflow in one day", "u1", "", "news")
    b = NewsItem("B", "Bitcoin ETF records 100M inflow in one day", "u2", "", "news")
    c = NewsItem("C", "Solana DeFi TVL grows fast this week", "u3", "", "news")
    recent = [set(xg._story_fingerprint(a).split())]
    assert xg._is_repeat(recent, b)
    assert not xg._is_repeat(recent, c)


def test_story_fingerprint_is_source_agnostic():
    a = NewsItem("A", "Bitcoin ETF approved", "u", "", "news")
    b = NewsItem("B", "Bitcoin ETF approved", "u", "", "news")
    assert xg._story_fingerprint(a) == xg._story_fingerprint(b)
    assert "A|" not in xg._story_fingerprint(a)


# ---------------------------------------------------------------------------
# Magnitude / deteccao de moedas
# ---------------------------------------------------------------------------

def test_flow_magnitude_usdm():
    assert xg.flow_magnitude_usdm("whale moved $52M in PEPE") == 52_000_000
    assert xg.flow_magnitude_usdm("ETF inflow of $1.2B") == 1_200_000_000
    assert xg.flow_magnitude_usdm("2.5 billion in outflows") == 2_500_000_000
    assert xg.flow_magnitude_usdm("no numbers here") == 0.0


def test_detect_coins():
    assert "Bitcoin" in xg.detect_coins("Bitcoin ETF sees inflow #btc")
    assert "Ethereum" in xg.detect_coins("ETH staking grows")
    assert xg.detect_coins("completely unrelated text") == []


# ---------------------------------------------------------------------------
# Cross-reference / temas
# ---------------------------------------------------------------------------

def test_cross_reference_weights_flow_more_than_news():
    items = [
        NewsItem("Lookonchain", "whale moved $500M of ETH into exchange", "u", "2025-09-22T10:00:00Z", "flow"),
        NewsItem("Cointelegraph", "Ethereum ETF inflow", "u", "2025-09-22T10:00:00Z", "news"),
    ]
    topics = xg.cross_reference(items, {})
    assert topics and topics[0].coin == "Ethereum"
    assert len(topics[0].items) == 2


def test_cross_reference_empty():
    assert xg.cross_reference([], {}) == []


def test_build_themes_buckets():
    items = [
        NewsItem("A", "New $40M raise led by VC for stablecoin payments", "u", "", "news"),
        NewsItem("B", "SEC rules on ETF regulation", "u", "", "news"),
    ]
    themes = xg.build_themes(items)
    labels = {t.coin for t in themes}
    assert "VC Funding" in labels
    assert "Regulação & Institucional" in labels


def test_merge_and_sort():
    t1 = Topic("a", {}, [], 5)
    t2 = Topic("b", {}, [], 9)
    merged = xg.merge_and_sort([t1], [t2])
    assert merged[0].coin == "b"


# ---------------------------------------------------------------------------
# Post: gate de qualidade e truncamento
# ---------------------------------------------------------------------------

def test_has_filler_detects_banned_phrases():
    assert xg.has_filler("This is all over the feed today")
    assert xg.has_filler("worth a closer look")
    assert xg.has_filler("3 sources say")
    assert xg.has_filler("A move this size gets my attention before I trust any take")
    assert xg.has_filler("Real money tends to show up before the narrative does")
    assert xg.has_filler("Whether it holds is the question now")
    assert xg.has_filler("That decides what comes next")
    assert xg.has_filler("I watch moves like this before I ever buy the story")
    assert xg.has_filler("Discrepancies this big are rarely noise")
    # promessas / hype / conselho / engagement bait
    assert xg.has_filler("BTC set for next leg up, price target incoming")
    assert xg.has_filler("Don't miss this one, buying before it's too late")
    assert xg.has_filler("I'm all in on SOL right now")
    assert xg.has_filler("Are you bullish or not?")
    assert xg.has_filler("Follow for more alpha, link in bio")
    assert xg.has_filler("This will explode after the halving")
    # honesto e limpo: nao deve dar flag
    assert not xg.has_filler("A whale moved $52M in PEPE. Could be rotation, could be exit.")
    assert not xg.has_filler("No exchange deposit so far. The trail is public if you want to follow it.")


def test_dangling_end_and_strip():
    assert xg._dangling_end("Some thought...")
    assert not xg._dangling_end("Some thought.")
    assert xg._strip_dangling_ellipsis("thought...") == "thought"


def test_truncate_post_cuts_at_sentence():
    text = ("A" * 50 + ". ") * 10
    out = xg.truncate_post(text, hard=297)
    assert len(out) <= 297
    assert out.endswith(".")


def test_template_post_uses_real_fact():
    topic = Topic(
        coin="Ethereum",
        scores={"Lookonchain": 2},
        items=[NewsItem("Lookonchain", "Whale moved $52M in ETH to exchange", "u", "", "flow")],
        total_score=10,
    )
    post = xg.template_post(topic)
    assert "$52M" in post
    assert len(post) <= 297
    assert not xg.has_filler(post)
    assert not post.endswith("...")
    assert "..." not in post  # sem reticencia pendurada em lugar nenhum


def test_template_post_followup_matches_kind():
    flow = Topic(
        coin="Bitcoin",
        scores={"Lookonchain": 1},
        items=[NewsItem("Lookonchain", "Whale moved $900M in BTC between wallets", "u", "", "flow")],
        total_score=5,
    )
    news = Topic(
        coin="Regulação & Institucional",
        scores={"Cointelegraph": 1},
        items=[NewsItem("Cointelegraph", "SEC sets new rules for crypto custody", "u", "", "news")],
        total_score=5,
    )
    post_flow = xg.template_post(flow)
    post_news = xg.template_post(news)
    # fallback honesto: fala de incerteza/rastro, nunca de promessa
    assert any(w in post_flow.lower() for w in ("rotation", "exit", "deposit", "trail"))
    assert any(w in post_news.lower() for w in ("second read", "counterparties", "comply"))
    for post in (post_flow, post_news):
        assert not xg.has_filler(post)
        assert "target" not in post.lower() and "miss" not in post.lower()
        assert len(post) <= 297


def test_template_post_without_items():
    topic = Topic(coin="Solana", scores={}, items=[], total_score=0)
    post = xg.template_post(topic)
    assert "Solana" in post and len(post) <= 297


def test_compact_fact_normalizes_headline():
    item = NewsItem("S", "  A whale  moved $52M in ETH...  ", "u", "", "flow")
    assert xg._compact_fact(item) == "A whale moved $52M in ETH."


def test_compact_fact_closes_dangling_parenthesis():
    item = NewsItem(
        "S",
        "According to YuEmber monitoring, a whale or institution that has held ETH for three "
        "years realized further profit-taking of 30,825 ETH (worth approximately $83.03 million) 9 hours ago",
        "u", "", "flow",
    )
    out = xg._compact_fact(item)
    assert "(" not in out
    assert "30,825 ETH" in out
    assert out.endswith("ETH.") and len(out) <= 152


def test_template_post_varies_followup_within_run():
    t1 = Topic(coin="Bitcoin", scores={"Lookonchain": 1},
               items=[NewsItem("Lookonchain", "Whale moved $900M in BTC between wallets", "u", "", "flow")],
               total_score=5)
    t2 = Topic(coin="Ethereum", scores={"Lookonchain": 1},
               items=[NewsItem("Lookonchain", "Whale moved $52M in ETH to exchange", "u", "", "flow")],
               total_score=5)
    used: set = set()
    p1 = xg.template_post(t1, used)
    p2 = xg.template_post(t2, used)
    tail1 = p1.split("\n\n")[-1]
    tail2 = p2.split("\n\n")[-1]
    assert tail1 != tail2


def test_template_post_hook_is_question_alternate():
    topic = Topic(coin="Bitcoin", scores={"Lookonchain": 1},
                  items=[NewsItem("Lookonchain", "Whale moved $900M in BTC between wallets", "u", "", "flow")],
                  total_score=5)
    plain = xg.template_post(topic, with_hook=False)
    hook = xg.template_post(topic, with_hook=True)
    assert "?" in hook  # gancho convida discussao
    assert "?" in plain or plain.split("\n\n")[-1]  # sem gancho segue normal
    assert not xg.has_filler(hook)
    # gancho deve ser especifico, nao engagement bait generico
    assert "what do you think" not in hook.lower()
    assert "are you bullish" not in hook.lower()
    assert len(hook) <= 297


# ---------------------------------------------------------------------------
# Fingerprint registrado nao repete (formato do state)
# ---------------------------------------------------------------------------

def test_prune_stories_removes_old(tmp_path, monkeypatch):
    from lib.common import StateManager
    import json
    from datetime import datetime, timedelta, timezone
    sm = StateManager(tmp_path / "s.json")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    sm.state["posted_fps"] = {"old fp": old, "new fp": fresh}
    xg._prune_stories(sm, hours=48)
    assert set(sm.state["posted_fps"].keys()) == {"new fp"}
