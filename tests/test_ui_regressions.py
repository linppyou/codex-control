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


def test_dashboard_account_users_follow_current_assignments(c, ctx):
    from codexctl.web.views import _account_rows

    first = add_acc(ctx, label="first", account_id="first-account")
    second = add_acc(ctx, label="second", account_id="second-account")
    repo.start_assignment(ctx.conn, "zoe", first.id, ctx.now())
    repo.start_assignment(ctx.conn, "alice", first.id, ctx.now())
    repo.start_assignment(ctx.conn, "former", first.id, ctx.now())
    repo.end_assignment(ctx.conn, "former", ctx.now())
    cards = {card["label"]: card for card in _account_rows(ctx)}
    assert cards["first"]["users"] == ["alice", "zoe"]
    assert cards["second"]["users"] == []
    page = c.get("/").text
    assert "当前分配用户 · 2 人" in page
    assert 'href="/users/alice">alice</a>' in page
    assert 'href="/users/zoe">zoe</a>' in page
    assert "暂无分配用户" in page
    assert not cards["first"]["has_quota"]
    repo.end_assignment(ctx.conn, "alice", ctx.now())
    repo.start_assignment(ctx.conn, "alice", second.id, ctx.now())
    cards = {card["label"]: card for card in _account_rows(ctx)}
    assert cards["first"]["users"] == ["zoe"]
    assert cards["second"]["users"] == ["alice"]


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


def test_member_page_shows_remaining_quota_and_line_chart(c, ctx):
    from codexctl import webauth
    from codexctl.web.app import SESSION_COOKIE
    from fastapi.testclient import TestClient

    account = add_acc(ctx)
    ctx.test_users.add("alice")
    repo.start_assignment(ctx.conn, "alice", account.id, ctx.now())
    repo.insert_quota_sample(ctx.conn, account.id, ctx.now(), "primary", 32, 10080, ctx.now() + 3600, "api")
    token, _ = webauth.create_session(ctx.conn, "alice", now=ctx.now())
    member = TestClient(c.app)
    member.cookies.set(SESSION_COOKIE, token)
    response = member.get("/users/alice")
    assert response.status_code == 200
    assert '<h1>我的数据</h1>' in response.text
    assert '剩余额度 68%' in response.text
    assert 'value="68"' in response.text
    assert '用量折线图' in response.text
    assert 'class="daily-usage-line"' in response.text
    assert 'class="daily-bar"' not in response.text
    assert response.text.count('class="chart-hit"') == 7
    assert 'src="/static/chart-inspector.js"' in response.text
    assert '/users/alice/fix' not in response.text
    assert response.text.index('current-account-card') < response.text.index('每日用量')


def test_account_quota_expired_and_missing_states(c, ctx):
    account = add_acc(ctx)
    ctx.test_users.add("alice")
    repo.start_assignment(ctx.conn, "alice", account.id, ctx.now())
    assert "暂无额度数据" in c.get("/users/alice").text
    repo.insert_quota_sample(ctx.conn, account.id, ctx.now() - 3600, "primary", 25, 10080, ctx.now() - 60, "api")
    response = c.get("/users/alice")
    assert "已重置（待更新）" in response.text
    assert '剩余额度 75%' not in response.text
    assert '/users/alice/fix' in response.text
