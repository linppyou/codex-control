(function () {
  if (document.querySelector("[data-dispatch-refresh]")) {
    window.setTimeout(() => {
      if (!document.hidden) window.location.reload();
      else document.addEventListener("visibilitychange", () => window.location.reload(), { once: true });
    }, 5000);
  }
})();
