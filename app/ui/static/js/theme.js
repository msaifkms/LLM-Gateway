// Runs before first paint (a plain script in <head>), so there is no flash of
// the wrong theme. The choice is a display preference, not a secret, so it
// lives in localStorage. The API key never does.
(function () {
  try {
    var theme = localStorage.getItem("llmgw.theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    }
  } catch (e) {
    /* storage blocked: follow the system setting */
  }
})();
