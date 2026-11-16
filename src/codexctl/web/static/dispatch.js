(function () {
  const assignment = document.querySelector("[data-site-assignment]");
  if (assignment) {
    const server = assignment.elements.server_id;
    const sites = assignment.elements.site_id;
    const placeholder = sites.options[0];
    const options = new Map(Array.from(sites.querySelectorAll("optgroup"), group =>
      [group.dataset.server, Array.from(group.children)]));
    function updateSites() {
      sites.replaceChildren(placeholder, ...(options.get(server.value) ?? []));
      sites.selectedIndex = 0;
    }
    server.addEventListener("change", updateSites);
    updateSites();
  }

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
