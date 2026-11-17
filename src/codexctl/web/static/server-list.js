(function () {
  document.querySelectorAll("[data-site-expand]").forEach(function (button) {
    button.addEventListener("click", function () {
      var expanded = button.dataset.siteExpand === "true";
      document.querySelectorAll("details.site-server").forEach(function (group) {
        group.open = expanded;
      });
    });
  });
})();
