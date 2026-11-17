const enrollmentCommand = document.getElementById("enrollment-command");
const copyEnrollment = document.getElementById("copy-enrollment");
const copyStatus = document.getElementById("copy-enrollment-status");

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
