(function () {
  document.querySelectorAll('.interactive-chart').forEach(function (chart) {
    var inspector = chart.parentElement.querySelector('[data-chart-inspector]');
    if (!inspector) inspector = chart.closest('.compare-chart-area, .daily-chart-wrap').querySelector('[data-chart-inspector]');
    var hits = Array.from(chart.querySelectorAll('.chart-hit'));
    if (!inspector || !hits.length) return;
    var names = chart.dataset.chartKind === 'comparison' ? JSON.parse(chart.dataset.chartNames) : [];
    var active = -1;
    var area = chart.closest('.compare-chart-area');
    var toggles = area ? Array.from(area.querySelectorAll('[data-series-toggle]')) : [];
    var seriesNodes = Array.from(chart.querySelectorAll('[data-series]'));
    function emphasize(index) {
      seriesNodes.forEach(function (node) {
        node.classList.toggle('is-muted', index !== null && Number(node.dataset.series) !== index);
      });
    }
    toggles.forEach(function (button, index) {
      button.addEventListener('pointerenter', function () { emphasize(index); });
      button.addEventListener('pointerleave', function () { emphasize(null); });
      button.addEventListener('focus', function () { emphasize(index); });
      button.addEventListener('blur', function () { emphasize(null); });
      button.addEventListener('click', function () {
        var enabled = button.getAttribute('aria-pressed') !== 'true';
        button.setAttribute('aria-pressed', String(enabled));
        seriesNodes.forEach(function (node) {
          if (Number(node.dataset.series) === index) node.classList.toggle('is-hidden', !enabled);
        });
        if (active >= 0) show(active);
      });
    });
    var reset = area && area.querySelector('[data-series-reset]');
    if (reset) reset.addEventListener('click', function () {
      toggles.forEach(function (button) { button.setAttribute('aria-pressed', 'true'); });
      seriesNodes.forEach(function (node) { node.classList.remove('is-hidden'); });
      if (active >= 0) show(active);
    });
    var format = function (value) { return Number(value).toLocaleString('en-US'); };
    function value(label, amount, short, index) {
      var item = document.createElement('span');
      item.className = 'chart-inspector-value';
      if (index !== undefined) {
        var swatch = document.createElement('i');
        swatch.className = 'compare-swatch compare-' + (index % 10);
        item.appendChild(swatch);
      }
      item.appendChild(document.createTextNode(label + ' '));
      var number = document.createElement('strong');
      number.textContent = short || format(amount);
      number.title = format(amount);
      item.appendChild(number);
      inspector.appendChild(item);
    }
    function show(index) {
      if (index < 0 || index >= hits.length) return;
      if (active >= 0) hits[active].classList.remove('is-active');
      active = index;
      var hit = hits[index];
      hit.classList.add('is-active');
      inspector.replaceChildren();
      var date = document.createElement('strong');
      date.className = 'chart-inspector-date';
      date.textContent = hit.dataset.day;
      inspector.appendChild(date);
      if (chart.dataset.chartKind === 'comparison') {
        var values = JSON.parse(hit.dataset.values);
        var shortValues = JSON.parse(hit.dataset.valuesShort);
        names.forEach(function (name, i) {
          if (!toggles[i] || toggles[i].getAttribute('aria-pressed') === 'true') value(name, values[i], shortValues[i], i);
        });
        value(chart.dataset.chartTotalLabel || '全部用户合计', hit.dataset.total, hit.dataset.totalShort);
      } else {
        value('总 Tokens', hit.dataset.total, hit.dataset.totalShort);
        value('输入', hit.dataset.input, hit.dataset.inputShort);
        value('缓存输入', hit.dataset.cached, hit.dataset.cachedShort);
        value('输出', hit.dataset.output, hit.dataset.outputShort);
        value('交互次数', hit.dataset.turns);
      }
    }
    hits.forEach(function (hit, index) {
      hit.addEventListener('pointerenter', function () { show(index); });
      hit.addEventListener('focus', function () { show(index); });
    });
    chart.addEventListener('focus', function (event) {
      if (event.target === chart) show(active < 0 ? 0 : active);
    });
    chart.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      show(Math.max(0, Math.min(hits.length - 1, (active < 0 ? 0 : active) + (event.key === 'ArrowRight' ? 1 : -1))));
    });
  });
})();
