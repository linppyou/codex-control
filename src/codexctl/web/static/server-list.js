(function () {
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
