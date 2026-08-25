(() => {
  "use strict";
  const root = document.documentElement;
  const metaTheme = document.querySelector('meta[name="theme-color"]');
  const clock = document.querySelector(".live-clock");
  if (document.querySelector("[data-drive-saved-banner]") && window.history.replaceState) {
    window.history.replaceState(null, "", window.location.pathname);
  }
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

  function zonedInputMilliseconds(value, timeZone, fold) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
    if (!match) return NaN;
    const [, year, month, day, hour, minute] = match;
    const localMilliseconds = Date.UTC(year, Number(month) - 1, day, hour, minute);
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
    const candidates = [];
    for (let offsetMinutes = -14 * 60; offsetMinutes <= 14 * 60; offsetMinutes += 15) {
      const candidate = localMilliseconds - offsetMinutes * 60000;
      const parts = Object.fromEntries(
        formatter.formatToParts(new Date(candidate))
          .filter(part => part.type !== "literal")
          .map(part => [part.type, part.value])
      );
      if (
        `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}` === value
      ) {
        candidates.push(candidate);
      }
    }
    candidates.sort((left, right) => left - right);
    return candidates[fold === "1" ? candidates.length - 1 : 0] ?? NaN;
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
    const shortDriveWarning = editor.querySelector("[data-short-drive-warning]");
    const overlapWarning = editor.querySelector("[data-overlap-warning]");
    const startFold = editor.querySelector("[data-time-start-fold]");
    const endFold = editor.querySelector("[data-time-end-fold]");
    const timeZone = editor.dataset.timeZone;
    // Existing intervals are canonical UTC instants. Resolve the form's local
    // fields in the server's timezone too, retaining the selected fold during
    // the repeated autumn hour instead of depending on the browser's timezone.
    const existingIntervals = editor.dataset.existingIntervals
      ? JSON.parse(editor.dataset.existingIntervals).map(interval => ({
          start: new Date(interval.start).getTime(),
          end: new Date(interval.end).getTime(),
        }))
      : [];
    const minimumSeconds = Number(editor.dataset.minimumDurationSeconds);
    const preciseStart = editor.dataset.preciseStart
      ? new Date(editor.dataset.preciseStart).getTime()
      : null;
    const preciseEnd = editor.dataset.preciseEnd
      ? new Date(editor.dataset.preciseEnd).getTime()
      : null;
    const originalStartValue = startInput.value;
    const originalEndValue = endInput.value;

    // The datetime-local inputs only carry minute precision, so a drive
    // shorter than a minute renders identical start/end values here even
    // though the server holds the true, more precise timestamps. Fall back
    // to those precise timestamps only while neither field has been edited
    // from what the server rendered; any edit makes the input values (and
    // their minute precision) the real intent.
    const effectiveDurationMs = () => {
      const start = localInputMilliseconds(startInput.value);
      const end = localInputMilliseconds(endInput.value);
      const diffMs = end - start;
      if (
        diffMs === 0 &&
        preciseStart !== null &&
        preciseEnd !== null &&
        startInput.value === originalStartValue &&
        endInput.value === originalEndValue
      ) {
        return preciseEnd - preciseStart;
      }
      return diffMs;
    };

    const updateShortDriveWarning = () => {
      if (!shortDriveWarning || !Number.isFinite(minimumSeconds)) return;
      const diffMs = effectiveDurationMs();
      const seconds = Math.round(diffMs / 1000);
      const tooShort = diffMs >= 0 && seconds < minimumSeconds;
      shortDriveWarning.hidden = !tooShort;
      if (tooShort) {
        const length =
          seconds <= 0 ? "less than a second" : `${seconds} ${seconds === 1 ? "second" : "seconds"}`;
        shortDriveWarning.textContent =
          `This drive is only ${length} long — drives must be at least ` +
          `${minimumSeconds} seconds.`;
      }
    };

    const updateOverlapWarning = () => {
      if (!overlapWarning || !existingIntervals.length) return;
      const start = zonedInputMilliseconds(startInput.value, timeZone, startFold.value);
      const end = zonedInputMilliseconds(endInput.value, timeZone, endFold.value);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
        overlapWarning.hidden = true;
        return;
      }
      const overlapCount = existingIntervals.filter(
        interval => start < interval.end && end > interval.start
      ).length;
      overlapWarning.hidden = overlapCount === 0;
      if (overlapCount) {
        overlapWarning.textContent =
          overlapCount === 1
            ? "This drive overlaps another saved drive."
            : `This drive overlaps ${overlapCount} other saved drives.`;
      }
    };

    const updateDuration = () => {
      const start = localInputMilliseconds(startInput.value);
      const end = localInputMilliseconds(endInput.value);
      const diffMs = end - start;
      // Only block on an end that's visibly before the start; let the
      // server's full-precision minimum-duration check be the real
      // authority on drives that are merely too short.
      const valid = Number.isFinite(diffMs) && diffMs >= 0;
      endInput.setCustomValidity(valid ? "" : "End time must be after start time.");
      if (valid) {
        const totalMinutes = Math.round(diffMs / 60000);
        hoursInput.value = String(Math.floor(totalMinutes / 60));
        minutesInput.value = String(totalMinutes % 60);
        hoursInput.setCustomValidity("");
        minutesInput.setCustomValidity("");
      }
      updateShortDriveWarning();
      updateOverlapWarning();
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
      if (valid) {
        endInput.value = formatLocalInput(start + totalMinutes * 60000);
        endInput.setCustomValidity("");
      }
      updateShortDriveWarning();
      updateOverlapWarning();
    };

    startInput.addEventListener("input", updateDuration);
    endInput.addEventListener("input", updateDuration);
    hoursInput.addEventListener("input", updateEnd);
    minutesInput.addEventListener("input", updateEnd);
    updateDuration();
  });

  document.querySelectorAll("[data-weather-picker]").forEach(picker => {
    const text = picker.querySelector("[data-weather-text]");
    const options = Array.from(picker.querySelectorAll("[data-weather-option]"));

    const tokensOf = () => text.value.split(",").map(token => token.trim()).filter(Boolean);
    const matches = (token, label) => label.toLowerCase().startsWith(token.toLowerCase());

    const syncTextFromCheckboxes = () => {
      const tokens = tokensOf();
      const extra = tokens.filter(token => !options.some(option => matches(token, option.value)));
      const parts = options
        .filter(option => option.checked)
        .map(option => tokens.find(token => matches(token, option.value)) || option.value);
      text.value = parts.concat(extra).join(", ");
    };

    const syncCheckboxesFromText = () => {
      const tokens = tokensOf();
      options.forEach(option => {
        option.checked = tokens.some(token => matches(token, option.value));
      });
    };

    options.forEach(option => option.addEventListener("change", syncTextFromCheckboxes));
    text.addEventListener("input", syncCheckboxesFromText);
    syncCheckboxesFromText();
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
    const selectCustom = () => { period.value = "custom"; };
    const keepEndAfterStart = () => {
      selectCustom();
      if (start.value && end.value && start.value > end.value) end.value = start.value;
    };
    start.addEventListener("input", keepEndAfterStart);
    start.addEventListener("change", keepEndAfterStart);
    end.addEventListener("input", selectCustom);
    end.addEventListener("change", selectCustom);
    form.querySelector("[data-clear-history-filters]").addEventListener("click", () => {
      form.querySelectorAll("input").forEach(input => {
        if (input.type === "checkbox" || input.type === "radio") input.checked = false;
        else input.value = "";
      });
      form.querySelectorAll("select").forEach(select => { select.selectedIndex = 0; });
    });
  });

  document.querySelectorAll("[data-cancel-form]").forEach(link => {
    const form = document.getElementById(link.dataset.cancelForm);
    if (!form) return;
    const serialize = () => new URLSearchParams(new FormData(form)).toString();
    const initialState = serialize();
    link.addEventListener("click", event => {
      if (serialize() !== initialState && !confirm("Discard your changes to this drive?")) {
        event.preventDefault();
      }
    });
  });

  document.querySelectorAll("[data-file-export]").forEach(link => {
    const status = link.parentElement.querySelector("[data-export-status]");
    const filename = link.dataset.exportFilename;
    const initialLabel = link.textContent;
    let file;

    const setStatus = message => {
      if (status) status.textContent = message;
    };

    const saveFile = async () => {
      if (navigator.canShare && navigator.canShare({files: [file]})) {
        try {
          await navigator.share({files: [file]});
          setStatus("Save options opened.");
        } catch (error) {
          if (error.name !== "AbortError") {
            setStatus("Could not open the save options. Please try again.");
          }
        }
        return;
      }
      const download = document.createElement("a");
      const url = URL.createObjectURL(file);
      download.href = url;
      download.download = filename;
      download.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      setStatus("File download started.");
    };

    link.addEventListener("click", async event => {
      event.preventDefault();
      if (file) {
        await saveFile();
        return;
      }
      if (link.dataset.preparing === "yes") return;
      link.dataset.preparing = "yes";
      link.setAttribute("aria-busy", "true");
      setStatus("Preparing file…");
      try {
        const response = await fetch(link.href, {cache: "no-store"});
        if (response.redirected) {
          window.location.assign(response.url);
          return;
        }
        if (!response.ok) throw new Error(`Request failed (${response.status})`);
        file = new File([await response.blob()], filename, {
          type: response.headers.get("content-type") || "application/octet-stream",
        });
        link.textContent = `Save ${initialLabel.replace(/^Download /, "")}`;
        setStatus(`Tap "${link.textContent}" above to download.`);
      } catch (error) {
        setStatus("Could not prepare the file. Please try again.");
      } finally {
        delete link.dataset.preparing;
        link.removeAttribute("aria-busy");
      }
    });
  });

  document.querySelectorAll("form[data-async-submit]").forEach(form => {
    form.addEventListener("submit", async event => {
      if (event.defaultPrevented || form.dataset.submitting === "yes") return;
      if (
        form.matches("[data-location-create], [data-end-location]") &&
        form.dataset.locationReady !== "yes"
      ) return;
      event.preventDefault();
      form.dataset.submitting = "yes";
      const submitter = event.submitter;
      if (submitter) submitter.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: form.method || "POST",
          body: new FormData(form),
          cache: "no-store",
          headers: { Accept: "application/json" },
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

  function currentPosition() {
    if (!navigator.geolocation) return Promise.reject(new Error("Location is not available in this browser."));
    return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(
      resolve,
      reject,
      {enableHighAccuracy: true, timeout: 5000, maximumAge: 15000}
    ));
  }

  function showLocationPreview(form, position) {
    if (!window.L) throw new Error("The map preview could not load.");
    const latitude = position.coords.latitude;
    const longitude = position.coords.longitude;
    const preview = form.querySelector("[data-location-preview]");
    const container = form.querySelector("[data-location-map]");
    const radiusInput = form.elements.radius_meters;
    const accuracy = form.querySelector("[data-location-accuracy]");
    if (!preview || !container || !radiusInput || !accuracy) throw new Error("The map preview is unavailable.");
    if (form.locationPreviewMap) form.locationPreviewMap.remove();
    preview.hidden = false;
    container.replaceChildren();
    const map = L.map(container, {scrollWheelZoom: false});
    form.locationPreviewMap = map;
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "© <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors",
      maxZoom: 19,
    }).addTo(map);
    const marker = L.circleMarker([latitude, longitude], {
      radius: 9,
      color: "#1769e0",
      fillColor: "#1769e0",
      fillOpacity: 1,
      weight: 2,
    }).addTo(map);
    const circle = L.circle([latitude, longitude], {radius: Number(radiusInput.value)}).addTo(map);
    const updateCoordinates = latlng => {
      form.elements.latitude.value = String(latlng.lat);
      form.elements.longitude.value = String(latlng.lng);
      marker.setLatLng(latlng);
      circle.setLatLng(latlng);
    };
    map.on("click", event => updateCoordinates(event.latlng));
    radiusInput.oninput = () => {
      const radius = Number(radiusInput.value);
      if (Number.isFinite(radius) && radius > 0) circle.setRadius(radius);
    };
    updateCoordinates(marker.getLatLng());
    accuracy.textContent = `Device accuracy is approximately ${Math.round(position.coords.accuracy)} meters.`;
    map.setView([latitude, longitude], 17);
    setTimeout(() => map.invalidateSize(), 0);
  }

  document.querySelectorAll("form[data-location-create], form[data-end-location]").forEach(form => {
    form.addEventListener("submit", async event => {
      if (form.dataset.locationReady === "yes") return;
      event.preventDefault();
      const status = form.querySelector("[data-location-status]");
      const submitter = event.submitter;
      if (submitter) submitter.disabled = true;
      if (status) status.textContent = "Getting your current location…";
      try {
        const position = await currentPosition();
        if (form.matches("[data-location-create]")) {
          try {
            showLocationPreview(form, position);
            if (status) status.textContent = "Check the pin and circle, then save this location.";
          } catch (error) {
            const preview = form.querySelector("[data-location-preview]");
            if (preview) preview.hidden = true;
            if (status) status.textContent = "Your location was found, but the map preview could not load. Please try again.";
          }
        } else {
          if (form.elements.end_latitude) form.elements.end_latitude.value = String(position.coords.latitude);
          if (form.elements.end_longitude) form.elements.end_longitude.value = String(position.coords.longitude);
          form.dataset.locationReady = "yes";
          form.requestSubmit();
        }
      } catch (error) {
        if (form.matches("[data-end-location]")) {
          // Ending a drive must remain available when a supervisor declines location access.
          form.dataset.locationReady = "yes";
          form.requestSubmit();
        } else if (status) {
          status.textContent = "Location was not shared, so this saved location was not created.";
        }
      } finally {
        if (form.dataset.locationReady !== "yes" && submitter) submitter.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-location-save]").forEach(button => {
    button.addEventListener("click", () => {
      button.form.dataset.locationReady = "yes";
    });
  });
})();
