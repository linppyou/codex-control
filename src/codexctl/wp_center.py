from __future__ import annotations

import base64
import json
import html
import re
import shlex
from pathlib import Path

import codexctl.sites.ssh as site_ssh
from codexctl.sites.registry import Server

from codexctl import wp_actions


class WPError(RuntimeError):
    pass


# The page's local uploader base64-encodes each chunk and then wraps it in a
# form-urlencoded request. Keep the encoded request comfortably below common
# reverse-proxy 1 MiB limits instead of using the panel's 1 MiB chunk size.
PROXY_UPLOAD_CHUNK_SIZE = 256 * 1024


_PAGE_READER = r'''
import json
from pathlib import Path
root=Path('/www/server/panel/plugin/ai_center')
def read(name,limit):
    value=(root/name).read_bytes()
    if len(value)>limit:raise ValueError('WP 建站中心页面过大')
    return value
before=read('info.json',65536)
page=read('static/page.html',4*1024*1024)
after=read('info.json',65536)
if before!=after:raise ValueError('WP 建站中心正在更新，请稍后重试')
version=str(json.loads(before.decode('utf-8')).get('versions',''))
if not version:raise ValueError('无法识别 WP 建站中心版本')
print(json.dumps({'version':version,'page':page.decode('utf-8')},ensure_ascii=False,separators=(',',':')))
'''


_PLUGIN_CALL = r'''
import json,os,sys
from contextlib import redirect_stdout
sys.path[:0]=['/www/server/panel/plugin/ai_center','/www/server/panel','/www/server/panel/class']
allowed=set(__WORKSPACE_ACTIONS__) | {'maintenance_download_path'}
request=json.load(sys.stdin);action=request.get('action');payload=request.get('payload')
try:
    if action not in allowed or not isinstance(payload,dict):raise ValueError('Unsupported action')
    os.chdir('/www/server/panel')
    with open(os.devnull,'w') as sink,redirect_stdout(sink):
        from BTPanel import app
        from aic_service import Service
        from aic_settings import SettingsService
        with app.test_request_context('/plugin?action=a&name=ai_center&s=total_center_ssh'):
            service=SettingsService() if action=='settings_overview' else Service('/www/server/panel/plugin/ai_center/data',panel=True)
            if action in ('data_agent_api','renovation_api'):
                from aic_agent_origin import agent_origin
                with agent_origin('ssh'):result=getattr(service,action)(payload)
            elif action=='maintenance_download_path':result=str(service.maintenance_download_path(payload.get('ticket','')))
            elif not callable(getattr(service,action,None)):raise ValueError('当前子服务器插件不支持此操作，请升级 wp建站中心')
            else:result=getattr(service,action)(payload)
    response={'status':True,'data':result}
except ValueError as error:response={'status':False,'msg':str(error)[:500]}
except Exception:response={'status':False,'msg':'SSH WP 工作台操作未完成，请检查服务器日志'}
print(json.dumps(response,ensure_ascii=False,separators=(',',':')))
'''.replace('__WORKSPACE_ACTIONS__', repr(sorted(wp_actions.WORKSPACE_ACTIONS)))


_THEME_READER = r'''
import json,re,sys
from pathlib import Path
root=Path('/www/server/panel/BTPanel/static')
css=(root/'css/style.css').read_text(encoding='utf-8')
if len(css)>8*1024*1024:raise ValueError('主题样式过大')
rules={'light':{},'dark':{}}
for selector,body in re.findall(r'(?:^|})(:root|html\.dark)\s*\{([^{}]*)\}',css):
    target=rules['dark' if selector=='html.dark' else 'light']
    for name,value in re.findall(r'(--el-[\w-]+)\s*:\s*([^;{}]+)',body):
        if len(value)<300 and not re.search(r'url\s*\(|expression\s*\(|[<>]',value,re.I):target[name]=value.strip().replace('!important','').strip()
try:
    js=(root/'js/login.js').read_text(encoding='utf-8')
    block=js.split('registerThemeVariables(e)',1)[1].split('e.login&&',1)[0]
    radii={}
    for name,part in re.findall(r'case"([a-z]+)":(.*?)break',block):
        radii[name]=dict(re.findall(r't\["(--el-border-radius-(?:small|base))"\]="([0-9.]+px)"',part))
except Exception:radii={}
try:
    sys.path[:0]=['/www/server/panel','/www/server/panel/class']
    from theme_config import ThemeConfigManager
    result=ThemeConfigManager().get_config()
    settings=result.get('data',{}) if isinstance(result,dict) else {}
except Exception:settings={}
print(json.dumps({'settings':settings,'assets':{'rules':rules,'radii':radii}},ensure_ascii=False,separators=(',',':')))
'''


