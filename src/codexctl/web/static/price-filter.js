(function () {
  var search = document.getElementById('price-search');
  if (!search) return;
  var rows = Array.from(document.querySelectorAll('[data-price-row]'));
  var status = document.getElementById('price-search-status');
  function filter() {
    var query = search.value.trim().toLocaleLowerCase();
    var visible = 0;
    rows.forEach(function (row) {
      var name = row.querySelector('[name="name"]').value;
      var aliases = row.querySelector('[name="aliases"]').value;
      row.hidden = !(name + ' ' + aliases).toLocaleLowerCase().includes(query);
      if (!row.hidden) visible += 1;
    });
    status.textContent = '显示 ' + visible + ' / ' + rows.length + ' 个模型';
  }
  search.addEventListener('input', filter);
  document.getElementById('price-table').addEventListener('input', function () {
    if (search.value.trim()) filter();
  });
  filter();
})();
