from html.parser import HTMLParser

from codexctl import repo
from test_web_pages import add_acc, c, mgr


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = []
        self.progress = []
        self.inline_styles = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input":
            self.fields.append(attributes)
        if tag == "progress":
            self.progress.append(attributes)
        if "style" in attributes:
            self.inline_styles.append(attributes["style"])


def test_usage_progress_is_csp_compatible_and_preserves_proportions(c, ctx):
    account = add_acc(ctx)
    for model, amount in (("quarter", 100), ("three-quarters", 300)):
        repo.insert_usage(
            ctx.conn, "alice", account.id, ctx.now(),
            dict(input_tokens=amount, cached_input_tokens=0, output_tokens=0,
                 reasoning_output_tokens=0, total_tokens=amount),
            "session.jsonl", model,
        )
    response = c.get("/usage")
    assert response.status_code == 200
    parser = Inputs()
    parser.feed(response.text)
    assert parser.inline_styles == []
    assert sorted(float(item["value"]) for item in parser.progress[:2]) == [25, 75]
    assert all(item["max"] == "100" and item["aria-label"] for item in parser.progress)


def test_dashboard_title_contains_only_title_text(c):
    page = c.get("/").text
    title = page.split("<title>", 1)[1].split("</title>", 1)[0]
    assert title == "总览 - codexctl"
    assert page.count('src="/static/chart-inspector.js"') == 1


def test_price_fields_have_names_and_mobile_layout(c):
    response = c.get("/pricing")
    assert response.status_code == 200
    parser = Inputs()
    parser.feed(response.text)
    prices = [field for field in parser.fields if field.get("form", "").startswith("price-")]
    assert prices
    assert all(field.get("aria-label") for field in prices)
    assert 'class="price-table mobile-cards"' in response.text
    assert 'data-label="操作"' in response.text


def test_mobile_account_records_label_actions(c, ctx):
    add_acc(ctx)
    page = c.get("/accounts").text
    assert 'class="mobile-cards accounts-table"' in page
    assert 'data-label="最近错误"' in page
    assert 'data-label="操作"' in page
    assert '>正常</span>' in page
