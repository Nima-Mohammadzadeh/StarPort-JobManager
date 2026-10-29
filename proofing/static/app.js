/* RFID Proofing Utility, served by Job Manager at /proofing.
   Changes from the standalone Beta 21.4 page are marked "JM:". */
(() => {
  "use strict";

  const API_BASE = "/proofing";                                   // JM: API lives under /proofing
  const jmOperator = () => { try { return (localStorage.getItem("jm_operator") || "").replace(/[^\x20-\x7E]/g, ""); } catch (_) { return ""; } };
  const $ = (q, root = document) => root.querySelector(q);
  const $$ = (q, root = document) => [...root.querySelectorAll(q)];
  const safe = v => v == null ? "" : String(v);
  const number = (v, fallback = 0) => Number.isFinite(Number(v)) ? Number(v) : fallback;

  const state = {
    connected: true,
    settings: {},
    inlays: [],
    recentJobs: [],
    recentJobDetails: [],
    queue: [],
    approvedQueue: [],
    archivedQueue: [],
    encodingTypes: ["SGTIN-96","SGTIN-198","SSCC-96","SGLN-96","GRAI-96","GIAI-96","CPI-96","GDTI-96","Custom EPC / Hex","Other / Customer Specific"],
    jobPath: "",
    jobRev: "",          // JM: change marker of the saved file, for "someone else saved it" checks
    jmJob: null,         // JM: the linked Job Manager job
    dataRoot: "",
    previewUrl: "",
    latestOutputPath: "",
    latestOutputUrl: "",
    dirty: false,
    validationTimer: null,
    job: null,
  };

  function defaultJob() {
    return {
      customer: "", preparing_company: "MCC Label", job_number: "", part_number: "", quantity: "", revision: "A", proof_revision: "1",
      gtin: "", encoding_type: "SGTIN-96", barcode_type_1: "UPC-A", barcode_data_1: "",
      barcode_type_2: "", barcode_data_2: "", font_size_callout: "", label_width: 4, label_height: 2,
      inlay: "", chip: "", inlay_width_mm: 0, inlay_height_mm: 0, inlay_image_path: "",
      inlay_preview_opacity: 45, inlay_x_mm: 0, inlay_y_mm: 0, inlay_rotation: 0,
      filter_value: 3, company_prefix_digits: 7, company_prefix: "", item_reference: "", serial: 1,
      epc_hex: "", template_path: "", preview_path: "", preview_generated_at: "", preview_template_path: "",
      preview_source: "", preview_fingerprint: "", notes: "", operator_initials: "", internal_comments: "",
      proof_id: "", created_at: "", updated_at: "", last_generated_at: "", lifecycle_status: "draft",
      last_generated_proof_revision: "", last_generated_master_company: "", last_generated_snapshot: {},
      last_generated_fingerprint: "", last_generated_output_path: "", last_generated_output_kind: "",
      approval_status: "Draft", approval_updated_at: "", approval_by: "", approval_note: "", approval_history: [],
      po_data_reference: "",
      variable_data_source: "Customer PO / approved data file",
      barcode_1_classification: "Static",
      barcode_1_source: "Customer PO / item specification",
      barcode_1_rule: "Use the approved barcode data shown on this proof",
      barcode_2_classification: "Static",
      barcode_2_source: "Customer PO / item specification",
      barcode_2_rule: "Use the approved barcode data shown on this proof",
      serial_classification: "Variable",
      serial_source: "Production serial allocation",
      serial_rule: "One unique serial / reference per label",
      epc_classification: "Variable",
      epc_source: "Generated from approved encoding fields",
      epc_rule: "Generate per the selected encoding scheme using the approved static fields and variable serial / reference",
      variable_data_notes: "",
    };
  }

  async function api(path, payload = {}) {
    try {
      const response = await fetch(API_BASE + path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Operator": jmOperator() },
        body: JSON.stringify(payload),
        cache: "no-store",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        const error = new Error(data.error || data.message || `Request failed (${response.status})`);
        error.data = data;
        error.status = response.status;
        throw error;
      }
      state.connected = true;
      renderConnection();
      return data;
    } catch (error) {
      if (error instanceof TypeError) {
        state.connected = false;
        renderConnection();
      }
      throw error;
    }
  }

  function toast(message, type = "info") {
    const host = $("#toastHost");
    const item = document.createElement("div");
    item.className = `toast ${type}`;
    item.textContent = message;
    host.appendChild(item);
    setTimeout(() => item.remove(), 3600);
  }

  function renderConnection() {
    $("#connectionDot").className = `dot ${state.connected ? "ok" : "bad"}`;
    $("#connectionText").textContent = state.connected ? "Connected" : "Disconnected";
  }

  const pageMeta = {
    home: ["Home", "Create, validate, and generate RFID proofs."],
    proof: ["Proof", "Build the proof from job data through customer approval."],
    queue: ["Queue", "Open saved proofs and resume work."],
    settings: ["Settings", "Workstation, BarTender, and inlay configuration."],
  };

  function showPage(name) {
    $$(".page").forEach(el => el.classList.toggle("active", el.id === `page-${name}`));
    $$(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.page === name));
    const [title, subtitle] = pageMeta[name] || pageMeta.home;
    $("#pageTitle").textContent = title;
    $("#pageSubtitle").textContent = subtitle;
    if (name === "home") { refreshHome(); loadJmJobs(); }
    if (name === "queue") refreshQueue();
    if (name === "settings") renderSettings();
  }

  function setDirty(value = true) {
    state.dirty = value;
    $("#saveStatus").textContent = value ? "Unsaved changes" : (state.jobPath ? "Saved" : "Draft ready");
    $("#saveStatus").className = `status ${value ? "warning" : ""}`;
  }

  function coerceField(name, value) {
    if (["label_width","label_height","inlay_width_mm","inlay_height_mm","inlay_x_mm","inlay_y_mm"].includes(name)) return number(value, 0);
    if (["inlay_preview_opacity","inlay_rotation","filter_value","company_prefix_digits"].includes(name)) return Math.round(number(value, 0));
    if (name === "serial" && safe(state.job.encoding_type) === "SGTIN-96") return Math.round(number(value, 0));
    return value;
  }

  function syncJobFromForm() {
    $$('[data-job]').forEach(el => {
      const key = el.dataset.job;
      state.job[key] = coerceField(key, el.value);
    });
  }

  function syncFormFromJob() {
    renderEncodingTypes();
    $$('[data-job]').forEach(el => {
      const value = state.job[el.dataset.job];
      el.value = value == null ? "" : value;
    });
    $("#masterCompany").value = state.job.preparing_company || state.job.last_generated_master_company || state.settings.master_company || "MCC Label";
    $("#approvalStatus").value = state.job.approval_status || "Draft";
    $("#approvalNote").value = state.job.approval_note || "";
    renderContext();
    renderEncodingMode();
    renderApprovalLock();
    renderPreview();
    updateVariableDataDefinitionStatus();
    scheduleValidation(20);
  }

  function renderContext() {
    const label = [state.job.customer, state.job.job_number].filter(Boolean).join(" · ") || "New RFID Proof";
    $("#contextTitle").textContent = label;
    const detail = [state.job.part_number && `Part ${state.job.part_number}`, state.job.proof_id, `Proof Rev ${state.job.proof_revision || "1"}`].filter(Boolean).join(" · ");
    $("#contextDetail").textContent = detail || (state.jobPath ? state.jobPath : "Unsaved draft");
    renderJmLink();
  }

  /* JM: which Job Manager job this proof belongs to */
  function renderJmLink() {
    const bar = $("#jmLinkBar"); if (!bar) return;
    const j = state.jmJob;
    if (j) {
      bar.innerHTML = `<span class="jm-chip">Job Manager</span> <a href="${escapeHtml(j.open_url)}" target="_blank" rel="noopener">${escapeHtml(j.job_number)} · ticket ${escapeHtml(j.job_ticket || "—")}</a> <span class="jm-note">${escapeHtml(j.title || "")} · ${escapeHtml(j.status || "")}${j.archived_at ? ", archived" : ""}</span> <button class="btn sm quiet" type="button" data-jm-link>Change</button>`;
    } else if (state.job && (state.job.jm_job_number || state.job.jm_job_id)) {
      bar.innerHTML = `<span class="jm-chip warn">Job Manager</span> <span class="jm-note">Linked to ${escapeHtml(state.job.jm_job_number || "#" + state.job.jm_job_id)}, which isn't in Job Manager.</span> <button class="btn sm quiet" type="button" data-jm-link>Change</button>`;
    } else {
      bar.innerHTML = state.jobPath
        ? `<span class="jm-note">Not linked to a Job Manager job.</span> <button class="btn sm quiet" type="button" data-jm-link>Link to job</button>`
        : `<span class="jm-note">Not linked to a job. Start proofs from <b>Jobs &amp; proofs</b> (or a job's drawer) to link them, or save and link.</span>`;
    }
  }

  function renderEncodingTypes() {
    const select = $("#encodingType");
    if (!select) return;
    const current = safe(state.job?.encoding_type || "SGTIN-96");
    const options = [...state.encodingTypes];
    if (current && !options.includes(current)) options.push(current);
    select.innerHTML = "";
    options.forEach(value => select.add(new Option(value, value)));
    select.value = current || "SGTIN-96";
  }

  function renderEncodingMode() {
    const encoding = safe(state.job.encoding_type || "SGTIN-96");
    const sgtin = encoding === "SGTIN-96";
    $$(".sgtin-only").forEach(el => el.hidden = !sgtin);
    $("#serialInput").inputMode = sgtin ? "numeric" : "text";
    $("#epcHex").readOnly = sgtin;
    const help = $("#encodingHelp");
    if (help) help.textContent = sgtin
      ? "SGTIN-96 uses automatic EPC calculation from GTIN, prefix, filter, and serial."
      : `${encoding} uses manual approved EPC/data entry. Serial / Reference remains editable.`;
  }

  function applyInlay(name) {
    const item = state.inlays.find(x => safe(x.name) === safe(name));
    if (!item) return;
    state.job.inlay = item.name || "";
    state.job.chip = item.chip || item.ic || "";
    state.job.inlay_width_mm = number(item.width_mm ?? item.inlay_width_mm, 0);
    state.job.inlay_height_mm = number(item.height_mm ?? item.inlay_height_mm, 0);
    state.job.inlay_image_path = item.image_path || item.preview_image || "";
    if (item.preview_opacity != null) state.job.inlay_preview_opacity = number(item.preview_opacity, 45);
  }

  function renderInlays() {
    const selectors = [$("#inlaySelect"), $("#settingDefaultInlay")];
    selectors.forEach(select => {
      const current = select === $("#inlaySelect") ? safe(state.job?.inlay) : safe(state.settings.default_inlay);
      select.innerHTML = select === $("#inlaySelect") ? '<option value="">Select RFID inlay</option>' : '<option value="">None</option>';
      state.inlays.forEach(item => select.add(new Option(item.name || "Unnamed", item.name || "")));
      select.value = current;
    });
  }

  function renderPreview() {
    const img = $("#previewImage"), empty = $("#previewEmpty");
    if (state.previewUrl) {
      img.src = `${state.previewUrl}${state.previewUrl.includes("?") ? "&" : "?"}t=${Date.now()}`;
      img.hidden = false; empty.hidden = true;
    } else {
      img.hidden = true; img.removeAttribute("src"); empty.hidden = false;
    }
    const source = safe(state.job.preview_source) || (state.job.preview_path ? "Preview" : "No preview");
    $("#previewSource").textContent = source;
    $("#previewTime").textContent = state.job.preview_generated_at ? safe(state.job.preview_generated_at).replace("T", " ") : "";
    const integrity = state.job.preview_fingerprint ? "Preview current" : (state.job.preview_path ? "Legacy preview" : "Preview missing");
    $("#previewIntegrity").textContent = integrity;
    $("#previewIntegrity").className = `mini-status ${state.job.preview_fingerprint ? "good" : ""}`;
  }

  function renderApprovalLock() {
    const locked = ["Approved", "Archived"].includes(safe(state.job.approval_status));
    $("#approvalLockText").textContent = locked ? "Locked" : "Editable";
    $("#approvalLockText").className = `mini-status ${locked ? "good" : ""}`;
    $("#reopenBtn").hidden = !locked;
    $$('[data-job]').forEach(el => {
      if (el.dataset.job === "operator_initials") return;
      el.disabled = locked;
    });
    $("#browseTemplateBtn").disabled = locked;
    $("#previewBtn").disabled = locked;
    $("#manualPreviewBtn").disabled = locked;
    $("#computeEpcBtn").disabled = locked;
  }

  function renderValidation(data) {
    const issues = data?.issues || [];
    const score = Math.max(0, 100 - (data?.blocking_count || 0) * 20 - (data?.warning_count || 0) * 5);
    $("#readinessScore").textContent = score;
    $("#validationMessage").textContent = data?.message || "Validation unavailable.";
    $("#validationMessage").className = `validation-message ${data?.status || ""}`;
    $("#proofStatus").textContent = data?.ready ? "Ready" : (data?.status === "warning" ? "Ready with warnings" : "Needs attention");
    $("#proofStatus").className = `status ${data?.ready ? "good" : data?.status === "warning" ? "warning" : ""}`;
    const host = $("#issueList"); host.innerHTML = "";
    if (!issues.length) {
      host.innerHTML = '<div class="issue ok"><strong>Proof validation passed</strong><span>No blocking proof issues found.</span></div>';
      return;
    }
    issues.forEach(issue => {
      const item = document.createElement("div"); item.className = `issue ${issue.severity || "error"}`;
      item.innerHTML = `<strong>${escapeHtml(issue.name)}</strong><span>${escapeHtml(issue.detail || "")}</span>`;
      host.appendChild(item);
    });
  }

  function escapeHtml(value) {
    return safe(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
  }

  function scheduleValidation(delay = 350) {
    clearTimeout(state.validationTimer);
    state.validationTimer = setTimeout(validateCurrent, delay);
  }

  async function validateCurrent() {
    syncJobFromForm();
    try {
      const result = await api("/api/validate", { job: state.job, require_preview: true });
      renderValidation(result);
    } catch (error) {
      $("#validationMessage").textContent = error.message;
    }
  }

  function resetJob() {
    const initials = (safe(state.settings.default_operator_initials) || initialsOf(jmOperator())).toUpperCase().slice(0, 8);
    state.job = defaultJob();
    state.job.preparing_company = state.settings.master_company || "MCC Label";
    state.job.operator_initials = initials;
    if (state.settings.default_inlay) applyInlay(state.settings.default_inlay);
    state.jobPath = ""; state.jobRev = ""; state.jmJob = null; state.previewUrl = ""; state.latestOutputPath = ""; state.latestOutputUrl = "";
    renderSync(null, null);
    $("#openLatestBtn").disabled = true; $("#latestOutputText").textContent = "No output generated in this session.";
    syncFormFromJob(); setDirty(false); showPage("proof");
  }


  /* ============================================================
     BETA21_5_CUSTOMER_VARIABLE_DATA_PROTECTION
     Customer-facing encoding definition / PO variable-data control.
     ============================================================ */
  const VARIABLE_SPEC_START = "[[RFID_VARIABLE_SPEC_V1]]";
  const VARIABLE_SPEC_END = "[[/RFID_VARIABLE_SPEC_V1]]";

  function stripVariableSpecBlock(value) {
    const text = safe(value);
    const start = text.indexOf(VARIABLE_SPEC_START);
    if (start < 0) return text.trim();
    const end = text.indexOf(VARIABLE_SPEC_END, start);
    if (end < 0) return text.slice(0, start).trim();
    return (text.slice(0, start) + text.slice(end + VARIABLE_SPEC_END.length)).trim();
  }

  function variableSpecFromJob(job) {
    job = job || state.job || {};          // JM fix: state.job is null until a proof is loaded
    return {
      version: 1,
      po_data_reference: safe(job.po_data_reference).trim(),
      variable_data_source: safe(job.variable_data_source).trim(),
      barcode_1_classification: safe(job.barcode_1_classification || "Static"),
      barcode_1_source: safe(job.barcode_1_source).trim(),
      barcode_1_rule: safe(job.barcode_1_rule).trim(),
      barcode_2_classification: safe(job.barcode_2_classification || "Static"),
      barcode_2_source: safe(job.barcode_2_source).trim(),
      barcode_2_rule: safe(job.barcode_2_rule).trim(),
      serial_classification: safe(job.serial_classification || "Variable"),
      serial_source: safe(job.serial_source).trim(),
      serial_rule: safe(job.serial_rule).trim(),
      epc_classification: safe(job.epc_classification || "Variable"),
      epc_source: safe(job.epc_source).trim(),
      epc_rule: safe(job.epc_rule).trim(),
      variable_data_notes: safe(job.variable_data_notes).trim(),
    };
  }

  function normalizeVariableSpecJob(job) {
    const normalized = { ...job };
    const raw = safe(normalized.internal_comments);
    const start = raw.indexOf(VARIABLE_SPEC_START);
    const end = start >= 0 ? raw.indexOf(VARIABLE_SPEC_END, start) : -1;

    if (start >= 0 && end > start) {
      const jsonText = raw.slice(start + VARIABLE_SPEC_START.length, end).trim();
      try {
        const spec = JSON.parse(jsonText);
        if (spec && typeof spec === "object") Object.assign(normalized, spec);
      } catch (error) {
        console.warn("Variable-data specification metadata could not be read.", error);
      }
      normalized.internal_comments = stripVariableSpecBlock(raw);
    }

    return normalized;
  }

  function packedVariableSpecJob(job) {
    const copy = JSON.parse(JSON.stringify(job || {}));
    const cleanComments = stripVariableSpecBlock(copy.internal_comments);
    const spec = variableSpecFromJob(job);
    const metadata = `${VARIABLE_SPEC_START}${JSON.stringify(spec)}${VARIABLE_SPEC_END}`;
    copy.internal_comments = [cleanComments, metadata].filter(Boolean).join("\n");
    return copy;
  }

  function variableDataDefinitionIssues(job) {
    // JM fix: this runs from bindEvents() before bootstrap() creates state.job (null, which a
    // default parameter doesn't replace), so the standalone Beta 21.5 page stopped at startup.
    job = job || state.job || {};
    const issues = [];
    const needed = (value, label) => {
      if (!safe(value).trim()) issues.push(label);
    };

    needed(job.po_data_reference, "PO / Data Reference");
    needed(job.variable_data_source, "Overall Encoding / Variable Data Source");

    const checkRow = (prefix, label, active = true) => {
      if (!active) return;
      needed(job[`${prefix}_classification`], `${label} classification`);
      needed(job[`${prefix}_source`], `${label} source`);
      needed(job[`${prefix}_rule`], `${label} production rule`);
    };

    checkRow("barcode_1", "Barcode 1", Boolean(safe(job.barcode_type_1 || job.barcode_data_1).trim()));
    checkRow("barcode_2", "Barcode 2", Boolean(safe(job.barcode_type_2 || job.barcode_data_2).trim()));
    checkRow("serial", "Serial / Reference", true);
    checkRow("epc", "EPC / Encoded Data", true);

    return issues;
  }

  function updateVariableDataDefinitionStatus() {
    const status = $("#variableDataDefinitionStatus");
    if (!status) return;
    const issues = variableDataDefinitionIssues(state.job);
    status.className = issues.length ? "need" : "ok";                     // JM: colours from the stylesheet
    status.textContent = issues.length ? `Needs: ${issues.join(", ")}` : "Complete";

    const barcode2Block = $("#variableBarcode2Block");
    if (barcode2Block) {
      barcode2Block.hidden = !Boolean(safe(state.job?.barcode_type_2 || state.job?.barcode_data_2).trim());
    }
  }

  function installVariableDataDefinitionUi() {
    if ($("#variableDataDefinitionCard")) return;
    const proofPage = $("#page-proof");
    if (!proofPage) return;
    const card = document.createElement("section");
    card.id = "variableDataDefinitionCard";
    card.className = "panel section variable-data-definition-card";        // JM: same panel as the other steps
    const row = (key, title, sub, first) => `
      <div class="vd-grid"${key === "barcode_2" ? ' id="variableBarcode2Block"' : ""}>
        <div class="el"><strong>${title}</strong><small>${sub}</small></div>
        <label>Classification<select data-job="${key}_classification">${first === "Variable" ? "<option>Variable</option><option>Static</option>" : "<option>Static</option><option>Variable</option>"}</select></label>
        <label>Source<input data-job="${key}_source"></label>
        <label>Rule / Format<input data-job="${key}_rule"></label>
      </div>`;
    card.innerHTML = `
      <div class="panel-head"><div><span class="step">◆</span><div><h2>Encoding &amp; variable data</h2><p>Printed on the customer proof: what stays fixed, what changes per label, where the data comes from.</p></div></div><span id="variableDataDefinitionStatus"></span></div>
      <div class="vd-two">
        <label>PO / Data Reference *<input data-job="po_data_reference" placeholder="PO number, PO line, data-file name, EDI reference"></label>
        <label>Overall Encoding / Variable Data Source *<input data-job="variable_data_source" placeholder="Customer PO / approved data file"></label>
      </div>
      <div class="vd-grid head"><div>Data element</div><div>Static / Variable</div><div>Source</div><div>Production rule</div></div>
      ${row("barcode_1", "Barcode 1", "Primary printed barcode", "Static")}
      ${row("barcode_2", "Barcode 2", "Secondary printed barcode", "Static")}
      ${row("serial", "Serial / Reference", "RFID serial or customer reference", "Variable")}
      ${row("epc", "EPC / Encoded Data", "Value written to the tag", "Variable")}
      <label class="vd-notes">Additional Variable Data / PO Instructions
        <textarea data-job="variable_data_notes" rows="3" placeholder="Example: serial values from the customer file column SERIAL_NO; barcode text must match the PO line; keep leading zeroes."></textarea>
      </label>
      <div class="vd-guard"><b>Proof protection:</b> the final PDF / package and Approved status are blocked until the sources and production rules above are filled in.</div>`;
    const approval = $("#approvalStatus");
    const anchor = approval?.closest("section, article, .card, .panel");
    if (anchor && anchor.id !== "page-proof" && anchor.parentElement) anchor.insertAdjacentElement("beforebegin", card);
    else proofPage.appendChild(card);
    updateVariableDataDefinitionStatus();
  }

  function requireVariableDataDefinition(actionLabel) {
    syncJobFromForm();
    const issues = variableDataDefinitionIssues(state.job);
    updateVariableDataDefinitionStatus();
    if (!issues.length) return true;

    showPage("proof");
    $("#variableDataDefinitionCard")?.scrollIntoView({ behavior: "smooth", block: "center" });
    toast(`${actionLabel} blocked — define: ${issues.join(", ")}`, "error");
    return false;
  }

  /* JM: the server refuses a save when someone else saved the same proof after it was
     opened here (409 conflict). Ask, then overwrite (force) or reload theirs. */
  async function resolveConflict(error, retry) {
    const d = error.data || {};
    if (!d.conflict) return null;
    if (d.moved) { toast(error.message, "error"); return false; }
    if (confirm(`${error.message}\n\nOK: overwrite with the version on this screen.\nCancel: keep theirs and reload it.`)) return retry(true);
    if (state.jobPath) await openQueueJob(state.jobPath);
    return false;
  }

  async function saveJob(force = false) {
    syncJobFromForm();
    state.job.preparing_company = $("#masterCompany").value || state.job.preparing_company || state.settings.master_company || "MCC Label";
    try {
      const r = await api("/api/job/save", { job: packedVariableSpecJob(state.job), path: state.jobPath, rev: state.jobRev, force: force === true });
      state.job = normalizeVariableSpecJob({ ...defaultJob(), ...r.job }); state.jobPath = r.path || state.jobPath;
      state.jobRev = r.rev || ""; if ("jm_job" in r) state.jmJob = r.jm_job;
      if (r.preview_url) state.previewUrl = r.preview_url;
      state.recentJobs = r.recent_jobs || state.recentJobs; state.recentJobDetails = r.recent_job_details || state.recentJobDetails;
      syncFormFromJob(); setDirty(false); toast("Proof saved.", "success");
      if ("sync" in r) renderSync(r.sync);
      if (r.filled_job?.length) toast(`Filled in on ${state.jmJob?.job_number || "the job"}: ${r.filled_job.map(f => `${f.label} ${f.value}`).join(", ")}`, "info");
      return true;
    } catch (error) {
      const handled = await resolveConflict(error, saveJob);
      if (handled !== null) return handled;
      toast(error.message, "error"); return false;
    }
  }

  function loadJobResponse(r) {
    state.job = normalizeVariableSpecJob({ ...defaultJob(), ...(r.job || {}) });
    state.jobPath = r.path || "";
    state.jobRev = r.rev || "";
    state.jmJob = r.jm_job || null;
    state.previewUrl = r.preview_url || "";
    state.latestOutputPath = state.job.last_generated_output_path || "";
    state.latestOutputUrl = r.latest_output_url || "";
    (r.notes || []).forEach((n, i) => setTimeout(() => toast(n, "info"), 400 + i * 900));
    if (r.path !== state.syncNotePath) state.syncNote = null;
    renderSync(r.sync || null);
    $("#openLatestBtn").disabled = !state.latestOutputPath;
    $("#latestOutputText").textContent = state.latestOutputPath || "No generated output saved on this proof.";
    syncFormFromJob(); setDirty(false); showPage("proof");
  }

  function openJob() { showPage("queue"); $("#queueSearch")?.focus(); }    // JM: open from the Queue

  async function openQueueJob(path) {
    try { const r = await api("/api/job/load", { path }); loadJobResponse(r); }
    catch (error) { toast(error.message, "error"); }
  }

  /* JM: choose the template from Job Manager's Master Templates index (the job's own
     Print copy and UPC matches first) instead of a file dialog on the host PC. */
  function openModal(id) { const m = $(id); m.hidden = false; setTimeout(() => m.querySelector("input")?.focus(), 30); }
  function closeModals() { $$(".modal").forEach(m => m.hidden = true); }

  function pickRow(item, extra = "") {
    const why = (item.reasons || []).join(" · ");
    const missing = item.exists === false ? '<em class="bad-text">not reachable</em>' : "";
    return `<button type="button" class="pick-row" data-template-path="${escapeHtml(item.file_path)}"><strong>${escapeHtml(item.file_name || item.name)}</strong><small>${escapeHtml(why || item.file_path)} ${missing}${extra}</small></button>`;
  }

  async function searchTemplates() {
    syncJobFromForm();
    const q = safe($("#templateSearch").value).trim();
    try {
      const r = await api("/api/templates/search", { q, job: state.job });
      $("#templateSuggestions").innerHTML = (r.suggestions || []).length
        ? `<div class="pick-head">Suggested for this proof</div>` + r.suggestions.map(x => pickRow(x)).join("") : "";
      $("#templateResults").innerHTML = `<div class="pick-head">${q ? "Matching templates" : "All templates"} (${(r.results || []).length}${r.total > (r.results || []).length ? ` of ${r.total}` : ""})</div>`
        + ((r.results || []).map(x => pickRow(x)).join("") || '<div class="empty">No templates match. Check the Master Templates folder in Job Manager Settings.</div>');
    } catch (error) { $("#templateResults").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }
  const searchTemplatesSoon = (() => { let t; return () => { clearTimeout(t); t = setTimeout(searchTemplates, 220); }; })();

  function browseTemplate() { $("#templateSearch").value = ""; openModal("#templateModal"); searchTemplates(); }

  async function chooseTemplate(path) {
    try {
      const r = await api("/api/templates/choose", { path });
      closeModals();
      state.job.template_path = r.path; state.job.preview_path = ""; state.job.preview_fingerprint = ""; state.previewUrl = "";
      syncFormFromJob(); setDirty(); renderPreview(); scheduleValidation(30);
    } catch (error) { toast(error.message, "error"); }
  }

  async function bartenderPreview() {
    syncJobFromForm();
    if (!state.job.template_path) { toast("Select a BarTender template first.", "error"); return; }
    const button = $("#previewBtn");
    const sideText = $("#previewSource");
    const originalText = button ? button.textContent : "Preview";
    if (button) { button.disabled = true; button.textContent = "Starting Preview…"; }
    if (sideText) sideText.textContent = "Starting BarTender preview…";
    try {
      const started = await api("/api/bartender/preview/start", { job: state.job });
      const taskId = started.task_id;
      if (!taskId) throw new Error("BarTender preview did not return a task ID.");
      const deadline = Date.now() + 180000;
      let result = null;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 650));
        const status = await api("/api/bartender/preview/status", { task_id: taskId });
        if (status.status === "done") { result = status; break; }
        if (status.status === "error") throw new Error(status.error || "BarTender preview failed.");
        const waiting = status.status === "waiting";
        if (button) button.textContent = waiting ? "Waiting for BarTender…" : "Generating Preview…";
        if (sideText) sideText.textContent = waiting ? "Another preview is running on the host PC; this one is next…" : "BarTender is generating Record 1 / Page 1…";
      }
      if (!result) throw new Error("BarTender preview timed out after 3 minutes. Check the preview log in the app logs folder.");
      state.job = { ...state.job, ...(result.job || {}) }; state.previewUrl = result.url || "";
      syncFormFromJob(); renderPreview(); setDirty(); scheduleValidation(40); toast("Artwork preview generated.", "success");
    } catch (error) {
      toast(error.message || "BarTender preview failed.", "error");
      if (sideText) sideText.textContent = `Preview failed: ${error.message || "Unknown BarTender error"}`;
    } finally {
      if (button) { button.disabled = false; button.textContent = originalText; }
    }
  }

  function manualPreview() { const f = $("#previewFileInput"); f.value = ""; f.click(); }   // JM: upload from this PC

  async function uploadPreview(file) {
    if (!file) return;
    syncJobFromForm();
    const form = new FormData(); form.append("file", file); form.append("job", JSON.stringify(state.job));
    try {
      const response = await fetch(API_BASE + "/api/preview/upload", { method: "POST", body: form, headers: { "X-Operator": jmOperator() } });
      const r = await response.json().catch(() => ({}));
      if (!response.ok || r.ok === false) throw new Error(r.error || `Upload failed (${response.status})`);
      if (r.job) state.job = { ...state.job, ...r.job }; else state.job.preview_path = r.path;
      state.previewUrl = r.url || ""; syncFormFromJob(); renderPreview(); setDirty(); scheduleValidation(40);
      toast("Preview image added.", "success");
    } catch (error) { toast(error.message, "error"); }
  }

  async function computeEpc() {
    syncJobFromForm();
    try {
      const r = await api("/api/compute-epc", { job: state.job });
      state.job.epc_hex = r.epc_hex || r.hex || "";
      state.job.company_prefix = r.company_prefix || "";
      state.job.item_reference = r.item_reference || "";
      state.job.serial = r.serial ?? state.job.serial;
      syncFormFromJob(); setDirty(); scheduleValidation(20); toast("SGTIN-96 EPC calculated.", "success");
    } catch (error) { toast(error.message, "error"); }
  }

  async function generate(kind = "pdf", allowSameRevision = false) {
    syncJobFromForm();
    state.job.preparing_company = $("#masterCompany").value || state.job.preparing_company || state.settings.master_company || "MCC Label";
    if (!requireVariableDataDefinition(kind === "package" ? "Proof package generation" : "Final PDF generation")) return;
    if (!state.jobPath && !(await saveJob())) return;
    const endpoint = kind === "package" ? "/api/proof/package" : "/api/proof/pdf";
    try {
      const r = await api(endpoint, { job: packedVariableSpecJob(state.job), allow_same_revision_changes: allowSameRevision });
      state.job = normalizeVariableSpecJob({ ...state.job, ...(r.job || {}) });
      state.latestOutputPath = r.path || ""; state.latestOutputUrl = kind === "package" ? (r.pdf_url || r.url || "") : (r.url || "");
      $("#openLatestBtn").disabled = !state.latestOutputPath;
      $("#latestOutputText").textContent = state.latestOutputPath || "Output generated.";
      setDirty(true); await saveJob();
      toast(kind === "package" ? "Customer package created." : "Editable proof PDF generated.", "success");
      if (kind === "pdf" && r.url) window.open(r.url, "_blank", "noopener");
    } catch (error) {
      const d = error.data || {};
      if (d.revision_guard && !allowSameRevision) {   // (unchanged)
        if (confirm(`This proof revision changed after the last generated output (${d.change_count || d.changes?.length || 0} change(s)). Generate again using the same Proof Revision?`)) return generate(kind, true);
      }
      if (d.validation) renderValidation(d.validation);
      toast(error.message, "error");
    }
  }

  async function nextRevision() {
    syncJobFromForm();
    try {
      const r = await api("/api/next-revision", { value: state.job.proof_revision, proof_id: state.job.proof_id });
      state.job.proof_revision = r.value || state.job.proof_revision;
      if (r.proof_id) state.job.proof_id = r.proof_id;
      state.job.approval_status = "Draft"; state.job.approval_note = "";
      state.job.last_generated_snapshot = {}; state.job.last_generated_output_path = ""; state.job.last_generated_fingerprint = "";
      syncFormFromJob(); setDirty(); toast(`Started Proof Revision ${state.job.proof_revision}.`, "success");
    } catch (error) { toast(error.message, "error"); }
  }

  async function recordApproval() {
    syncJobFromForm();
    if (safe($("#approvalStatus").value) === "Approved" && !requireVariableDataDefinition("Customer approval")) return;
    if (!state.jobPath && !(await saveJob())) return;
    try {
      const r = await api("/api/approval/update", { job: packedVariableSpecJob(state.job), path: state.jobPath, rev: state.jobRev, status: $("#approvalStatus").value, note: $("#approvalNote").value, operator: state.job.operator_initials });
      state.job = normalizeVariableSpecJob({ ...state.job, ...r.job }); state.jobPath = r.path || state.jobPath;
      state.jobRev = r.rev || state.jobRev; if ("jm_job" in r) state.jmJob = r.jm_job;
      syncFormFromJob(); setDirty(false); toast(`Approval status: ${state.job.approval_status}.`, "success");
      await refreshQueue(); await refreshHome();
    } catch (error) {
      if (error.data?.conflict) { toast(`${error.message} Save or reload first.`, "error"); return; }
      toast(error.message, "error");
    }
  }

  async function reopenApproval() {
    try {
      const r = await api("/api/approval/reopen", { job: packedVariableSpecJob(state.job), path: state.jobPath, rev: state.jobRev, operator: state.job.operator_initials, note: "Reopened for correction" });
      state.job = normalizeVariableSpecJob({ ...state.job, ...r.job }); state.jobPath = r.path || state.jobPath;
      state.jobRev = r.rev || state.jobRev; if ("jm_job" in r) state.jmJob = r.jm_job; syncFormFromJob(); setDirty(false); toast("Proof reopened for editing and returned to the active queue.", "success");
      await refreshQueue(); await refreshHome();
    } catch (error) { toast(error.message, "error"); }
  }

  function queueStatusLabel(row) {
    if (row.status === "ready") return "Ready";
    if (row.status === "generated") return "Generated";
    if (row.status === "warning") return "Warning";
    return "Needs attention";
  }

  async function refreshQueue() {
    try {
      const r = await api("/api/work-queue", { limit: 250, include_archived: true });
      state.queue = r.queue || [];
      state.approvedQueue = r.approved || [];
      state.archivedQueue = r.archived || [];
      renderQueue();
    } catch (error) { toast(error.message, "error"); }
  }

  function filteredQueueRows(rows) {
    const q = safe($("#queueSearch").value).trim().toLowerCase();
    const filter = $("#queueFilter").value;
    return rows.filter(row => {
      const hay = [row.preparing_company,row.customer,row.job_number,row.part_number,row.proof_id,row.proof_revision,row.jm_job_number,row.jm_ticket].join(" ").toLowerCase();
      return (!q || hay.includes(q)) && (!filter || row.status === filter);
    });
  }

  function renderActiveQueueBody(body, rows) {
    body.innerHTML = "";
    if (!rows.length) { body.innerHTML = '<tr><td colspan="7" class="empty-cell">No active proofs match this view.</td></tr>'; return; }
    rows.forEach(row => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><strong>${escapeHtml(row.customer || "—")}</strong><small>${escapeHtml(row.job_number || "No job #")}${row.jm_job_number ? ` · <span class="jm-chip">${escapeHtml(row.jm_job_number)}</span>` : ""}${syncChip(row.jm_sync_state)}</small></td><td>${escapeHtml(row.part_number || "—")}</td><td>${escapeHtml(row.proof_id || `P${row.proof_revision || "1"}`)}</td><td>${escapeHtml(row.approval_status || "Draft")}</td><td><span class="queue-status ${escapeHtml(row.status || "")}">${escapeHtml(queueStatusLabel(row))}</span></td><td>${escapeHtml(safe(row.updated_at).replace("T"," ").slice(0,16))}</td><td><div class="queue-actions"><button class="button secondary small" data-open-path="${escapeHtml(row.path)}">Open</button><button class="button text small" data-archive-path="${escapeHtml(row.path)}">Archive</button><button class="button text small danger-text" data-delete-path="${escapeHtml(row.path)}">Delete</button></div></td>`;
      body.appendChild(tr);
    });
  }

  function renderApprovedQueueGroups(rows) {
    const host = $("#queueApprovedGroups"); host.innerHTML = "";
    if (!rows.length) {
      host.innerHTML = '<div class="approved-empty">No approved proofs match this view.</div>';
      return;
    }

    const groups = new Map();
    rows.forEach(row => {
      const customer = safe(row.customer).trim() || "Unassigned Customer";
      const key = customer.toLowerCase();
      if (!groups.has(key)) groups.set(key, { customer, rows: [] });
      groups.get(key).rows.push(row);
    });

    [...groups.values()]
      .sort((a, b) => a.customer.localeCompare(b.customer, undefined, { sensitivity: "base" }))
      .forEach(group => {
        group.rows.sort((a, b) => safe(b.approval_updated_at || b.updated_at).localeCompare(safe(a.approval_updated_at || a.updated_at)));
        const details = document.createElement("details");
        details.className = "approved-customer-group";
        details.open = true;

        const summary = document.createElement("summary");
        summary.innerHTML = `<span class="approved-customer-title"><strong>${escapeHtml(group.customer)}</strong><small>${group.rows.length} approved proof${group.rows.length === 1 ? "" : "s"}</small></span><span class="queue-count">${group.rows.length}</span>`;
        details.appendChild(summary);

        const wrap = document.createElement("div");
        wrap.className = "table-wrap";
        const table = document.createElement("table");
        table.innerHTML = '<thead><tr><th>Company</th><th>Job</th><th>Part</th><th>Proof</th><th>Approved By</th><th>Approved</th><th>Actions</th></tr></thead><tbody></tbody>';
        const body = table.querySelector("tbody");

        group.rows.forEach(row => {
          const tr = document.createElement("tr");
          const approvedAt = safe(row.approval_updated_at || row.updated_at).replace("T", " ").slice(0,16);
          tr.innerHTML = `<td><strong>${escapeHtml(row.preparing_company || "MCC Label")}</strong></td><td><strong>${escapeHtml(row.job_number || "No job #")}</strong>${row.jm_job_number ? `<small><span class="jm-chip">${escapeHtml(row.jm_job_number)}</span>${syncChip(row.jm_sync_state)}</small>` : ""}</td><td>${escapeHtml(row.part_number || "—")}</td><td>${escapeHtml(row.proof_id || `P${row.proof_revision || "1"}`)}</td><td>${escapeHtml(row.approval_by || "—")}</td><td>${escapeHtml(approvedAt)}</td><td><div class="queue-actions"><button class="button secondary small" data-open-path="${escapeHtml(row.path)}">Open</button><button class="button text small" data-archive-path="${escapeHtml(row.path)}">Archive</button><button class="button text small danger-text" data-delete-path="${escapeHtml(row.path)}">Delete</button></div></td>`;
          body.appendChild(tr);
        });

        wrap.appendChild(table);
        details.appendChild(wrap);
        host.appendChild(details);
      });
  }

  function renderArchivedQueueBody(rows) {
    const body = $("#queueArchivedBody"); body.innerHTML = "";
    if (!rows.length) { body.innerHTML = '<tr><td colspan="7" class="empty-cell">No archived proofs match this view.</td></tr>'; return; }
    rows.forEach(row => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><strong>${escapeHtml(row.preparing_company || "MCC Label")}</strong></td><td><strong>${escapeHtml(row.customer || "—")}</strong><small>${escapeHtml(row.job_number || "No job #")}</small></td><td>${escapeHtml(row.part_number || "—")}</td><td>${escapeHtml(row.proof_id || `P${row.proof_revision || "1"}`)}</td><td>${escapeHtml(row.approval_status || "Draft")}</td><td>${escapeHtml(safe(row.updated_at).replace("T"," ").slice(0,16))}</td><td><div class="queue-actions"><button class="button secondary small" data-restore-path="${escapeHtml(row.path)}">Restore</button><button class="button text small danger-text" data-delete-path="${escapeHtml(row.path)}">Delete</button></div></td>`;
      body.appendChild(tr);
    });
  }

  function renderQueue() {
    const active = filteredQueueRows(state.queue);
    const mcc = active.filter(row => safe(row.preparing_company).toLowerCase() !== "starport technologies");
    const starport = active.filter(row => safe(row.preparing_company).toLowerCase() === "starport technologies");
    const approved = filteredQueueRows(state.approvedQueue);
    const archived = filteredQueueRows(state.archivedQueue);
    $("#queueMccCount").textContent = mcc.length;
    $("#queueStarportCount").textContent = starport.length;
    $("#queueApprovedCount").textContent = approved.length;
    $("#queueArchivedCount").textContent = archived.length;
    renderActiveQueueBody($("#queueMccBody"), mcc);
    renderActiveQueueBody($("#queueStarportBody"), starport);
    renderApprovedQueueGroups(approved);
    renderArchivedQueueBody(archived);
  }

  async function archiveQueueJob(path) {
    if (!confirm("Archive this proof? It will leave the active queue but can be restored later.")) return;
    try { await api("/api/job/archive", { path }); if (path === state.jobPath) { state.jobPath = ""; setDirty(false); } toast("Proof archived.", "success"); await refreshQueue(); await refreshHome(); }
    catch (error) { toast(error.message, "error"); }
  }

  async function restoreQueueJob(path) {
    try { await api("/api/job/restore", { path }); toast("Proof restored.", "success"); await refreshQueue(); await refreshHome(); }
    catch (error) { toast(error.message, "error"); }
  }

  async function deleteQueueJob(path) {
    if (!confirm("Permanently delete this saved proof record? This cannot be undone. Generated customer PDFs/packages are not deleted.")) return;
    try { await api("/api/job/delete", { path }); if (path === state.jobPath) { state.jobPath = ""; setDirty(false); } toast("Saved proof record deleted.", "success"); await refreshQueue(); await refreshHome(); }
    catch (error) { toast(error.message, "error"); }
  }

  async function refreshHome() {
    try {
      const r = await api("/api/operations/overview", {}); const stats = r.stats || {}; const queue = r.queue || [];
      $("#homeQueueTotal").textContent = stats.queue_total ?? queue.length;
      $("#homeReady").textContent = stats.queue_ready ?? queue.filter(x=>x.status==="ready").length;
      $("#homeAttention").textContent = stats.queue_attention ?? queue.filter(x=>x.status==="needs_attention").length;
      state.queue = queue;
      const recent = (state.recentJobDetails || []).slice(0, 8); const host = $("#homeRecent"); host.innerHTML = "";
      if (!recent.length) host.innerHTML = '<div class="empty">No saved proofs yet.</div>';
      recent.forEach(row => { const b=document.createElement("button"); b.className="list-row"; b.dataset.openPath=row.path; b.innerHTML=`<span><strong>${escapeHtml(row.customer||"Untitled")}</strong><small>${escapeHtml(row.job_number||"")} ${escapeHtml(row.part_number||"")}</small></span><span>${escapeHtml(row.lifecycle_status||"Saved")}</span>`; host.appendChild(b); });
    } catch (error) { toast(error.message, "error"); }
  }

  function renderSettings() {
    $("#settingMasterCompany").value = state.settings.master_company || "MCC Label";
    $("#settingOperator").value = state.settings.default_operator_initials || "";
    $("#settingOutputFolder").value = state.settings.proof_output_folder || "";
    $("#settingPreviewDpi").value = state.settings.preview_dpi || 300;
    $("#settingBartenderExe").value = state.settings.bartender_executable || "";
    $("#settingInlayFolder").value = state.settings.inlay_source_folder || "";
    renderInlays();
  }

  async function saveSettings() {
    const updates = {
      master_company: $("#settingMasterCompany").value,
      default_operator_initials: safe($("#settingOperator").value).toUpperCase().slice(0,8),
      proof_output_folder: $("#settingOutputFolder").value,
      preview_dpi: number($("#settingPreviewDpi").value, 300),
      bartender_executable: $("#settingBartenderExe").value,
      inlay_source_folder: $("#settingInlayFolder").value,
      default_inlay: $("#settingDefaultInlay").value,
    };
    try { const r=await api("/api/settings/save", {settings:updates}); state.settings=r.settings||{...state.settings,...updates}; renderSettings(); toast("Settings saved.","success"); }
    catch(error){ toast(error.message,"error"); }
  }

  async function syncInlays() {
    try { const r=await api("/api/inlays/sync-folder", {folder:$("#settingInlayFolder").value}); state.inlays=r.inlays||[]; renderInlays(); $("#inlaySyncStatus").textContent=`${r.summary?.added||0} added · ${r.summary?.updated||0} updated`; toast("Inlays synchronized.","success"); }
    catch(error){ toast(error.message,"error"); }
  }

  async function diagnoseBartender() { try { const r=await api("/api/bartender/diagnostics",{}); $("#bartenderDiagnostic").textContent=r.available?`Detected on the host PC: ${r.executable||"BarTender"}`:(r.message||"BarTender not detected"); } catch(error){ $("#bartenderDiagnostic").textContent=error.message; } }
  async function installationHealth(){
    try{
      const r=await api("/api/maintenance/installation-health",{});
      $("#supportStatus").textContent=r.ok_all===false?"Installation needs attention":"Installation check passed";
      $("#healthList").innerHTML=(r.checks||[]).map(c=>`<div class="issue ${c.ok?"ok":(c.critical?"error":"warning")}"><strong>${escapeHtml(c.name)}</strong><span>${escapeHtml(c.detail||"")}</span></div>`).join("");
      toast($("#supportStatus").textContent,r.ok_all===false?"error":"success");
    }catch(error){ toast(error.message,"error"); }
  }
  async function backup(){ try{const r=await api("/api/maintenance/backup",{}); $("#supportStatus").innerHTML=`Backup: <a href="${escapeHtml(r.url)}">${escapeHtml(r.path.split(/[\\/]/).pop())}</a>`; toast("Backup created.","success");}catch(error){toast(error.message,"error");} }
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) { try { await navigator.clipboard.writeText(text); return; } catch (_) {} }
    const ta=document.createElement("textarea"); ta.value=text; ta.style.position="fixed"; ta.style.opacity="0"; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  /* JM: outputs are opened through the browser (any PC), not on the host PC's screen */
  async function openLatestOutput() {
    if (state.latestOutputUrl) { window.open(state.latestOutputUrl, "_blank", "noopener"); return; }
    if (!state.latestOutputPath) return;
    try { const r = await api("/api/output/link", { path: state.latestOutputPath }); state.latestOutputUrl = r.url; window.open(r.url, "_blank", "noopener"); }
    catch (error) { toast(error.message, "error"); }
  }
  async function openOutputFolder() {
    syncJobFromForm();
    try {
      const r = await api("/api/output/folder", { job: state.job });
      const jm = r.jm_job;
      if (jm && r.in_job_folder) {
        const res = await fetch(`/api/jobs/${jm.id}/open-folder`, { method: "POST", headers: { "X-Operator": jmOperator() } });
        if (res.ok) return;                                                     // the host PC itself
        let helper = false; try { helper = localStorage.getItem("jm_helper") === "1"; } catch (_) {}
        if (helper) { window.location.href = "jobmanager://open/" + [...r.path].map(c => c.charCodeAt(0).toString(16).padStart(4, "0")).join(""); return; }
      }
      await copyText(r.path); toast(`Folder path copied: ${r.path}`, "success");
    } catch (error) { toast(error.message, "error"); }
  }
  async function relinkProofs() {
    try {
      const r = await api("/api/jm/relink", {});
      const msg = `${r.linked} proof(s) linked to jobs · ${r.indexed} indexed` + (r.ambiguous ? ` · ${r.ambiguous} matched more than one job (link those by hand)` : "");
      $("#supportStatus").textContent = msg; toast(msg, "success"); refreshQueue();
    } catch (error) { toast(error.message, "error"); }
  }

  /* JM: link this proof to a Job Manager job */
  async function openLinkDialog() {
    if (!state.jobPath) { toast("Save the proof first, then link it to a job.", "error"); return; }
    $("#linkSearch").value = safe(state.job.job_number); $("#unlinkRow").hidden = !state.jmJob;
    openModal("#linkModal"); searchLinkJobs();
  }
  async function searchLinkJobs() {
    const q = safe($("#linkSearch").value).trim();
    if (!q) { $("#linkResults").innerHTML = '<div class="empty">Type a ticket, job number or customer.</div>'; return; }
    try {
      const r = await api("/api/jm/find-jobs", { q });
      $("#linkResults").innerHTML = (r.jobs || []).map(j => `<button type="button" class="pick-row" data-link-job="${j.id}"><strong>${escapeHtml(j.customer)} · ticket ${escapeHtml(j.job_ticket || "—")}</strong><small>${escapeHtml(j.job_number)} · ${escapeHtml(j.title || "")}${j.upc ? " · UPC " + escapeHtml(j.upc) : ""} · ${escapeHtml(j.status)}${j.archived_at ? ", archived" : ""}</small></button>`).join("")
        || '<div class="empty">No Job Manager job matches.</div>';
    } catch (error) { $("#linkResults").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }
  const searchLinkJobsSoon = (() => { let t; return () => { clearTimeout(t); t = setTimeout(searchLinkJobs, 220); }; })();
  async function linkToJob(jobId) {
    if (state.dirty && !(await saveJob())) return;
    try { const r = await api("/api/jm/link", { path: state.jobPath, rev: state.jobRev, job_id: jobId }); closeModals(); loadJobResponse(r); toast(jobId ? `Linked to ${r.jm_job?.job_number || "the job"}.` : "Unlinked.", "success"); }
    catch (error) { toast(error.message, "error"); }
  }
  function initialsOf(name) {
    const w = safe(name).match(/[A-Za-z0-9]+/g) || [];
    return w.length === 1 ? w[0].slice(0, 3) : w.map(x => x[0]).join("").slice(0, 8);
  }

  /* ============================================================
     JM: Jobs & proofs list, job sync bar, live updates
     ============================================================ */
  const PROOF_PILL = { "Approved": "ok", "Customer Review": "warn", "Internal Review": "warn", "Changes Requested": "bad", "Draft": "info" };
  function syncChip(stateName) {
    if (stateName === "differs") return ' <span class="pill warn">Differs from job</span>';
    if (stateName === "approved_changed") return ' <span class="pill bad">Job changed after approval</span>';
    return "";
  }
  const jmList = { show: "needs", req: 0 };
  async function loadJmJobs() {
    const req = ++jmList.req;
    try {
      const r = await api("/api/jm/jobs", { q: safe($("#jmJobSearch")?.value).trim(), show: jmList.show });
      if (req === jmList.req) renderJmJobs(r);
    } catch (error) {
      $("#jmJobsBody").innerHTML = `<tr><td colspan="7"><div class="empty">${escapeHtml(error.message)}</div></td></tr>`;
    }
  }
  function renderJmJobs(r) {
    const rows = r.jobs || [];
    $("#jmJobsBody").innerHTML = rows.length ? rows.map(j => {
      const proof = j.proof_status
        ? `<span class="pill ${PROOF_PILL[j.proof_status] || "info"}">${escapeHtml(j.proof_status)}</span>${syncChip(j.proof_sync)}`
        : '<span class="pill none">No proof</span>';
      const act = j.proof_status
        ? `<button class="btn sm" type="button" data-jm-open="${j.id}">Open proof</button>`
        : `<button class="btn sm primary" type="button" data-jm-start="${j.id}">Start proof</button>`;
      return `<tr>
        <td><div class="tk">${escapeHtml(j.job_ticket || "—")}</div><div class="sub data">${escapeHtml(j.job_number)}</div></td>
        <td class="cust">${escapeHtml(j.customer || "")}</td>
        <td><div class="item" title="${escapeHtml(j.title || "")}">${escapeHtml(j.title || "")}</div>${j.upc ? `<div class="sub data">${escapeHtml(j.upc)}</div>` : '<span class="chip-special">Special</span>'}</td>
        <td class="hide-md">${escapeHtml(j.label_size || "")}</td>
        <td class="num data">${j.quantity ? Number(j.quantity).toLocaleString() : "—"}</td>
        <td>${proof}${j.proof_sync_note ? `<div class="sub">${escapeHtml(j.proof_sync_note)}</div>` : ""}</td>
        <td class="act">${act}</td></tr>`;
    }).join("") : `<tr><td colspan="7"><div class="empty">${jmList.show === "needs" ? "Every open job has an approved, current proof." : "No open jobs match."}</div></td></tr>`;
    $("#jmJobsNote").textContent = jmList.show === "needs"
      ? `Open jobs with no proof yet, a proof not approved yet, or a proof approved before the job changed. ${r.without_proof || 0} job(s) have no proof.`
      : "All open jobs in Job Manager.";
  }
  async function openFromJm(id, isNew = false) {
    if (state.dirty && !confirm("Discard unsaved changes and open this job's proof?")) return;
    try {
      const r = await api(`/api/jm/jobs/${encodeURIComponent(id)}/open`, { new: isNew });
      loadJobResponse(r);
      if (r.created) toast(`New proof started from ${r.jm_job?.job_number || "the job"}. Check every field before generating.`, "success");
    } catch (error) { toast(error.message, "error"); }
  }

  /* Sync bar: a note of what was taken from the job (kept until dismissed), and what differs. */
  function renderSync(sync, note) {
    const bar = $("#syncBar"); if (!bar) return;
    state.sync = sync || null;
    const job = escapeHtml(sync?.job_number || state.jmJob?.job_number || "the job");
    if (note !== undefined) { state.syncNote = note; state.syncNotePath = state.jobPath; }
    if (sync?.updated?.length) {
      state.syncNote = `<b>Updated from ${job}:</b> ${sync.updated.map(u => `${escapeHtml(u.label)} ${escapeHtml(u.from || "(blank)")} → ${escapeHtml(u.to)}`).join("; ")}`;
      state.syncNotePath = state.jobPath;
    }
    const msgs = [];
    if (state.syncNote) msgs.push(`<div class="sync-msg info"><span>${state.syncNote}</span><div class="acts"><button class="btn sm quiet" type="button" data-sync-dismiss>OK</button></div></div>`);
    if (sync?.differs?.length) {
      const list = sync.differs.map(d => `<span><b>${escapeHtml(d.label)}</b>: proof ${escapeHtml(d.proof || "(blank)")} · job ${escapeHtml(d.job)}${sync.locked ? "" : ` <button class="btn sm quiet" type="button" data-sync-use="${escapeHtml(d.field)}">Use job value</button>`}</span>`).join("");
      msgs.push(sync.locked
        ? `<div class="sync-msg bad"><span><b>${job} changed after this proof was ${escapeHtml(safe(state.job?.approval_status || "approved").toLowerCase())}.</b> Start the next proof revision to bring it up to date.</span><div class="acts"><button class="btn sm" type="button" data-sync-nextrev>Start next revision</button></div><div class="sync-diff">${list}</div></div>`
        : `<div class="sync-msg warn"><span><b>Differs from ${job}.</b> These were typed on the proof, so they weren't replaced.</span><div class="acts"><button class="btn sm" type="button" data-sync-use="all">Use all job values</button></div><div class="sync-diff">${list}</div></div>`);
    }
    bar.innerHTML = msgs.join("");
    bar.hidden = !msgs.length;
  }
  async function useJobValues(field) {
    if (!state.jobPath) return;
    if (state.dirty && !(await saveJob())) return;
    try {
      const r = await api("/api/jm/use-job-values", { path: state.jobPath, rev: state.jobRev, fields: field === "all" ? null : [field] });
      loadJobResponse(r); toast("Took the job's values.", "success");
    } catch (error) {
      const handled = await resolveConflict(error, () => useJobValues(field));
      if (handled === null) toast(error.message, "error");
    }
  }
  async function startNextRevisionFromSync() {
    await nextRevision();
    await saveJob();
  }
  /* The server keeps proofs in step with their jobs. Check every 15 s whether the open proof
     changed there; reload it when nothing is being typed, otherwise say so. */
  async function pollOpenProof() {
    if (document.hidden || !state.jobPath || !$("#page-proof").classList.contains("active")) return;
    try {
      const r = await api("/api/job/rev", { path: state.jobPath });
      if (!r.exists) { renderServerChange("This proof was moved, archived or deleted. Open it again from the Queue."); return; }
      if (!r.rev || r.rev === state.jobRev) {
        if (JSON.stringify(r.sync?.differs || []) !== JSON.stringify(state.sync?.differs || [])) renderSync(r.sync);
        return;
      }
      const what = (r.last_sync?._changes || []).join(", ");
      if (!state.dirty) {
        await openQueueJob(state.jobPath);
        const when = safe(r.last_sync?._at).slice(11, 16);
        renderSync(state.sync, what ? `<b>Updated from ${escapeHtml(r.last_sync._job || "the job")}${when ? " at " + escapeHtml(when) : ""}:</b> ${escapeHtml(what)}. Regenerate the preview and the PDF before sending.`
                                    : "<b>Reloaded:</b> someone else saved this proof.");
      } else {
        renderServerChange(what ? `${r.last_sync._job || "The job"} changed this proof (${what}) while you were editing.` : "Someone else saved this proof while you were editing.");
      }
    } catch (_) {}
  }
  function renderServerChange(text) {
    const bar = $("#syncBar");
    if (bar.querySelector("[data-server-change]")) return;
    bar.insertAdjacentHTML("afterbegin", `<div class="sync-msg warn" data-server-change><span>${escapeHtml(text)}</span><div class="acts"><button class="btn sm" type="button" data-sync-reload>Reload (drops your changes)</button></div></div>`);
    bar.hidden = false;
  }

  function bindEvents() {
    installVariableDataDefinitionUi();
    $$(".nav-item").forEach(btn => btn.addEventListener("click", () => showPage(btn.dataset.page)));
    $("#homeNewBtn").onclick = resetJob; $("#homeOpenBtn").onclick = openJob; $("#homeQueueBtn").onclick = () => showPage("queue"); $("#homeRefreshBtn").onclick = () => { refreshHome(); loadJmJobs(); };
    $("#homeQueueCard").onclick=()=>showPage("queue"); $("#homeReadyCard").onclick=()=>{showPage("queue"); $("#queueFilter").value="ready"; renderQueue();}; $("#homeAttentionCard").onclick=()=>{showPage("queue"); $("#queueFilter").value="needs_attention"; renderQueue();};
    $("#newBtn").onclick = () => { if(!state.dirty || confirm("Discard unsaved changes and start a new proof?")) resetJob(); };
    $("#openBtn").onclick = openJob; $("#saveBtn").onclick = () => saveJob(); $("#browseTemplateBtn").onclick = browseTemplate; $("#previewBtn").onclick = bartenderPreview; $("#manualPreviewBtn").onclick = manualPreview;
    $("#generateBtn").onclick = () => generate("pdf"); $("#sideGenerateBtn").onclick = () => generate("pdf"); $("#packageBtn").onclick = () => generate("package"); $("#sidePackageBtn").onclick = () => generate("package");
    $("#nextRevisionBtn").onclick = nextRevision; $("#openOutputBtn").onclick = openOutputFolder; $("#openLatestBtn").onclick = openLatestOutput;
    $("#previewFileInput").onchange = e => uploadPreview(e.target.files[0]);
    $("#headerNewBtn").onclick = () => { if (!state.dirty || confirm("Discard unsaved changes and start a new proof?")) resetJob(); };
    $("#jmJobSearch").oninput = (() => { let t; return () => { clearTimeout(t); t = setTimeout(loadJmJobs, 220); }; })();
    $("#jmJobSeg").onclick = e => {
      const b = e.target.closest("button[data-show]"); if (!b) return;
      jmList.show = b.dataset.show;
      $$("#jmJobSeg button").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
      loadJmJobs();
    };
    $("#jmJobsBody").onclick = e => {
      const s = e.target.closest("[data-jm-start]"); if (s) return openFromJm(s.dataset.jmStart);
      const o = e.target.closest("[data-jm-open]"); if (o) return openFromJm(o.dataset.jmOpen);
    };
    $("#syncBar").onclick = e => {
      const u = e.target.closest("[data-sync-use]"); if (u) return useJobValues(u.dataset.syncUse);
      if (e.target.closest("[data-sync-nextrev]")) return startNextRevisionFromSync();
      if (e.target.closest("[data-sync-reload]")) { setDirty(false); return openQueueJob(state.jobPath); }
      if (e.target.closest("[data-sync-dismiss]")) { state.syncNote = null; renderSync(state.sync); }
    };
    setInterval(pollOpenProof, 15000);
    setInterval(() => { if (!document.hidden && $("#page-home").classList.contains("active")) loadJmJobs(); }, 30000);
    $("#templateSearch").oninput = searchTemplatesSoon; $("#linkSearch").oninput = searchLinkJobsSoon;
    $("#unlinkBtn").onclick = () => { if (confirm("Unlink this proof from its Job Manager job?")) linkToJob(0); };
    document.addEventListener("click", e => {
      if (e.target.closest("[data-close-modal]") || e.target.classList.contains("modal")) return closeModals();
      const t = e.target.closest("[data-template-path]"); if (t) return chooseTemplate(t.dataset.templatePath);
      const l = e.target.closest("[data-link-job]"); if (l) return linkToJob(+l.dataset.linkJob);
      if (e.target.closest("[data-jm-link]")) return openLinkDialog();
    });
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeModals(); });
    $("#computeEpcBtn").onclick = computeEpc; $("#useGtinBtn").onclick = () => { state.job.barcode_data_1 = state.job.gtin; syncFormFromJob(); setDirty(); };
    $("#recordApprovalBtn").onclick = recordApproval; $("#reopenBtn").onclick = reopenApproval;
    $("#queueRefreshBtn").onclick = refreshQueue; $("#queueNewBtn").onclick = resetJob; $("#queueSearch").oninput = renderQueue; $("#queueFilter").onchange = renderQueue;
    $("#page-queue").onclick = e => {
      const open=e.target.closest("[data-open-path]"); if(open) return openQueueJob(open.dataset.openPath);
      const archive=e.target.closest("[data-archive-path]"); if(archive) return archiveQueueJob(archive.dataset.archivePath);
      const restore=e.target.closest("[data-restore-path]"); if(restore) return restoreQueueJob(restore.dataset.restorePath);
      const del=e.target.closest("[data-delete-path]"); if(del) return deleteQueueJob(del.dataset.deletePath);
    };
    $("#homeRecent").onclick = e => { const b=e.target.closest("[data-open-path]"); if(b) openQueueJob(b.dataset.openPath); };
    $("#saveSettingsBtn").onclick=saveSettings; $("#diagnoseBartenderBtn").onclick=diagnoseBartender; $("#syncInlaysBtn").onclick=syncInlays; $("#healthBtn").onclick=installationHealth; $("#backupBtn").onclick=backup; $("#relinkBtn").onclick=relinkProofs;
    $("#openDataBtn").onclick=async()=>{ if(!state.dataRoot) return; await copyText(state.dataRoot); toast(`Copied: ${state.dataRoot}`,"success"); };
    $("#masterCompany").onchange = e => { state.job.preparing_company=e.target.value; setDirty(); scheduleValidation(); };
    $("#inlaySelect").onchange = e => { applyInlay(e.target.value); syncFormFromJob(); setDirty(); scheduleValidation(); };
    $("#encodingType").onchange = () => { syncJobFromForm(); renderEncodingMode(); setDirty(); scheduleValidation(); };
    $$('[data-job]').forEach(el => {
      const event = el.tagName === "SELECT" || el.type === "range" ? "change" : "input";
      el.addEventListener(event, () => { syncJobFromForm(); renderContext(); if(el.dataset.job==="encoding_type") renderEncodingMode(); updateVariableDataDefinitionStatus(); setDirty(); scheduleValidation(); });
    });
    document.addEventListener("keydown", e => {
      const mod=e.ctrlKey||e.metaKey, k=e.key.toLowerCase();
      if(mod&&k==="s"){e.preventDefault();saveJob();}   // (Ctrl+O shows the Queue)
      if(mod&&k==="o"){e.preventDefault();openJob();}
      if(mod&&k==="n"){e.preventDefault();resetJob();}
      if(mod&&e.key==="Enter"){e.preventDefault();generate("pdf");}
    });
    window.addEventListener("beforeunload", e => { if(state.dirty){e.preventDefault(); e.returnValue="";} });
    // JM: no heartbeat; the shared server never shuts down when a page closes.
  }

  async function bootstrap() {
    state.job = defaultJob();
    for(let i=6;i<=12;i++) $("#prefixDigits").add(new Option(i,i));
    for(let i=0;i<=7;i++) $("#filterValue").add(new Option(i,i));
    try {
      const response = await fetch(API_BASE + "/api/bootstrap", {cache:"no-store", headers:{"X-Operator": jmOperator()}}); const r=await response.json();
      if (!response.ok || r.ok === false) throw new Error(r.error || `Startup failed (${response.status})`);
      state.dataRoot = r.data_root || "";
      state.settings=r.settings||{}; state.inlays=r.inlays||[]; state.recentJobs=r.recent_jobs||[]; state.recentJobDetails=r.recent_job_details||[]; state.encodingTypes=r.encoding_types||state.encodingTypes;
      state.job.preparing_company=state.settings.master_company||"MCC Label";
      state.job.operator_initials=(safe(state.settings.default_operator_initials)||initialsOf(jmOperator())).toUpperCase().slice(0,8);
      if(state.settings.default_inlay) applyInlay(state.settings.default_inlay);
      $("#versionText").textContent=r.version||"V 1.0.0"; $("#releaseText").textContent=(safe(r.release).match(/Beta\s*[0-9.]+/i)||["BETA"])[0].toUpperCase();
      const detected=Boolean(r.bartender?.detected); $("#homeBarTender").textContent=detected?"Ready":"Not detected"; $("#homeBarTenderDetail").textContent=detected?(r.bartender.executable||"Preview engine available"):"Configure in Settings";
      renderInlays(); syncFormFromJob(); renderSettings(); await refreshHome(); loadJmJobs();
      await openFromAddress();
    } catch(error) { state.connected=false; renderConnection(); toast(`Startup failed: ${error.message}`,"error"); }
  }

  /* JM: /proofing/?job=<id> opens (or starts) the job's proof; ?proof=<key> opens one proof. */
  async function openFromAddress() {
    const q = new URLSearchParams(location.search);
    const jobId = q.get("job"), key = q.get("proof"), wantNew = q.get("new") === "1";
    if (!jobId && !key) return;
    history.replaceState(null, "", location.pathname);
    try {
      let r;
      if (jobId) r = await api(`/api/jm/jobs/${encodeURIComponent(jobId)}/open`, { new: wantNew });
      else { const res = await fetch(`${API_BASE}/api/jm/proof/${encodeURIComponent(key)}`, { cache: "no-store" }); r = await res.json(); if (!res.ok || r.ok === false) throw new Error(r.error || "Proof not found"); }
      loadJobResponse(r);
      if (r.created) toast(`New proof started from ${r.jm_job?.job_number || "the job"}. Check every field before generating.`, "success");
    } catch (error) { toast(error.message, "error"); }
  }

  bindEvents(); bootstrap();
})();

