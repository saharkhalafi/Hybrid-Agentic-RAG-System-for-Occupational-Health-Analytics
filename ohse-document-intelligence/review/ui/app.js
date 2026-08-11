const API = "/review";
let currentTaskId = null;
let currentTask = null;
let currentEvidence = null;

function $(id) { return document.getElementById(id); }

function reviewerId() {
  return $("reviewerId").value.trim() || "sahar khalafi";
}

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function priorityClass(p) {
  return p === "critical" ? "priority-critical" : p === "high" ? "priority-high" : "";
}

async function loadQueue() {
  const params = new URLSearchParams();
  const status = $("filterStatus").value;
  const priority = $("filterPriority").value;
  const page = $("filterPage").value;
  if (status) params.set("status", status);
  if (priority) params.set("priority", priority);
  if (page) params.set("page_number", page);
  const data = await api(`/tasks?${params}`);
  const tbody = $("queueBody");
  tbody.innerHTML = "";
  for (const t of data.tasks) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="${priorityClass(t.priority)}">${t.priority.toUpperCase()}</td>
      <td>${t.page_number ?? "—"}</td>
      <td>${t.target_type} / ${t.target_id}</td>
      <td>${t.primary_issue_code}</td>
      <td>${t.status}</td>
      <td>${t.assigned_to ?? "—"}</td>
      <td>${t.created_at ? new Date(t.created_at).toLocaleString() : "—"}</td>
      <td><button data-id="${t.id}">Open</button></td>`;
    tr.querySelector("button").onclick = () => openTask(t.id);
    tbody.appendChild(tr);
  }
}

async function openTask(taskId) {
  currentTaskId = taskId;
  $("queueView").classList.add("hidden");
  $("detailView").classList.remove("hidden");
  $("correctPanel").classList.add("hidden");
  $("resultBanner").classList.add("hidden");

  try {
    await api(`/tasks/${taskId}/claim`, { method: "POST", body: JSON.stringify({ reviewer_id: reviewerId() }) });
  } catch (e) {
    console.warn("Claim:", e.message);
  }

  currentTask = await api(`/tasks/${taskId}`);
  currentEvidence = await api(`/tasks/${taskId}/evidence`);
  renderDetail();
  loadHistory();
}

function renderDetail() {
  const t = currentTask;
  $("detailHeader").innerHTML = `
    <h2>${t.title}</h2>
    <p>Priority: <strong>${t.priority}</strong> | Issue: <strong>${t.primary_issue_code}</strong> | Status: <strong>${t.status}</strong></p>
    <p class="lease">${t.assigned_to ? `Claimed by ${t.assigned_to}` : ""} ${t.claim_expires_at ? `| Lease expires: ${new Date(t.claim_expires_at).toLocaleTimeString()}` : ""}</p>
    <p style="white-space: pre-wrap;">${t.description || ""}</p>`;

  const img = $("pageImage");
  if (currentEvidence.page_image_url) {
    img.src = currentEvidence.page_image_url + "?t=" + Date.now();
    img.classList.remove("hidden");
  } else {
    img.classList.add("hidden");
  }
  $("evidenceMeta").textContent = currentEvidence.pdf_available
    ? `PDF page ${currentEvidence.page_number} — evidence cells: ${currentEvidence.evidence_cells.length}`
    : "PDF not available — configure pdf_source_path";

  renderCandidateTable(currentTask.candidate || currentEvidence.candidate_payload);
  renderIssues(currentTask.machine_issues || []);
  buildCorrectionForms(currentTask.candidate || currentEvidence.candidate_payload);

  const hasCandidate = !!currentTask.candidate_id;
  $("correctBtn").disabled = !hasCandidate;
  $("correctBtn").title = hasCandidate ? "" : "No linked candidate — nothing to correct. Use Approve or Reject.";
}

function renderCandidateTable(payload) {
  const el = $("candidateTable");
  const hasCandidate = !!currentTask.candidate_id;
  if (!payload) {
    el.innerHTML = hasCandidate
      ? "No candidate data"
      : "<p><em>This is an observational review item (no versioned candidate — e.g. an unlinked entity or text-chunk note).</em></p>" +
        "<p>Use <strong>APPROVE</strong> to confirm and resolve it, or <strong>REJECT</strong> with a reason. " +
        "There is nothing here to CORRECT.</p>";
    return;
  }
  if (!payload.rows && (payload.semantics || payload.reconstruction)) {
    renderFormulaCandidate(payload);
    return;
  }
  if (!payload.rows) {
    el.innerHTML = "No candidate data";
    return;
  }
  const mapping = payload.header_mapping || {};
  $("candidateMeta").innerHTML = `<p>Mapping confidence: ${payload.mapping_confidence ?? "—"} | Status: ${payload.mapping_status ?? "—"}</p>
    <p><strong>Header mapping:</strong> ${JSON.stringify(mapping)}</p>`;

  const fields = new Set();
  payload.rows.forEach(r => Object.keys(r).forEach(k => fields.add(k)));
  const cols = [...fields];

  let html = "<table class='candidate-table'><thead><tr>";
  cols.forEach(c => { html += `<th>${c}</th>`; });
  html += "</tr></thead><tbody>";
  payload.rows.forEach(row => {
    html += "<tr>";
    cols.forEach(c => {
      const cell = row[c];
      if (cell && typeof cell === "object") {
        html += `<td><div>${cell.value ?? ""}</div><div class="orig">orig: ${cell.original_value ?? ""}</div></td>`;
      } else {
        html += `<td>${cell ?? ""}</td>`;
      }
    });
    html += "</tr>";
  });
  html += "</tbody></table>";
  el.innerHTML = html;
}

function renderFormulaCandidate(payload) {
  const el = $("candidateTable");
  const expr = (payload.reconstruction && payload.reconstruction.expression) || payload.normalized_expression || "—";
  const vars = (payload.semantics && payload.semantics.variables) || {};
  const units = (payload.semantics && payload.semantics.units) || {};
  const refs = (payload.semantics && payload.semantics.reference_values) || [];

  $("candidateMeta").innerHTML = `<p><strong>Formula:</strong> ${payload.formula_id || ""} | Status: ${payload.status ?? "—"}</p>`;

  let html = `<p><strong>Expression:</strong> ${expr}</p>`;
  html += "<p><strong>Variables</strong></p><table class='candidate-table'><thead><tr><th>Symbol</th><th>Role</th><th>Description</th><th>Unit</th></tr></thead><tbody>";
  Object.entries(vars).forEach(([sym, info]) => {
    html += `<tr><td>${sym}</td><td>${info.role || "—"}</td><td>${info.description || "—"}</td><td>${units[sym] ?? "—"}</td></tr>`;
  });
  html += "</tbody></table>";

  if (refs.length) {
    html += "<p><strong>Reference values</strong></p><table class='candidate-table'><thead><tr><th>Variable</th><th>Value</th><th>Unit</th><th>Source</th></tr></thead><tbody>";
    refs.forEach(r => {
      html += `<tr><td>${r.variable ?? "—"}</td><td>${r.reference_value ?? "—"}</td><td>${r.unit ?? "—"}</td><td>${r.source ?? "—"}</td></tr>`;
    });
    html += "</tbody></table>";
  }
  el.innerHTML = html;
}

function renderIssues(issues) {
  const ul = $("issuesList");
  ul.innerHTML = "";
  if (!issues.length && currentTask.issue_metadata?.issues) {
    currentTask.issue_metadata.issues.forEach(i => {
      const li = document.createElement("li");
      li.textContent = i.human_message || `[${i.severity || "medium"}] ${i.message || i.type}`;
      ul.appendChild(li);
    });
    return;
  }
  issues.forEach(i => {
    const li = document.createElement("li");
    li.textContent = i.human_message || `[${i.severity}] ${i.issue_code}: ${i.message}`;
    ul.appendChild(li);
  });
}

function buildCorrectionForms(payload) {
  const container = $("correctionForms");
  container.innerHTML = "";
  if (!payload) return;

  function addCorrectionInput({ label, field, current, evidence, type = "field_value", allowRemove = false, choices = null }) {
    const row = document.createElement("div");
    row.className = "corr-row";
    row.dataset.field = field;
    row.dataset.original = current ?? "";
    row.dataset.type = type;

    const labelEl = document.createElement("label");
    const title = document.createElement("strong");
    title.textContent = label;
    const currentEl = document.createElement("span");
    currentEl.className = "corr-current";
    currentEl.textContent = `Current candidate: ${current ?? "—"}`;
    const evidenceEl = document.createElement("span");
    evidenceEl.className = "corr-evidence";
    evidenceEl.textContent = `Original evidence (immutable): ${evidence ?? "—"}`;
    labelEl.append(title, currentEl, evidenceEl);

    const input = document.createElement("input");
    input.type = "text";
    input.value = current ?? "";
    input.dataset.corrected = "";
    input.setAttribute("aria-label", `Corrected value for ${label}`);
    if (choices) {
      const listId = `choices-${field.replaceAll(".", "-")}`;
      input.setAttribute("list", listId);
      const list = document.createElement("datalist");
      list.id = listId;
      choices.forEach(choice => {
        const option = document.createElement("option");
        option.value = choice;
        list.appendChild(option);
      });
      row.appendChild(list);
    }

    row.append(labelEl, input);
    if (allowRemove) {
      const removeLabel = document.createElement("label");
      removeLabel.className = "corr-remove";
      const remove = document.createElement("input");
      remove.type = "checkbox";
      remove.dataset.remove = "";
      remove.onchange = () => { input.disabled = remove.checked; };
      removeLabel.append(remove, document.createTextNode(" Remove this mapping (no real source column)"));
      row.appendChild(removeLabel);
    }
    container.appendChild(row);
  }

  if (payload.header_mapping) {
    const h3 = document.createElement("h3");
    h3.textContent = "Header mapping";
    container.appendChild(h3);
    Object.entries(payload.header_mapping).forEach(([col, field]) => {
      addCorrectionInput({
        label: `Column ${col}`,
        field: `header_mapping.${col}`,
        current: field,
        evidence: (payload.headers || [])[Number(col)] ?? "No source header",
        type: "header_mapping",
        allowRemove: true,
        choices: ["health_effect", "symbols", "STEL", "TWA", "ceiling", "molecular_weight", "chemical_name", "row_number"],
      });
    });

    if (
      payload.table_type === "chemical_oel"
      && payload.header_mapping["2"] === "TWA"
      && payload.header_mapping["3"] === "TWA"
    ) {
      const notice = document.createElement("div");
      notice.className = "source-column-preview";
      const sourceCells = (currentEvidence?.source_table_cells || [])
        .filter(cell => Number(cell.column) === 2 && Number(cell.row) > 0 && (cell.text || "").trim());
      notice.innerHTML = "<h3>Missing STEL/C column detected</h3>" +
        "<p>Columns 2 and 3 are both mapped to TWA. For this OEL layout, column 2 is <strong>STEL</strong> " +
        "and column 3 is <strong>TWA</strong>. Change <strong>Column 2</strong> above from TWA to STEL. " +
        "Saving will restore the STEL values below from immutable source cells.</p>";
      const list = document.createElement("ul");
      sourceCells.forEach(cell => {
        const item = document.createElement("li");
        item.textContent = `Source row ${cell.row}: ${cell.text}`;
        list.appendChild(item);
      });
      notice.appendChild(list);
      container.appendChild(notice);
    }
  }

  if (payload.rows) {
    const h3 = document.createElement("h3");
    h3.textContent = "Table cell values";
    container.appendChild(h3);

    payload.rows.forEach((tableRow, rowIdx) => {
      const chemical = tableRow.chemical_name?.value;
      const rowNumber = tableRow.row_number?.value;
      const h4 = document.createElement("h4");
      h4.className = "corr-row-heading";
      h4.textContent = `Row ${rowNumber ?? rowIdx + 1}${chemical ? ` — ${chemical}` : ""}`;
      container.appendChild(h4);

      Object.entries(tableRow).forEach(([fieldName, cell]) => {
        if (!cell || typeof cell !== "object" || !Object.prototype.hasOwnProperty.call(cell, "value")) return;
        addCorrectionInput({
          label: fieldName,
          field: `rows.${rowIdx}.${fieldName}.value`,
          current: cell.value,
          evidence: cell.original_value,
        });
      });
    });
  }

  if (payload.semantics) {
    const refs = payload.semantics.reference_values || [];
    if (refs.length) {
      const h3 = document.createElement("h3");
      h3.textContent = "Reference values";
      container.appendChild(h3);
      refs.forEach((r, idx) => {
        addCorrectionInput({
          label: `${r.variable ?? "value"} reference value`,
          field: `semantics.reference_values.${idx}.reference_value`,
          current: r.reference_value,
          evidence: r.evidence_snippet,
        });
      });
    }

    const units = payload.semantics.units || {};
    const varNames = Object.keys(payload.semantics.variables || {});
    if (varNames.length) {
      const h3 = document.createElement("h3");
      h3.textContent = "Units";
      container.appendChild(h3);
      varNames.forEach(sym => {
        addCorrectionInput({
          label: `Unit for ${sym}`,
          field: `semantics.units.${sym}`,
          current: units[sym],
          evidence: "Check the PDF near the formula",
        });
      });
    }
  }
}

async function loadHistory() {
  const events = await api(`/tasks/${currentTaskId}/history`);
  const ul = $("historyList");
  ul.innerHTML = "";
  events.forEach(e => {
    const li = document.createElement("li");
    li.textContent = `${new Date(e.created_at).toLocaleString()} — ${e.event_type} by ${e.actor_id}`;
    ul.appendChild(li);
  });
}

function showResult(msg, ok) {
  const b = $("resultBanner");
  b.textContent = msg;
  b.className = "banner " + (ok ? "ok" : "fail");
  b.classList.remove("hidden");
}

$("refreshBtn").onclick = loadQueue;
$("backBtn").onclick = () => {
  $("detailView").classList.add("hidden");
  $("queueView").classList.remove("hidden");
  currentTaskId = null;
  loadQueue();
};

$("approveBtn").onclick = async () => {
  try {
    const r = await api(`/tasks/${currentTaskId}/approve`, {
      method: "POST",
      body: JSON.stringify({ reviewer_id: reviewerId() }),
    });
    showResult(`APPROVE → ${r.new_status} | passed=${r.passed} | candidate=${r.candidate_status}`, r.passed);
    currentTask = await api(`/tasks/${currentTaskId}`);
    renderDetail();
    loadHistory();
  } catch (e) { showResult(e.message, false); }
};

$("correctBtn").onclick = () => $("correctPanel").classList.toggle("hidden");

$("saveCorrectBtn").onclick = async () => {
  const corrections = [];
  document.querySelectorAll("#correctionForms .corr-row").forEach(row => {
    const input = row.querySelector("[data-corrected]");
    const corrected = input.value.trim();
    const original = row.dataset.original;
    const remove = row.querySelector("[data-remove]")?.checked;
    if (remove) {
      corrections.push({
        field_name: row.dataset.field,
        original_value: original,
        corrected_value: null,
        correction_type: "structure",
        reason: $("correctReason").value || undefined,
      });
    } else if (corrected && corrected !== original) {
      corrections.push({
        field_name: row.dataset.field,
        original_value: original,
        corrected_value: corrected,
        correction_type: row.dataset.type || "field_value",
        reason: $("correctReason").value || undefined,
      });
    }
  });
  if (!corrections.length) {
    showResult("No corrections entered", false);
    return;
  }
  try {
    const r = await api(`/tasks/${currentTaskId}/correct`, {
      method: "POST",
      body: JSON.stringify({ reviewer_id: reviewerId(), corrections }),
    });
    showResult(`CORRECT → v${r.candidate_id} ${r.new_status} | passed=${r.passed}`, r.passed);
    currentTask = await api(`/tasks/${currentTaskId}`);
    currentEvidence = await api(`/tasks/${currentTaskId}/evidence`);
    renderDetail();
    loadHistory();
  } catch (e) { showResult(e.message, false); }
};

$("rejectBtn").onclick = async () => {
  const reason = prompt("Rejection reason (required):");
  if (!reason || reason.length < 3) return;
  try {
    const r = await api(`/tasks/${currentTaskId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reviewer_id: reviewerId(), reason }),
    });
    showResult(`REJECTED — ${r.new_status}`, false);
    loadHistory();
  } catch (e) { showResult(e.message, false); }
};

loadQueue();
