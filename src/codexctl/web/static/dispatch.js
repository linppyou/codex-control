(function () {
  const server = document.getElementById("dispatch-server");
  const domains = document.getElementById("dispatch-domains");
  if (server && domains) {
    const options = Array.from(domains.options);
    function updateDomains() {
      domains.replaceChildren(...options.filter(option => option.dataset.server === server.value));
    }
    server.addEventListener("change", updateDomains);
    updateDomains();
  }

  if (document.querySelector("[data-dispatch-refresh]")) {
    window.setTimeout(() => {
      if (!document.hidden) window.location.reload();
      else document.addEventListener("visibilitychange", () => window.location.reload(), { once: true });
    }, 5000);
  }
})();
