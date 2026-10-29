(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const list = (value) => Array.isArray(value) ? value : [];
  const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value) => finite(value) ? String(value) : "未提供";
  const text = (value) => value === null || value === undefined ? "未提供" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const hashValid = (value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const statusNames = {
    draft: "基线草稿", proposed: "措施草稿", approved: "已批准", rejected: "已拒绝",
    executed: "已人工确认执行", followup_recorded: "后续验证合格 · 待人工关闭", closed: "已关闭",
    stable: "未发现统计信号", signals: "存在统计信号", not_evaluable: "不可评价", available: "可评价",
    ready: "待主动运行", running: "运行中", blocked: "阻塞 · 需对账", escalated: "已升级人工处理",
    completed: "调查完成", cancelled: "已取消", unknown: "未知", started: "已开始 · 结果待定",
    failed: "调用失败", reconciled_no_result: "已对账 · 无结果", unreviewed: "未经人工审核",
    statistical_signal: "统计信号风险", data_or_applicability: "数据 / 适用性风险"
  };
  const ruleNames = {
    R1: "1 点超出 3σ", R2: "连续 9 点位于中心线同侧", R3: "连续 6 点递增或递减",
    R4: "连续 14 点上下交替", R5: "3 点中至少 2 点同侧超出 2σ",
    R6: "5 点中至少 4 点同侧超出 1σ", R7: "连续 15 点位于 ±1σ 内",
    R8: "连续 8 点均在 ±1σ 外且分布两侧", R_LIMIT: "R 超出控制限"
  };

  // API ADAPTER: collection envelopes, me/provider, investigation state, proposal records.
  // All writes use the server's request schemas. No statistics are calculated in this UI.
  const adapt = {
    collection: (data, key) => Array.isArray(data) ? data : list(object(data)[key]),
    me: (data) => ({ actor: object(data.actor || data), provider: object(data.provider) }),
    investigation: (data) => ({ ...object(data.state), ...object(data) }),
    proposal: (data) => ({ ...object(data.state), ...object(data) }),
    proposalPayload: (data) => object(data && (data.payload || data.proposal)),
    followup: (data) => {
      const entries = list(data && data.followups);
      const record = entries.length ? entries[entries.length - 1] : data && data.followup;
      if (!record) return null;
      return { ...object(record.payload || record), followup_hash: record.followup_hash || object(record.payload).followup_hash };
    },
    // Domain adapter: null and invalid subgroup points remain gaps, never Number(null).
    points: (analysis) => list(analysis.points).map((p, i) => {
      const raw = object(p);
      const read = (keys) => {
        for (const key of keys) if (Object.hasOwn(raw, key)) return finite(raw[key]) ? raw[key] : null;
        return null;
      };
      const valid = raw.valid !== false;
      return { ...raw, index: Number.isInteger(raw.index) ? raw.index : i,
        mean: valid ? read(["mean", "xbar", "x_bar"]) : null,
        range: valid ? read(["range", "r"]) : null };
    }),
    limits: (baseline, analysis) => {
      // Frozen baseline payload is authoritative. analysis.control_limits is its server snapshot.
      const payload = object(baseline && baseline.payload);
      const source = Object.keys(payload).length ? payload : object(analysis.control_limits);
      const xbar = object(object(source.control_limits).xbar || object(source.limits).xbar);
      const range = object(object(source.control_limits).r || object(source.limits).r);
      return { xbar: { lower: source.xbar_lcl ?? xbar.lcl, center: source.center ?? xbar.center ?? xbar.cl, upper: source.xbar_ucl ?? xbar.ucl },
        r: { lower: source.r_lcl ?? range.lcl, center: source.rbar ?? range.center ?? range.cl, upper: source.r_ucl ?? range.ucl },
        unit: source.unit, source: Object.keys(payload).length ? "冻结基线 payload" : "服务端 analysis.control_limits 快照" };
    }
  };

  const freshState = () => ({ actor: null, provider: {}, datasets: [], baselines: [], evaluations: [], risks: [], proposals: [],
    baselineId: "", evaluationId: "", riskId: "", proposalId: "", investigation: null, busy: false, audit: null, auditId: "" });
  let state = freshState();
  let token = "";
  let sessionEpoch = 0;
  const controllers = new Set();
  class StaleSession extends Error {}
  class UIError extends Error {}
  const guard = (epoch) => { if (epoch !== sessionEpoch) throw new StaleSession(); };
  const actionId = () => `ui_${crypto.randomUUID().replaceAll("-", "")}`;
  const pathId = (value) => encodeURIComponent(value);
  const selectedBaseline = () => state.baselines.find((b) => b.baseline_id === state.baselineId);
  const selectedEvaluation = () => state.evaluations.find((e) => e.evaluation_id === state.evaluationId);
  const selectedRisk = () => state.risks.find((r) => r.risk_id === state.riskId);
  const selectedProposal = () => state.proposals.find((p) => p.proposal_id === state.proposalId);
  const reviewer = () => ["reviewer", "admin"].includes(state.actor && state.actor.role);
  const upsert = (key, value, id) => {
    if (!value || !value[id]) throw new UIError("服务端未返回预期对象标识。请刷新工作区后核对，不要直接重复提交。");
    state[key] = [...state[key].filter((item) => item[id] !== value[id]), value];
  };
  function notice(message, kind = "neutral") {
    $("notice").textContent = message;
    $("notice").className = `notice ${kind}`;
  }
  function node(tag, content, className) {
    const result = document.createElement(tag);
    if (content !== undefined) result.textContent = text(content);
    if (className) result.className = className;
    return result;
  }
  function svgNode(tag, attrs = {}, content) {
    const result = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) result.setAttribute(key, String(value));
    if (content !== undefined) result.textContent = text(content);
    return result;
  }
  function badge(status) {
    const kind = ["approved", "closed", "stable", "available", "completed", "followup_recorded"].includes(status) ? "positive"
      : ["signals", "blocked", "rejected", "not_evaluable", "escalated", "failed"].includes(status) ? "warning" : "neutral";
    return node("span", statusNames[status] ? `${statusNames[status]} · ${status}` : (status || "状态未提供"), `badge ${kind}`);
  }
  function empty(parent, message) { parent.replaceChildren(node("p", message, "empty-state")); }
  function facts(parent, entries) {
    const dl = node("dl", undefined, "facts");
    for (const [key, value] of entries) dl.append(node("dt", key), node("dd", value));
    parent.append(dl);
  }
  function fullHash(parent, title, value) {
    const box = node("div", undefined, "hash-entry");
    box.append(node("span", title, "helper"), node("code", value || "未提供；不能绑定此对象进行审批", "hash-value"));
    parent.append(box);
  }
  function jsonDetail(parent, label, value) {
    const details = node("details", undefined, "evidence-detail");
    details.append(node("summary", label));
    // Lazily materialize large evidence, always as text, never parsed as HTML.
    details.addEventListener("toggle", () => {
      if (details.open && !details.querySelector("pre")) details.append(node("pre", JSON.stringify(value, null, 2)));
    });
    parent.append(details);
  }
  function textList(parent, values, noItems = "无") {
    if (!values.length) { parent.append(node("p", noItems, "muted")); return; }
    const ul = node("ul", undefined, "plain-list");
    values.forEach((value) => ul.append(node("li", value)));
    parent.append(ul);
  }
  function table(parent, headers, rows) {
    const wrap = node("div", undefined, "table-scroll");
    const result = node("table");
    const tr = node("tr");
    headers.forEach((header) => { const th = node("th", header); th.scope = "col"; tr.append(th); });
    const thead = node("thead"); thead.append(tr);
    const body = node("tbody");
    rows.forEach((row) => {
      const line = node("tr");
      row.forEach((value) => { const cell = node("td"); cell.append(value instanceof Node ? value : node("span", value)); line.append(cell); });
      body.append(line);
    });
    result.append(thead, body); wrap.append(result); parent.append(wrap);
  }
  function formatDate(value, milliseconds = true) {
    if (!finite(value)) return "未提供";
    const date = new Date(milliseconds ? value : value * 1000);
    return Number.isNaN(date.getTime()) ? "无效时间" : date.toLocaleString("zh-CN", { hour12: false });
  }
  function datasetLabel(data) {
    const label = data.label || data.scenario || data.case_name || data.name;
    return `${data.dataset_id}${label ? ` · ${label}` : ""} · ${data.unit || "单位未提供"} · 原始合成数据`;
  }
  function selectOptions(id, items, getId, getLabel, wanted, prompt = "请主动选择") {
    const select = $(id);
    const previous = wanted === undefined ? select.value : wanted;
    select.replaceChildren();
    const placeholder = node("option", items.length ? prompt : "暂无可用对象"); placeholder.value = ""; select.append(placeholder);
    items.forEach((item) => { const option = node("option", getLabel(item)); option.value = getId(item); select.append(option); });
    select.value = items.some((item) => getId(item) === previous) ? previous : "";
  }
  function resetApproval(formId) { $(formId).reset(); }
  function clearSession(message = "已退出：页面内令牌和对象已清除。已提交的服务端任务不保证被中止。") {
    sessionEpoch += 1;
    controllers.forEach((controller) => controller.abort()); controllers.clear();
    token = ""; state = freshState();
    document.querySelectorAll("form").forEach((form) => form.reset());
    $("token-input").value = "";
    $("reconcile-section").open = false;
    render(); notice(message);
  }
  const errorMessages = {
    unauthorized: "访问令牌无效或会话身份已失效，请重新登录。", forbidden: "当前身份没有此操作权限。",
    not_found: "对象不存在或不在当前身份可见范围内。", validation_error: "请求字段、类型或长度不符合约定。",
    stale_hash: "对象哈希已变化，请刷新并重新人工核对。", hash_mismatch: "哈希不匹配，请刷新并重新核对。",
    version_conflict: "对象状态已变化，请刷新后核对。", baseline_not_approved: "基线尚未经过人工批准。",
    invalid_state: "当前对象状态不允许此操作。", active_worker: "调查仍有活动 worker，请稍后主动读取状态。",
    followup_window: "后续数据必须严格晚于执行观察时间和原风险数据窗口。",
    followup_reused: "后续数据不得复用原风险或已有后续证据。",
    future_observation: "实际观察时间或证据数据时间不能位于未来。", model_unknown: "模型结果未知，需要人工对账，不会自动重试。",
    provider_invalid: "本地模型调用失败，未自动重试。", immutable_review: "此基线已经审批，不能覆盖原审批。",
    invalid_transition: "当前措施状态不允许此步骤，请刷新并按顺序操作。", invalid_request: "请求字段或人工说明不符合约定。",
    reused_evidence: "后续验证必须使用独立新观测，不能重命名或复用原风险 / 后续数据。",
    scope_mismatch: "后续数据的身份或工厂范围与原证据不匹配。", baseline_changed: "原批准基线发生变化，不能继续此操作。",
    evidence_mismatch: "证据与冻结对象不匹配，请停止操作并核查。", artifact_integrity: "服务端证据完整性检查失败，请停止操作并核查。",
    BASELINE_GROUP_COUNT: "Phase I 基线要求恰好 25 个子组，每组 5 个观测。",
    BASELINE_NOT_EVALUABLE: "Phase I 数据质量或适用性检查不通过，不能建立基线。",
    BASELINE_UNSTABLE: "Phase I 数据存在统计信号，不能作为稳定基线。",
    ZERO_VARIATION: "变异为零或无法可靠表示，不能建立有效控制限。",
    NUMERICAL_RANGE: "统计计算超出可表示数值范围，不能评价。",
    INVALID_BASELINE: "冻结基线不符合约定，不能用于本次计算。", BASELINE_HASH_MISMATCH: "冻结基线哈希不一致，不能继续计算。",
    INVALID_DATASET: "数据集不符合严格的合成数据输入约定。"
  };
  async function api(path, { method = "GET", body, epoch = sessionEpoch, timeout = 40000 } = {}) {
    guard(epoch);
    const controller = new AbortController(); controllers.add(controller);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    try {
      const headers = { Accept: "application/json", Authorization: `Bearer ${token}` };
      if (method !== "GET") headers["Content-Type"] = "application/json";
      const response = await fetch(path, { method, headers, body: method === "GET" ? undefined : JSON.stringify(body || {}),
        credentials: "omit", cache: "no-store", mode: "same-origin", redirect: "error", referrerPolicy: "no-referrer", signal: controller.signal });
      guard(epoch);
      let data;
      try { data = await response.json(); } catch { throw new UIError("服务端未返回有效 JSON。请主动刷新核对服务端状态。"); }
      guard(epoch);
      if (response.status === 401) { clearSession("会话认证失效，已清除本地令牌和旧数据。请重新登录。"); throw new StaleSession(); }
      if (!response.ok) {
        const code = object(object(data).error).code;
        if (code === "identity_changed") { clearSession("身份或权限已变化，已清除本地令牌及旧数据。请重新登录。"); throw new StaleSession(); }
        // Do not reflect arbitrary server errors/validation input (which may contain credentials).
        const known = typeof code === "string" && Object.hasOwn(errorMessages, code);
        throw new UIError(`${known ? errorMessages[code] : "服务端拒绝了此请求，请核对输入和对象状态。"}（HTTP ${response.status}${known ? ` · ${code}` : ""}）`);
      }
      return data;
    } catch (error) {
      guard(epoch);
      if (error instanceof UIError || error instanceof StaleSession) throw error;
      throw new UIError(timedOut ? "请求等待超时。结果未知，请主动读取状态；不会自动重试。" : "本地连接中断或请求失败。请检查服务并主动读取状态；已提交的操作可能仍在服务端执行。");
    } finally { clearTimeout(timer); controllers.delete(controller); }
  }
  async function operate(label, task) {
    if (state.busy || !state.actor) return;
    const epoch = sessionEpoch;
    state.busy = true; gate(); notice(`${label}，请等待服务端返回…`);
    try {
      const result = await task(epoch);
      guard(epoch); render(); notice(result || `${label}完成。`, "positive");
    } catch (error) {
      if (epoch !== sessionEpoch || error instanceof StaleSession) return;
      render();
      notice(error instanceof UIError ? error.message : "页面未能完成操作。请刷新并核对服务端状态，不要直接重复提交。", "warning");
    } finally {
      if (epoch === sessionEpoch) { state.busy = false; gate(); }
    }
  }
  async function refreshCollections(epoch) {
    const keys = ["datasets", "baselines", "evaluations", "risks", "proposals"];
    const responses = await Promise.all(keys.map((key) => api(`/api/${key}`, { epoch })));
    guard(epoch);
    keys.forEach((key, i) => { state[key] = adapt.collection(responses[i], key); });
    state.proposals = state.proposals.map(adapt.proposal);
    if (!selectedBaseline()) state.baselineId = "";
    if (!selectedEvaluation()) state.evaluationId = "";
    if (!selectedRisk()) state.riskId = "";
    if (!selectedProposal()) state.proposalId = "";
  }

  function renderDatasetMeta(selectId, targetId) {
    const target = $(targetId); target.replaceChildren();
    const data = state.datasets.find((item) => item.dataset_id === $(selectId).value);
    if (!data) return;
    facts(target, [["数据来源", data.source === "original_synthetic" ? "原始合成数据 · 非生产" : `来源待核对：${text(data.source)}`],
      ["工厂 / 过程", `${text(data.plant_id)} / ${text(data.process_id)}`], ["质量特性", data.characteristic],
      ["子组 / 采样周期", `${text(data.group_count)} / ${text(data.sampling_seconds)} 秒`]]);
    fullHash(target, "数据 content_hash", data.content_hash);
  }
  function renderBaseline() {
    const current = selectedBaseline();
    const target = $("baseline-detail"); target.replaceChildren();
    $("baseline-review-hash").replaceChildren();
    if (!current) { empty(target, "创建或选择一个基线，查看冻结参数与完整内容哈希。"); return; }
    const payload = object(current.payload);
    target.append(badge(current.status));
    facts(target, [["基线 ID", current.baseline_id], ["工厂 / 过程", `${text(payload.plant_id)} / ${text(payload.process_id)}`],
      ["质量特性 / 单位", `${text(payload.characteristic)} / ${text(payload.unit)}`],
      ["子组数量 / 子组大小", `${text(payload.group_count)} / ${text(payload.subgroup_size)}`],
      ["Phase I 窗口", `${formatDate(payload.window_start_ms)} — ${formatDate(payload.window_end_ms)}`],
      ["X̄ 中心 / LCL / UCL", `${number(payload.center)} / ${number(payload.xbar_lcl)} / ${number(payload.xbar_ucl)}`],
      ["R̄ / LCL / UCL", `${number(payload.rbar)} / ${number(payload.r_lcl)} / ${number(payload.r_ucl)}`]]);
    fullHash(target, "内容哈希 content_hash（审批绑定）", current.content_hash);
    fullHash(target, "统计基线哈希 payload.baseline_hash", payload.baseline_hash);
    fullHash($("baseline-review-hash"), "待审批的完整 content_hash", current.content_hash);
    if (current.review) {
      facts(target, [["审批人", current.review.actor_id], ["审批依据", current.review.note]]);
      jsonDetail(target, "完整审批记录", current.review);
    }
    jsonDetail(target, "完整冻结基线与适用性声明", current);
  }
  function renderIssues(parent, analysis) {
    const section = node("section", undefined, "result-section"); section.append(node("h3", "数据质量与适用性"));
    const issues = list(analysis.issues);
    if (!issues.length) section.append(node("p", Object.hasOwn(analysis, "issues") ? "服务端未报告适用性问题。" : "服务端未提供 issues；不推断数据适用。", "muted"));
    else table(section, ["问题代码", "子组 / 位置", "说明"], issues.map((issue) => typeof issue === "object"
      ? [issue.code, issue.group_id ?? issue.index ?? "全局", issue.message ?? issue.reason] : [issue, "—", "—"]));
    parent.append(section);
  }
  function renderHits(parent, analysis) {
    const section = node("section", undefined, "result-section");
    section.append(node("h3", "Nelson 规则与 R 图命中窗口"));
    const hits = [...list(analysis.rule_hits), ...list(analysis.range_hits)];
    if (!hits.length) section.append(node("p", ["stable", "signals"].includes(analysis.status) ? "服务端未返回规则命中。" : "未返回命中不代表稳定；请先处理不可评价和适用性问题。", "muted"));
    else {
      section.append(node("p", "保留全部重叠窗口与同时命中的规则。序号使用服务端 0 起始索引；X̄ window 为标准化 z，R window 为原单位极差。", "helper"));
      table(section, ["图 / 规则", "窗口（服务端索引）", "组号 / 时间", "完整 window"], hits.map((hit) => {
        const windowCell = node("div");
        windowCell.append(node("code", `window = ${text(hit.window)}`));
        const details = node("details"); details.append(node("summary", "窗口与完整证据"), node("pre", JSON.stringify(hit, null, 2)));
        windowCell.append(details);
        return [`${hit.chart || (hit.rule === "R_LIMIT" ? "r" : "xbar")} · ${text(hit.rule)} ${ruleNames[hit.rule] || ""}`,
          `${text(hit.start_index)} → ${text(hit.end_index)}；n=${text(hit.window_size)}`,
          `${text(hit.start_group_id)} → ${text(hit.end_group_id)}\n${formatDate(hit.start_timestamp_ms)} → ${formatDate(hit.end_timestamp_ms)}`,
          windowCell];
      }));
    }
    parent.append(section);
  }
  function renderCapability(parent, analysis, dataset) {
    const capability = object(analysis.capability);
    const area = node("section", undefined, "capability-panel");
    area.append(node("h3", "过程能力 · 独立规格区"), badge(capability.status));
    const specs = object(capability.specifications || object(dataset).specifications);
    facts(area, [["LSL / USL（非控制限）", `${number(specs.lsl)} / ${number(specs.usl)} ${specs.unit || ""}`], ["规格修订", specs.revision]]);
    const values = node("div", undefined, "metrics");
    for (const [key, label] of [["cpk", "Cpk"], ["ppk", "Ppk"]]) {
      const value = capability[key] ?? capability[label];
      const cell = node("div", undefined, "metric");
      const available = capability.status === "available" && finite(value);
      cell.append(node("span", label), node("strong", available ? number(value) : "不可评价"), node("small", available ? "服务端统计值 · 不表示生产批准" : "不以 0 代替未知或不适用")); values.append(cell);
    }
    area.append(values);
    const reasons = [...list(capability.reasons), ...list(capability.issues)];
    textList(area, reasons, capability.status === "available" ? "服务端报告满足本次能力计算条件。" : "能力不可评价；请核对服务端状态、数据和规格。");
    if (list(capability.assumptions).length) { const details = node("details"); details.append(node("summary", "能力计算前提与限制")); textList(details, capability.assumptions); area.append(details); }
    jsonDetail(area, "完整 capability（含方法、样本量与问题）", capability);
    parent.append(area);
  }
  function drawChart(parent, points, limits, kind, hits, unit, chartKey) {
    const title = kind === "xbar" ? "X̄ · 子组均值控制图" : "R · 子组极差控制图";
    const panel = node("section", undefined, "chart-panel"); panel.append(node("h3", title));
    const validLimits = [limits.lower, limits.center, limits.upper].every(finite);
    const key = kind === "xbar" ? "mean" : "range";
    if (!validLimits) panel.append(node("p", "基线控制限未完整返回。仅展示可用点，不推导或补造控制限。", "warning-box"));
    if (!points.length) { panel.append(node("p", "服务端尚未提供 analysis.points，不使用原始数据自行计算。", "empty-state")); parent.append(panel); return; }
    const width = 1040, height = 280, left = 95, right = 145, top = 22, bottom = 42;
    const values = points.map((p) => p[key]).filter(finite);
    const domain = [...values, ...[limits.lower, limits.center, limits.upper].filter(finite)];
    if (!domain.length) { panel.append(node("p", "无可用控制限或有效点；缺失数据不填零。", "empty-state")); parent.append(panel); return; }
    let low = Math.min(...domain), high = Math.max(...domain);
    const padding = high === low ? Math.max(Math.abs(high) * 0.01, 0.001) : (high - low) * 0.14;
    low -= padding; high += padding;
    const x = (i) => left + (width - left - right) * (points.length === 1 ? 0.5 : i / (points.length - 1));
    const y = (value) => top + (high - value) / (high - low) * (height - top - bottom);
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-labelledby": `${chartKey}-${kind}-title ${chartKey}-${kind}-desc` });
    svg.append(svgNode("title", { id: `${chartKey}-${kind}-title` }, title),
      svgNode("desc", { id: `${chartKey}-${kind}-desc` }, "显示服务端子组统计值与冻结基线控制限。缺失或无效子组断线，红点是服务端规则命中窗口中的点，规格不在控制图内。完整点值见下方数据表。"));
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = low + (high - low) * tick / 4;
      svg.append(svgNode("line", { x1: left, x2: width - right, y1: y(value), y2: y(value), class: "chart-grid" }),
        svgNode("text", { x: left - 10, y: y(value) + 4, "text-anchor": "end", class: "chart-axis-text" }, Number(value.toPrecision(6)).toString()));
    }
    svg.append(svgNode("text", { x: left, y: 13, class: "chart-axis-text" }, unit || "单位未提供"));
    for (const [field, label] of [["upper", "UCL"], ["center", "CL"], ["lower", "LCL"]]) {
      if (!finite(limits[field])) continue;
      svg.append(svgNode("line", { x1: left, x2: width - right, y1: y(limits[field]), y2: y(limits[field]), class: field === "center" ? "chart-center" : "chart-limit" }),
        svgNode("text", { x: width - right + 10, y: y(limits[field]) + 4, class: field === "center" ? "chart-center-label" : "chart-limit-label" }, `${label} ${Number(limits[field].toPrecision(7))}`));
    }
    let segment = [];
    const flush = () => { if (segment.length > 1) svg.append(svgNode("polyline", { points: segment.join(" "), class: "chart-series" })); segment = []; };
    points.forEach((point, i) => { if (!finite(point[key])) flush(); else { if (point.break_before === true) flush(); segment.push(`${x(i)},${y(point[key])}`); } }); flush();
    points.forEach((point, i) => {
      if (!finite(point[key])) {
        svg.append(svgNode("line", { x1: x(i) - 3, x2: x(i) + 3, y1: height - bottom + 7, y2: height - bottom + 13, class: "chart-missing" }),
          svgNode("line", { x1: x(i) - 3, x2: x(i) + 3, y1: height - bottom + 13, y2: height - bottom + 7, class: "chart-missing" }));
        return;
      }
      const hit = hits.some((h) => (h.chart || (h.rule === "R_LIMIT" ? "r" : "xbar")) === kind &&
        (Number.isInteger(h.start_index) && Number.isInteger(h.end_index) ? point.index >= h.start_index && point.index <= h.end_index : h.index === point.index));
      const dot = svgNode("circle", { cx: x(i), cy: y(point[key]), r: hit ? 4.2 : 3.1, class: hit ? "chart-point hit" : "chart-point" });
      dot.append(svgNode("title", {}, `${text(point.group_id)} · index ${point.index}\n${formatDate(point.timestamp_ms)}\n${key}: ${point[key]} ${unit || ""}${hit ? "\n服务端命中窗口" : ""}`)); svg.append(dot);
    });
    const tickStep = Math.max(1, Math.ceil(points.length / 9));
    points.forEach((point, i) => { if (i % tickStep === 0 || i === points.length - 1) svg.append(svgNode("text", { x: x(i), y: height - 15, "text-anchor": "middle", class: "chart-axis-text" }, point.index)); });
    svg.append(svgNode("text", { x: width - right + 10, y: height - 15, class: "chart-axis-text" }, "子组 index（0 起始）"));
    const scroll = node("div", undefined, "chart-scroll"); scroll.append(svg); panel.append(scroll);
    panel.append(node("p", "实线：子组统计值　虚线：控制限　青线：中心线　红点：服务端命中窗口　×：缺失 / 无效（断线）", "chart-legend"));
    facts(panel, [["LCL", number(limits.lower)], ["CL", number(limits.center)], ["UCL", number(limits.upper)]]);
    parent.append(panel);
  }
  function renderAnalysis(target, evaluation, chartKey) {
    target.replaceChildren();
    if (!evaluation || !evaluation.analysis) { empty(target, "选择已批准基线和监测数据，运行确定性 SPC 评估后查看控制图。前端不生成统计结论。"); return; }
    const analysis = object(evaluation.analysis), dataset = object(evaluation.dataset);
    const baselineId = evaluation.baseline_id || analysis.baseline_id;
    const baseline = state.baselines.find((item) => item.baseline_id === baselineId);
    const expectedHash = evaluation.baseline_hash || analysis.baseline_hash;
    const baselineMatches = !baseline || !expectedHash || [baseline.content_hash, object(baseline.payload).baseline_hash].includes(expectedHash);
    const limits = adapt.limits(baselineMatches ? baseline : null, analysis);
    if (!baselineMatches) target.append(node("p", "当前基线对象的哈希与评估绑定不一致；不使用该对象限值，仅使用此评估的服务端限值快照。请刷新并核查。", "warning-box"));
    const points = adapt.points(analysis);
    const summary = node("div", undefined, "analysis-summary"); summary.append(badge(analysis.status));
    facts(summary, [["评估数据", evaluation.dataset_id || dataset.dataset_id || analysis.dataset_id], ["冻结基线", baselineId],
      ["子组总数 / 有效子组", `${text(object(analysis.counts).groups)} / ${text(object(analysis.counts).valid_groups)}`],
      ["控制限来源", limits.source], ["数据来源", "原始合成数据 · 非生产"]]);
    target.append(summary);
    if (analysis.status === "not_evaluable") target.append(node("p", "本次不可评价。图上的局部有效点不代表总体适用，也不能据此宣称稳定或能力合格。", "warning-box"));
    const hits = [...list(analysis.rule_hits), ...list(analysis.range_hits)];
    drawChart(target, points, limits.xbar, "xbar", hits, limits.unit || dataset.unit, chartKey);
    drawChart(target, points, limits.r, "r", hits, limits.unit || dataset.unit, chartKey);
    renderIssues(target, analysis); renderHits(target, analysis); renderCapability(target, analysis, dataset);
    fullHash(target, "评估 / 证据 content_hash", evaluation.content_hash || evaluation.analysis_hash);
    fullHash(target, "服务端绑定的 baseline_hash", evaluation.baseline_hash || analysis.baseline_hash);
    const pointDetails = node("details", undefined, "evidence-detail"); pointDetails.append(node("summary", `完整点值表 · ${points.length} 个子组（显示值不重算）`));
    pointDetails.addEventListener("toggle", () => {
      if (!pointDetails.open || pointDetails.querySelector("table")) return;
      table(pointDetails, ["index / 子组", "采样时间", "均值", "极差", "有效性"], points.map((point) => [
        `${point.index} / ${text(point.group_id)}`, formatDate(point.timestamp_ms), point.mean === null ? "缺失 / 无效" : number(point.mean),
        point.range === null ? "缺失 / 无效" : number(point.range), point.valid === false ? "无效" : point.valid === true ? "有效" : "服务端未声明"]));
    }); target.append(pointDetails);
    if (dataset.groups) jsonDetail(target, "原始合成测量数据（不是生产材料）", dataset);
    jsonDetail(target, "完整确定性分析与证据绑定", evaluation);
  }
  function renderRisk() {
    const target = $("risk-detail"); target.replaceChildren();
    const risk = selectedRisk();
    $("proposal-risk-label").textContent = risk ? `新措施将关联当前风险：${risk.risk_id}。切换风险会清空未提交的措施表单。` : "请先在第 03 节选择风险；不得从模型草稿自动生成措施。";
    if (!risk) { empty(target, "没有选择风险。稳定评估可能不产生风险；风险列表只显示服务端返回的对象。"); return; }
    target.append(badge(risk.classification));
    facts(target, [["风险 ID", risk.risk_id], ["评估 ID", risk.evaluation_id], ["数据集", risk.dataset_id || object(risk.dataset).dataset_id],
      ["统计状态", statusNames[object(risk.analysis).status] || object(risk.analysis).status], ["严重性", risk.severity]]);
    target.append(node("p", "风险是统计信号或适用性提示，不是根因证据，也不是已批准措施。", "helper"));
    fullHash(target, "风险绑定 evaluation_hash", risk.evaluation_hash);
    jsonDetail(target, "完整风险、确定性分析与原始合成数据", risk);
  }
  function renderInvestigation() {
    const detail = $("investigation-detail"), reportTarget = $("investigation-report"); detail.replaceChildren(); reportTarget.replaceChildren();
    const inv = state.investigation;
    selectOptions("reconcile-call-select", inv && inv.pending_call_id ? [{ call_id: inv.pending_call_id }] : [], (c) => c.call_id, (c) => c.call_id);
    if (!inv) { empty(detail, "尚未创建或读取调查。创建后还需要主动点击运行。"); return; }
    detail.append(badge(inv.status));
    facts(detail, [["调查 ID", inv.investigation_id], ["绑定风险", inv.risk_id], ["调用 / 工具", `${text(inv.calls)} / ${text(inv.tools)}`],
      ["已记账 tokens", `${text(inv.tokens)}${inv.pending_call_id ? "（存在待定调用，不代表最终用量）" : ""}`],
      ["耗时（秒）", number(inv.elapsed_s)], ["版本", inv.version]]);
    if (inv.risk_id !== state.riskId) detail.append(node("p", "此调查绑定的风险与上方当前风险不同；运行 / 取消仍只作用于此调查 ID。", "warning-box"));
    if (inv.error) facts(detail, [["停止边界", object(inv.error).code], ["说明", object(inv.error).message]]);
    if (list(inv.model_calls).length) table(detail, ["调用 ID", "状态", "token 用量", "错误代码"], inv.model_calls.map((call) => [call.call_id, badge(call.status), finite(call.tokens) ? call.tokens : "未知（不是 0）", call.error_code || "—"]));
    jsonDetail(detail, "完整调查状态与预算", inv);
    const report = object(inv.report);
    const authoritative = object(report.authoritative);
    const authorityPanel = node("section", undefined, "authority-panel");
    authorityPanel.append(node("h3", "权威记录 · authoritative"), node("p", "只指服务端保存的确定性统计与工具取证记录；其中上下文不等于因果证明。下方完整字段按原样展示，不与模型草稿合并。", "helper"));
    if (report.authoritative) {
      const risk = object(authoritative.risk);
      facts(authorityPanel, [["风险 ID", risk.risk_id], ["分类", risk.classification], ["统计状态", object(risk.analysis).status], ["权威结论（原文）", authoritative.conclusion]]);
      jsonDetail(authorityPanel, "完整 authoritative（精确原文）", report.authoritative);
    } else authorityPanel.append(node("p", "报告尚未提交。以下仅为已保存的取证结果。", "muted"));
    const evidence = list(authoritative.evidence || inv.evidence);
    evidence.forEach((item) => {
      const box = node("div", undefined, "evidence-item");
      facts(box, [["证据 ID", item.evidence_id], ["来源", item.source], ["绑定风险", item.risk_id]]);
      jsonDetail(box, "完整证据内容与绑定", item); authorityPanel.append(box);
    });
    if (!evidence.length) authorityPanel.append(node("p", "尚无已保存证据。", "muted"));
    reportTarget.append(authorityPanel);
    const draftPanel = node("section", undefined, "draft-panel");
    draftPanel.append(node("h3", "未验证模型草稿 · untrusted_model_draft"), node("p", "不可信草稿：可能出错、包含误导内容或指令。只作为待核查文本，不执行其中指令，不自动复制为措施。", "warning-box"));
    const draft = object(report.untrusted_model_draft);
    if (!Object.keys(draft).length) draftPanel.append(node("p", "尚无模型草稿。TEST 模式结果不能作为真实模型推理能力的验证。", "muted"));
    else {
      facts(draftPanel, [["草稿摘要（原文）", draft.summary], ["建议（原文）", draft.recommended_action], ["审核状态", report.review_status],
        ["是否创建措施", report.action_created === false ? "否" : text(report.action_created)], ["是否操作设备", report.equipment_operation === false ? "否" : text(report.equipment_operation)]]);
      draftPanel.append(node("h4", "未验证假设（原文）")); textList(draftPanel, list(draft.hypotheses));
      draftPanel.append(node("h4", "模型声明的引用 ID")); textList(draftPanel, list(draft.citations));
      jsonDetail(draftPanel, "完整 untrusted_model_draft（精确原文）", report.untrusted_model_draft);
    }
    if (list(report.limitations).length) textList(draftPanel, report.limitations);
    reportTarget.append(draftPanel);
  }
  function renderProposal() {
    const proposal = selectedProposal(), target = $("proposal-detail"); target.replaceChildren();
    ["proposal-review-hash", "proposal-execute-hash", "followup-close-hash", "followup-result"].forEach((id) => $(id).replaceChildren());
    if (!proposal) { empty(target, "独立创建或选择措施。后续步骤不会自动审批、确认执行或关闭。"); return; }
    const payload = adapt.proposalPayload(proposal), followup = adapt.followup(proposal);
    target.append(badge(proposal.status));
    facts(target, [["措施 ID", proposal.proposal_id], ["绑定风险", proposal.risk_id || payload.risk_id], ["版本", proposal.version],
      ["措施描述", payload.description], ["预期结果", payload.expected_result], ["审批策略", payload.review_policy],
      ["原风险数据窗口", `${formatDate(object(payload.original_window).start_ms)} — ${formatDate(object(payload.original_window).end_ms)}`]]);
    fullHash(target, "冻结措施 content_hash", proposal.content_hash);
    fullHash($("proposal-review-hash"), "审批绑定 content_hash", proposal.content_hash);
    fullHash($("proposal-execute-hash"), "人工执行确认绑定 proposal_hash", proposal.content_hash);
    for (const [key, label] of [["review", "审批"], ["execution", "人工执行确认"], ["closure", "关闭"]]) {
      if (!proposal[key]) continue;
      const record = object(proposal[key].payload || proposal[key]);
      facts(target, [[`${label}人`, record.actor_id], [`${label}依据`, record.note]]);
      if (key === "execution") facts(target, [["实际观察时间", formatDate(record.observed_at_ms)], ["设备执行", "无 · 仅人工记录"]]);
      jsonDetail(target, `${label}完整记录`, proposal[key]);
    }
    jsonDetail(target, "完整措施状态、哈希和证据引用", proposal);
    if (followup) {
      const result = $("followup-result");
      const banner = node("div", undefined, followup.eligible === true ? "notice positive" : "warning-box");
      banner.append(node("strong", followup.eligible === true ? "服务端判定后续证据满足关闭前提；仍须人工关闭。" : "后续证据不满足关闭前提，不能关闭。"));
      if (list(followup.blockers).length) textList(banner, followup.blockers);
      if (followup.explanation) banner.append(node("p", followup.explanation));
      result.append(banner);
      const analysisBox = node("div"); renderAnalysis(analysisBox, followup, "followup"); result.append(analysisBox);
      fullHash($("followup-close-hash"), "最新后续证据 followup_hash（关闭绑定）", followup.followup_hash);
      jsonDetail(result, "全部后续评估记录（含不合格记录）", proposal.followups || proposal.followup);
    }
  }
  function renderAuditOptions() {
    const items = [];
    for (const [key, id, label] of [["baselines", "baseline_id", "基线"], ["evaluations", "evaluation_id", "评估"], ["risks", "risk_id", "风险"], ["proposals", "proposal_id", "措施"]]) {
      state[key].forEach((value) => items.push({ id: value[id], label }));
    }
    if (state.investigation) items.push({ id: state.investigation.investigation_id, label: "调查" });
    selectOptions("audit-object-select", items, (item) => item.id, (item) => `${item.label} · ${item.id}`);
    const target = $("audit-result"); target.replaceChildren();
    if (!state.audit) return;
    target.append(node("h3", `对象审计 · ${state.auditId}`));
    if (!state.audit.length) target.append(node("p", "服务端未返回审计条目。", "empty-state"));
    else table(target, ["序号 / 事件", "操作人", "时间", "事件详情"], state.audit.map((event) => {
      const detail = node("details"); detail.append(node("summary", "完整事件"), node("pre", JSON.stringify(event, null, 2)));
      return [`${text(event.id)} · ${text(event.event)}`, event.actor_id, formatDate(event.created_at, false), detail];
    }));
  }
  function render() {
    const loggedIn = Boolean(state.actor);
    $("workspace").hidden = !loggedIn; $("login-section").hidden = loggedIn;
    $("logout-button").hidden = !loggedIn && !token;
    $("actor-label").textContent = loggedIn ? `${state.actor.actor_id} · ${state.actor.role}` : "未登录";
    $("scope-label").textContent = loggedIn ? list(state.actor.plants).join(" / ") : "—";
    $("collection-counts").textContent = `${state.datasets.length} 个数据集 · ${state.baselines.length} 个基线 · ${state.risks.length} 个风险 · ${state.proposals.length} 个措施`;
    const provider = state.provider;
    const testMode = provider.testOnly === true || provider.mode === "synthetic-test";
    $("mode-badge").textContent = !loggedIn ? "模型模式待验证" : testMode ? "TEST · 合成测试提供方" : provider.mode === "local" ? "LOCAL · 本地模型" : "模型模式未声明";
    $("mode-badge").className = `badge ${testMode ? "warning" : "neutral"}`;
    $("provider-detail").textContent = loggedIn ? `${testMode ? "TEST 不是实际本地模型推理验证。" : "LOCAL 仅表示配置模式，不保证推理服务已通过检查。"} 提供方指纹：${text(provider.fingerprint)}；所有分析使用原始合成数据。` : "";
    ["baseline-dataset-select", "monitor-dataset-select", "followup-dataset-select"].forEach((id) => selectOptions(id, state.datasets, (d) => d.dataset_id, datasetLabel));
    selectOptions("baseline-select", state.baselines, (b) => b.baseline_id, (b) => `${b.baseline_id} · ${statusNames[b.status] || b.status}`, state.baselineId);
    selectOptions("evaluation-baseline-select", state.baselines.filter((b) => b.status === "approved"), (b) => b.baseline_id, (b) => `${b.baseline_id} · 已批准`);
    selectOptions("evaluation-select", state.evaluations, (e) => e.evaluation_id, (e) => `${e.dataset_id || e.evaluation_id} · ${statusNames[object(e.analysis).status] || "未提供统计状态"}`, state.evaluationId);
    selectOptions("risk-select", state.risks, (r) => r.risk_id, (r) => `${r.risk_id} · ${r.dataset_id || ""} · ${statusNames[r.classification] || r.classification}`, state.riskId);
    selectOptions("proposal-select", state.proposals, (p) => p.proposal_id, (p) => `${p.proposal_id} · ${statusNames[p.status] || p.status}`, state.proposalId);
    renderDatasetMeta("baseline-dataset-select", "baseline-dataset-meta"); renderDatasetMeta("monitor-dataset-select", "monitor-dataset-meta"); renderDatasetMeta("followup-dataset-select", "followup-dataset-meta");
    renderBaseline(); renderAnalysis($("analysis-content"), selectedEvaluation(), "evaluation"); renderRisk(); renderInvestigation(); renderProposal(); renderAuditOptions(); gate();
  }
  function gate() {
    const locked = state.busy || !state.actor;
    $("workspace").setAttribute("aria-busy", String(state.busy));
    document.querySelectorAll("#workspace input, #workspace textarea, #workspace select, #workspace button").forEach((input) => { input.disabled = locked; });
    $("login-button").disabled = state.busy; $("token-input").disabled = state.busy;
    const enabled = (id, condition) => { $(id).disabled = locked || !condition; };
    const baseline = selectedBaseline(), proposal = selectedProposal(), inv = state.investigation, followup = adapt.followup(proposal);
    enabled("baseline-create-button", Boolean($("baseline-dataset-select").value));
    enabled("baseline-review-button", reviewer() && baseline && baseline.status === "draft" && hashValid(baseline.content_hash) && $("baseline-review-confirm").checked && Boolean($("baseline-decision").value));
    $("baseline-review-gate").textContent = "需要 reviewer / admin 角色、draft 状态、有效完整哈希、主动选择决定与勾选核对。";
    enabled("evaluate-button", Boolean($("monitor-dataset-select").value) && state.baselines.some((b) => b.baseline_id === $("evaluation-baseline-select").value && b.status === "approved"));
    enabled("investigation-create-button", Boolean(selectedRisk()));
    enabled("investigation-run-button", inv && inv.status === "ready");
    enabled("investigation-refresh-button", Boolean(inv && inv.investigation_id));
    enabled("investigation-cancel-button", inv && ["ready", "blocked"].includes(inv.status));
    $("reconcile-section").hidden = !state.actor || state.actor.role !== "admin";
    enabled("reconcile-button", state.actor && state.actor.role === "admin" && inv && inv.status === "blocked" && Boolean(inv.pending_call_id) && $("reconcile-call-select").value === inv.pending_call_id && $("reconcile-confirm").checked);
    enabled("proposal-create-button", Boolean(selectedRisk()));
    enabled("proposal-review-button", reviewer() && proposal && proposal.status === "proposed" && hashValid(proposal.content_hash) && $("proposal-review-confirm").checked && Boolean($("proposal-decision").value));
    enabled("proposal-execute-button", proposal && proposal.status === "approved" && hashValid(proposal.content_hash) && $("execution-confirm").checked);
    enabled("proposal-followup-button", proposal && proposal.status === "executed" && Boolean($("followup-dataset-select").value));
    enabled("proposal-close-button", reviewer() && proposal && proposal.status === "followup_recorded" && followup && followup.eligible === true && hashValid(followup.followup_hash) && $("proposal-close-confirm").checked);
    $("proposal-review-gate").textContent = "需要 reviewer / admin 角色、proposed 状态与完整哈希。批准不会触发执行。";
    $("proposal-close-gate").textContent = "需要 reviewer / admin 角色、followup_recorded 状态、最新后续证据 eligible=true 及其完整哈希。未知或不合格证据不能关闭。";
  }

  function bindForm(id, buttonId, label, task) {
    $(id).addEventListener("submit", (event) => {
      event.preventDefault();
      if ($(buttonId).disabled || !$(id).reportValidity()) return;
      void operate(label, task);
    });
  }
  function bindButton(id, label, task) { $(id).addEventListener("click", () => { if (!$(id).disabled) void operate(label, task); }); }
  function requiredNote(id) {
    const value = $(id).value.trim();
    if (value.length < 10 || value.length > 1500) throw new UIError("依据须填写 10–1500 字符，不能只有空白。");
    return value;
  }
  function requireChecked(id) { if (!$(id).checked) throw new UIError("请先主动勾选对应的人工确认项。"); }
  function changeRisk(id) {
    if (state.riskId !== id) resetApproval("proposal-create-form");
    state.riskId = id;
  }
  function resetProposalForms() {
    ["proposal-review-form", "proposal-execute-form", "proposal-followup-form", "proposal-close-form"].forEach(resetApproval);
  }
  async function saveProposal(epoch, operation, body) {
    const id = state.proposalId;
    const result = adapt.proposal(await api(`/api/proposals/${pathId(id)}/${operation}`, { method: "POST", body, epoch }));
    guard(epoch); upsert("proposals", result, "proposal_id"); state.proposalId = result.proposal_id; resetProposalForms();
  }
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.busy || !$("login-form").reportValidity()) return;
    const candidate = $("token-input").value.trim(); $("token-input").value = "";
    if (!/^[A-Za-z0-9._~+/\-]+=*$/.test(candidate) || candidate.length > 200) { notice("令牌格式无效；请粘贴本地签发的 token，不包含 Bearer 前缀。", "warning"); return; }
    clearSession("正在验证本地身份…"); token = candidate;
    const epoch = sessionEpoch; state.busy = true; $("logout-button").hidden = false; gate();
    try {
      const result = adapt.me(await api("/api/me", { epoch })); guard(epoch);
      if (!result.actor.actor_id || !["operator", "reviewer", "admin"].includes(result.actor.role)) throw new UIError("身份响应不符合约定，不能进入工作台。");
      state.actor = result.actor; state.provider = result.provider;
      await refreshCollections(epoch); guard(epoch); render(); notice("本地身份已验证。请选择 Phase I 数据创建基线，或显式选择已有对象继续。", "positive");
    } catch (error) {
      if (epoch !== sessionEpoch || error instanceof StaleSession) return;
      const message = error instanceof UIError ? error.message : "无法加载工作台。请检查本地服务。";
      clearSession(message); notice(message, "warning");
    } finally { if (epoch === sessionEpoch) { state.busy = false; gate(); } }
  });
  $("logout-button").addEventListener("click", () => { clearSession(); $("token-input").focus(); });
  window.addEventListener("pagehide", () => clearSession());
  bindButton("refresh-button", "刷新工作区", async (epoch) => {
    const identity = adapt.me(await api("/api/me", { epoch })); guard(epoch);
    if (identity.actor.actor_id !== state.actor.actor_id) { clearSession("身份已变化。请重新登录，旧数据已清除。"); throw new StaleSession(); }
    state.actor = identity.actor; state.provider = identity.provider;
    await refreshCollections(epoch); guard(epoch);
    resetApproval("baseline-review-form"); resetProposalForms(); state.audit = null;
    if (state.investigation) {
      const investigation = await api(`/api/investigations/${pathId(state.investigation.investigation_id)}`, { epoch });
      guard(epoch); state.investigation = adapt.investigation(investigation); resetApproval("reconcile-form");
    }
    return "工作区已刷新。所有审批 / 执行 / 关闭确认已重置，请按最新哈希重新核对。";
  });
  bindForm("baseline-create-form", "baseline-create-button", "建立基线草稿", async (epoch) => {
    const result = await api("/api/baselines", { method: "POST", epoch, body: { action_id: actionId(), dataset_id: $("baseline-dataset-select").value } });
    guard(epoch); upsert("baselines", result, "baseline_id"); state.baselineId = result.baseline_id; resetApproval("baseline-review-form");
    return "基线 draft 已创建。尚未批准，请独立核对完整内容与哈希后人工审批。";
  });
  bindForm("baseline-review-form", "baseline-review-button", "提交基线人工审批", async (epoch) => {
    requireChecked("baseline-review-confirm"); const baseline = selectedBaseline();
    const body = { action_id: actionId(), content_hash: baseline.content_hash, decision: $("baseline-decision").value, note: requiredNote("baseline-review-note") };
    const result = await api(`/api/baselines/${pathId(baseline.baseline_id)}/review`, { method: "POST", epoch, body });
    guard(epoch); upsert("baselines", result, "baseline_id"); resetApproval("baseline-review-form");
    return "基线审批已记录。没有自动评估，也没有生产批准。";
  });
  bindForm("evaluation-form", "evaluate-button", "运行确定性统计评估", async (epoch) => {
    const result = await api("/api/evaluations", { method: "POST", epoch, body: { action_id: actionId(), dataset_id: $("monitor-dataset-select").value, baseline_id: $("evaluation-baseline-select").value } });
    guard(epoch); upsert("evaluations", result, "evaluation_id"); state.evaluationId = result.evaluation_id;
    changeRisk("");
    // Risk creation is server-owned. Refresh only its authoritative collection, never infer a risk.
    try {
      const risks = await api("/api/risks", { epoch }); guard(epoch); state.risks = adapt.collection(risks, "risks"); changeRisk(result.risk_id || "");
    } catch (error) {
      guard(epoch); if (error instanceof StaleSession) throw error;
      render(); throw new UIError("统计评估已保存，但风险列表读取失败。请刷新工作区，不要重复评估。");
    }
    return result.risk_id ? "统计评估已保存并关联服务端风险。调查和措施仍需分别主动创建。" : "统计评估已保存；服务端未为此结果创建风险。";
  });
  bindButton("investigation-create-button", "创建只读调查", async (epoch) => {
    const result = await api("/api/investigations", { method: "POST", epoch, body: { action_id: actionId(), risk_id: state.riskId } });
    guard(epoch); state.investigation = adapt.investigation(result); $("investigation-id-input").value = state.investigation.investigation_id || ""; resetApproval("reconcile-form");
    return "调查已创建，尚未运行。请主动点击“主动运行调查”；这不会创建措施。";
  });
  bindForm("investigation-load-form", "investigation-load-button", "读取调查", async (epoch) => {
    const id = $("investigation-id-input").value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(id)) throw new UIError("请输入有效调查 ID，不包含路径或查询参数。");
    const result = await api(`/api/investigations/${pathId(id)}`, { epoch }); guard(epoch); state.investigation = adapt.investigation(result); resetApproval("reconcile-form");
  });
  bindButton("investigation-run-button", "等待有界模型调查（不会自动重试）", async (epoch) => {
    const id = state.investigation.investigation_id;
    const result = await api(`/api/investigations/${pathId(id)}/run`, { method: "POST", body: {}, epoch, timeout: 360000 });
    guard(epoch); state.investigation = adapt.investigation(result); resetApproval("reconcile-form");
    return `调查请求已返回：${statusNames[state.investigation.status] || state.investigation.status}。请分别核查 authoritative 与未验证草稿。`;
  });
  bindButton("investigation-refresh-button", "读取最新调查状态", async (epoch) => {
    const result = await api(`/api/investigations/${pathId(state.investigation.investigation_id)}`, { epoch });
    guard(epoch); state.investigation = adapt.investigation(result); resetApproval("reconcile-form");
  });
  bindButton("investigation-cancel-button", "取消未运行的调查", async (epoch) => {
    const result = await api(`/api/investigations/${pathId(state.investigation.investigation_id)}/cancel`, { method: "POST", body: {}, epoch });
    guard(epoch); state.investigation = adapt.investigation(result); resetApproval("reconcile-form");
  });
  bindForm("reconcile-form", "reconcile-button", "提交未知调用对账", async (epoch) => {
    requireChecked("reconcile-confirm");
    const body = { action_id: actionId(), call_id: $("reconcile-call-select").value, no_result: true, note: requiredNote("reconcile-note") };
    const rawTokens = $("reconcile-tokens").value.trim();
    if (rawTokens !== "") {
      const tokens = Number(rawTokens);
      if (!Number.isInteger(tokens) || tokens < 0 || tokens > 100000) throw new UIError("已知 token 用量必须是 0–100000 的整数；未知请留空。");
      body.tokens = tokens;
    }
    const result = await api(`/api/investigations/${pathId(state.investigation.investigation_id)}/reconcile`, { method: "POST", body, epoch });
    guard(epoch); state.investigation = adapt.investigation(result); resetApproval("reconcile-form");
    return "人工对账已记录。没有自动重试或恢复调查；如需继续，请主动运行。";
  });
  bindForm("proposal-create-form", "proposal-create-button", "创建独立人工措施", async (epoch) => {
    const description = $("proposal-description").value.trim(), expected = $("proposal-expected-result").value.trim();
    if (description.length < 10 || expected.length < 10) throw new UIError("措施描述与预期结果均需至少 10 字符，不能只有空白。");
    const result = adapt.proposal(await api("/api/proposals", { method: "POST", epoch, body: { action_id: actionId(), risk_id: state.riskId, description, expected_result: expected } }));
    guard(epoch); upsert("proposals", result, "proposal_id"); state.proposalId = result.proposal_id; resetApproval("proposal-create-form"); resetProposalForms();
    return "人工措施草稿已创建。未审批、未执行，需要继续独立核查。";
  });
  bindForm("proposal-review-form", "proposal-review-button", "提交措施审批", async (epoch) => {
    requireChecked("proposal-review-confirm");
    await saveProposal(epoch, "review", { action_id: actionId(), content_hash: selectedProposal().content_hash, decision: $("proposal-decision").value, note: requiredNote("proposal-review-note") });
    return "措施审批已记录。批准不代表执行；没有发送设备指令。";
  });
  bindForm("proposal-execute-form", "proposal-execute-button", "记录人工执行确认（非设备执行）", async (epoch) => {
    requireChecked("execution-confirm");
    const observed = new Date($("execution-observed-at").value).getTime();
    if (!Number.isSafeInteger(observed) || observed < 0 || observed > Date.now()) throw new UIError("请填写有效且不晚于当前时间的实际观察时间，不会代填当前时间。");
    await saveProposal(epoch, "execute", { action_id: actionId(), proposal_hash: selectedProposal().content_hash, observed_at_ms: observed, confirmed: true, note: requiredNote("execution-note") });
    return "仅记录了人工执行确认，没有执行设备操作。请选择独立后续数据进行验证。";
  });
  bindForm("proposal-followup-form", "proposal-followup-button", "评估独立后续数据", async (epoch) => {
    await saveProposal(epoch, "followup", { action_id: actionId(), dataset_id: $("followup-dataset-select").value });
    return "后续评估已保存。请核对 eligible、blockers、完整统计与 followup_hash；没有自动关闭。";
  });
  bindForm("proposal-close-form", "proposal-close-button", "提交人工关闭", async (epoch) => {
    requireChecked("proposal-close-confirm");
    await saveProposal(epoch, "close", { action_id: actionId(), followup_hash: adapt.followup(selectedProposal()).followup_hash, note: requiredNote("proposal-close-note") });
    return "措施已按人工审核与绑定的后续证据关闭。这不证明已发现真实根因，也不是生产批准。";
  });
  bindForm("audit-form", "audit-load-button", "读取对象审计", async (epoch) => {
    const id = $("audit-object-input").value.trim();
    const result = await api(`/api/audit/${pathId(id)}`, { epoch }); guard(epoch); state.audit = adapt.collection(result, "audit"); state.auditId = id;
  });

  $("baseline-select").addEventListener("change", () => { state.baselineId = $("baseline-select").value; resetApproval("baseline-review-form"); renderBaseline(); gate(); });
  $("evaluation-select").addEventListener("change", () => {
    state.evaluationId = $("evaluation-select").value;
    const evaluation = selectedEvaluation(); changeRisk(evaluation && evaluation.risk_id || "");
    $("risk-select").value = state.riskId; renderAnalysis($("analysis-content"), evaluation, "evaluation"); renderRisk(); gate();
  });
  $("risk-select").addEventListener("change", () => { changeRisk($("risk-select").value); renderRisk(); renderInvestigation(); gate(); });
  $("proposal-select").addEventListener("change", () => { state.proposalId = $("proposal-select").value; resetProposalForms(); renderProposal(); renderDatasetMeta("followup-dataset-select", "followup-dataset-meta"); gate(); });
  $("audit-object-select").addEventListener("change", () => { $("audit-object-input").value = $("audit-object-select").value; state.audit = null; $("audit-result").replaceChildren(); });
  [["baseline-dataset-select", "baseline-dataset-meta"], ["monitor-dataset-select", "monitor-dataset-meta"], ["followup-dataset-select", "followup-dataset-meta"]].forEach(([select, target]) => $(select).addEventListener("change", () => renderDatasetMeta(select, target)));
  $("workspace").addEventListener("input", () => gate());
  $("workspace").addEventListener("change", () => gate());
  render();
})();
