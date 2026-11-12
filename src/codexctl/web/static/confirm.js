(function () {
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var submitter = event.submitter;
    var message =
      (submitter && submitter.getAttribute ? submitter.getAttribute("data-confirm") : null) ||
      (form && form.getAttribute ? form.getAttribute("data-confirm") : null);
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });
})();
