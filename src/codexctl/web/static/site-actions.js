(function () {
  const section = document.querySelector('[data-sites]');
  const dialog = document.querySelector('[data-site-dialog]');
  if (!section || !dialog) return;

  const title = dialog.querySelector('[data-dialog-title]');
  const target = dialog.querySelector('[data-dialog-target]');
  const content = dialog.querySelector('[data-dialog-content]');
  const error = dialog.querySelector('[data-dialog-error]');
  const submit = dialog.querySelector('[data-dialog-submit]');
  const notice = section.querySelector('[data-site-notice]');
  let onSubmit = null;
  let dialogRevision = 0;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  async function request(url, values) {
    const options = values ? {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': section.dataset.csrf},
      body: new URLSearchParams(values),
    } : {};
    const response = await fetch(url, options);
    let result;
    try { result = await response.json(); } catch (_) { result = {}; }
    if (!response.ok) {
      const failure = new Error(result.detail || '请求未完成，请刷新后核对站点');
      failure.status = response.status;
      throw failure;
    }
    return result;
  }

  function reload(message, row) {
    try {
      sessionStorage.setItem('site-action-result', JSON.stringify({
        message,
        server: row.closest('details.site-server')?.querySelector('summary')?.id,
        scroll: window.scrollY,
      }));
    } catch (_) {}
    window.location.reload();
  }

  try {
    const saved = JSON.parse(sessionStorage.getItem('site-action-result') || 'null');
    sessionStorage.removeItem('site-action-result');
    if (saved) {
      notice.textContent = saved.message;
      const server = saved.server && document.getElementById(saved.server);
      if (server) server.parentElement.open = true;
      if (Number.isFinite(saved.scroll)) window.scrollTo(0, saved.scroll);
    }
  } catch (_) {}

  function openDialog(row, heading, buttonText) {
    dialogRevision += 1;
    title.textContent = heading;
    target.textContent = row.dataset.domain + ' · ' + row.dataset.server;
    content.replaceChildren();
    error.textContent = '';
    submit.textContent = buttonText;
    submit.disabled = false;
    onSubmit = null;
    if (!dialog.open) dialog.showModal();
  }

  dialog.querySelector('[data-dialog-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { dialogRevision += 1; });
  submit.addEventListener('click', async () => {
    if (!onSubmit) return;
    submit.disabled = true;
    error.textContent = '';
    try {
      await onSubmit();
    } catch (failure) {
      error.textContent = failure.message || '请求未完成，请刷新后核对站点';
      submit.disabled = !failure.status || failure.status >= 500;
    }
  });

  function showState(row) {
    const enabled = row.dataset.enabled === 'true';
    const next = !enabled;
    openDialog(row, next ? '启用站点' : '停用站点', next ? '确认启用' : '确认停用');
    content.append(element('p', next ? '启用后站点将恢复运行。' : '停用后访问者将看到站点停用提示。'));
    onSubmit = async () => {
      await request(row.dataset.endpoint + '/state', {name: row.dataset.name, expected: String(enabled), enabled: String(next)});
      reload(next ? '站点已启用。' : '站点已停用。', row);
    };
  }

  function showRemark(row) {
    openDialog(row, '编辑备注', '保存');
    const label = element('label', '宝塔站点备注');
    const input = document.createElement('textarea');
    input.value = row.dataset.remark;
    input.maxLength = 500;
    input.rows = 4;
    label.append(input);
    content.append(label);
    onSubmit = async () => {
      await request(row.dataset.endpoint + '/remark', {name: row.dataset.name, remark: input.value});
      reload('备注已保存。', row);
    };
    input.focus();
  }

  async function showWaf(row) {
    openDialog(row, '网站防火墙', '保存');
    const revision = dialogRevision;
    submit.disabled = true;
    content.append(element('p', '正在读取防火墙状态…'));
    const data = await request(row.dataset.endpoint + '/waf');
    if (!dialog.open || revision !== dialogRevision) return;
    content.replaceChildren();
    const label = element('label', undefined, 'site-option');
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.checked = data.enabled;
    label.append(toggle, element('span', '开启网站防火墙'));
    content.append(label);
    submit.disabled = false;
    onSubmit = async () => {
      await request(row.dataset.endpoint + '/waf', {name: row.dataset.name, expected: String(data.enabled), enabled: String(toggle.checked)});
      reload('防火墙状态已更新。', row);
    };
  }

  function option(name, description) {
    const label = element('label', undefined, 'site-option');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.name = name;
    label.append(checkbox, element('span', description));
    return label;
  }

  async function showDelete(row) {
    openDialog(row, '删除站点', '下一步');
    const revision = dialogRevision;
    submit.disabled = true;
    content.append(element('p', '正在读取宝塔站点信息…'));
    const data = await request(row.dataset.endpoint + '/inspect');
    if (!dialog.open || revision !== dialogRevision) return;
    const path = data.site.path;
    content.replaceChildren();
    content.append(element('p', '网站目录 ' + (path || '未获取')));
    content.append(element('p', '删除网站记录和域名绑定。可选择同时删除下列资源。'));
    content.append(option('delete_ftp', '关联 FTP'), option('delete_database', '关联数据库'), option('delete_path', '网站根目录及文件'));
    submit.disabled = false;
    onSubmit = async () => {
      const selected = [...content.querySelectorAll('input:checked')];
      const values = {name: row.dataset.name, path, confirm: 'true', delete_ftp: 'false', delete_database: 'false', delete_path: 'false'};
      selected.forEach(input => { values[input.name] = 'true'; });
      const labels = selected.map(input => input.nextElementSibling.textContent);
      title.textContent = '再次确认删除';
      content.replaceChildren(
        element('p', '网站目录 ' + (path || '未获取')),
        element('p', '将删除网站记录和绑定域名' + (labels.length ? '，以及' + labels.join('、') : '') + '。'),
        element('p', '目录和数据库能否从宝塔回收站恢复取决于服务器设置。', 'hint'),
      );
      submit.textContent = '确认删除';
      submit.disabled = false;
      onSubmit = async () => {
        await request(row.dataset.endpoint + '/delete', values);
        reload('站点已删除，请核对所选资源和宝塔回收站。', row);
      };
    };
  }

  section.addEventListener('click', async event => {
    const refreshButton = event.target.closest('[data-site-refresh]');
    if (refreshButton) {
      refreshButton.disabled = true;
      const message = refreshButton.parentElement.querySelector('[data-refresh-status]');
      const url = '/servers/' + encodeURIComponent(refreshButton.dataset.siteRefresh) + '/sites/refresh';
      try {
        message.textContent = '正在刷新网站清单…';
        await request(url, {});
        for (let attempt = 0; attempt < 60; attempt += 1) {
          const status = await request(url);
          if (status.done) {
            if (status.error) throw new Error(status.error);
            const group = refreshButton.closest('details.site-server');
            reload('网站清单已刷新。', group.querySelector('[data-site-row]'));
            return;
          }
          await new Promise(resolve => setTimeout(resolve, 2000));
        }
        message.textContent = '刷新仍在进行，请稍后查看。';
      } catch (failure) {
        message.textContent = failure.message || '刷新失败，请稍后重试。';
      } finally {
        refreshButton.disabled = false;
      }
      return;
    }
    const button = event.target.closest('[data-site-action]');
    const row = button?.closest('[data-site-row]');
    if (!row || button.disabled) return;
    button.disabled = true;
    notice.textContent = '';
    try {
      switch (button.dataset.siteAction) {
        case 'probe':
          notice.textContent = '正在检测 ' + row.dataset.domain + '…';
          await request(row.dataset.endpoint + '/probe', {});
          reload('检测已完成。', row);
          break;
        case 'waf': await showWaf(row); break;
        case 'state': showState(row); break;
        case 'remark': showRemark(row); break;
        case 'delete': await showDelete(row); break;
      }
    } catch (failure) {
      if (dialog.open) error.textContent = failure.message;
      else notice.textContent = failure.message;
    } finally {
      button.disabled = false;
    }
  });
})();
