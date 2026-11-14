(function () {
  document.querySelectorAll(".user-usage-toggle").forEach(function (button) {
    button.addEventListener("click", function () {
      var expanded = button.getAttribute("aria-expanded") === "true";
      button.closest("tbody").querySelectorAll(".model-sub").forEach(function (row) {
        row.hidden = expanded;
      });
      button.setAttribute("aria-expanded", String(!expanded));
      button.setAttribute("aria-label", (expanded ? "展开 " : "折叠 ") + button.querySelector("b").textContent + " 的模型用量");
    });
  });
})();
