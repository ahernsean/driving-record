(() => {
  "use strict";
  const root = document.documentElement;
  const metaTheme = document.querySelector('meta[name="theme-color"]');
  const clock = document.querySelector(".live-clock");
  let boundaries = (window.DRIVING_LOG_THEME || []).map(value => new Date(value));
  let serverOffset = new Date(window.DRIVING_LOG_SERVER_NOW).getTime() - Date.now();
  let themeTimer;

  function setTheme(theme) {
    root.dataset.theme = theme;
    if (metaTheme) metaTheme.content = theme === "light" ? "#f4f8ff" : "#101b2d";
  }

  function applyThemeAt(now) {
    const elapsed = boundaries.filter(boundary => boundary <= now).length;
    if (elapsed % 2) setTheme(root.dataset.theme === "light" ? "dark" : "light");
    boundaries = boundaries.filter(boundary => boundary > now);
  }

  function scheduleTheme() {
    clearTimeout(themeTimer);
    applyThemeAt(new Date(Date.now() + serverOffset));
    const next = boundaries[0];
    if (next) {
      themeTimer = setTimeout(
        scheduleTheme,
        Math.max(0, next.getTime() - (Date.now() + serverOffset)) + 20
      );
    } else {
      themeTimer = setTimeout(() => refreshState().catch(() => {}), 60000);
    }
  }

  let start;
  let serverNow;
  let receivedMono;
  const output = clock && clock.querySelector("[data-duration]");
  const sync = clock && clock.querySelector("[data-sync-status]");

  function rebaseClock(state) {
    serverNow = new Date(state.server_now_utc);
    serverOffset = serverNow.getTime() - Date.now();
    receivedMono = performance.now();
    if (state.live) start = new Date(state.live.started_at_utc);
  }

  function render(useWall = false) {
    if (!clock || !start) return;
    const projectedNow = useWall
      ? Date.now() + serverOffset
      : serverNow.getTime() + (performance.now() - receivedMono);
    const total = Math.max(0, Math.floor((projectedNow - start.getTime()) / 1000));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    output.textContent =
      `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function applyState(state) {
    setTheme(state.theme);
    boundaries = (state.theme_boundaries || []).map(value => new Date(value));
    rebaseClock(state);
    scheduleTheme();
    if (clock) {
      const currentId = clock.dataset.liveId;
      if (!state.live || state.live.id !== currentId || state.live.status !== "active") {
        window.location.reload();
        return;
      }
      render();
      sync.textContent = "Server confirmed";
    }
  }

  function refreshState() {
    return fetch("/live/state", {cache: "no-store"})
      .then(response => response.ok ? response.json() : Promise.reject())
      .then(applyState);
  }

  if (clock) {
    start = new Date(clock.dataset.liveStart);
    serverNow = new Date(clock.dataset.serverNow);
    receivedMono = performance.now();
    render();
    setInterval(render, 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        render(true);
        sync.textContent = "Checking server…";
        refreshState().catch(() => {
          sync.textContent = "Projected — server unavailable";
        });
      }
    });
  }
  scheduleTheme();

  function localInputMilliseconds(value) {
    return new Date(value).getTime();
  }

  function formatLocalInput(milliseconds) {
    const value = new Date(milliseconds);
    const pad = number => String(number).padStart(2, "0");
    return (
      `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}` +
      `T${pad(value.getHours())}:${pad(value.getMinutes())}`
    );
  }

  document.querySelectorAll("[data-time-editor]").forEach(editor => {
    const startInput = editor.querySelector("[data-time-start]");
    const endInput = editor.querySelector("[data-time-end]");
    const hoursInput = editor.querySelector("[data-duration-hours]");
    const minutesInput = editor.querySelector("[data-duration-minutes]");

    const updateDuration = () => {
      const start = localInputMilliseconds(startInput.value);
      const end = localInputMilliseconds(endInput.value);
      const totalMinutes = Math.round((end - start) / 60000);
      const valid = Number.isFinite(totalMinutes) && totalMinutes > 0;
      endInput.setCustomValidity(valid ? "" : "End time must be after start time.");
      if (!valid) return;
      hoursInput.value = String(Math.floor(totalMinutes / 60));
      minutesInput.value = String(totalMinutes % 60);
      hoursInput.setCustomValidity("");
      minutesInput.setCustomValidity("");
    };

    const updateEnd = () => {
      const start = localInputMilliseconds(startInput.value);
      const hours = Number(hoursInput.value);
      const minutes = Number(minutesInput.value);
      const totalMinutes = hours * 60 + minutes;
      const valid = Number.isFinite(start) && Number.isFinite(totalMinutes) && totalMinutes > 0;
      const message = valid ? "" : "Duration must be at least one minute.";
      hoursInput.setCustomValidity(message);
      minutesInput.setCustomValidity(message);
      if (!valid) return;
      endInput.value = formatLocalInput(start + totalMinutes * 60000);
      endInput.setCustomValidity("");
    };

    startInput.addEventListener("input", updateDuration);
    endInput.addEventListener("input", updateDuration);
    hoursInput.addEventListener("input", updateEnd);
    minutesInput.addEventListener("input", updateEnd);
    updateDuration();
  });

  document.querySelectorAll("form[data-history-filters]").forEach(form => {
    const period = form.querySelector('select[name="period"]');
    const start = form.querySelector('input[name="start_date"]');
    const end = form.querySelector('input[name="end_date"]');
    const ranges = JSON.parse(form.dataset.dateRanges || "{}");
    period.addEventListener("change", () => {
      const range = ranges[period.value];
      if (!range) return;
      start.value = range.start;
      end.value = range.end;
    });
    [start, end].forEach(input => {
      input.addEventListener("input", () => {
        period.value = "custom";
      });
      input.addEventListener("change", () => {
        period.value = "custom";
      });
    });
  });

  document.querySelectorAll("form[data-async-submit]").forEach(form => {
    form.addEventListener("submit", async event => {
      if (event.defaultPrevented || form.dataset.submitting === "yes") return;
      event.preventDefault();
      form.dataset.submitting = "yes";
      const submitter = event.submitter;
      if (submitter) submitter.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: form.method || "POST",
          body: new FormData(form),
          cache: "no-store",
        });
        if (response.redirected) {
          window.location.assign(response.url);
          return;
        }
        if (!response.ok) {
          const type = response.headers.get("content-type") || "";
          const message = type.includes("application/json")
            ? (await response.json()).detail
            : (await response.text()).replace(/<[^>]+>/g, " ").trim();
          throw new Error(message || `Request failed (${response.status})`);
        }
        window.location.reload();
      } catch (error) {
        alert(error.message || "The request could not be completed.");
        form.dataset.submitting = "no";
        if (submitter) submitter.disabled = false;
      }
    });
  });
})();
