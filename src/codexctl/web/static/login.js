(function () {
  var holder = document.getElementById("login-data");
  if (!holder) { return; }
  var id = holder.getAttribute("data-login-id");
  var terminal = ["succeeded", "failed", "cancelled", "expired"];
  function tick() {
    fetch("/accounts/login/" + encodeURIComponent(id) + "/status", {credentials: "same-origin"})
      .then(function (r) { return r.json(); })
      .then(function (s) {
        document.getElementById("state").textContent = s.state;
        document.getElementById("code").textContent = s.code;
        document.getElementById("error").textContent = s.error;
        var a = document.getElementById("url");
        if (s.url) {
          if (a.textContent !== s.url) { a.textContent = s.url; }
          a.setAttribute("href", s.url);
        }
        if (terminal.indexOf(s.state) >= 0) { location.reload(); }
      })
      .catch(function () {});
  }
  setInterval(tick, 3000);
})();
