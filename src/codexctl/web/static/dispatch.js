(function () {
  async function loadModels(form) {
    const status = document.getElementById('dispatch-model-status');
    status.textContent = '正在读取 Codex 模型目录…';
    status.hidden = false;
    let catalog;
    try {
      const response = await fetch(form.dataset.modelsUrl, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('Model catalog unavailable');
      catalog = (await response.json()).models;
      if (!Array.isArray(catalog)) throw new Error('Invalid model catalog');
    } catch (error) {
      status.textContent = '暂时无法读取 Codex 模型目录，可手动填写模型 ID 和思考程度。';
      return;
    }
    if (!catalog.length) {
      status.textContent = '目录中没有可选的 GPT 模型，可手动填写模型 ID 和思考程度。';
      return;
    }
    status.hidden = true;

    const models = new Map(catalog.map(model => [model.id, model]));
    const modelInput = form.elements.model;
    const effortInput = form.elements.reasoning_effort;
    const modelChoice = document.getElementById('dispatch-model-choice');
    const effortChoice = document.getElementById('dispatch-effort-choice');
    const effortLabel = document.getElementById('dispatch-effort-label');

    function showEfforts(keepDraftEffort) {
      const model = models.get(modelChoice.value);
      modelInput.hidden = Boolean(model);
      const selectableEfforts = Boolean(model && model.efforts.length);
      effortInput.hidden = selectableEfforts;
      effortChoice.hidden = !selectableEfforts;
      effortLabel.htmlFor = selectableEfforts ? effortChoice.id : effortInput.id;
      if (!selectableEfforts) return;

      effortChoice.replaceChildren(...model.efforts.map(effort => new Option(effort, effort)));
      const current = effortInput.value.trim();
      const supported = model.efforts.includes(current);
      const keepCurrent = supported || (keepDraftEffort && current !== '');
      if (keepCurrent && !supported) {
        effortChoice.add(new Option(`${current}（目录未列出）`, current));
      }
      effortChoice.value = keepCurrent ? current : model.default_effort;
      effortInput.value = effortChoice.value;
    }

    modelChoice.replaceChildren(...catalog.map(model => new Option(model.name, model.id)), new Option('自定义模型 ID', ''));
    const current = modelInput.value.trim();
    modelChoice.value = models.has(current) ? current : '';
    document.getElementById('dispatch-model-label').htmlFor = modelChoice.id;
    modelInput.setAttribute('aria-label', '自定义模型 ID');
    modelChoice.hidden = false;
    showEfforts(true);

    modelChoice.addEventListener('change', function () {
      if (modelChoice.value) modelInput.value = modelChoice.value;
      showEfforts(false);
    });
    effortChoice.addEventListener('change', function () { effortInput.value = effortChoice.value; });
  }

  const confirmForm = document.getElementById('dispatch-confirm');
  if (confirmForm) loadModels(confirmForm);

  var progress = document.getElementById('dispatch-progress');
  if (!progress) return;
  var timer;
  var pending;
  var warning = document.getElementById('dispatch-refresh-error');

  function schedule() {
    window.clearTimeout(timer);
    if (!document.hidden && progress.dataset.dispatchRefresh === 'true') {
      timer = window.setTimeout(refresh, 5000);
    }
  }

  async function refresh() {
    if (document.hidden || pending) return;
    pending = new AbortController();
    var timeout = window.setTimeout(function () { pending.abort(); }, 15000);
    try {
      var response = await fetch(window.location.href, {
        credentials: 'same-origin', cache: 'no-store', signal: pending.signal
      });
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error('Status update failed');
      var page = new DOMParser().parseFromString(await response.text(), 'text/html');
      var updated = page.getElementById('dispatch-progress');
      if (!updated) throw new Error('Missing task status');
      progress.replaceChildren(...updated.childNodes);
      progress.dataset.dispatchRefresh = updated.dataset.dispatchRefresh;
      warning.hidden = true;
    } catch (error) {
      if (error.name !== 'AbortError') warning.hidden = false;
    } finally {
      window.clearTimeout(timeout);
      pending = null;
      schedule();
    }
  }

  document.addEventListener('visibilitychange', function () {
    window.clearTimeout(timer);
    if (document.hidden) {
      if (pending) pending.abort();
    } else if (progress.dataset.dispatchRefresh === 'true') {
      refresh();
    }
  });
  schedule();
})();
