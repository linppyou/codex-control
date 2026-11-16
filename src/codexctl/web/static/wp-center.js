(() => {
  const center = document.querySelector('#wp-center');
  const base = '/wp-center/' + encodeURIComponent(center.dataset.server);
  const form = document.querySelector('#wp-create');
  const templates = form.elements.namedItem('template');
  const databases = form.elements.namedItem('sql');
  const oldDomain = form.elements.namedItem('old_domain');
  const submit = form.querySelector('button[type=submit]');
  const refreshCatalog = document.querySelector('#wp-refresh-catalog');
  const templateNote = document.querySelector('#wp-template-note');
  const submitNote = document.querySelector('#wp-submit-note');
  const history = document.querySelector('#wp-history');
  const refresh = document.querySelector('#wp-refresh');
  let externalDatabases = [];
  let inspected = null;
  let inspection;
  let sending = false;

  async function json(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || data.detail || '请求失败，请刷新页面后重试');
    return data;
  }

  function option(resource) {
    const entry = new Option(resource.name, resource.id);
    entry.dataset.domain = resource.suggested_domain || '';
    if (resource.origin === 'upload') entry.text += '（已上传）';
    return entry;
  }

  function updateSubmit() {
    const selected = databases.selectedOptions[0];
    const defaultDatabase = inspected?.databases.length === 1 ? inspected.databases[0] : null;
    const suggestion = selected?.value ? selected.dataset.domain : defaultDatabase?.suggested_domain;
    oldDomain.placeholder = suggestion || '请输入 SQL 中的原站域名';
    oldDomain.required = !suggestion;
    submit.disabled = sending || !templates.value || !inspected || databases.disabled;
  }

  function showDatabases() {
    const embedded = inspected?.databases || [];
    databases.replaceChildren(new Option(embedded.length === 1 ? '自动选择模板内 SQL' : '请选择数据库 SQL', ''));
    for (const [label, items] of [['模板内 SQL', embedded], ['服务器上的 SQL', externalDatabases]]) {
      if (items.length === 0) continue;
      const group = document.createElement('optgroup');
      group.label = label;
      group.append(...items.map(option));
      databases.append(group);
    }
    databases.disabled = !inspected || (!embedded.length && !externalDatabases.length);
    databases.required = embedded.length !== 1;
    updateSubmit();
  }

  function note(element, message) {
    element.textContent = message;
    element.hidden = !message;
  }

  async function loadTemplate() {
    inspection?.abort();
    inspected = null;
    showDatabases();
    note(templateNote, '');
    if (!templates.value) return;
    const request = new AbortController();
    inspection = request;
    note(templateNote, '正在检查模板内的 SQL…');
    try {
      const template = encodeURIComponent(templates.value);
      const data = await json(base + '/template?template=' + template, {signal: request.signal});
      if (request.signal.aborted) return;
      inspected = data;
      showDatabases();
      const warnings = [...data.warnings];
      if (!data.databases.length && !externalDatabases.length) warnings.push('请先在目标服务器的 WP 建站中心登记 SQL 文件。');
      note(templateNote, warnings.join(' '));
    } catch (error) {
      if (!request.signal.aborted) note(templateNote, error.message);
    }
  }

  async function loadCatalog() {
    inspection?.abort();
    inspected = null;
    refreshCatalog.disabled = true;
    templates.disabled = true;
    const previous = templates.value;
    templates.replaceChildren(new Option('正在读取模板…', ''));
    showDatabases();
    try {
      const data = await json(base + '/catalog');
      externalDatabases = data.databases;
      templates.replaceChildren(new Option(data.templates.length ? '请选择网站模板 ZIP' : '服务器上没有可用的模板 ZIP', ''));
      templates.append(...data.templates.map(option));
      templates.disabled = !data.templates.length;
      if (data.templates.some(item => item.id === previous)) templates.value = previous;
      await loadTemplate();
    } catch (error) {
      templates.replaceChildren(new Option('读取模板失败', ''));
      note(templateNote, error.message);
    } finally {
      refreshCatalog.disabled = false;
    }
  }

  async function loadHistory() {
    if (refresh.disabled) return;
    refresh.disabled = true;
    history.setAttribute('aria-busy', 'true');
    const details = [...history.querySelectorAll('details')].map(item => item.open);
    try {
      const response = await fetch(base + '/history?page=' + encodeURIComponent(center.dataset.page));
      if (response.redirected || ![200, 502].includes(response.status)) throw new Error();
      history.innerHTML = await response.text();
      history.querySelectorAll('details').forEach((item, index) => { item.open = details[index] || false; });
    } catch {
      const error = document.createElement('p');
      error.className = 'wp-history-error flash bad';
      error.setAttribute('role', 'status');
      error.textContent = '读取任务失败，请刷新任务。';
      history.replaceChildren(error);
    } finally {
      history.setAttribute('aria-busy', 'false');
      refresh.disabled = false;
    }
  }

  templates.addEventListener('change', loadTemplate);
  databases.addEventListener('change', updateSubmit);
  refreshCatalog.addEventListener('click', loadCatalog);
  refresh.addEventListener('click', loadHistory);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (sending) return;
    sending = true;
    const body = new FormData(form);
    for (const field of [templates, databases, oldDomain, refreshCatalog]) field.disabled = true;
    updateSubmit();
    submitNote.className = 'wp-full flash';
    note(submitNote, '正在检查网站资源并提交建站任务…');
    try {
      const result = await json(form.action, {method: 'POST', body});
      form.elements.namedItem('domain').value = '';
      note(submitNote, result.message);
      submitNote.classList.add('ok');
      if (center.dataset.page !== '1') {
        center.dataset.page = '1';
        window.history.replaceState(null, '', result.history);
      }
      await loadHistory();
    } catch (error) {
      note(submitNote, error.message);
      submitNote.classList.add('bad');
    } finally {
      sending = false;
      for (const field of [templates, databases, oldDomain, refreshCatalog]) field.disabled = false;
      updateSubmit();
    }
  });
  loadCatalog();
  loadHistory();
  setInterval(() => {
    if (!document.hidden && !history.contains(document.activeElement) && history.querySelector('[data-active]')) {
      loadHistory();
    }
  }, 12000);
})();