/* ============================================================
   BETA21.5_AUTO_PREVIEW_CONTROLLER
   Performance-safe automatic BarTender preview starter.
   - No global MutationObserver
   - Debounced template detection
   - Programmatic-template detection via low-frequency visible-tab scan
   - Duplicate generation suppression
   - Reuses the app's existing Preview button / async preview workflow
   ============================================================ */
(() => {
    "use strict";

    if (window.__rfidBeta215AutoPreviewInstalled) {
        return;
    }
    window.__rfidBeta215AutoPreviewInstalled = true;

    const AUTO_PREVIEW = {
        debounceMs: 500,
        scanMs: 900,
        retryMs: 1200,
        minRetriggerMs: 1500,
        timer: null,
        scanTimer: null,
        lastObservedTemplate: "",
        lastTriggeredTemplate: "",
        pendingTemplate: "",
        triggerStartedAt: 0,
        busy: false,
        statusNode: null,
    };

    function text(value) {
        return String(value == null ? "" : value).trim();
    }

    function lowered(value) {
        return text(value).toLowerCase();
    }

    function associatedText(el) {
        if (!el) return "";
        const parts = [
            el.id,
            el.name,
            el.getAttribute && el.getAttribute("aria-label"),
            el.getAttribute && el.getAttribute("placeholder"),
            el.getAttribute && el.getAttribute("title"),
        ];

        if (el.labels && el.labels.length) {
            for (const label of el.labels) {
                parts.push(label.innerText || label.textContent || "");
            }
        }

        const parent = el.closest && el.closest("label, .field, .form-field, .control, .input-group");
        if (parent) {
            parts.push(parent.innerText || parent.textContent || "");
        }

        return lowered(parts.filter(Boolean).join(" "));
    }

    function templateValue(el) {
        if (!el) return "";

        if (el.tagName === "SELECT") {
            const option = el.options && el.selectedIndex >= 0
                ? el.options[el.selectedIndex]
                : null;
            return text(el.value || (option && option.textContent));
        }

        if (el.type === "file") {
            const file = el.files && el.files.length ? el.files[0] : null;
            return text((file && file.name) || el.value);
        }

        return text(el.value);
    }

    function templateScore(el) {
        const meta = associatedText(el);
        let score = 0;

        if (meta.includes("bartender")) score += 8;
        if (meta.includes("bar tender")) score += 8;
        if (meta.includes("template")) score += 6;
        if (meta.includes(".btw") || meta.includes("btw")) score += 4;

        const idName = lowered(`${el.id || ""} ${el.name || ""}`);
        if (idName.includes("template")) score += 8;
        if (idName.includes("bartender")) score += 10;
        if (idName.includes("btw")) score += 5;

        const value = lowered(templateValue(el));
        if (value.endsWith(".btw") || value.includes(".btw")) score += 12;

        return score;
    }

    function findTemplateControl() {
        const selectors = [
            "select",
            "input[type='text']",
            "input[type='search']",
            "input[type='file']",
            "input:not([type])",
        ];

        const candidates = [];
        for (const el of document.querySelectorAll(selectors.join(","))) {
            if (el.disabled) continue;
            const value = templateValue(el);
            const score = templateScore(el);
            if (score >= 8 && value) {
                candidates.push({ el, score, value });
            }
        }

        candidates.sort((a, b) => b.score - a.score);
        return candidates.length ? candidates[0] : null;
    }

    function findTemplateTextFallback() {
        const selectors = [
            "[id*='template' i]",
            "[class*='template' i]",
            "[data-template]",
        ];

        const candidates = [];
        for (const el of document.querySelectorAll(selectors.join(","))) {
            if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(el.tagName)) continue;
            const meta = lowered(`${el.id || ""} ${el.className || ""} ${el.textContent || ""}`);
            if (!meta.includes("template")) continue;

            const content = text(el.textContent);
            const btwMatch = content.match(/[^\s<>"]+\.btw\b/i);
            if (btwMatch) {
                candidates.push({ el, score: 20, value: btwMatch[0] });
            }
        }

        return candidates.length ? candidates[0] : null;
    }

    function currentTemplate() {
        // Current Proofing Utility has a dedicated job field for the .BTW path.
        // Prefer it so automatic preview follows the exact template held by state.job.
        const exact = document.querySelector('[data-job="template_path"]');
        if (exact && !exact.disabled) {
            const value = templateValue(exact);
            if (value) return { el: exact, score: 100, value };
        }

        return findTemplateControl() || findTemplateTextFallback();
    }

    function previewButtonScore(button) {
        const meta = lowered([
            button.id,
            button.name,
            button.getAttribute && button.getAttribute("aria-label"),
            button.getAttribute && button.getAttribute("title"),
            button.innerText,
            button.textContent,
        ].filter(Boolean).join(" "));

        if (
            meta.includes("rfid construction") ||
            meta.includes("construction preview") ||
            meta.includes("final pdf") ||
            meta.includes("proof package") ||
            meta.includes("final output")
        ) {
            return -100;
        }

        let score = 0;
        if (meta.includes("bartender")) score += 12;
        if (meta.includes("bar tender")) score += 12;
        if (meta.includes("preview")) score += 8;
        if (meta.includes("label")) score += 2;
        if (meta.includes("artwork")) score += 2;
        if (meta.includes("generate")) score += 1;

        const idName = lowered(`${button.id || ""} ${button.name || ""}`);
        if (idName.includes("preview")) score += 8;
        if (idName.includes("bartender")) score += 10;

        return score;
    }

    function findPreviewButton() {
        // Use the known BarTender preview control first. This prevents the
        // automatic controller from ever selecting Manual Preview / image browse.
        const exact = document.querySelector("#previewBtn");
        if (exact) return exact;

        const candidates = [];
        for (const button of document.querySelectorAll("button, input[type='button'], input[type='submit']")) {
            if (button.id === "manualPreviewBtn") continue;
            const score = previewButtonScore(button);
            if (score >= 10) {
                candidates.push({ button, score });
            }
        }

        candidates.sort((a, b) => b.score - a.score);
        return candidates.length ? candidates[0].button : null;
    }

    function ensureStatusNode(button) {
        if (AUTO_PREVIEW.statusNode && AUTO_PREVIEW.statusNode.isConnected) {
            return AUTO_PREVIEW.statusNode;
        }

        if (!button || !button.parentElement) return null;

        const existing = button.parentElement.querySelector("[data-beta215-auto-preview-status]");
        if (existing) {
            AUTO_PREVIEW.statusNode = existing;
            return existing;
        }

        const node = document.createElement("span");
        node.setAttribute("data-beta215-auto-preview-status", "1");
        node.style.cssText = [
            "display:inline-block",
            "margin-left:8px",
            "font-size:11px",
            "opacity:.72",
            "vertical-align:middle",
            "white-space:nowrap",
        ].join(";");

        button.insertAdjacentElement("afterend", node);
        AUTO_PREVIEW.statusNode = node;
        return node;
    }

    function setStatus(message) {
        const button = findPreviewButton();
        const node = ensureStatusNode(button);
        if (node) node.textContent = message;
    }

    function normalizedTemplate(value) {
        return lowered(value).replace(/\\/g, "/");
    }

    function validTemplate(value) {
        const v = text(value);
        if (!v) return false;

        // Prefer a real .BTW name/path. A non-.BTW value is still allowed if
        // it came from a strongly identified template selector.
        return true;
    }

    function releaseBusySoon() {
        window.setTimeout(() => {
            AUTO_PREVIEW.busy = false;

            if (AUTO_PREVIEW.pendingTemplate) {
                const pending = AUTO_PREVIEW.pendingTemplate;
                AUTO_PREVIEW.pendingTemplate = "";
                schedulePreview(pending, "pending");
            }
        }, AUTO_PREVIEW.minRetriggerMs);
    }

    function triggerPreview(template, reason) {
        if (document.visibilityState === "hidden") {
            AUTO_PREVIEW.pendingTemplate = template;
            return;
        }

        const button = findPreviewButton();
        if (!button) {
            setStatus("Auto preview waiting for preview control");
            AUTO_PREVIEW.pendingTemplate = template;
            return;
        }

        if (button.disabled || button.getAttribute("aria-disabled") === "true") {
            setStatus("Auto preview queued");
            AUTO_PREVIEW.pendingTemplate = template;
            window.setTimeout(() => {
                if (AUTO_PREVIEW.pendingTemplate) scanForTemplate("retry");
            }, AUTO_PREVIEW.retryMs);
            return;
        }

        const normalized = normalizedTemplate(template);
        if (!normalized || normalized === AUTO_PREVIEW.lastTriggeredTemplate) {
            return;
        }

        if (AUTO_PREVIEW.busy) {
            AUTO_PREVIEW.pendingTemplate = template;
            return;
        }

        AUTO_PREVIEW.busy = true;
        AUTO_PREVIEW.triggerStartedAt = Date.now();
        AUTO_PREVIEW.lastTriggeredTemplate = normalized;
        AUTO_PREVIEW.pendingTemplate = "";

        setStatus("Generating preview…");

        try {
            button.click();
            window.setTimeout(() => {
                setStatus("Auto preview running…");
            }, 350);
        } catch (error) {
            console.warn("[Beta 21.5 Auto Preview] Could not start preview:", error);
            AUTO_PREVIEW.lastTriggeredTemplate = "";
            setStatus("Auto preview could not start");
        } finally {
            releaseBusySoon();
        }
    }

    function schedulePreview(template, reason) {
        if (!validTemplate(template)) return;

        if (AUTO_PREVIEW.timer) {
            window.clearTimeout(AUTO_PREVIEW.timer);
        }

        AUTO_PREVIEW.timer = window.setTimeout(() => {
            AUTO_PREVIEW.timer = null;
            triggerPreview(template, reason);
        }, AUTO_PREVIEW.debounceMs);
    }

    function scanForTemplate(reason) {
        if (document.visibilityState === "hidden") return;

        const candidate = currentTemplate();
        if (!candidate || !validTemplate(candidate.value)) {
            AUTO_PREVIEW.lastObservedTemplate = "";
            return;
        }

        const normalized = normalizedTemplate(candidate.value);
        if (!normalized) return;

        if (normalized !== AUTO_PREVIEW.lastObservedTemplate) {
            AUTO_PREVIEW.lastObservedTemplate = normalized;

            if (normalized !== AUTO_PREVIEW.lastTriggeredTemplate) {
                schedulePreview(candidate.value, reason || "template-change");
            }
        }
    }

    function eventMightBeTemplate(event) {
        const el = event.target;
        if (!el || !["INPUT", "SELECT"].includes(el.tagName)) return false;
        return templateScore(el) >= 8;
    }

    document.addEventListener("change", (event) => {
        if (!eventMightBeTemplate(event)) return;

        const value = templateValue(event.target);
        AUTO_PREVIEW.lastObservedTemplate = normalizedTemplate(value);

        if (normalizedTemplate(value) !== AUTO_PREVIEW.lastTriggeredTemplate) {
            schedulePreview(value, "change");
        }
    }, true);

    document.addEventListener("input", (event) => {
        if (!eventMightBeTemplate(event)) return;

        const value = templateValue(event.target);
        if (!value) return;

        AUTO_PREVIEW.lastObservedTemplate = normalizedTemplate(value);

        if (normalizedTemplate(value) !== AUTO_PREVIEW.lastTriggeredTemplate) {
            schedulePreview(value, "input");
        }
    }, true);

    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
            window.setTimeout(() => scanForTemplate("visible"), 150);
        }
    });

    function startScanner() {
        if (AUTO_PREVIEW.scanTimer) return;

        AUTO_PREVIEW.scanTimer = window.setInterval(() => {
            if (document.visibilityState !== "hidden") {
                scanForTemplate("background-scan");
            }
        }, AUTO_PREVIEW.scanMs);
    }

    function initialize() {
        startScanner();
        window.setTimeout(() => scanForTemplate("startup"), 450);
        window.setTimeout(() => scanForTemplate("startup-followup"), 1400);
        console.info("[Beta 21.5 Auto Preview] Ready.");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialize, { once: true });
    } else {
        initialize();
    }
})();
