from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

import codexctl.events as events
import codexctl.usage.pricing as pricing
import codexctl.usage.repository as usage_repo
from codexctl.web.dependencies import clean_text, current_admin
from codexctl.web.responses import _redirect


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins

    def actor_of(admin: tuple[str, str]) -> str:
        return f"web:{admin[0]}"


    def _price_from_form(
        name: str, input_usd: str, cached_usd: str, output_usd: str, aliases: str
    ) -> pricing.Price:
        label = name.strip()
        if not label:
            raise ValueError("模型名不能为空")
        if len(label) > pricing.MAX_NAME:
            raise ValueError(f"模型名最多 {pricing.MAX_NAME} 个字符")
        label = clean_text(label, pricing.MAX_NAME)

        def rate(field: str, raw: str) -> float:
            try:
                return pricing.parse_usd(raw)
            except ValueError as e:
                raise ValueError(f"{field}：{e}") from None

        inp = rate("输入价格", input_usd)
        cached = rate("缓存输入价格", cached_usd)
        out = rate("输出价格", output_usd)
        try:
            alias_tuple = pricing.parse_aliases(clean_text(aliases, pricing.MAX_NAME * pricing.MAX_ALIASES + 40))
        except ValueError as e:
            raise ValueError(f"别名：{e}") from None
        return pricing.Price(label, inp, cached, out, alias_tuple)


    def _price_detail(price: pricing.Price) -> str:
        return f"输入 {price.input_usd:g} / 缓存 {price.cached_usd:g} / 输出 {price.output_usd:g}"


    def _price_diff(old: pricing.Price, new: pricing.Price) -> str:
        parts = []
        if old.name != new.name:
            parts.append(f"名称 {old.name}→{new.name}")
        for label, before, after in (
            ("输入", old.input_usd, new.input_usd),
            ("缓存", old.cached_usd, new.cached_usd),
            ("输出", old.output_usd, new.output_usd),
        ):
            if before != after:
                parts.append(f"{label} {before:g}→{after:g}")
        if old.aliases != new.aliases:
            parts.append("别名已更新")
        return "；".join(parts)


    @app.get("/pricing", response_class=HTMLResponse)
    def pricing_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            prices = usage_repo.list_prices(ctx.conn)
        return render(request, "pricing.html", admin=admin[0], csrf=admin[1], prices=prices)


    @app.post("/pricing/add")
    def pricing_add(
        name: str = Form(""), input_usd: str = Form(""), cached_usd: str = Form(""),
        output_usd: str = Form(""), aliases: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        with lock:
            devlogins.sweep()
            try:
                price = _price_from_form(name, input_usd, cached_usd, output_usd, aliases)
                error = pricing.validate_prices([*usage_repo.list_prices(ctx.conn), price])
                if error:
                    raise ValueError(error)
                usage_repo.insert_price(ctx.conn, price)
            except ValueError as e:
                return _redirect("/pricing", err=str(e))
            events.audit(
                ctx.conn, actor_of(admin), "pricing.add", price.name, _price_detail(price), now=ctx.now()
            )
        return _redirect("/pricing", msg=f"已添加 {price.name}")


    @app.post("/pricing/{price_id}")
    def pricing_update(
        price_id: int, name: str = Form(""), input_usd: str = Form(""), cached_usd: str = Form(""),
        output_usd: str = Form(""), aliases: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        with lock:
            devlogins.sweep()
            old = usage_repo.get_price(ctx.conn, price_id)
            if old is None:
                return _redirect("/pricing", err="模型不存在或已被删除")
            try:
                price = _price_from_form(name, input_usd, cached_usd, output_usd, aliases)
                prices = [price if p.id == price_id else p for p in usage_repo.list_prices(ctx.conn)]
                error = pricing.validate_prices(prices)
                if error:
                    raise ValueError(error)
                usage_repo.update_price(ctx.conn, price_id, price)
            except ValueError as e:
                return _redirect("/pricing", err=str(e))
            events.audit(
                ctx.conn, actor_of(admin), "pricing.update", price.name,
                _price_diff(old, price) or "未改变", now=ctx.now(),
            )
        return _redirect("/pricing", msg=f"{price.name} 的价格已保存")


    @app.post("/pricing/{price_id}/delete")
    def pricing_delete(price_id: int, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            devlogins.sweep()
            old = usage_repo.get_price(ctx.conn, price_id)
            if old is None:
                return _redirect("/pricing", err="模型不存在或已被删除")
            usage_repo.delete_price(ctx.conn, price_id)
            events.audit(ctx.conn, actor_of(admin), "pricing.delete", old.name, now=ctx.now())
        return _redirect("/pricing", msg=f"已删除 {old.name}")

