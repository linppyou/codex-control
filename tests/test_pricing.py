import pytest

from codexctl import pricing


def test_price_table_matches_image():
    assert [(p.name, p.input_usd, p.cached_usd, p.output_usd) for p in pricing.PRICES] == [
        ("Codex Auto Review (ChatGPT)", 0.0, 0.0, 0.0),
        ("GPT-6.1 Sol", 2.00, 0.10, 10.00),
        ("GPT-6.1 Terra", 1.00, 0.05, 5.00),
        ("GPT-6 Astra", 10.00, 1.00, 50.00),
        ("GPT-6 Astra Law*", 10.00, 1.00, 50.00),
        ("GPT-6 Sol", 2.00, 0.20, 10.00),
        ("GPT-6 Luna", 0.10, 0.01, 0.50),
        ("GPT-5.6 Sol", 4.00, 0.40, 20.00),
        ("GPT-5.6 Terra", 2.00, 0.20, 12.00),
        ("GPT-5.6 Luna", 0.20, 0.02, 1.20),
        ("GPT-6 Rosalind Research", 15.00, 1.50, 75.00),
        ("GPT-5.5 Rosalind Research", 5.00, 0.50, 25.00),
        ("GPT-5.5 Rosalind Discovery", 5.00, 0.50, 25.00),
        ("Daybreak Blue (GPT-5.6 Sol)", 4.00, 0.40, 20.00),
        ("Daybreak Red (GPT-5.5 Cyber 和 GPT-5.6 Cyber)", 12.50, 1.25, 75.00),
        ("Daybreak Red (GPT-6 Cyber, 仅限 Codex)", 20.00, 2.00, 100.00),
        ("GPT-5.3-Codex", 1.75, 0.175, 14.00),
        ("GPT-5.2", 1.75, 0.175, 14.00),
    ]


def test_chatgpt_auto_review_is_free():
    assert pricing.cost_usd("codex-auto-review", 1_000_000, 100_000, 100_000) == 0


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "GPT-6.1 Sol", "GPT-6.1-SOL", "gpt_6_1 sol"])
def test_lookup_accepts_model_ids_and_display_names(model):
    assert pricing.lookup(model).name == "GPT-6.1 Sol"


def test_lookup_distinguishes_daybreak_red_tiers():
    assert pricing.lookup("gpt-5.5-cyber").output_usd == 75.00
    assert pricing.lookup("gpt-5.6-cyber").output_usd == 75.00
    assert pricing.lookup("gpt-6-cyber").output_usd == 100.00
    assert pricing.lookup("daybreak-red-codex").output_usd == 100.00


def test_lookup_unknown_model():
    assert pricing.lookup(None) is None
    assert pricing.lookup("") is None
    assert pricing.lookup("gpt-a") is None


def test_estimate_splits_cached_input_and_output():
    price = pricing.lookup("gpt-5.2")
    # 600K 普通输入 × 1.75 + 400K 缓存输入 × 0.175 + 100K 输出 × 14
    assert pricing.estimate(price, 1_000_000, 400_000, 100_000) == pytest.approx(2.52)
    assert pricing.cost_usd("gpt-5.2", 1_000_000, 400_000, 100_000) == pytest.approx(2.52)


def test_estimate_clamps_invalid_token_counts():
    price = pricing.lookup("gpt-5.2")
    assert pricing.estimate(price, 100, 500, 0) == pytest.approx(100 * 0.175 / 1_000_000)
    assert pricing.estimate(price, -100, 10, -5) == 0
    assert pricing.estimate(None, 100, 0, 0) is None


def test_format_currency():
    assert pricing.fmt_usd(None) == "-"
    assert pricing.fmt_usd(0) == "$0.00"
    assert pricing.fmt_usd(1.5) == "$1.50"
    assert pricing.fmt_usd(2.625) == "$2.63"
    assert pricing.fmt_usd(0.004321) == "$0.004321"
    assert pricing.fmt_usd(1234.5) == "$1,234.50"
    assert pricing.fmt_rate(2) == "$2.00"
    assert pricing.fmt_rate(0.1) == "$0.10"
    assert pricing.fmt_rate(0.175) == "$0.175"
    assert pricing.fmt_rate(12.5) == "$12.50"
    assert pricing.fmt_rate_value(2) == "2.00"
    assert pricing.fmt_rate_value(0.175) == "0.175"


def test_price_book_uses_editable_prices():
    book = pricing.PriceBook([
        pricing.Price("Custom", 3.0, 0.3, 6.0, ("custom-id",), id=7),
        pricing.Price("Another", 1.0, 0.1, 2.0),
    ])
    assert book.lookup("CUSTOM-ID").name == "Custom"
    assert book.lookup("custom id").id == 7
    assert book.cost("custom-id", 1_000_000, 0, 0) == pytest.approx(3.0)
    assert book.cost("missing", 1_000_000, 0, 0) is None


def test_parse_usd_and_aliases():
    assert pricing.parse_usd(" 0.175 ") == 0.175
    assert pricing.parse_usd("$1,234.50") == 1234.5
    for bad in ("", "abc", "-1", "nan", "inf", "1000001"):
        with pytest.raises(ValueError):
            pricing.parse_usd(bad)
    assert pricing.parse_aliases("gpt-a, GPT-A\nfoo，bar") == ("gpt-a", "foo", "bar")
    assert pricing.parse_aliases(" , ") == ()
    with pytest.raises(ValueError):
        pricing.parse_aliases(",".join(f"m{i}" for i in range(21)))


def test_validate_prices_rejects_duplicate_name_or_alias():
    prices = [
        pricing.Price("One", 1, 0.1, 2, ("shared",)),
        pricing.Price("Two", 1, 0.1, 2, ("two",)),
    ]
    assert pricing.validate_prices(prices) is None
    assert "重复" in pricing.validate_prices([*prices, pricing.Price("SHARED", 1, 0.1, 2)])
    assert "重复" in pricing.validate_prices([*prices, pricing.Price("one", 1, 0.1, 2)])
    assert "不能为空" in pricing.validate_prices([pricing.Price("", 1, 0.1, 2)])