def _command(script: str) -> str:
    encoded = base64.b64encode(script.encode()).decode()
    return '/www/server/panel/pyenv/bin/python -c ' + shlex.quote(
        "import base64;exec(base64.b64decode(" + repr(encoded) + "))"
    )


def _run(server: Server, key: Path, command: str, *, timeout: int, payload: str | None = None):
    if not server.host_key:
        raise WPError("服务器尚未确认 SSH 主机公钥")
    result = site_ssh.run(server, key, command, timeout=timeout, payload=payload)
    if result.returncode:
        detail = result.stderr.strip()[:200]
        raise WPError(detail or "SSH WP 工作台操作失败")
    return result.stdout


def installed_workspace(server: Server, key: Path) -> tuple[str, str]:
    output = _run(server, key, _command(_PAGE_READER), timeout=60)
    if len(output) > 6 * 1024 * 1024:
        raise WPError("WP 建站中心响应过大")
    try:
        result = json.loads(output)
        version, page = str(result['version']), str(result['page'])
    except (TypeError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise WPError("WP 建站中心页面格式异常") from exc
    runtime = re.search(r"window\.AI_CENTER_RUNTIME\s*=\s*\{([^}]{0,500})\}", page)
    page_version = re.search(r'''\bversion\s*:\s*["']([^"']+)["']''', runtime.group(1)) if runtime else None
    if page_version and page_version.group(1) != version:
        raise WPError("WP 建站中心页面与插件版本不一致，请稍后重试")
    required = ("panelForm('/plugin?action=a&name=ai_center&s=", "headers['x-http-token']=csrf", '<head>')
    if any(marker not in page for marker in required):
        raise WPError("WP 建站中心页面结构已变化，请更新适配")
    return version, page


def theme_assets(server: Server, key: Path) -> tuple[dict, dict]:
    output = _run(server, key, _command(_THEME_READER), timeout=40)
    if len(output) > 512 * 1024:
        raise WPError("WP 建站中心主题响应过大")
    try:
        result = json.loads(output)
        settings = result.get('settings', {})
        assets = result.get('assets', {})
        if not isinstance(settings, dict) or not isinstance(assets, dict):
            raise ValueError
        if not assets.get('rules', {}).get('light'):
            raise ValueError
        return settings, assets
    except (TypeError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise WPError("WP 建站中心主题格式异常") from exc


_FRAME_BRIDGE = """<script>
(() => {
    const fetchDirectly = window.fetch.bind(window);
    window.vite_public_request_token = 'wp-center-frame';
    window.fetch = (input, options) => {
        if (typeof input !== 'string' || options?.method?.toUpperCase() !== 'POST') {
            return fetchDirectly(input, options);
        }
        const url = new URL(input, location.href);
        if (url.origin !== location.origin || !['/plugin', '/config'].includes(url.pathname)) {
            return fetchDirectly(input, options);
        }
        return new Promise((resolve, reject) => {
            const channel = new MessageChannel();
            const timeout = setTimeout(() => {
                channel.port1.close();
                reject(new Error('WP 建站中心请求超时'));
            }, 100000);
            channel.port1.onmessage = ({data}) => {
                clearTimeout(timeout);
                channel.port1.close();
                if (data.error) reject(new Error(data.error));
                else resolve(new Response(data.body, {
                    status: data.status,
                    headers: {'Content-Type': 'application/json'}
                }));
            };
            parent.postMessage({
                endpoint: url.pathname,
                query: url.search,
                body: new URLSearchParams(options.body).toString(),
            }, location.origin, [channel.port2]);
        });
    };
})();
</script>"""


def embed(page: str, origin: str, server_name: str = '', server_host: str = '') -> str:
    runtime_pattern = re.compile(
        r"(window\.AI_CENTER_RUNTIME\s*=\s*\{)",
    )
    page, runtime_rewrites = runtime_pattern.subn(r'\1codexctl_proxy_upload:true,', page, count=1)
    if not runtime_rewrites:
        raise WPError("WP 建站中心上传通道结构已变化，请更新适配")
    page, upload_rewrites = re.subn(
        r"if\(window\.AI_CENTER_RUNTIME\?\.transport==='panel'\) return nativeUpload\(",
        "if(window.AI_CENTER_RUNTIME?.transport==='panel' && !window.AI_CENTER_RUNTIME?.codexctl_proxy_upload) return nativeUpload(",
        page,
        count=1,
    )
    if not upload_rewrites and ('nativeUpload' in page or 'AICFileDrop.upload' not in page):
        raise WPError("WP 建站中心上传逻辑已变化，请更新适配")
    page = page.replace('location.origin', json.dumps(origin).replace('<', '\\u003c'), 1)
    label = html.escape(server_name or '目标服务器', quote=True)
    host = html.escape(server_host, quote=True)
    context = (
        '<div id="codexctl-wp-server-context" role="status">'
        '<span class="codexctl-wp-context-product">WP 建站中心</span>'
        '<strong class="codexctl-wp-server-name">' + label + '</strong>'
        '<span class="codexctl-wp-server-ip">' + host + '</span>'
        '</div>'
    )
    style = '''<style id="codexctl-wp-server-context-style">
#codexctl-wp-server-context{display:flex;align-items:center;gap:10px;min-height:42px;padding:0 24px;background:var(--aic-card,#fff);border-bottom:1px solid var(--aic-border,#e8eeeb);color:var(--aic-regular,#20302c);font:12px/1.4 Inter,"Segoe UI","Microsoft YaHei",sans-serif;position:sticky;top:0;z-index:100000}
#codexctl-wp-server-context .codexctl-wp-context-product{color:var(--aic-primary,#168b61);font-size:10px;letter-spacing:.4px;font-weight:600}
#codexctl-wp-server-context .codexctl-wp-server-name{font-size:13px;font-weight:650}
#codexctl-wp-server-context .codexctl-wp-server-ip{color:var(--aic-muted,#87908d);font-size:11px;font-family:Consolas,monospace}
</style>'''
    page = page.replace('<head>', '<head>' + _FRAME_BRIDGE + style, 1)
    page = page.replace('<div class="aic-workspace">', '<div class="aic-workspace">' + context, 1)
    page = page.replace('<title>wp建站中心', '<title>' + html.escape(server_name or '目标服务器') + ' · wp建站中心', 1)
    return page


def embed_theme(page: str, settings: dict, assets: dict) -> str:
    climb = 'try { for (let n=0; n<8 && host.parent!==host; n++) { void host.parent.document.documentElement; host=host.parent; } } catch (_) {}'
    if climb not in page:
        raise WPError("WP 建站中心主题同步接口已变化")
    theme = settings.get('theme') if isinstance(settings.get('theme'), dict) else {}
    interface = settings.get('interface') if isinstance(settings.get('interface'), dict) else {}
    preset = theme.get('preset') if theme.get('preset') in ('light', 'dark', 'auto') else 'auto'
    color = str(theme.get('color') or '')
    if not re.fullmatch(r'#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?', color):
        color = ''
    radii = assets.get('radii', {}).get(interface.get('rounded'), {})
    data = json.dumps(
        {'preset': preset, 'color': color, 'rules': assets['rules'], 'radii': radii},
        ensure_ascii=True,
    ).replace('<', '\\u003c')
    script = """<script>(function(){const t=DATA,root=document.documentElement,media=matchMedia('(prefers-color-scheme: dark)');function apply(){const dark=t.preset==='dark'||(t.preset==='auto'&&media.matches);root.classList.toggle('dark',dark);for(const [k,v] of Object.entries(t.rules.light||{}))root.style.setProperty(k,v);if(dark)for(const [k,v] of Object.entries(t.rules.dark||{}))root.style.setProperty(k,v);if(t.color){root.style.setProperty('--el-color-primary',t.color);root.style.setProperty('--el-color-primary-dark-2','color-mix(in srgb, '+t.color+', black 20%)')}for(const [k,v] of Object.entries(t.radii||{}))root.style.setProperty(k,v);window.AIC_THEME_SYNC?.sync()}apply();if(t.preset==='auto')media.addEventListener('change',apply)})();</script>""".replace('DATA', data)
    return page.replace(climb, '/* Use this child panel theme source. */', 1).replace('<head>', '<head>' + script, 1)


def plugin(server: Server, key: Path, action: str, payload: dict) -> dict:
    if action not in wp_actions.WORKSPACE_ACTIONS:
        raise WPError("不支持的 WP 建站中心操作")
    output = _run(
        server,
        key,
        _command(_PLUGIN_CALL),
        timeout=90,
        payload=json.dumps({'action': action, 'payload': payload}, ensure_ascii=False),
    )
    try:
        result = json.loads(output)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WPError("WP 建站中心响应格式异常") from exc
    if not result.get('status'):
        raise WPError(str(result.get('msg', 'WP 建站中心操作失败'))[:300])
    return result.get('data')
