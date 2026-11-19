const enrollmentCommand = document.getElementById("enrollment-command");
const copyEnrollment = document.getElementById("copy-enrollment");
const copyStatus = document.getElementById("copy-enrollment-status");

for (const form of document.querySelectorAll("form[data-password-enrollment]")) {
  const button = form.querySelector('button[type="submit"]');
  const label = button.textContent;
  form.addEventListener("submit", () => {
    if (!form.elements.ssh_password.value) return;
    form.setAttribute("aria-busy", "true");
    button.disabled = true;
    button.textContent = "正在自动接入…";
  });
  window.addEventListener("pageshow", () => {
    form.removeAttribute("aria-busy");
    button.disabled = false;
    button.textContent = label;
  });
}

if (enrollmentCommand && copyEnrollment) {
  copyEnrollment.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(enrollmentCommand.value);
      copyStatus.textContent = "已复制";
    } catch {
      enrollmentCommand.focus();
      enrollmentCommand.select();
      copyStatus.textContent = "请复制已选中的完整命令";
    }
  });
}
