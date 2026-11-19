(function () {
  document.querySelectorAll("[data-panel-copy]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var status = button.parentElement.querySelector("[data-panel-status]");
      button.disabled = true;
      try {
        var csrf = button.closest("[data-panel-csrf]").dataset.panelCsrf;
        var response = await fetch(button.dataset.panelCopy, { method: "POST", headers: { "X-CSRFToken": csrf }, cache: "no-store" });
        if (!response.ok) throw new Error("request failed");
        var result = await response.json();
        await navigator.clipboard.writeText(result.value);
        status.textContent = "已复制";
      } catch (error) {
        status.textContent = "复制失败，请检查登录状态和浏览器剪贴板权限";
      } finally {
        button.disabled = false;
      }
    });
  });
  var filters = document.querySelectorAll("[data-site-filter]");
  filters.forEach(function (filter) {
    var checkboxes = filter.querySelectorAll('input[type="checkbox"]');
    var selection = filter.querySelector("[data-filter-selection]");
    var clear = filter.querySelector("[data-filter-clear]");

    function updateSelection() {
      var count = filter.querySelectorAll('input[type="checkbox"]:checked').length;
      selection.textContent = count ? "已选 " + count + " 项" : "全部";
    }

    updateSelection();
    filter.addEventListener("change", updateSelection);
    clear.hidden = false;
    clear.addEventListener("click", function () {
      checkboxes.forEach(function (checkbox) { checkbox.checked = false; });
      updateSelection();
    });
    filter.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        filter.open = false;
        filter.querySelector("summary").focus();
      }
    });
  });
  document.addEventListener("click", function (event) {
    filters.forEach(function (filter) {
      if (!filter.contains(event.target)) filter.open = false;
    });
  });

  document.querySelectorAll("[data-site-expand]").forEach(function (button) {
    button.addEventListener("click", function () {
      var expanded = button.dataset.siteExpand === "true";
      document.querySelectorAll("details.site-server").forEach(function (group) {
        group.open = expanded;
      });
    });
  });
})();
