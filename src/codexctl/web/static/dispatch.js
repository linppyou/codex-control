(function () {
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
