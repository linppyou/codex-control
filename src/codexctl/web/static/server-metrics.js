(function () {
  var container = document.querySelector('[data-server-metrics]');
  var refreshButton = document.querySelector('[data-refresh-metrics]');
  var cards = Array.from(container.querySelectorAll('[data-server-id]'));
  var loading = false;

  function percent(value) {
    return value > 999 ? '999%+' : Math.round(value) + '%';
  }

  function gib(bytes) {
    return (bytes / (1024 * 1024 * 1024)).toFixed(1);
  }

  function updateMetric(card, key, label, value, detail) {
    var metric = card.querySelector('[data-metric="' + key + '"]');
    var display = percent(value);
    metric.querySelector('[data-detail]').textContent = detail;
    metric.querySelector('[data-percent]').textContent = display;
    metric.querySelector('.server-meter-fill').setAttribute('stroke-dasharray', Math.min(value, 100) + ' 100');
    metric.setAttribute('aria-label', label + ' ' + Math.round(value) + '%，' + detail);
  }

  function render(card, result, updatedAt) {
    var status = card.querySelector('[data-server-status]');
    if (result.error) {
      status.dataset.state = 'error';
      status.textContent = card.dataset.hasMetrics ? result.error + ' · 上次 ' + card.dataset.updatedAt : result.error;
      return;
    }
    var data = result.metrics;
    var loadPercent = data.load / data.cores * 100;
    var loadStatus = loadPercent < 70 ? '运行流畅' : loadPercent < 100 ? '负载偏高' : '负载过高';
    var diskBase = data.disk_used + data.disk_available;
    updateMetric(card, 'load', '负载', loadPercent, loadStatus + ' · ' + data.load.toFixed(2));
    updateMetric(card, 'cpu', 'CPU', data.cpu, data.cores + ' 核');
    updateMetric(card, 'memory', '内存', data.memory_used / data.memory_total * 100,
      gib(data.memory_used) + ' / ' + gib(data.memory_total) + ' GiB');
    updateMetric(card, 'disk', '磁盘 /', diskBase ? data.disk_used / diskBase * 100 : 0,
      gib(data.disk_used) + ' / ' + gib(data.disk_total) + ' GiB');
    card.dataset.hasMetrics = 'true';
    card.dataset.updatedAt = updatedAt;
    status.dataset.state = 'ok';
    status.textContent = '更新于 ' + updatedAt;
  }

  function refresh(force) {
    if (loading) return;
    loading = true;
    refreshButton.disabled = true;
    fetch('/dashboard/server-metrics' + (force ? '?refresh=1' : ''), {
      credentials: 'same-origin', headers: {'Accept': 'application/json'}
    })
      .then(function (response) {
        if (!response.ok) throw new Error('服务器数据请求失败');
        return response.json();
      })
      .then(function (data) {
        var readings = new Map(data.servers.map(function (item) { return [item.id, item]; }));
        cards.forEach(function (card) {
          render(card, readings.get(card.dataset.serverId) || {error: '服务器配置已变化，请刷新页面'}, data.updated_at);
        });
      })
      .catch(function () {
        cards.forEach(function (card) {
          render(card, {error: '服务器数据更新失败'}, card.dataset.updatedAt);
        });
      })
      .finally(function () {
        loading = false;
        refreshButton.disabled = false;
      });
  }

  refreshButton.addEventListener('click', function () { refresh(true); });
  refresh();
  window.setInterval(function () { if (!document.hidden) refresh(false); }, 60000);
})();
