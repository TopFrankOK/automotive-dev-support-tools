"use strict";

/* 最小可用的一层：tab 切换 + 各面板的数据装载。
   没有框架、没有构建 —— 改完刷新就能看。 */

const $ = (sel) => document.querySelector(sel);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`服务返回 ${res.status}：${body.slice(0, 300)}`);
  }
  return res.json();
}

function setStatus(text, kind) {
  const el = $("#status");
  el.textContent = text;
  el.className = `pill pill-${kind || "warn"}`;
}

function initTabs() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      button.classList.add("active");
      $(`#panel-${button.dataset.tab}`).classList.add("active");
    });
  });
}

function tags(list) {
  return (list || []).map((t) => `<span class="tag">${t}</span>`).join("");
}

async function loadPlugins() {
  const data = await api("/api/plugins");
  const rows = $("#plugin-rows");
  rows.innerHTML = "";

  for (const p of data.plugins) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${p.id}</code></td>
      <td>${p.name}<div class="muted" style="font-size:12px">v${p.version}</div></td>
      <td>${tags(p.capabilities)}</td>
      <td>${tags(p.handles)}</td>
      <td class="${p.usable ? "ok" : "bad"}">${p.usable ? "可用" : "不可用"}</td>`;
    if (p.error) tr.title = p.error;
    rows.append(tr);
  }

  $("#plugin-empty").hidden = data.plugins.length > 0;
  const s = data.summary;
  setStatus(`${s.usable}/${s.total} 插件可用`, s.usable === s.total ? "ok" : "warn");
}

async function boot() {
  initTabs();
  try {
    await loadPlugins();
  } catch (err) {
    setStatus("服务未连接", "bad");
    $("#plugin-empty").textContent = String(err);
    $("#plugin-empty").hidden = false;
  }
  bindCompare();
  bindTrend();
  bindWriteback();
  bindFlash();
  loadFlashHistory();
}

/* ---- 标定比对 ---- */

function val(id) {
  return document.getElementById(id).value.trim();
}

function post(body) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function bindCompare() {
  $("#cmp-run").addEventListener("click", runCompare);
}

function fmt(n) {
  if (n === null || n === undefined) return "—";
  if (typeof n === "number") return Number.isInteger(n) ? String(n) : n.toFixed(4);
  return String(n);
}

function addr(n) {
  return n === null || n === undefined ? "—" : "0x" + n.toString(16).toUpperCase();
}

const KIND_LABEL = {
  added: "新增",
  removed: "删除",
  values_changed: "值变",
  attrs_changed: "属性变",
  address_moved: "地址漂移",
};

async function runCompare() {
  const button = $("#cmp-run");
  button.disabled = true;
  button.textContent = "比对中…";
  try {
    const body = {
      baseline: {
        a2l: val("cmp-base-a2l"),
        values: val("cmp-base-values") || null,
        image: val("cmp-base-image") || null,
      },
      candidate: {
        a2l: val("cmp-cand-a2l"),
        values: val("cmp-cand-values") || null,
        image: val("cmp-cand-image") || null,
      },
      byte_order: val("cmp-order"),
      tolerance: Number(val("cmp-tol") || 0),
      out: val("cmp-out") || null,
    };
    renderCompare(await api("/api/compare", post(body)));
  } catch (err) {
    renderCompareError(err);
  } finally {
    button.disabled = false;
    button.textContent = "开始比对";
  }
}

function card(kind, value, label) {
  return `<div class="card ${kind}"><b>${value}</b><span>${label}</span></div>`;
}

function renderCompare(data) {
  const s = data.summary;
  $("#cmp-summary").innerHTML = [
    card("added", s.added, "新增"),
    card("removed", s.removed, "删除"),
    card("changed", s.changed, "改动"),
    card("moves", s.address_moves, "地址漂移（已折叠）"),
  ].join("");

  $("#cmp-warnings").textContent = data.warnings.length
    ? "告警：\n" + data.warnings.join("\n")
    : "";

  const rows = $("#cmp-rows");
  rows.innerHTML = "";
  for (const c of data.changes) {
    const tr = document.createElement("tr");
    const detail = c.values.length
      ? c.values.map((v) => `${fmt(v.before)} → ${fmt(v.after)}`).join("<br>")
      : c.attributes.length
        ? c.attributes.join("<br>")
        : "—";
    tr.innerHTML = `
      <td><code>${c.path}</code><div class="muted" style="font-size:12px">${c.name || ""}</div></td>
      <td>${tags(c.kinds.map((k) => KIND_LABEL[k] || k))}</td>
      <td class="muted">${addr(c.address_before)} → ${addr(c.address_after)}</td>
      <td>${detail}</td>`;
    rows.append(tr);
  }
  if (!data.changes.length) {
    rows.innerHTML = `<tr><td colspan="4" class="muted">没有需要人看的改动。</td></tr>`;
  }

  $("#cmp-report").textContent = data.report_path ? `报告已生成：${data.report_path}` : "";
}

function renderCompareError(err) {
  $("#cmp-summary").innerHTML = "";
  $("#cmp-warnings").textContent = "";
  $("#cmp-report").textContent = "";
  $("#cmp-rows").innerHTML =
    `<tr><td colspan="4" class="bad">${String(err.message || err)}</td></tr>`;
}

/* ---- 测量数据 ---- */

function bindTrend() {
  $("#tr-load").addEventListener("click", loadSignals);
  $("#tr-run").addEventListener("click", runTrend);
}

async function loadSignals() {
  const file = val("tr-file");
  if (!file) return;
  try {
    const data = await api(
      `/api/measurement/signals?file=${encodeURIComponent(file)}`
    );
    const select = $("#tr-signal");
    select.innerHTML = "";
    for (const s of data.signals) {
      const option = document.createElement("option");
      option.value = s.path;
      option.textContent = `${s.name || s.path}${s.unit ? ` [${s.unit}]` : ""}`;
      select.append(option);
    }
    for (const key of data.events) {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = `${key}（报文速率）`;
      select.append(option);
    }
    const head = data.label
      ? `${data.label}：${data.signals.length} 个连续信号、${data.events.length} 个事件键`
      : `${data.signals.length} 个连续信号、${data.events.length} 个事件键`;
    $("#tr-note").textContent = data.warnings.length
      ? `${head}\n告警：${data.warnings.join("\n")}`
      : head;
    $("#tr-error").textContent = "";
  } catch (err) {
    $("#tr-error").textContent = String(err.message || err);
  }
}

async function runTrend() {
  const body = {
    file: val("tr-file"),
    signal: val("tr-signal"),
    points: Number(val("tr-points") || 2000),
    start: val("tr-from") ? Number(val("tr-from")) : null,
    end: val("tr-to") ? Number(val("tr-to")) : null,
  };
  try {
    renderTrend(await api("/api/trend", post(body)));
    $("#tr-error").textContent = "";
  } catch (err) {
    $("#tr-error").textContent = String(err.message || err);
    $("#tr-summary").innerHTML = "";
  }
}

function renderTrend(data) {
  const s = data.stats || {};
  const stat = (key) => (s[key] === undefined ? "—" : fmt(s[key]));
  $("#tr-summary").innerHTML = [
    card("changed", data.sample_count, "样本数"),
    card("added", stat("min"), "最小值"),
    card("removed", stat("max"), "最大值"),
    card("moves", stat("mean"), "均值"),
  ].join("");
  $("#tr-note").textContent = [
    `信号：${data.signal}${data.unit ? ` [${data.unit}]` : ""}（${data.kind}）`,
    `抽稀：${data.note}`,
    data.window
      ? `时间窗：${data.window[0].toFixed(3)} – ${data.window[1].toFixed(3)} s`
      : "",
  ]
    .filter(Boolean)
    .join("\n");
  drawChart(data.bands, data.unit);
}

function drawChart(bands, unit) {
  const svg = $("#tr-chart");
  const W = 900;
  const H = 320;
  const P = 38;
  if (!bands || !bands.length) {
    svg.innerHTML =
      '<text x="450" y="160" text-anchor="middle" class="axis">没有可画的数据</text>';
    return;
  }
  const t0 = bands[0].t[0];
  const t1 = bands[bands.length - 1].t[1];
  let lo = Infinity;
  let hi = -Infinity;
  for (const b of bands) {
    lo = Math.min(lo, b.min);
    hi = Math.max(hi, b.max);
  }
  if (!(hi > lo)) {
    lo -= 1;
    hi += 1;
  }
  const pad = (hi - lo) * 0.08;
  lo -= pad;
  hi += pad;
  const x = (t) => P + ((t - t0) / (t1 - t0 || 1)) * (W - 2 * P);
  const y = (v) => H - P - ((v - lo) / (hi - lo)) * (H - 2 * P);

  const toPath = (key) =>
    bands
      .map(
        (b) =>
          `M${x(b.t[0]).toFixed(1)},${y(b[key]).toFixed(1)}` +
          `L${x(b.t[1]).toFixed(1)},${y(b[key]).toFixed(1)}`
      )
      .join("");
  const fill =
    `${toPath("min")} ` +
    bands
      .slice()
      .reverse()
      .map(
        (b) =>
          `L${x(b.t[1]).toFixed(1)},${y(b.max).toFixed(1)}` +
          `L${x(b.t[0]).toFixed(1)},${y(b.max).toFixed(1)}`
      )
      .join("") +
    " Z";

  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((f) => {
      const v = lo + (hi - lo) * f;
      return (
        `<line x1="${P}" y1="${y(v).toFixed(1)}" x2="${W - P}" y2="${y(v).toFixed(1)}" class="grid"/>` +
        `<text x="${P - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" class="axis">${v.toFixed(2)}</text>`
      );
    })
    .join("");

  svg.innerHTML = `${grid}
    <path d="${fill}" class="band"/>
    <path d="${toPath("max")}" class="line max"/>
    <path d="${toPath("min")}" class="line min"/>
    <text x="${P}" y="${H - 8}" class="axis">${t0.toFixed(2)} s</text>
    <text x="${W - P}" y="${H - 8}" text-anchor="end" class="axis">${t1.toFixed(2)} s</text>
    <text x="${P}" y="16" class="axis">${unit ? `${unit}（min/max 带）` : "min/max 带"}</text>`;
}

/* ---- 写回 ---- */

function bindWriteback() {
  $("#wb-run").addEventListener("click", runWriteback);
}

async function runWriteback() {
  const button = $("#wb-run");
  button.disabled = true;
  button.textContent = "写回中…";
  try {
    const body = {
      image: val("wb-image"),
      a2l: val("wb-a2l"),
      values: val("wb-values"),
      out: val("wb-out") || null,
    };
    renderWriteback(await api("/api/writeback", post(body)));
  } catch (err) {
    renderWritebackError(err);
  } finally {
    button.disabled = false;
    button.textContent = "开始写回";
  }
}

function renderWriteback(data) {
  $("#wb-summary").innerHTML = [
    card("added", data.written.length, "已写入"),
    card("removed", Object.keys(data.skipped).length, "跳过"),
    card("changed", Object.keys(data.checksums).length, "校验和"),
  ].join("");

  const lines = [`输出：${data.output}`];
  for (const [path, reason] of Object.entries(data.skipped)) {
    lines.push(`跳过 ${path}：${reason}`);
  }
  for (const [name, value] of Object.entries(data.checksums)) {
    lines.push(`校验和 ${name} = 0x${value.toString(16).toUpperCase()}`);
  }
  if (!Object.keys(data.checksums).length) {
    lines.push("提示：没有校验和规则 —— 值改了但校验和没更新，刷进 ECU 可能被拒");
  }
  if (data.warnings.length) lines.push(`告警：\n${data.warnings.join("\n")}`);
  $("#wb-detail").textContent = lines.join("\n");
}

function renderWritebackError(err) {
  $("#wb-summary").innerHTML = "";
  $("#wb-detail").textContent = String(err.message || err);
}

/* ---- 刷写 ---- */

function bindFlash() {
  $("#fl-run").addEventListener("click", runFlash);
  $("#fl-history").addEventListener("click", loadFlashHistory);
}

async function runFlash() {
  const button = $("#fl-run");
  button.disabled = true;
  button.textContent = "刷写中…";
  $("#fl-steps").innerHTML = "";
  $("#fl-result").textContent = "";
  try {
    const body = {
      image: val("fl-image"),
      target_id: val("fl-target"),
      target_name: val("fl-name"),
      operator: val("fl-operator"),
      block_size: Number(val("fl-block") || 32),
      recipe_id: val("fl-recipe"),
      gateway_id: val("fl-gateway"),
    };
    const data = await api("/api/flash/run", post(body));
    renderFlash(data);
    loadFlashHistory();
  } catch (err) {
    $("#fl-result").textContent = String(err.message || err);
  } finally {
    button.disabled = false;
    button.textContent = "开始刷写";
  }
}

function renderFlash(data) {
  const box = $("#fl-steps");
  box.innerHTML = "";
  data.steps.forEach((s, i) => {
    const div = document.createElement("div");
    div.className = "step done";
    div.innerHTML = `<span class="no">${s.index + 1}</span><span>${s.label}</span>`;
    box.append(div);
    // 逐条显示，让「一步步在跑」看得出来
    setTimeout(() => div.classList.add("shown"), i * 60);
  });

  const lines = [];
  lines.push(
    data.ok
      ? `刷写完成：${data.segments} 段 / ${data.bytes_done} 字节`
      : `刷写失败：${data.error || "未知原因"}`
  );
  if (data.resumed_from) lines.push(`（从第 ${data.resumed_from} 段续刷）`);
  $("#fl-result").textContent = lines.join("\n");
}

async function loadFlashHistory() {
  try {
    const data = await api("/api/flash/history?limit=50");
    const rows = $("#fl-rows");
    rows.innerHTML = "";
    for (const r of data.records) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="muted">${r.time}</td>
        <td>${r.target}<div class="muted" style="font-size:12px">${r.target_name}</div></td>
        <td>${r.operator || "—"}</td>
        <td>${r.segments}</td>
        <td>${r.bytes}</td>
        <td class="${r.success ? "ok" : "bad"}">${r.success ? "成功" : "失败"}</td>`;
      if (!r.success && r.error) tr.title = r.error;
      rows.append(tr);
    }
    if (!data.records.length) {
      rows.innerHTML = `<tr><td colspan="6" class="muted">还没有刷写记录。</td></tr>`;
    }
  } catch (err) {
    $("#fl-rows").innerHTML =
      `<tr><td colspan="6" class="bad">${String(err.message || err)}</td></tr>`;
  }
}

boot();
