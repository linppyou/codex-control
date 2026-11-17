"""Compatibility helpers and registration for the primary web routes."""

from codexctl.web.presenters import (
    _TERMINAL,
    HANDLED,
    _account_rows,
    _account_table,
    _card,
    _complete_finished_logins,
    _countdown,
    _current_account_quota,
    _finish_login,
    _finish_login_import,
    _mask_email,
    _projection,
    _quota_days,
    _quota_view,
    _safe_next,
    _tone,
    _used,
    _week_window,
)
from codexctl.web.responses import _csv_cell, _flash, _redirect
from codexctl.web.routes import accounts, audit, dashboard, pricing, usage, users


def register(app) -> None:
    dashboard.register(app)
    accounts.register(app)
    users.register(app)
    usage.register(app)
    pricing.register(app)
    audit.register(app)
