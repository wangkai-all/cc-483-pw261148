/* Metric semantic layer SPA — vanilla JS, no build step. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = {
  user: "alice",
  metrics: [],
  sources: [],
  selected: null,
};

// -- http helpers ------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      "X-User": state.user,
      ...(opts.headers || {}),
    },
    ...opts,
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const msg = body.detail || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return body;
}

function toast(msg, kind = "info") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 4200);
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function chip(status) {
  return `<span class="chip ${esc(status)}">${esc(status)}</span>`;
}

// -- tabs --------------------------------------------------------------------

$$(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".panel").forEach((p) =>
      p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "cache") loadCacheAndAudit();
    if (btn.dataset.tab === "lineage" || btn.dataset.tab === "diff" ||
        btn.dataset.tab === "query" || btn.dataset.tab === "admin") {
      refreshMetricSelectors();
    }
  })
);

$("#user-select").addEventListener("change", (e) => {
  state.user = e.target.value;
  toast(`已切换用户：${state.user}`, "info");
});

// -- metric list & detail ----------------------------------------------------

async function loadMetrics() {
  state.metrics = await api("/api/metrics");
  const tbody = $("#metrics-table tbody");
  tbody.innerHTML = state.metrics.map((m) => `
    <tr data-code="${esc(m.code)}">
      <td>${esc(m.code)}</td><td>${esc(m.name)}</td>
      <td>v${m.version}</td><td>${chip(m.status)}</td>
      <td>${esc(m.agg_func)}</td><td>${esc(m.measure_field)}</td>
      <td>${esc(m.time_grain)}</td>
      <td><span class="chip ${m.additivity === "additive" ? "" : "non_additive"}">${esc(m.additivity)}</span></td>
      <td>${esc(m.updated_at)}</td>
    </tr>`).join("");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => showDetail(tr.dataset.code)));
}

async function showDetail(code) {
  state.selected = code;
  const full = await api(`/api/metrics/${code}`);
  const latest = full.versions[0];
  const versionChips = full.versions
    .map((v) => `<span class="chip ${esc(v.status)}">v${v.version} · ${esc(v.agg_func)} · ${esc(v.status)}</span>`)
    .join("");
  $("#metric-detail").classList.remove("hidden");
  $("#metric-detail").innerHTML = `
    <h3>${esc(full.name)} <span class="muted">(${esc(full.code)})</span></h3>
    <dl class="kv">
      <dt>负责人</dt><dd>${esc(full.owner)}</dd>
      <dt>版本链</dt><dd>${versionChips}</dd>
      <dt>最新版本定义</dt><dd><pre>${esc(JSON.stringify(latest, null, 2))}</pre></dd>
    </dl>`;
}

$("#btn-refresh-metrics").addEventListener("click", () =>
  loadMetrics().catch((e) => toast(e.message, "error")));

// -- new metric dialog -------------------------------------------------------

async function openDialog() {
  state.sources = await api("/api/sources");
  $("#source-select").innerHTML = state.sources
    .map((s) => `<option>${esc(s.name)}</option>`).join("");
  $("#metric-dialog").showModal();
}
$("#btn-new-metric").addEventListener("click", openDialog);

$("#metric-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  let filters;
  try {
    filters = JSON.parse(fd.get("filters") || "[]");
  } catch {
    return toast("过滤条件 JSON 格式错误", "error");
  }
  const body = {
    code: fd.get("code"), name: fd.get("name"), owner: fd.get("owner"),
    change_note: fd.get("change_note"),
    spec: {
      source_table: fd.get("source_table"),
      agg_func: fd.get("agg_func"),
      measure_field: fd.get("measure_field"),
      time_grain: fd.get("time_grain"),
      additivity: fd.get("additivity"),
      dimensions: fd.get("dimensions").split(",").map((s) => s.trim()).filter(Boolean),
      ratio_num_field: fd.get("ratio_num_field") || null,
      ratio_den_field: fd.get("ratio_den_field") || null,
      filters,
      description: fd.get("description"),
    },
  };
  try {
    const r = await api("/api/metrics", { method: "POST", body: JSON.stringify(body) });
    toast(`指标 ${r.code} 已登记并发布 v${r.version}`, "success");
    e.target.reset();
    await loadMetrics();
  } catch (err) {
    toast(err.status === 409 ? `口径冲突，发布阻断：${err.message}` : err.message, "error");
  }
});

// -- shared selectors --------------------------------------------------------

async function refreshMetricSelectors() {
  if (!state.metrics.length) state.metrics = await api("/api/metrics");
  const codes = state.metrics.map((m) => m.code);
  const opts = codes.map((c) => `<option>${esc(c)}</option>`).join("");
  ["#lineage-metric", "#diff-metric", "#query-metric", "#admin-metric"].forEach((sel) => {
    const el = $(sel);
    if (!el.options.length || [...el.options].map((o) => o.value).join() !== codes.join()) {
      el.innerHTML = opts;
    }
  });
  await syncVersionSelectors();
}

async function versionsOf(code) {
  return (await api(`/api/metrics/${code}`)).versions;
}

async function syncVersionSelectors() {
  const code = $("#admin-metric").value || state.metrics[0]?.code;
  if (!code) return;
  const versions = await versionsOf(code);
  const vOpts = versions
    .map((v) => `<option value="${v.version}">v${v.version} ${esc(v.status)}</option>`).join("");
  $("#lineage-version").innerHTML = vOpts;
  $("#diff-left").innerHTML = vOpts;
  $("#diff-right").innerHTML = vOpts;
  $("#query-version").innerHTML =
    `<option value="">最新发布版</option>` + vOpts;
  $("#admin-version").innerHTML = vOpts;
  if (versions.length >= 2) $("#diff-right").value = versions[0].version,
    $("#diff-left").value = versions[versions.length - 1].version;
}

["#lineage-metric", "#diff-metric", "#query-metric", "#admin-metric"].forEach((sel) =>
  $(sel).addEventListener("change", syncVersionSelectors));

// -- lineage -----------------------------------------------------------------

$("#btn-lineage").addEventListener("click", async () => {
  const code = $("#lineage-metric").value;
  const version = $("#lineage-version").value;
  try {
    const data = await api(`/api/metrics/${code}/lineage?version=${version}`);
    const rows = data.upstream.map((e) => `
      <tr><td>${esc(e.upstream_kind)}</td><td>${esc(e.upstream_ref)}</td>
      <td>${esc(e.field_name)}</td></tr>`).join("");
    $("#lineage-out").innerHTML = `
      <h3>${esc(code)} v${data.version} ${chip(data.status)}</h3>
      <table><thead><tr><th>上游类型</th><th>上游对象</th><th>来源字段</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  } catch (e) { toast(e.message, "error"); }
});

// -- diff --------------------------------------------------------------------

$("#btn-diff").addEventListener("click", async () => {
  const code = $("#diff-metric").value;
  const l = $("#diff-left").value, r = $("#diff-right").value;
  try {
    const d = await api(`/api/metrics/${code}/diff?left=${l}&right=${r}`);
    if (d.identical) {
      $("#diff-out").innerHTML = `<h3>v${l} → v${r}：无差异</h3>`;
      return;
    }
    $("#diff-out").innerHTML = `
      <h3>${esc(code)} v${d.left_version} → v${d.right_version}</h3>
      <table><thead><tr><th>定义字段</th><th>旧值</th><th>新值</th></tr></thead>
      <tbody>${d.changes.map((c) => `
        <tr><td>${esc(c.field)}</td>
        <td class="diff-from">${esc(JSON.stringify(c.from))}</td>
        <td class="diff-to">${esc(JSON.stringify(c.to))}</td></tr>`).join("")}</tbody></table>`;
  } catch (e) { toast(e.message, "error"); }
});

// -- query & export ----------------------------------------------------------

function queryBody() {
  return {
    version: $("#query-version").value ? Number($("#query-version").value) : null,
    time_grain: $("#query-grain").value,
    dimensions: $("#query-dims").value.split(",").map((s) => s.trim()).filter(Boolean),
    filters: [],
    date_from: $("#query-from").value || null,
    date_to: $("#query-to").value || null,
    use_cache: $("#query-cache").checked,
  };
}

async function runQuery() {
  const code = $("#query-metric").value;
  try {
    const r = await api(`/api/query/${code}`, {
      method: "POST", body: JSON.stringify(queryBody()),
    });
    renderResult(r);
  } catch (e) {
    toast(e.status === 403 ? `权限拒绝：${e.message}` : e.message, "error");
  }
}

function renderResult(r) {
  $("#query-meta").classList.remove("hidden");
  const cacheCls = r.cache === "HIT" ? "cache-hit" : "cache-miss";
  $("#query-meta").innerHTML = `
    <h3>${esc(r.metric)} v${r.version} · ${esc(r.time_grain)} 粒度
      <span class="${cacheCls}">缓存${esc(r.cache)}</span>
      <span class="chip ${r.additivity === "additive" ? "" : "non_additive"}">${esc(r.additivity)}</span>
    </h3>
    <p class="muted">${r.row_count} 行 · 所有粒度均由原始明细直接重算，未使用预聚合结果</p>
    <pre>${esc(r.sql)}</pre>`;
  const head = r.columns.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = r.rows.map((row) =>
    `<tr>${r.columns.map((c) => `<td>${esc(row[c])}</td>`).join("")}</tr>`).join("");
  $("#query-table thead").innerHTML = `<tr>${head}</tr>`;
  $("#query-table tbody").innerHTML = body;
}

$("#btn-query").addEventListener("click", runQuery);

async function doExport(fmt) {
  const code = $("#query-metric").value;
  const res = await fetch(`/api/export/${code}?fmt=${fmt}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-User": state.user },
    body: JSON.stringify(queryBody()),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    return toast(err.detail || "导出失败", "error");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${code}.${fmt}`;
  a.click();
  URL.revokeObjectURL(url);
  toast(`已导出 ${fmt.toUpperCase()}`, "success");
}
$("#btn-export-csv").addEventListener("click", () => doExport("csv"));
$("#btn-export-json").addEventListener("click", () => doExport("json"));

// -- cache & audit -----------------------------------------------------------

async function loadCacheAndAudit() {
  const cache = await api("/api/cache");
  $("#cache-table tbody").innerHTML = cache.map((c) => `
    <tr><td title="${esc(c.cache_key)}">${esc(c.cache_key.slice(0, 16))}…</td>
    <td>${esc(c.metric_code)}</td><td>v${c.version}</td>
    <td>${c.hit_count}</td><td>${esc(c.created_at)}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">暂无缓存</td></tr>`;

  const audit = await api("/api/audit");
  $("#audit-table tbody").innerHTML = audit.map((a) => `
    <tr><td>${esc(a.ts)}</td><td>${esc(a.actor)}</td>
    <td>${esc(a.action)}</td><td>${esc(a.entity)}</td>
    <td>${esc(a.detail)}</td></tr>`).join("");
}
$("#btn-cache-list").addEventListener("click", () =>
  loadCacheAndAudit().catch((e) => toast(e.message, "error")));
$("#btn-audit").addEventListener("click", () =>
  loadCacheAndAudit().catch((e) => toast(e.message, "error")));
$("#btn-cache-clear").addEventListener("click", async () => {
  const r = await api("/api/cache/invalidate", { method: "POST" });
  toast(`已清空 ${r.invalidated} 条缓存`, "success");
  loadCacheAndAudit();
});

// -- admin: rollback / publish / deprecate / conflict retry ------------------

$("#btn-rollback").addEventListener("click", async () => {
  const code = $("#admin-metric").value;
  const version = $("#admin-version").value;
  try {
    const r = await api(
      `/api/metrics/${code}/rollback?version=${version}`, { method: "POST" });
    $("#admin-out").innerHTML = `
      <h3>回滚已生成草稿</h3>
      <p>已将 v${r.rolled_back_from} 的完整定义快照克隆为 v${r.draft_version} 草稿。
      历史发布版本保留不变；草稿仍需通过冲突门禁后发布。</p>`;
    toast(`回滚草稿 v${r.draft_version} 已创建`, "success");
    await syncVersionSelectors();
  } catch (e) { toast(e.message, "error"); }
});

$("#btn-publish-draft").addEventListener("click", async () => {
  const code = $("#admin-metric").value;
  try {
    const r = await api(`/api/metrics/${code}/publish`, { method: "POST" });
    toast(`${code} v${r.version} 发布成功`, "success");
    await loadMetrics();
    await syncVersionSelectors();
  } catch (e) {
    toast(e.status === 409 ? `口径冲突，发布阻断：${e.message}` : e.message, "error");
  }
});

$("#btn-deprecate").addEventListener("click", async () => {
  const code = $("#admin-metric").value;
  if (!confirm(`确认下线指标 ${code}？已发布版本将标记 deprecated。`)) return;
  try {
    await api(`/api/metrics/${code}/deprecate`, { method: "POST" });
    toast(`${code} 已下线`, "success");
    loadMetrics();
  } catch (e) { toast(e.message, "error"); }
});

$("#btn-retry-conflict").addEventListener("click", async () => {
  try {
    const r = await api(
      "/api/metrics/revenue_avg_conflict/publish", { method: "POST" });
    toast(`意外发布成功：v${r.version}`, "info");
  } catch (e) {
    toast(`冲突门禁生效（HTTP ${e.status}）：${e.message}`, e.status === 409 ? "success" : "error");
    $("#admin-out").innerHTML = `
      <h3>发布阻断验证</h3>
      <p>指标 <b>revenue_avg_conflict</b> 试图在 revenue 同一数据范围上以 AVG 重新聚合
      sales_amount，冲突检测器返回 HTTP ${e.status}：</p>
      <pre>${esc(e.message)}</pre>`;
  }
});

// -- boot --------------------------------------------------------------------

loadMetrics()
  .then(refreshMetricSelectors)
  .catch((e) => toast(`初始化失败：${e.message}`, "error"));
