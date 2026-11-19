(function () {
  var range = document.querySelector('.dashboard-trends-heading .analytics-range');
  var status = document.querySelector('[data-trend-status]');
  var results = document.querySelector('[data-trend-results]');
  if (!range || !status || !results) return;

  var links = Array.from(range.querySelectorAll('a'));
  var cache = new Map();
  var pending = null;
  var cacheLifetime = 30000;

  function daysFrom(url) {
    var days = url.searchParams.get('days');
    return ['7', '30', '90'].includes(days) ? days : '7';
  }

  function setStatus(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  function select(days) {
    links.forEach(function (link) {
      var selected = daysFrom(new URL(link.href)) === days;
      link.classList.toggle('active', selected);
      if (selected) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function remember(days, html) {
    cache.set(days, { html: html, expires: Date.now() + cacheLifetime });
  }

  function replaceResults(next) {
    var open = Array.from(results.querySelectorAll('details'), function (detail) { return detail.open; });
    next.querySelectorAll('details').forEach(function (detail, index) { detail.open = Boolean(open[index]); });
    results.replaceWith(next);
    results = next;
    document.dispatchEvent(new CustomEvent('dashboard:trends', { detail: next }));
  }

  async function load(url, updateHistory) {
    var days = daysFrom(url);
    if (pending) pending.abort();
    pending = null;
    results.removeAttribute('aria-busy');
    setStatus('');
    if (days === results.dataset.trendResults) return;

    var request = new AbortController();
    pending = request;
    try {
      var entry = cache.get(days);
      var html;
      if (entry && entry.expires > Date.now()) {
        html = entry.html;
      } else {
        results.setAttribute('aria-busy', 'true');
        setStatus('正在更新 ' + days + ' 天的数据趋势…');
        var response = await fetch('/dashboard/trends?days=' + days, { signal: request.signal });
        if (response.redirected && new URL(response.url).pathname === '/login' && pending === request) {
          window.location.assign(response.url);
          return;
        }
        if (!response.ok) throw new Error('趋势请求失败');
        html = await response.text();
      }
      if (pending !== request) return;
      var template = document.createElement('template');
      template.innerHTML = html;
      var next = template.content.querySelector('[data-trend-results="' + days + '"]');
      if (!next) throw new Error('趋势响应格式错误');
      if (!entry || entry.expires <= Date.now()) remember(days, html);

      replaceResults(next);
      select(days);
      if (updateHistory) history.pushState(null, '', url);
      setStatus('');
    } catch (error) {
      if (pending === request) setStatus('数据趋势更新失败。请再次点击时间范围重试。');
    } finally {
      if (pending === request) {
        pending = null;
        results.removeAttribute('aria-busy');
      }
    }
  }

  remember(results.dataset.trendResults, results.outerHTML);
  range.addEventListener('click', function (event) {
    var link = event.target.closest('a');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    load(new URL(link.href), true);
  });
  window.addEventListener('popstate', function () { load(new URL(window.location.href), false); });
})();
