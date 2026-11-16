(function () {
  const assignment = document.querySelector("[data-site-assignment]");
  if (assignment) {
    const server = assignment.elements.server_id;
    const recipient = assignment.elements.recipient;
    const search = assignment.querySelector("[data-site-search]");
    const selectVisible = assignment.querySelector("[data-select-visible]");
    const results = assignment.querySelector("[data-site-results]");
    const noResults = assignment.querySelector("[data-site-no-results]");
    const summary = assignment.querySelector("[data-selected-sites]");
    const clear = assignment.querySelector("[data-clear-sites]");
    const submit = assignment.querySelector('[type="submit"]');
    const list = assignment.querySelector(".assignment-list");
    const groups = Array.from(assignment.querySelectorAll("[data-site-server]"), fieldset => ({
      fieldset,
      days: Array.from(fieldset.querySelectorAll("[data-assignment-day]"), section => ({
        section,
        sites: Array.from(section.querySelectorAll("[data-assignment-site]"), row => ({
          row,
          checkbox: row.querySelector('[name="site_ids"]'),
          domains: row.textContent.toLowerCase(),
        })),
      })),
    }));
    let days = [];
    let sites = [];
    let visible = [];

    function updateSelection() {
      const count = sites.filter(site => site.checkbox.checked).length;
      const visibleChecked = visible.filter(site => site.checkbox.checked).length;
      summary.textContent = `已选 ${count} 个站点`;
      selectVisible.disabled = !visible.length;
      selectVisible.checked = visible.length > 0 && visibleChecked === visible.length;
      selectVisible.indeterminate = visibleChecked > 0 && visibleChecked < visible.length;
      clear.disabled = !count;
      submit.disabled = !count || !recipient.value;
    }

    function filterSites() {
      const query = search.value.trim().toLowerCase();
      visible = days.flatMap(day => {
        const matches = day.sites.filter(site => {
          site.row.hidden = !site.domains.includes(query);
          return !site.row.hidden;
        });
        day.section.hidden = !matches.length;
        return matches;
      });
      results.textContent = query ? `${visible.length} / ${sites.length} 个站点` : `${sites.length} 个站点`;
      noResults.hidden = visible.length > 0;
      list.scrollTop = 0;
      updateSelection();
    }

    function showServer() {
      days = [];
      for (const group of groups) {
        const active = group.fieldset.dataset.siteServer === server.value;
        group.fieldset.hidden = !active;
        group.fieldset.disabled = !active;
        if (active) days = group.days;
      }
      sites = days.flatMap(day => day.sites);
      filterSites();
    }

    server.addEventListener("change", () => {
      sites.forEach(site => { site.checkbox.checked = false; });
      search.value = "";
      showServer();
    });
    search.addEventListener("input", filterSites);
    search.addEventListener("keydown", event => {
      if (event.key === "Enter") event.preventDefault();
    });
    recipient.addEventListener("change", updateSelection);
    list.addEventListener("change", updateSelection);
    selectVisible.addEventListener("change", () => {
      visible.forEach(site => { site.checkbox.checked = selectVisible.checked; });
      updateSelection();
    });
    clear.addEventListener("click", () => {
      sites.forEach(site => { site.checkbox.checked = false; });
      updateSelection();
    });
    for (const element of [search, assignment.querySelector("[data-assignment-tools]"), summary, clear]) {
      element.hidden = false;
    }
    showServer();
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
