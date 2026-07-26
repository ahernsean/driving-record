(() => {
  "use strict";
  const root = document.documentElement;
  const metaTheme = document.querySelector('meta[name="theme-color"]');
  let boundaries = (window.DRIVING_LOG_THEME || []).map(value => new Date(value));

  function applyThemeAt(now) {
    const elapsed = boundaries.filter(boundary => boundary <= now).length;
    if (elapsed % 2) {
      const theme = root.dataset.theme === "light" ? "dark" : "light";
      root.dataset.theme = theme;
      if (metaTheme) metaTheme.content = theme === "light" ? "#f4f8ff" : "#101b2d";
      boundaries = boundaries.filter(boundary => boundary > now);
    }
  }
  function scheduleTheme() {
    applyThemeAt(new Date());
    const next = boundaries[0];
    if (next) setTimeout(scheduleTheme, Math.max(0, next - new Date()) + 20);
  }
  scheduleTheme();

  const clock = document.querySelector(".live-clock");
  if (clock) {
    const start = new Date(clock.dataset.liveStart);
    const serverNow = new Date(clock.dataset.serverNow);
    const receivedWall = Date.now();
    const receivedMono = performance.now();
    const serverOffset = serverNow.getTime() - receivedWall;
    const output = clock.querySelector("[data-duration]");
    const sync = clock.querySelector("[data-sync-status]");
    function render(useWall = false) {
      const projectedNow = useWall
        ? Date.now() + serverOffset
        : serverNow.getTime() + (performance.now() - receivedMono);
      const total = Math.max(0, Math.floor((projectedNow - start.getTime()) / 1000));
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const seconds = total % 60;
      output.textContent = `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }
    render();
    setInterval(render, 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        render(true);
        sync.textContent = "Checking server…";
        fetch("/live/state", {cache: "no-store"})
          .then(response => response.ok ? response.json() : Promise.reject())
          .then(() => { sync.textContent = "Server confirmed"; })
          .catch(() => { sync.textContent = "Projected — server unavailable"; });
      }
    });
  }

  document.querySelectorAll("form[data-live-action]").forEach(form => {
    form.addEventListener("submit", event => {
      if (form.dataset.tokenRefreshed === "yes") return;
      event.preventDefault();
      fetch("/live/state", {cache: "no-store"})
        .then(response => response.json())
        .then(state => {
          const fresh = state.tokens && state.tokens[form.dataset.liveAction];
          if (fresh) form.elements.form_token.value = fresh;
          form.dataset.tokenRefreshed = "yes";
          form.requestSubmit();
        })
        .catch(() => {
          form.dataset.tokenRefreshed = "yes";
          form.requestSubmit();
        });
    });
  });
})();
