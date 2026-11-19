"""Regression coverage for module moves, schema and route preservation."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import subprocess
import sys

import pytest

from codexctl import db, repo
from codexctl.accounts import repository as account_repository
from codexctl.sites import inventory
from codexctl.usage import repository as usage_repository
from codexctl.users import repository as user_repository
from codexctl.users import webauth
from codexctl.web.app import create_app

LEGACY_MODULES = [
    ("codexctl.authfile", "codexctl.accounts.authfile"),
    ("codexctl.oauth", "codexctl.accounts.oauth"),
    ("codexctl.refresher", "codexctl.accounts.refresher"),
    ("codexctl.devicelogin", "codexctl.accounts.devicelogin"),
    ("codexctl.publisher", "codexctl.accounts.publisher"),
    ("codexctl.useradmin", "codexctl.users.service"),
    ("codexctl.assignments", "codexctl.users.assignments"),
    ("codexctl.sysusers", "codexctl.users.sysusers"),
    ("codexctl.procs", "codexctl.users.procs"),
    ("codexctl.codexinstall", "codexctl.users.codexinstall"),
    ("codexctl.webauth", "codexctl.users.webauth"),
    ("codexctl.pam", "codexctl.users.pam"),
    ("codexctl.quota", "codexctl.usage.quota"),
    ("codexctl.logscan", "codexctl.usage.logscan"),
    ("codexctl.pricing", "codexctl.usage.pricing"),
    ("codexctl.watcher", "codexctl.usage.watcher"),
    ("codexctl.site_registry", "codexctl.sites.registry"),
    ("codexctl.site_enrollment", "codexctl.sites.enrollment"),
    ("codexctl.site_install", "codexctl.sites.installer"),
    ("codexctl.site_access", "codexctl.sites.access"),
    ("codexctl.site_ssh", "codexctl.sites.ssh"),
    ("codexctl.site_probe", "codexctl.sites.probe"),
    ("codexctl.dispatch_tasks", "codexctl.dispatch.tasks"),
    ("codexctl.dispatch_skills", "codexctl.dispatch.skills"),
    ("codexctl.dispatch_worker", "codexctl.dispatch.worker"),
    ("codexctl.dispatch_transport", "codexctl.dispatch.transport"),
    ("codexctl.web.server_views", "codexctl.web.routes.servers"),
    ("codexctl.web.dispatch_views", "codexctl.web.routes.dispatch"),
    ("codexctl.web.gateway", "codexctl.web.routes.gateway"),
]

EXPECTED_ROUTES = set(
    """
GET /
GET /accounts
GET /accounts/login/{login_id}
GET /accounts/login/{login_id}/status
GET /accounts/{label}/models
GET /accounts/{label}/quota
GET /accounts/{label}/quota.csv
GET /audit
GET /dashboard/server-metrics
GET /dashboard/trends
GET /favicon.ico
GET /login
GET /pricing
GET /servers
GET /servers/new
GET /servers/{server_id}
GET /servers/{server_id}/edit
GET /servers/{server_id}/wp-center
GET /servers/{server_id}/wp-center/document
GET /servers/{server_id}/sites/{site_id}/{operation}
GET /servers/{server_id}/sites/refresh
GET /skills
GET /skills/{skill_id}/download
GET /tasks
GET /tasks/{task_id}
GET /tasks/{task_id}/models
GET /usage
GET /usage.csv
GET /usage/api
GET /users
GET /users/{name}
GET /users/{name}/api-key
GET /v1/
GET /v1/codex/models
GET /v1/models
GET /wp-center
POST /accounts/login
POST /accounts/login/{login_id}/cancel
POST /accounts/{label}/disable
POST /accounts/{label}/enable
POST /accounts/{label}/models/action
POST /accounts/{label}/models/refresh
POST /accounts/{label}/name
POST /accounts/{label}/quota-refresh
POST /accounts/{label}/refresh
POST /accounts/{label}/republish
POST /login
POST /logout
POST /pricing/add
POST /pricing/{price_id}
POST /pricing/{price_id}/delete
POST /servers
POST /servers/{server_id}/delete
POST /servers/{server_id}/edit
POST /servers/{server_id}/enrollment
POST /servers/{server_id}/enrollment/password
POST /servers/{server_id}/wp-center/proxy/{endpoint}
POST /servers/{server_id}/sites/{site_id}/{operation}
POST /servers/{server_id}/sites/refresh
POST /site-enrollment/complete
POST /site-enrollment/install
POST /skills/save
POST /skills/{skill_id}/delete
POST /tasks/prompt-template
POST /tasks/import
POST /tasks/{task_id}/cancel
POST /tasks/{task_id}/confirm
POST /tasks/{task_id}/prompt
POST /users/assign
POST /users/create
POST /users/unassign
POST /users/{name}/api-key
POST /users/{name}/fix
POST /users/{name}/restart-codex
POST /users/{name}/web-password
POST /v1/responses
POST /v1/responses/compact
""".splitlines()[1:]
)


@pytest.mark.parametrize("legacy,current", LEGACY_MODULES)
def test_legacy_modules_share_implementation(legacy, current):
    assert importlib.import_module(legacy) is importlib.import_module(current)


def test_original_package_exports():
    from codexctl import accounts, sites
    from codexctl.accounts import service

    assert accounts.import_account is service.import_account
    assert accounts.AccountError is service.AccountError
    assert sites.Site is inventory.Site
    assert sites.refresh is inventory.refresh


def test_repository_exports_keep_identity():
    assert repo.get_account is account_repository.get_account
    assert repo.active_assignment is user_repository.active_assignment
    assert repo.usage_summary is usage_repository.usage_summary


def test_schema_matches_expected_definition():
    assert (
        hashlib.sha256(db.SCHEMA.encode()).hexdigest()
        == "c9e7bebe076b8014c8a95d19b6659d158c8aadcc56a505e44b1cc59d8b8a1dcf"
    )


@pytest.mark.parametrize("gateway_enabled", [False, True])
def test_route_contract_is_preserved(ctx, gateway_enabled):
    ctx.settings = dataclasses.replace(ctx.settings, gateway_enabled=gateway_enabled)
    webauth.create_admin(ctx.conn, "admin", "test-admin-password", now=ctx.now())
    app = create_app(ctx)
    actual = set()
    for route in app.routes:
        children = route.app.routes if route.path == "/v1" else [route]
        prefix = "/v1" if route.path == "/v1" else ""
        for child in children:
            for method in getattr(child, "methods", set()):
                if method in {"GET", "POST"}:
                    actual.add(f"{method} {prefix}{child.path}")
    expected = (
        EXPECTED_ROUTES
        if gateway_enabled
        else {
            route
            for route in EXPECTED_ROUTES
            if not route.split()[1].startswith("/v1/")
            and route.split()[1] != "/usage/api"
            and not (route.split()[1].startswith("/accounts/") and "/models" in route.split()[1])
            and "/api-key" not in route.split()[1]
            and "/web-password" not in route.split()[1]
        }
    )
    assert actual == expected


@pytest.mark.parametrize(
    "first",
    [
        "codexctl.config",
        "codexctl.accounts.service",
        "codexctl.usage.analytics",
        "codexctl.sites.inventory",
        "codexctl.web.app",
        "codexctl.gateway.api",
    ],
)
def test_imports_work_in_fresh_process(first):
    code = (
        "import importlib, pkgutil, codexctl; "
        f"importlib.import_module({first!r}); "
        "[importlib.import_module(module.name) for module in "
        "pkgutil.walk_packages(codexctl.__path__, codexctl.__name__ + '.')]; "
        "print('imports ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imports ok"
