(function () {
  var password = document.getElementById("login-password");
  var toggle = document.getElementById("toggle-password");
  var status = document.getElementById("caps-lock-status");
  if (!password || !toggle || !status) return;
  toggle.addEventListener("click", function () {
    var visible = password.type === "password";
    password.type = visible ? "text" : "password";
    var label = visible ? "隐藏密码" : "显示密码";
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("title", label);
    toggle.setAttribute("aria-pressed", String(visible));
  });
  function updateCapsLock(event) {
    status.hidden = !event.getModifierState("CapsLock");
  }
  password.addEventListener("keydown", updateCapsLock);
  password.addEventListener("keyup", updateCapsLock);
  password.addEventListener("blur", function () { status.hidden = true; });
})();
