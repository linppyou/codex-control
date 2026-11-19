"""Fixed-purpose BaoTa site operations over the existing pinned SSH connection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from codexctl.sites.registry import Server
from codexctl.wp_center import _command
import codexctl.sites.ssh as site_ssh


class PanelError(Exception):
    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


_REMOTE = r'''
import json, os, sqlite3, sys
from contextlib import redirect_stdout
from pathlib import Path

root = Path('/www/server/panel')
sys.path[:0] = [str(root), str(root / 'class'), str(root / 'plugin/btwaf')]
request = json.load(sys.stdin)
operation = request.get('operation')
started = False

def read_site(site_id):
    database = root / 'data/db/site.db'
    if not database.is_file():
        database = root / 'data/default.db'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3) as conn:
        row = conn.execute('SELECT name, status, ps, path FROM sites WHERE id=?', (site_id,)).fetchone()
    if row is None:
        return None
    enabled = {'1': True, '0': False}.get(str(row[1]))
    return {'name': row[0], 'enabled': enabled, 'remark': row[2] or '', 'path': row[3] or ''}

try:
    if operation not in ('inspect', 'waf_list', 'waf_read', 'waf_write', 'state', 'remark', 'delete'):
        raise ValueError('不支持的操作')
    site_id = request.get('site_id')
    if operation != 'waf_list' and (not isinstance(site_id, str) or not site_id.isascii() or not site_id.isdecimal()):
        raise ValueError('站点编号无效')
    site = read_site(site_id) if operation != 'waf_list' else None
    if operation != 'waf_list' and site is None:
        response = {'ok': False, 'kind': 'not_found', 'message': '站点不存在，请刷新列表'}
    elif site is not None and request.get('name') != site['name']:
        response = {'ok': False, 'kind': 'conflict', 'message': '宝塔网站名称已变化，请刷新列表'}
    elif operation == 'delete' and request.get('path') != site['path']:
        response = {'ok': False, 'kind': 'conflict', 'message': '网站目录已变化，请重新确认'}
    elif operation == 'state' and type(site['enabled']) is not bool:
        response = {'ok': False, 'kind': 'unavailable', 'message': '无法读取站点运行状态'}
    elif operation == 'state' and type(request.get('expected')) is bool and site['enabled'] != request['expected']:
        response = {'ok': False, 'kind': 'conflict', 'message': '站点状态已变化，请刷新列表'}
    else:
        os.chdir(str(root))
        with open(os.devnull, 'w') as sink, redirect_stdout(sink):
            import public
            from BTPanel import app, session
            with app.test_request_context('/site?action=codexctl'):
                session['username'] = 'admin'
                session['login'] = True
                if operation == 'inspect':
                    response = {'ok': True, 'site': site}
                elif operation.startswith('waf_'):
                    import btwaf_main
                    waf = btwaf_main.btwaf_main()
                    if operation == 'waf_list':
                        values = waf.get_site_config(public.to_dict_obj({}))
                        if not isinstance(values, list):
                            raise ValueError('防火墙状态不可用')
                        response = {'ok': True, 'sites': values}
                    else:
                        args = public.to_dict_obj({'siteName': site['name']})
                        current = waf.get_site_config_byname(args)
                        enabled = current.get('open') if isinstance(current, dict) else None
                        if type(enabled) is not bool:
                            raise ValueError('防火墙状态不可用')
                        if operation == 'waf_write':
                            if type(request.get('enabled')) is not bool or type(request.get('expected')) is not bool:
                                raise ValueError('防火墙状态参数无效')
                            if enabled != request['expected']:
                                response = {'ok': False, 'kind': 'conflict', 'message': '防火墙状态已变化，请重新读取'}
                            else:
                                if enabled != request['enabled']:
                                    started = True
                                    result = waf.set_site_obj_open(public.to_dict_obj({'siteName': site['name'], 'obj': 'open'}))
                                    if isinstance(result, dict) and result.get('status') is False:
                                        raise ValueError('防火墙操作未完成')
                                    current = waf.get_site_config_byname(args)
                                    enabled = current.get('open') if isinstance(current, dict) else None
                                if type(enabled) is not bool or enabled != request['enabled']:
                                    raise ValueError('防火墙回读状态不一致')
                                response = {'ok': True, 'enabled': enabled, 'site': site}
                        else:
                            response = {'ok': True, 'enabled': enabled, 'site': site}
                else:
                    if operation == 'state':
                        if type(request.get('enabled')) is not bool or type(request.get('expected')) is not bool:
                            raise ValueError('站点状态无效')
                        if site['enabled'] != request['enabled']:
                            import panelSite
                            started = True
                            action = 'SiteStart' if request['enabled'] else 'SiteStop'
                            result = getattr(panelSite.panelSite(), action)(public.to_dict_obj({'id': site_id, 'name': site['name']}))
                            if isinstance(result, dict) and result.get('status') is False:
                                raise ValueError('站点操作未完成')
                    elif operation == 'remark':
                        remark = request.get('remark')
                        if not isinstance(remark, str) or len(remark) > 500:
                            raise ValueError('备注无效')
                        if site['remark'] != public.xssencode2(remark):
                            import data
                            started = True
                            result = data.data().setPs(public.to_dict_obj({'table': 'sites', 'id': site_id, 'ps': remark}))
                            if isinstance(result, dict) and result.get('status') is False:
                                raise ValueError('备注保存未完成')
                    elif operation == 'delete':
                        if request.get('confirm') is not True or any(type(request.get(key)) is not bool for key in ('delete_ftp', 'delete_database', 'delete_path')):
                            raise ValueError('删除范围无效')
                        if request['delete_path'] and not site['path']:
                            raise ValueError('无法确认网站目录')
                        import panelSite
                        args = {'id': site_id, 'webname': site['name'], 'ftp': '1' if request['delete_ftp'] else '0', 'database': '1' if request['delete_database'] else '0'}
                        if request['delete_path']:
                            args['path'] = '1'
                        started = True
                        result = panelSite.panelSite().DeleteSite(public.to_dict_obj(args))
                        if isinstance(result, dict) and result.get('status') is False:
                            raise ValueError('删除未完成')
                    actual = read_site(site_id)
                    if operation == 'delete':
                        if actual is not None:
                            raise ValueError('宝塔仍存在该站点')
                        response = {'ok': True, 'deleted': True}
                    else:
                        if actual is None or actual['name'] != site['name']:
                            raise ValueError('站点回读失败')
                        if operation == 'state' and actual['enabled'] != request['enabled']:
                            raise ValueError('站点状态回读不一致')
                        if operation == 'remark' and actual['remark'] != public.xssencode2(request['remark']):
                            raise ValueError('备注回读不一致')
                        response = {'ok': True, 'site': actual}
except Exception:
    response = {'ok': False, 'kind': 'unknown' if started else 'unavailable',
                'message': '操作结果待核对，请刷新后查看宝塔状态' if started else '无法读取宝塔站点或防火墙，请检查服务器和插件'}
print(json.dumps(response, ensure_ascii=False, separators=(',', ':')))
'''


def call(server: Server, key: Path, operation: str, **params) -> dict:
    if not server.host_key:
        raise PanelError("服务器尚未确认 SSH 主机公钥")
    try:
        result = site_ssh.run(
            server, key, _command(_REMOTE), timeout=90,
            payload=json.dumps({"operation": operation, **params}, ensure_ascii=False),
        )
        if result.returncode or len(result.stdout) > 2_000_000:
            raise PanelError("宝塔连接未完成，请刷新后核对站点", "unknown" if operation in {"waf_write", "state", "remark", "delete"} else "unavailable")
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("invalid response")
        if response.get("ok") is not True:
            raise PanelError(str(response.get("message", "宝塔操作未完成"))[:200], str(response.get("kind", "unavailable")))
        if operation in {"inspect", "waf_read", "waf_write", "state", "remark"}:
            site = response.get("site")
            if not isinstance(site, dict) or not isinstance(site.get("name"), str) or "enabled" not in site or type(site["enabled"]) not in (bool, type(None)) or not isinstance(site.get("remark"), str) or not isinstance(site.get("path"), str):
                raise ValueError("invalid site")
            if operation == "state" and type(site["enabled"]) is not bool:
                raise ValueError("invalid site status")
        if operation in {"waf_read", "waf_write"} and type(response.get("enabled")) is not bool:
            raise ValueError("invalid WAF status")
        if operation == "delete" and response.get("deleted") is not True:
            raise ValueError("invalid deletion result")
        return response
    except PanelError:
        raise
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
        kind = "unknown" if operation in {"waf_write", "state", "remark", "delete"} else "unavailable"
        raise PanelError("宝塔连接未完成，请刷新后核对站点", kind) from exc


def waf_statuses(server: Server, key: Path) -> dict[str, bool]:
    rows = call(server, key, "waf_list").get("sites")
    if not isinstance(rows, list):
        raise PanelError("防火墙状态格式异常")
    return {
        row["siteName"]: row["open"] for row in rows
        if isinstance(row, dict) and isinstance(row.get("siteName"), str) and type(row.get("open")) is bool
    }
