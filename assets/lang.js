/* 中英文切换：默认中文，选择记在 localStorage；无 JS 时保持中文。 */
(function () {
  var KEY = "ghw-lang";
  var root = document.documentElement;
  var btn = document.getElementById("lang-switch");

  function apply(lang) {
    var isEn = lang === "en";
    root.setAttribute("data-lang", isEn ? "en" : "zh");
    root.setAttribute("lang", isEn ? "en" : "zh-CN");
    if (btn) {
      btn.textContent = isEn ? "中文" : "EN";
      btn.setAttribute("aria-label", isEn ? "切换到中文" : "Switch to English");
      btn.setAttribute("aria-pressed", isEn ? "true" : "false");
    }
  }

  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* 隐私模式下忽略 */ }
  apply(saved === "en" ? "en" : "zh");

  if (btn) {
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-lang") === "en" ? "zh" : "en";
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* ignore */ }
    });
  }
})();
