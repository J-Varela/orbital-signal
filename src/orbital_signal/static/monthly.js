"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("monthly-form");
  const start = $("monthly-start");
  const end = $("monthly-end");
  const refresh = $("monthly-refresh");
  const windowDays = $("monthly-window-days");
  const agency = $("monthly-agency");
  const submit = $("monthly-submit");
  const load = $("monthly-load");
  const status = $("monthly-status");
  const progress = $("monthly-progress");
  const submitLabel = submit.textContent;
  const number = new Intl.NumberFormat();
  const state = {
    busy: false,
    legacyBusy: false,
    postPending: false,
    collectionVersion: 0,
    progressVersion: 0,
    progressTask: null,
    pollTimer: null,
  };

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function setStatus(message, error = false) {
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  function updateControls() {
    start.disabled = end.disabled = refresh.disabled = windowDays.disabled = agency.disabled = state.busy;
    submit.disabled = state.busy || state.legacyBusy;
    submit.textContent = state.busy ? "Collecting…" : submitLabel;
  }

  function selection() {
    if (!form.reportValidity()) return null;
    if (!start.value || !end.value || end.value < start.value) {
      setStatus("Choose a start month and an end month on or after it.", true);
      return null;
    }
    return new URLSearchParams({ start_month: start.value, end_month: end.value, window_days: windowDays.value, agency: agency.value });
  }

  async function request(parameters, options = {}) {
    let response;
    try {
      response = await fetch(`/api/v1/collections/monthly?${parameters}`, {
        headers: { Accept: "application/json" },
        ...options,
      });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("The server could not be reached. Check that Orbital Signal is running and load saved progress before retrying collection.");
    }
    if (!response.ok) {
      if (response.status === 422) throw new Error("Choose valid completed calendar months, with the end month on or after the start month.");
      if (response.status === 409) throw new Error("Another collection is already running. Load saved progress and wait for it to finish before retrying.");
      throw new Error(`Collection request failed (${response.status}). Check the server and database connection, then load saved progress.`);
    }
    let result;
    try { result = await response.json(); }
    catch { throw new Error("The server returned unreadable progress. Load saved progress to retry."); }
    if (!result || !Array.isArray(result.windows) || !["completed", "incomplete"].includes(result.status)) {
      throw new Error("The server returned unexpected progress. Load saved progress to retry.");
    }
    return result;
  }

  function count(value) {
    return typeof value === "number" && Number.isFinite(value) ? number.format(value) : "—";
  }

  function windowStatus(window) {
    const labels = {
      completed: ["No reported truncation", "green"],
      split: ["Split / probe", "amber"],
      incomplete: ["More results remain", "amber"],
      failed: ["Failed", "red"],
      running: ["Running", "amber"],
      pending: ["Pending", "muted"],
    };
    const [label, tone] = labels[window.status] || ["Unknown", "muted"];
    return node("span", `badge badge-${tone}`, label);
  }

  function render(summary) {
    const heading = node("div", "monthly-summary");
    heading.append(node("h3", "", summary.status === "completed"
      ? "No reported truncation in the selected windows"
      : "Collection needs more work"));
    heading.append(node("p", "subtext", summary.status === "completed"
      ? "The source reported no more pages for the searched windows. This does not establish complete source coverage."
      : "Some selected windows are missing, unfinished, failed, or still report more source results. Collect or resume to retry work that remains."));
    progress.replaceChildren(heading);
    if (summary.coverage_note) progress.append(node("p", "subtext", summary.coverage_note));
    if (summary.cumulative) {
      const metrics = summary.cumulative;
      const prefix = metrics.history_complete ? "Recorded observations" : "Retained observations (historical totals are lower bounds)";
      progress.append(node("p", "subtext", `${prefix}: ${count(metrics.attempts)} attempts · ${count(metrics.fetched_count)} fetched rows · ${count(metrics.relevant_count)} relevant observations · ${count(metrics.stored_count)} signal insertions · ${count(metrics.duplicate_count)} duplicates · ${count(metrics.failed_attempts)} failed attempts. These are not unique award counts.`));
    }
    if (!summary.windows.length) {
      progress.append(node("p", "subtext", "No saved checkpoints for this selection. Collect or resume to begin."));
      return;
    }

    const wrapper = node("div", "table-wrap");
    const table = node("table");
    table.append(node("caption", "sr-only", "Saved monthly collection checkpoints"));
    const head = node("thead");
    const header = node("tr");
    for (const label of ["AGENCY / WINDOW", "STATUS", "PAGES / ROWS", "RELEVANT", "NEW SIGNALS", "DUPLICATES", "ATTEMPTS"]) {
      const cell = node("th", "", label);
      cell.scope = "col";
      header.append(cell);
    }
    head.append(header);
    const body = node("tbody");
    for (const window of summary.windows) {
      const row = node("tr");
      const agency = window.agency === "National Aeronautics and Space Administration" ? "NASA" : window.agency;
      const identity = node("td", "date-cell");
      identity.append(node("strong", "", agency), node("span", "subtext", `${window.start_date} — ${window.end_date}`));
      const outcome = node("td");
      outcome.append(windowStatus(window), node("span", "subtext", window.has_more === true
        ? "More source pages reported"
        : window.has_more === false ? "No further pages reported" : "Pagination not yet known"));
      if (window.status === "split") outcome.append(node("span", "subtext", "Probe reached the page cap; inspect the narrower windows."));
      if (window.error_message) outcome.append(node("p", "run-error", window.error_message));
      const fetched = node("td", "date-cell", `${count(window.pages_fetched)} pages`);
      fetched.append(node("span", "subtext", `${count(window.fetched_count)} source rows`));
      row.append(identity, outcome, fetched);
      for (const field of ["relevant_count", "stored_count", "duplicate_count", "attempts"]) {
        row.append(node("td", "amount-cell", count(window[field])));
      }
      body.append(row);
    }
    table.append(head, body);
    wrapper.append(table);
    progress.append(wrapper, node("p", "subtext", "Counts describe each window's last attempt. Split probes and retried or overlapping observations can include the same award more than once."));
  }

  function cancelProgress() {
    ++state.progressVersion;
    if (state.progressTask) state.progressTask.controller.abort();
    state.progressTask = null;
    progress.setAttribute("aria-busy", "false");
  }

  function savedProgress(parameters, force = false) {
    const key = parameters.toString();
    if (!force && state.progressTask?.key === key) return state.progressTask.promise;
    cancelProgress();
    const task = { key, controller: new AbortController(), version: state.progressVersion, timedOut: false };
    state.progressTask = task;
    progress.setAttribute("aria-busy", "true");
    const timeout = window.setTimeout(() => {
      task.timedOut = true;
      task.controller.abort();
    }, 30000);
    task.promise = (async () => {
      try {
        const data = await request(parameters, { signal: task.controller.signal });
        const current = task.version === state.progressVersion;
        if (current) render(data);
        return { data, current };
      } catch (error) {
        if (task.timedOut) {
          return { error: new Error("Loading saved progress took too long. Check the server connection and use Load saved progress to retry."), current: task.version === state.progressVersion };
        }
        return { error, current: task.version === state.progressVersion && error.name !== "AbortError" };
      } finally {
        window.clearTimeout(timeout);
        if (state.progressTask === task) {
          state.progressTask = null;
          progress.setAttribute("aria-busy", "false");
        }
      }
    })();
    return task.promise;
  }

  function progressNotice(summary) {
    return summary.status === "completed"
      ? "Saved progress loaded. No further pagination was reported for the searched windows; source completeness is not guaranteed."
      : "Saved progress loaded. Work remains in the selected months; collect or resume to continue.";
  }

  async function loadProgress() {
    const parameters = selection();
    if (!parameters) return;
    setStatus(state.busy ? "Collection is running. Loading saved progress…" : "Loading saved progress…");
    const result = await savedProgress(parameters);
    if (!result.current) return;
    if (result.error) setStatus(result.error.message, true);
    else setStatus(state.busy ? "Collection is running. Saved checkpoints are shown below." : progressNotice(result.data));
  }

  function stopPolling() {
    state.postPending = false;
    if (state.pollTimer !== null) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  function schedulePoll(parameters, version) {
    if (!state.postPending || version !== state.collectionVersion) return;
    state.pollTimer = window.setTimeout(async () => {
      state.pollTimer = null;
      const result = await savedProgress(parameters);
      if (!state.postPending || version !== state.collectionVersion) return;
      if (result.current && result.error) setStatus("Collection is still running, but saved progress could not be loaded. Check the server connection or try Load saved progress.", true);
      schedulePoll(parameters, version);
    }, 3000);
  }

  async function collect(event) {
    event.preventDefault();
    if (state.busy || state.legacyBusy) return;
    const parameters = selection();
    if (!parameters) return;
    const postParameters = new URLSearchParams(parameters);
    postParameters.set("refresh", String(refresh.checked));
    cancelProgress();
    state.busy = state.postPending = true;
    const version = ++state.collectionVersion;
    updateControls();
    document.dispatchEvent(new CustomEvent("orbital:monthly-busy", { detail: true }));
    setStatus("Collecting the selected months. This can take several minutes; saved checkpoints will update below. Keep this page open to follow the request.");
    schedulePoll(parameters, version);
    let notice;
    let failed = false;
    try {
      const summary = await request(postParameters, { method: "POST" });
      stopPolling();
      cancelProgress();
      render(summary);
      notice = summary.status === "completed"
        ? "Collection finished with no reported pagination remaining. Source completeness is not guaranteed."
        : "Collection returned with work remaining. Inspect failed or unfinished windows, then collect or resume to retry them.";
      if (summary.stopped_early) notice = summary.stop_reason || "Collection stopped after repeated failures. Saved progress is retained; resume after checking the service.";
      setStatus(notice);
    } catch (error) {
      failed = true;
      notice = `Collection did not return a successful result. ${error.message} Earlier records may have been retained.`;
      setStatus(notice, true);
    } finally {
      stopPolling();
      cancelProgress();
      document.dispatchEvent(new Event("orbital:collection-updated"));
      const result = await savedProgress(parameters, true);
      if (result.current && result.error) {
        setStatus(`${notice} ${result.error.message}`, true);
      } else if (result.current) {
        setStatus(failed ? `${notice} Saved checkpoints are shown below.` : notice, failed);
      }
      state.busy = false;
      updateControls();
      document.dispatchEvent(new CustomEvent("orbital:monthly-busy", { detail: false }));
    }
  }

  function selectionChanged() {
    cancelProgress();
    end.min = start.value;
    progress.replaceChildren(node("p", "subtext", "Selection changed. Load saved progress or collect this period."));
    setStatus("Choose Collect or resume, or load saved progress for this selection.");
  }

  const now = new Date();
  const previousMonth = new Date(now.getFullYear(), now.getMonth(), 0);
  const month = `${previousMonth.getFullYear()}-${String(previousMonth.getMonth() + 1).padStart(2, "0")}`;
  start.value = `${previousMonth.getFullYear()}-01`;
  end.value = start.max = end.max = month;
  end.min = start.value;
  form.addEventListener("submit", collect);
  load.addEventListener("click", loadProgress);
  start.addEventListener("change", selectionChanged);
  end.addEventListener("change", selectionChanged);
  windowDays.addEventListener("change", selectionChanged);
  agency.addEventListener("change", selectionChanged);
  document.addEventListener("orbital:legacy-busy", (event) => {
    state.legacyBusy = Boolean(event.detail);
    updateControls();
  });
  updateControls();
})();
