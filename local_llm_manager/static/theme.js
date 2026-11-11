// Apply the saved theme before first paint (no flash). Kept as a file so the CSP can be script-src 'self'.
try {
  var th = localStorage.getItem("llm.theme") || "dark";
  if (th === "auto") th = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = th;
} catch { /* storage unavailable */ }
