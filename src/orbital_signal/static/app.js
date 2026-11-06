"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    view: "signals", signals: [], companies: [], runs: [],
    loaded: { signals: false, companies: false },
    errors: { signals: null, companies: null },
    pending: 0, importRunning: false, rankingRunning: false, monthlyRunning: false,
    requestVersion: 0,
  };
  const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const compactMoney = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
  const number = new Intl.NumberFormat();
  const titles = {
    signals: ["Signals", "Follow the signal.", "Discover relevant activity. Trace every signal to its source."],
    companies: ["Companies", "Understand the company.", "Connect the awards. See the evidence behind each profile."],
    coverage: ["Collection", "Build your evidence base.", "Collect a source window and refresh rankings from retained evidence."],
  };

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined && text !== null) result.textContent = String(text);
    return result;
  }

  function button(text, className, action) {
    const result = node("button", className, text);
    result.type = "button";
    result.addEventListener("click", action);
    return result;
  }

  function badge(text, tone = "") { return node("span", `badge ${tone ? `badge-${tone}` : ""}`, text); }
  function normalizedUei(value) { return String(value || "").trim().toUpperCase(); }
  function localIso(date) { return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`; }
  function eventDate(value) {
    if (!value) return "Date unavailable";
    const result = new Date(`${String(value).slice(0, 10)}T12:00:00`);
    return Number.isNaN(result.getTime()) ? "Date unavailable" : result.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }
  function timestamp(value, includeTime = false) {
    const result = value ? new Date(value) : null;
    if (!result || Number.isNaN(result.getTime())) return "Unavailable";
    return result.toLocaleString(undefined, includeTime
      ? { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }
      : { month: "short", day: "numeric", year: "numeric" });
  }
  function agencyName(value) {
    if (value === "National Aeronautics and Space Administration") return "NASA";
    if (value === "Department of Defense") return "Department of Defense";
    return value || "Unknown agency";
  }
  function sourceName(value) { return value === "usaspending" ? "USAspending" : String(value || "Unknown source"); }
  function amount(value, compact = false) { return (compact ? compactMoney : money).format(Number(value) || 0); }
  function queryText() { return $("search-input").value.trim().toLocaleLowerCase(); }

  function setNotice(text, error = false) {
    const notice = $("workspace-notice");
    notice.textContent = text;
    notice.hidden = !text;
    notice.classList.toggle("error", error);
  }

  function errorMessage(error) {
    if (error?.name === "AbortError" || error?.name === "TimeoutError") return "The request took too long. Refresh the workspace and try again.";
    return error?.message || "The request could not be completed. Check that Orbital Signal is running and try again.";
  }

  async function request(path, options = {}) {
    const timeout = typeof AbortSignal.timeout === "function" && options.method !== "POST" ? AbortSignal.timeout(30000) : undefined;
    const response = await fetch(path, { headers: { Accept: "application/json" }, signal: timeout, ...options });
    let data;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) {
      const detail = typeof data?.detail === "string" ? data.detail : null;
      if (detail) throw new Error(detail);
      if (response.status === 422) throw new Error("Check the dates and filter values, then try again.");
      throw new Error(`Request failed (${response.status}). Check the database connection and try again.`);
    }
    return data;
  }

  function updateConnection() {
    const connection = $("connection-status");
    const hasErrors = Object.values(state.errors).some(Boolean);
    connection.classList.toggle("error", hasErrors);
    connection.classList.toggle("connected", !hasErrors && !state.pending);
    $("connection-text").textContent = state.pending ? "Refreshing workspace" : hasErrors ? "Some data unavailable" : "Local workspace";
    $("refresh-button").disabled = state.pending > 0;
  }

  function updateMetrics() {
    $("metric-signals").textContent = state.loaded.signals ? number.format(state.signals.length) : "—";
    $("metric-companies").textContent = state.loaded.companies ? number.format(state.companies.length) : "—";
    $("metric-value").textContent = state.loaded.signals ? amount(state.signals.reduce((sum, item) => sum + Number(item.amount || 0), 0), true) : "—";
    $("nav-signal-count").textContent = state.loaded.signals ? number.format(state.signals.length) : "—";
    $("nav-company-count").textContent = state.loaded.companies ? number.format(state.companies.length) : "—";
    const latest = state.runs.find((run) => run.status === "completed");
    $("metric-import").textContent = latest ? timestamp(latest.completed_at) : "None this session";
    $("metric-import-note").textContent = "This page session only · local time";
    $("metric-import").title = latest ? timestamp(latest.completed_at || latest.started_at, true) : "";
  }

  function serverParams() {
    return new URLSearchParams({
      minimum_score: $("relevance-filter").value || "0",
      minimum_priority_score: $("priority-filter").value || "0",
      startup_candidates_only: String($("candidate-filter").checked), limit: "500",
    });
  }

  async function refreshData() {
    const version = ++state.requestVersion;
    state.pending += 1;
    updateConnection();
    if (!state.loaded.signals || !state.loaded.companies) renderBrowse();
    const jobs = [
      ["signals", `/api/v1/signals?${serverParams()}`],
      ["companies", `/api/v1/companies?${new URLSearchParams({ startup_candidates_only: String($("candidate-filter").checked), limit: "500" })}`],
    ];
    const results = await Promise.allSettled(jobs.map(([, path]) => request(path)));
    if (version === state.requestVersion) {
      results.forEach((result, index) => {
        const key = jobs[index][0];
        if (result.status === "fulfilled" && Array.isArray(result.value)) {
          state[key] = result.value;
          state.loaded[key] = true;
          state.errors[key] = null;
        } else {
          state.errors[key] = result.status === "rejected" ? errorMessage(result.reason) : "The server returned an unexpected data format.";
        }
      });
      updateAgencyOptions();
      updateMetrics();
      renderBrowse();
      renderCoverage();
    }
    state.pending -= 1;
    updateConnection();
  }

  function updateAgencyOptions() {
    const select = $("agency-filter");
    const previous = select.value;
    const agencies = [...new Set(state.signals.map((item) => item.agency))].sort((a, b) => a.localeCompare(b));
    select.replaceChildren(node("option", "", "All agencies"));
    select.firstChild.value = "";
    for (const agency of agencies) {
      const option = node("option", "", agencyName(agency));
      option.value = agency;
      select.append(option);
    }
    if (agencies.includes(previous)) select.value = previous;
  }

  function renderInlineError(target, text, retry) {
    target.hidden = !text;
    target.replaceChildren();
    if (!text) return;
    target.append(node("span", "", text), button("Retry", "text-button", retry));
  }

  function loading(text) {
    const result = node("div", "loading-state");
    result.append(node("span", "spinner"), node("span", "", text));
    return result;
  }

  function empty(title, description, actionText, action) {
    const result = node("div", "empty-state");
    result.append(node("div", "empty-symbol", "◉"), node("h3", "", title), node("p", "", description));
    if (actionText && action) result.append(button(actionText, "button button-secondary", action));
    return result;
  }

  function resetFilters() {
    $("search-input").value = "";
    $("agency-filter").value = "";
    $("date-filter").value = "";
    $("date-through-filter").value = "";
    $("priority-filter").value = "0";
    $("relevance-filter").value = "4";
    $("candidate-filter").checked = false;
    refreshData();
  }

  function filteredSignals() {
    const query = queryText();
    const agency = $("agency-filter").value;
    const from = $("date-filter").value;
    const through = $("date-through-filter").value;
    return state.signals.filter((item) => {
      const haystack = [item.company_name, item.company_uei, item.summary, item.agency, item.source_award_id, ...(item.matched_terms || [])].join(" ").toLocaleLowerCase();
      return (!query || haystack.includes(query)) && (!agency || item.agency === agency)
        && (!from || (item.occurred_on && item.occurred_on >= from))
        && (!through || (item.occurred_on && item.occurred_on <= through));
    });
  }

  function companyIdentity(company) {
    const uei = normalizedUei(company.company_uei);
    return uei ? `uei:${uei}` : `name:${company.company_name.trim().toLowerCase()}`;
  }

  function companyButton(company) {
    const identity = companyIdentity(company);
    const profile = state.companies.find((item) => companyIdentity(item) === identity);
    return profile ? button(company.company_name, "company-name", () => openCompany(profile)) : node("span", "company-name", company.company_name);
  }

  function evidenceLink(url) {
    let parsed;
    try { parsed = new URL(url); } catch { return node("span", "subtext", "Link unavailable"); }
    if (!["https:", "http:"].includes(parsed.protocol)) return node("span", "subtext", "Link unavailable");
    const result = node("a", "evidence-link", "Source record");
    result.href = parsed.href;
    result.target = "_blank";
    result.rel = "noopener noreferrer";
    result.setAttribute("aria-label", "Open the public source record in a new tab");
    return result;
  }

  function priority(score) {
    const value = Math.max(0, Math.min(100, Number(score) || 0));
    const result = node("div", "priority");
    const label = node("div", "priority-number", value);
    label.append(node("span", "", " / 100"));
    const track = node("div", "priority-track");
    const fill = node("span");
    fill.style.width = `${value}%`;
    track.setAttribute("aria-hidden", "true");
    track.append(fill);
    result.append(label, track);
    return result;
  }

  function explanation(signal) {
    const result = node("details", "signal-explanation");
    result.append(node("summary", "", "Why this signal"));
    const body = node("div", "explanation-body");
    for (const [title, entries] of [[`Relevance · ${signal.relevance_score}/100`, signal.reasons], ["Priority reasons", signal.priority_reasons], ["Quality flags", signal.quality_flags]]) {
      if (!entries?.length) continue;
      const list = node("ul");
      entries.forEach((entry) => list.append(node("li", "", String(entry).replaceAll("_", " "))));
      body.append(node("h3", "", title), list);
    }
    body.append(node("p", "", `First detected ${timestamp(signal.detected_at, true)}. Activity date may reflect the latest transaction and can fall outside the import window.`));
    body.append(node("p", "source-id", `Award ID: ${signal.source_award_id}`));
    result.append(body);
    return result;
  }

  function table(headers) {
    const wrapper = node("div", "table-wrap");
    const result = node("table");
    const head = node("thead");
    const row = node("tr");
    headers.forEach((title) => {
      const th = node("th", "", title);
      th.scope = "col";
      row.append(th);
    });
    head.append(row);
    const body = node("tbody");
    result.append(head, body);
    wrapper.append(result);
    return { wrapper, body };
  }

  function renderSignals(items) {
    const { wrapper, body } = table(["COMPANY", "AWARD EVIDENCE", "AWARD VALUE", "PRIORITY", "ACTIVITY DATE", "EVIDENCE"]);
    for (const signal of items) {
      const row = node("tr");
      const company = node("td", "company-cell");
      company.append(companyButton(signal), node("span", "subtext", agencyName(signal.agency)));
      if (signal.is_startup_candidate) company.append(badge("Startup candidate", "green"));
      else if ((signal.quality_flags || []).includes("established_contractor")) company.append(badge("Established contractor"));
      const description = node("td", "description-cell");
      description.append(node("p", "award-summary", signal.summary || "No description provided by the source."));
      if (signal.matched_terms?.length) {
        const terms = node("div", "term-list");
        signal.matched_terms.slice(0, 4).forEach((term) => terms.append(badge(term)));
        description.append(terms);
      }
      description.append(explanation(signal));
      const value = node("td", "amount-cell", amount(signal.amount));
      value.append(node("span", "subtext", "Source award amount"));
      const score = node("td"); score.append(priority(signal.priority_score));
      const date = node("td", "date-cell", eventDate(signal.occurred_on));
      date.append(node("span", "subtext", `Detected ${timestamp(signal.detected_at)}`));
      const evidence = node("td");
      evidence.append(evidenceLink(signal.evidence_url), node("span", "subtext", sourceName(signal.source)));
      row.append(company, description, value, score, date, evidence);
      body.append(row);
    }
    return wrapper;
  }

  function renderCompanies(items) {
    const { wrapper, body } = table(["COMPANY", "QUALIFYING AWARDS", "TOTAL AWARD VALUE", "TOP PRIORITY", "LATEST ACTIVITY", "AGENCIES"]);
    for (const company of items) {
      const row = node("tr");
      const name = node("td", "company-cell");
      name.append(companyButton(company));
      if (company.startup_candidate) name.append(badge("Startup candidate", "green"));
      else if (company.established_contractor) name.append(badge("Established contractor"));
      else name.append(badge("Other recipient", "muted"));
      if (company.company_uei) name.append(node("span", "subtext", `UEI ${company.company_uei}`));
      const count = node("td", "amount-cell", number.format(company.signal_count));
      count.append(node("span", "subtext", "Source-linked signals"));
      const total = node("td", "amount-cell", amount(company.total_amount));
      total.append(node("span", "subtext", "Across retained signals"));
      const score = node("td"); score.append(priority(company.max_priority_score));
      const latest = node("td", "date-cell", eventDate(company.latest_signal_date));
      const agencies = node("td", "date-cell");
      company.agencies.forEach((agency) => agencies.append(node("div", "", agencyName(agency))));
      row.append(name, count, total, score, latest, agencies);
      body.append(row);
    }
    return wrapper;
  }

  function renderBrowse() {
    const isCompanies = state.view === "companies";
    const key = isCompanies ? "companies" : "signals";
    const content = $("results-content");
    content.setAttribute("aria-busy", String(!state.loaded[key] && !state.errors[key]));
    $("signal-filters").hidden = isCompanies;
    $("results-heading").textContent = isCompanies ? "Company directory" : "Activity feed";
    $("results-sort").textContent = isCompanies ? "TOP PRIORITY ORDER" : "PRIORITY ORDER";
    $("results-footnote").textContent = isCompanies
      ? "Company values sum retained source award snapshots. They are not verified revenue or obligations within a date window. Candidate status is heuristic."
      : "Candidate status is a heuristic. Activity dates and first detection are separate; inspect the public evidence behind each result.";
    renderInlineError($("results-error"), state.errors[key] ? `${state.loaded[key] ? "Showing previously loaded data. " : ""}${state.errors[key]}` : null, refreshData);
    if (!state.loaded[key]) {
      content.replaceChildren(state.errors[key] ? empty("Evidence is unavailable", "Check the database connection, then retry the request.", "Retry", refreshData) : loading("Loading public award evidence…"));
      $("results-note").textContent = state.errors[key] ? "Data unavailable" : "Loading public award evidence…";
      return;
    }
    const query = queryText();
    const items = isCompanies ? state.companies.filter((company) => [company.company_name, company.company_uei, ...company.agencies].join(" ").toLocaleLowerCase().includes(query)) : filteredSignals();
    $("results-note").textContent = `${number.format(items.length)} ${isCompanies ? "companies" : "signals"} shown · ${number.format(state[key].length)} loaded${state[key].length === 500 ? " · limit reached; narrow your filters" : " · up to 500 per request"}`;
    if (!items.length) {
      const hasData = state[key].length > 0;
      const narrowedServer = $("candidate-filter").checked || Number($("priority-filter").value) > 0 || Number($("relevance-filter").value) > 4;
      content.replaceChildren(hasData || narrowedServer
        ? empty("No matches for these filters", "Try a broader search or reset your filters. Missing results can also reflect a gap in source coverage.", "Reset filters", resetFilters)
        : empty(isCompanies ? "Your company directory starts here" : "Your next signal starts with evidence", "Collect a window of NASA and Department of Defense awards to build a source-linked view of space company activity.", "Collect awards", openImport));
      return;
    }
    content.replaceChildren(isCompanies ? renderCompanies(items) : renderSignals(items));
  }

  function runStatus(run) {
    const value = String(run.status || "unknown");
    if (value === "completed") return badge("Completed", "green");
    if (value === "failed") return badge("Failed", "red");
    if (value === "running") return badge("Running", "amber");
    return badge(value, "muted");
  }

  function renderCoverage() {
    const content = $("coverage-content");
    content.setAttribute("aria-busy", "false");
    $("history-note").textContent = `${state.runs.length} ${state.runs.length === 1 ? "request" : "requests"} since this page opened`;
    if (!state.runs.length) {
      content.replaceChildren(empty("No collection requests this session", "Choose a date window to collect public awards. Results here last until this page is reloaded; earlier imports remain in the company and signal feeds.", "Collect awards", openImport));
      return;
    }
    const history = node("div", "run-list");
    for (const run of state.runs) {
      const details = node("details", "run-item");
      const heading = node("summary");
      const date = node("span", "run-date", timestamp(run.started_at, true));
      date.append(node("span", "subtext", sourceName(run.source)));
      const status = node("span");
      status.append(runStatus(run));
      const window = node("span", "run-window", `${eventDate(run.requested_start_date)} — ${eventDate(run.requested_end_date)}`);
      heading.append(date, status, window);
      details.append(heading);
      const body = node("div", "run-details");
      body.append(node("p", "subtext", `Requested activity window: ${eventDate(run.requested_start_date)} — ${eventDate(run.requested_end_date)}`));
      if (run.status === "completed") {
        const counts = node("div", "run-counts");
        for (const [label, value] of [["Fetched", run.fetched_count], ["Relevant", run.relevant_count], ["New signals", run.stored_count], ["Existing signals", run.duplicate_count]]) {
          const item = node("div");
          item.append(node("span", "", label), node("strong", "", number.format(Number(value) || 0)));
          counts.append(item);
        }
        body.append(counts);
      }
      if (run.status === "running") body.append(node("p", "", "Waiting for the collection response. This can take several minutes."));
      if (run.completed_at) body.append(node("p", "", `Response received ${timestamp(run.completed_at, true)}`));
      if (run.error_message) body.append(node("p", "run-error", run.error_message));
      details.append(body);
      history.append(details);
    }
    content.replaceChildren(history);
  }

  function setView(value) {
    state.view = Object.hasOwn(titles, value) ? value : "signals";
    const [label, title, description] = titles[state.view];
    document.querySelectorAll("[data-view]").forEach((link) => {
      if (link.dataset.view === state.view) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    $("breadcrumb-current").textContent = label;
    $("page-title").textContent = title;
    $("page-description").textContent = description;
    $("browse-panel").hidden = state.view === "coverage";
    $("coverage-view").hidden = state.view !== "coverage";
    document.title = `${label} · Orbital Signal`;
    renderBrowse();
  }

  function openImport() {
    $("import-status").textContent = state.importRunning ? "An import is in progress. It can take several minutes. You can close this dialog and inspect Collection for its status." : "";
    $("import-status").classList.remove("error");
    $("import-dialog").showModal();
  }

  async function submitImport(event) {
    event.preventDefault();
    if (state.importRunning || state.monthlyRunning || !$("import-form").reportValidity()) return;
    const start = $("import-start").value;
    const end = $("import-end").value;
    const status = $("import-status");
    status.classList.remove("error");
    if (state.rankingRunning) { status.textContent = "Wait for the ranking refresh to finish before collecting awards."; status.classList.add("error"); return; }
    if (end < start) { status.textContent = "The end date must be on or after the start date."; status.classList.add("error"); return; }
    state.importRunning = true;
    document.dispatchEvent(new CustomEvent("orbital:legacy-busy", { detail: true }));
    ["submit-import", "import-start", "import-end", "refresh-rankings"].forEach((id) => { $(id).disabled = true; });
    $("submit-import").textContent = "Collecting…";
    $("open-import").textContent = "Collection in progress…";
    status.textContent = "Collecting awards and enriching transaction dates. This can take several minutes. You can close this dialog and inspect Collection while it runs.";
    const run = { source: "usaspending", status: "running", started_at: new Date().toISOString(), requested_start_date: start, requested_end_date: end };
    state.runs.unshift(run);
    renderCoverage();
    setNotice("Collection is in progress. This request appears in session activity until the page is reloaded.");
    try {
      const result = await request(`/api/v1/ingestions/usaspending?${new URLSearchParams({ start_date: start, end_date: end })}`, { method: "POST" });
      Object.assign(run, result, { status: "completed", completed_at: new Date().toISOString() });
      setNotice(`Collection finished: ${number.format(result.fetched_count)} records fetched, ${number.format(result.relevant_count)} relevant, ${number.format(result.stored_count)} new signals, ${number.format(result.duplicate_count)} existing signals. The source search is bounded; completion does not establish full coverage.`);
      status.textContent = "Collection finished. Your workspace has been refreshed.";
      if ($("import-dialog").open) $("import-dialog").close();
    } catch (error) {
      const message = errorMessage(error);
      status.textContent = message;
      status.classList.add("error");
      Object.assign(run, { status: "failed", completed_at: new Date().toISOString(), error_message: `${message} The stored outcome may be partial or unknown; refresh the feeds before retrying.` });
      setNotice(`Collection did not return a successful result. ${run.error_message}`, true);
    } finally {
      state.importRunning = false;
      document.dispatchEvent(new CustomEvent("orbital:legacy-busy", { detail: false }));
      ["submit-import", "import-start", "import-end"].forEach((id) => { $(id).disabled = false; });
      $("refresh-rankings").disabled = state.rankingRunning;
      $("submit-import").textContent = "Collect awards";
      $("open-import").textContent = "Collect awards";
      updateMetrics();
      renderCoverage();
      await refreshData();
    }
  }

  async function refreshRankings() {
    if (state.importRunning || state.rankingRunning || state.monthlyRunning) return;
    state.rankingRunning = true;
    document.dispatchEvent(new CustomEvent("orbital:legacy-busy", { detail: true }));
    $("refresh-rankings").disabled = true;
    $("refresh-rankings").textContent = "Refreshing rankings…";
    try {
      const result = await request(`/api/v1/reclassifications/signals?${new URLSearchParams({ as_of: localIso(new Date()) })}`, { method: "POST" });
      setNotice(`Rankings refreshed from retained evidence: ${number.format(result.scanned_count)} awards scanned, ${number.format(result.reclassified_count)} signals reclassified, ${number.format(result.removed_count)} signals removed. Source records were retained.`);
    } catch (error) { setNotice(`Ranking refresh failed. ${errorMessage(error)}`, true); }
    finally {
      state.rankingRunning = false;
      document.dispatchEvent(new CustomEvent("orbital:legacy-busy", { detail: false }));
      $("refresh-rankings").disabled = state.importRunning;
      $("refresh-rankings").textContent = "Refresh rankings";
      await refreshData();
    }
  }

  function openCompany(profile) {
    const signals = state.signals.filter((signal) => companyIdentity(signal) === companyIdentity(profile))
      .sort((a, b) => String(b.occurred_on || "").localeCompare(String(a.occurred_on || "")));
    renderCompanyDetail({ profile, signals, aliases: [...new Set(signals.map((signal) => signal.company_name))],
      identity_basis: profile.company_uei ? "Matched by recipient UEI from public award records." : "Matched by normalized recipient name; this identity is not independently verified." });
    $("company-detail").setAttribute("aria-busy", "false");
    if (!$("company-dialog").open) $("company-dialog").showModal();
  }

  function renderCompanyDetail(detail) {
    const profile = detail.profile;
    $("company-title").textContent = profile.company_name;
    const content = $("company-detail");
    content.replaceChildren();
    const meta = node("div", "detail-meta");
    if (profile.company_uei) meta.append(node("span", "subtext", `UEI ${profile.company_uei}`));
    if (profile.startup_candidate) meta.append(badge("Startup candidate", "green"));
    else if (profile.established_contractor) meta.append(badge("Established contractor"));
    else meta.append(badge("Other recipient", "muted"));
    profile.agencies.forEach((agency) => meta.append(badge(agencyName(agency))));
    content.append(meta);
    const metrics = node("div", "detail-metrics");
    for (const [label, value] of [["QUALIFYING AWARDS", number.format(profile.signal_count)], ["TOTAL AWARD VALUE", amount(profile.total_amount, true)], ["TOP PRIORITY", `${profile.max_priority_score} / 100`]]) {
      const item = node("div", "detail-metric");
      item.append(node("span", "", label), node("strong", "", value));
      metrics.append(item);
    }
    content.append(metrics);
    const identity = node("div", "identity-note");
    identity.append(node("strong", "", "Identity and interpretation"), node("p", "", detail.identity_basis || "Identity is inferred from public award records."));
    if (detail.aliases?.length) identity.append(node("p", "", `Names in source evidence: ${detail.aliases.join("; ")}`));
    identity.append(node("p", "", "Award value sums retained award snapshots; it is not verified revenue or new obligations in the import window. Startup candidacy is heuristic."));
    content.append(identity, node("h3", "timeline-heading", "Loaded evidence timeline"), node("p", "timeline-intro", `${detail.signals.length} matching signals in the loaded feed, ordered by activity date. The feed uses the current server filters and a 500-signal limit; this timeline may show only part of the company profile. First detection records when evidence was first observed.`));
    if (state.errors.signals) content.append(node("p", "inline-error", "The signal feed could not be refreshed. Evidence below may be missing or previously loaded."));
    const timeline = node("ol", "timeline");
    for (const signal of detail.signals) {
      const item = node("li", "timeline-item");
      const top = node("div", "timeline-top");
      top.append(node("span", "date-cell", eventDate(signal.occurred_on)), node("span", "timeline-value", amount(signal.amount)));
      item.append(top, node("h3", "", agencyName(signal.agency)), node("p", "timeline-summary", signal.summary || "No source description available."));
      const bottom = node("div", "timeline-bottom");
      bottom.append(evidenceLink(signal.evidence_url), badge(`Priority ${signal.priority_score}`), node("span", "subtext", `First detected ${timestamp(signal.detected_at, true)}`));
      item.append(bottom, explanation(signal));
      timeline.append(item);
    }
    if (!detail.signals.length) content.append(empty("No company signals in the loaded feed", "The company profile includes retained evidence outside the current signal filters or request limit. Broaden the signal filters and refresh to inspect more evidence."));
    else content.append(timeline);
  }

  const today = new Date();
  const start = new Date(today); start.setDate(start.getDate() - 30);
  $("import-start").value = localIso(start);
  $("import-end").value = localIso(today);
  $("filter-form").addEventListener("submit", (event) => { event.preventDefault(); if ($("filter-form").reportValidity()) refreshData(); });
  $("search-input").addEventListener("input", renderBrowse);
  ["agency-filter", "date-filter", "date-through-filter"].forEach((id) => $(id).addEventListener("change", renderBrowse));
  ["priority-filter", "relevance-filter", "candidate-filter"].forEach((id) => $(id).addEventListener("change", () => { if ($("filter-form").reportValidity()) refreshData(); }));
  $("clear-filters").addEventListener("click", resetFilters);
  $("refresh-button").addEventListener("click", refreshData);
  document.addEventListener("orbital:collection-updated", refreshData);
  document.addEventListener("orbital:monthly-busy", (event) => {
    state.monthlyRunning = Boolean(event.detail);
    $("open-import").disabled = state.monthlyRunning;
    $("submit-import").disabled = state.monthlyRunning || state.importRunning;
    $("refresh-rankings").disabled = state.monthlyRunning || state.importRunning || state.rankingRunning;
  });
  $("open-import").addEventListener("click", openImport);
  $("import-form").addEventListener("submit", submitImport);
  $("refresh-rankings").addEventListener("click", refreshRankings);
  $("import-start").addEventListener("change", () => { $("import-end").min = $("import-start").value; });
  document.querySelectorAll("[data-close]").forEach((control) => control.addEventListener("click", () => $(control.dataset.close).close()));
  window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
  setView(location.hash.slice(1));
  renderCoverage();
  updateMetrics();
  refreshData();
})();
