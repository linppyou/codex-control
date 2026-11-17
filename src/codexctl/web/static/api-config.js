(function () {
  var status = document.getElementById('copy-status');
  document.querySelectorAll('[data-copy-target]').forEach(function (button) {
    button.addEventListener('click', async function () {
      var target = document.getElementById(button.dataset.copyTarget);
      var text = target.value === undefined ? target.textContent : target.value;
      try {
        await navigator.clipboard.writeText(text);
        status.textContent = '已复制到剪贴板';
      } catch (error) {
        status.textContent = '复制失败，请选中内容后手动复制';
      }
    });
  });
  document.querySelectorAll('[data-secret-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var target = document.getElementById(button.dataset.secretToggle);
      var visible = target.type === 'password';
      target.type = visible ? 'text' : 'password';
      button.textContent = visible ? '隐藏密钥' : '显示密钥';
      button.setAttribute('aria-pressed', String(visible));
    });
  });
})();
