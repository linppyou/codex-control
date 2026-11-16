#!/www/server/panel/pyenv/bin/python
"""Restricted SSH entry point for the BaoTa AI Center provisioning service."""

import contextlib
import fcntl
import json
import os
import re
import sys
from pathlib import Path

PANEL = Path("/www/server/panel")
PLUGIN = PANEL / "plugin/ai_center"
COMMAND = "codexctl-wp-center"
PAGE_SIZE = 10
ACTIVE = {"queued", "deploying", "running", "ssl_pending"}
RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
JOB_ID = re.compile(r"AIC-[A-Za-z0-9-]{1,60}")


class RequestError(ValueError):
    pass


def identifier(value, pattern=RESOURCE_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RequestError("资源或任务编号无效，请刷新后重新选择")
    return value


def resource_summary(resource):
    return {key: resource[key] for key in ("id", "name", "size", "origin") if key in resource}


def candidates(resource):
    return [
        {"domain": item["domain"], "primary": item.get("evidence") == "WordPress home / siteurl"}
        for item in resource.get("candidates", []) if item.get("domain")
    ]


def suggested_domain(resource):
    choices = candidates(resource)
    primary = {item["domain"] for item in choices if item["primary"]}
    values = primary or {item["domain"] for item in choices}
    return next(iter(values)) if len(values) == 1 else ""


def catalog(service):
    service.scan_resources([])
    resources = service.resources()
    return {
        "templates": [resource_summary(item) for item in resources if item["kind"] == "template"],
        "databases": [
            {**resource_summary(item), "suggested_domain": suggested_domain(item)}
            for item in resources if item["kind"] == "sql" and item.get("origin") != "template"
        ],
    }


def inspect(service, payload):
    result = service.inspect_template({"id": identifier(payload.get("template"))})
    return {
        "databases": [
            {**resource_summary(item), "suggested_domain": suggested_domain(item)}
            for item in result["databases"]
        ],
        "warnings": result.get("warnings", []),
    }


def history(service, page, retryable_ssl, public_reason):
    if type(page) is not int or not 1 <= page <= 100_000:
        raise RequestError("无效的任务页码")
    service._ensure_provision_worker()
    with service.connection() as conn:
        stored = conn.execute(
            "SELECT body FROM jobs ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
        ).fetchall()
    jobs = []
    for (body,) in stored[:PAGE_SIZE]:
        job = json.loads(body)
        plan = job["plan"]
        status = job["status"]
        results = job.get("execution", {}).get("results", {})
        rows = []
        for row in plan["rows"]:
            outcome = results.get(row["domain"], {})
            ssl = outcome.get("ssl", {})
            state = outcome.get("status") or row["status"]
            error = outcome.get("operation_error") or outcome.get("error")
            if not error and state in {"ready", "skipped", "invalid"}:
                if status == "failed" and state == "ready":
                    error = "远程建站任务启动失败，请查看宝塔 WP 建站中心任务详情"
                else:
                    error = row.get("errors") or row.get("warnings")
            operation = outcome.get("operation_state")
            can_retry = retryable_ssl(outcome)
            ssl_state = ssl.get("status", "")
            if can_retry:
                if status == "ssl_pending" and operation in {"queued", "running"}:
                    ssl_state = "pending"
                elif outcome.get("ssl_retry_failed"):
                    ssl_state = "retry_failed"
                elif ssl_state == "installed" and ssl.get("excluded_domains"):
                    ssl_state = "partial"
            ssl_detail = ssl.get("last_retry", {}).get("reason") or ssl.get("reason")
            rows.append({
                "domain": row["domain"], "site_state": state,
                "ssl": ssl_state, "error": public_reason(error) if error else "",
                "ssl_detail": public_reason(ssl_detail) if ssl_detail else "",
                "can_retry": status not in ACTIVE and can_retry,
            })
        jobs.append({
            "id": job["id"], "created_at": job["created_at"], "status": status,
            "template": plan["resources"].get("template", {}).get("name", ""), "rows": rows,
        })
    return {"jobs": jobs, "more": len(stored) > PAGE_SIZE}


def create(service, payload, clean_domain):
    domain = clean_domain(payload.get("domain", ""))[1]
    template = identifier(payload.get("template"))
    sql = payload.get("sql", "")
    if sql:
        sql = identifier(sql)
    else:
        inspection = service.inspect_template({"id": template})
        databases = inspection["databases"]
        if len(databases) != 1:
            raise RequestError("请选择要导入的 SQL 文件")
        sql = databases[0]["id"]
    old_domain = payload.get("old_domain", "")
    if not old_domain:
        resource = service.inspect_sql_resource({"id": sql})
        old_domain = suggested_domain(resource)
        if not old_domain:
            raise RequestError("请填写模板 SQL 中的原站域名")
    old_domain = clean_domain(old_domain)[1]
    prefixes = "www" if payload.get("www") is True else ""
    bindings = {domain, f"www.{domain}"} if prefixes and not domain.startswith("www.") else {domain}
    with (service.root / "codexctl-create.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RequestError("服务器正在处理另一项建站提交，请稍后重试") from exc
        for run in service._provision_runs(("queued", "running")):
            if bindings.intersection(set(run.get("bindings", ())) | {run["domain"]}):
                raise RequestError("这个域名已有远程任务正在执行，请查看任务列表")
        return service.create_plan({
            "batch": domain, "prefixes": prefixes, "resources": {"template": template, "sql": sql},
            "old_domain": old_domain, "old_domain_confirmed": True, "ssl": True,
        })


def dispatch(service, payload, clean_domain, retryable_ssl, public_reason):
    operation = payload.get("operation")
    if operation == "catalog":
        return catalog(service)
    if operation == "inspect":
        return inspect(service, payload)
    if operation == "history":
        return history(service, payload.get("page"), retryable_ssl, public_reason)
    if operation == "create":
        return create(service, payload, clean_domain)
    if operation == "retry_ssl":
        return service.retry_ssl({
            "id": identifier(payload.get("job"), JOB_ID),
            "domains": [clean_domain(payload.get("domain", ""))[1]],
            "confirm": True,
        })
    raise RequestError("不支持的建站操作")


def main():
    if os.environ.get("SSH_ORIGINAL_COMMAND") != COMMAND:
        raise SystemExit("Unsupported command")
    os.umask(0o077)
    os.chdir(PANEL)
    sys.path[:0] = [str(PLUGIN), str(PANEL / "class"), str(PANEL)]
    # Native panel imports and methods may print diagnostics.
    with contextlib.redirect_stdout(sys.stderr):
        from aic_core import ValidationError, clean_domain
        from aic_service import Service
        from aic_ssl import credential_values, public_reason, retryable_ssl

        try:
            body = sys.stdin.buffer.read(8173)
            if len(body) > 8172:
                raise RequestError("建站请求过大")
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise RequestError("无效的建站请求") from exc
            if not isinstance(payload, dict):
                raise RequestError("无效的建站请求")
            secrets = credential_values()
            service = Service(PLUGIN / "data", panel=True)
            data = dispatch(
                service, payload, clean_domain, retryable_ssl, lambda value: public_reason(value, secrets),
            )
            result = {"ok": True, "data": data}
        except (RequestError, ValidationError) as exc:
            result = {"ok": False, "message": public_reason(str(exc), credential_values())}
        except Exception as exc:
            print(f"WP center error: {type(exc).__name__}", file=sys.stderr)
            result = {"ok": False, "message": "远程 WP 建站中心执行异常，请查看宝塔任务状态"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
