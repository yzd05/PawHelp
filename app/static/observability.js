"use strict";
(() => {
  const { byId, node, api, busy, number } = window.PawHelpOperations;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const count = (value) =>
    finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 0 }) : "未提供";
  const percent = (value) => (finite(value) ? `${(value * 100).toFixed(1)}%` : "未评估");
  const time = (value) => (value ? new Date(value).toLocaleString("zh-CN") : "未记录");
  const labels = {
    recall_at_k: "Recall@K",
    mrr: "MRR",
    faithfulness: "事实有据率",
    refusal_accuracy: "超纲正确拒答率",
  };
  let groups = [];
  let loadVersion = 0;
  let calibrationVersion = 0;

  // Soft Neo-Brutalist palette with muted comparison tones and spotlight accents.
  const COLORS = {
    ink: "#1f2937",
    rule: "#e5e5e5",
    skyMuted: "#93c5fd",
    amberMuted: "#fcd34d",
    sky: "#38bdf8",
    lime: "#a3e635",
    pink: "#f472b6",
  };

  function semanticTable(headers, rows, label) {
    const t = node("table");
    if (label) t.setAttribute("aria-label", label);
    const headRow = node("tr");
    headers.forEach((title) => {
      const th = node("th", title);
      th.scope = "col";
      headRow.append(th);
    });
    const thead = node("thead");
    thead.append(headRow);
    const tbody = node("tbody");
    rows.forEach((cells) => {
      const tr = node("tr");
      cells.forEach((cell) => tr.append(node("td", cell ?? "未提供")));
      tbody.append(tr);
    });
    t.append(thead, tbody);
    return t;
  }

  function legendItem(label, color, dashed = false) {
    const item = node("span");
    const swatch = node("i", undefined, dashed ? "dashed" : "");
    swatch.setAttribute("aria-hidden", "true");
    if (dashed) swatch.style.borderTopColor = color;
    else swatch.style.background = color;
    item.append(swatch, label);
    return item;
  }

  function cards(id, items) {
    byId(id).replaceChildren(
      ...items.map(([label, value, note, warning, highlight]) => {
        const cls = warning ? " warning" : highlight ? " highlight" : "";
        const card = node("div", undefined, `summary-card${cls}`);
        card.append(node("span", label, "card-label"), node("strong", value), node("p", note));
        return card;
      }),
    );
  }

  function empty(id, text) {
    byId(id).replaceChildren(node("p", text, "empty-report"));
  }

  function plot(id, rows, series, { x, caption, ticks, markers = [] }) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 900 350");
    svg.setAttribute("class", "chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", caption);
    const make = (tag, attrs, text) => {
      const el = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
      if (text !== undefined) el.textContent = text;
      svg.append(el);
      return el;
    };
    make("title", {}, caption);
    for (let i = 0; i <= 4; i++) {
      const y = 25 + (1 - i / 4) * 245;
      make("line", {
        x1: 55,
        y1: y,
        x2: 850,
        y2: y,
        stroke: COLORS.rule,
        "stroke-width": i === 0 ? 2 : 1.5,
        ...(i > 0 ? { "stroke-dasharray": "4 4" } : {}),
      });
      make("text", { x: 8, y: y + 5 }, (i / 4).toFixed(2));
    }
    const legend = node("div", undefined, "legend");
    series.forEach(([key, color, label, primary = false]) => {
      let points = [];
      const flush = () => {
        if (points.length) {
          make("polyline", {
            points: points.join(" "),
            fill: "none",
            stroke: COLORS.ink,
            "stroke-width": primary ? 6 : 4.5,
            "stroke-linejoin": "round",
          });
          make("polyline", {
            points: points.join(" "),
            fill: "none",
            stroke: color,
            "stroke-width": primary ? 3.2 : 2.2,
            "stroke-linejoin": "round",
          });
        }
        points = [];
      };
      rows.forEach((row, i) => {
        if (!finite(row[key])) {
          flush();
          return;
        }
        const px = 55 + x(row, i) * 795;
        const py = 25 + (1 - row[key]) * 245;
        points.push(`${px},${py}`);
        if (rows.length < 25) {
          make("circle", {
            cx: px,
            cy: py,
            r: primary ? 4.5 : 3.5,
            fill: color,
            stroke: COLORS.ink,
            "stroke-width": 2,
          });
        }
      });
      flush();
      legend.append(legendItem(label, color));
    });
    ticks.forEach(([position, label]) =>
      make("text", { x: 55 + position * 795, y: 300, "text-anchor": "middle" }, label),
    );
    markers.forEach(([position, color, label]) => {
      if (!finite(position)) return;
      make("line", {
        x1: 55 + position * 795,
        y1: 25,
        x2: 55 + position * 795,
        y2: 270,
        stroke: color,
        "stroke-width": 2,
        "stroke-dasharray": "5 5",
      });
      legend.append(legendItem(label, color, true));
    });
    byId(id).replaceChildren(svg, legend);
  }

  function renderCosts(costs) {
    const s = costs.summary;
    const partial = s.unknown_usage_calls > 0 || (s.unobserved_requests || 0) > 0;
    const tokens = (row, key) => (row[key] > 0 || row.usage_complete ? count(row[key]) : "未采集");
    const average = (row) =>
      row.total_tokens > 0 || row.usage_complete
        ? `${count(row.average_tokens)}${row.usage_complete ? "" : "（部分）"}`
        : "未知";
    const allPriced =
      costs.items.length > 0 && costs.items.every((item) => finite(item.estimated_cost));
    const totalEstimatedCost = allPriced
      ? costs.items.reduce((sum, item) => sum + item.estimated_cost, 0).toFixed(4)
      : costs.items.length === 0
        ? "0.0000"
        : "待补全";
    cards("costSummary", [
      [
        "最高消耗意图",
        s.highest_intent || "暂无消耗记录",
        partial ? "存在未采集调用，按已知 Token 排序" : "按窗口内累计 Token 排序",
        false,
        Boolean(s.highest_intent && !partial),
      ],
      [
        partial ? "已知 Token（部分）" : "累计 Token 消耗",
        s.total_tokens === 0 && partial ? "未采集" : count(s.total_tokens),
        `近 ${costs.days} 天共 ${count(s.requests)} 次请求执行`,
        partial,
      ],
      [
        "预估模型费用",
        totalEstimatedCost,
        allPriced || costs.items.length === 0
          ? `覆盖 ${count(s.intent_count)} 类意图 · ${count(s.model_calls)} 次调用`
          : "需配置输入/输出单价且用量完整",
        partial,
      ],
      [
        "调用健康状态",
        s.failed_calls > 0 ? `${count(s.failed_calls)} 次失败` : "运行正常",
        `未上报用量 ${count(s.unknown_usage_calls)} 次 · 无记录请求 ${count(s.unobserved_requests || 0)} 次`,
        s.failed_calls > 0 || partial,
      ],
    ]);
    byId("costBars").replaceChildren(
      ...costs.items.map((row) => {
        const bar = node("div", undefined, "cost-bar");
        const caption = node("div", undefined, "bar-caption");
        const track = node("div", undefined, "bar-track");
        const fill = node("div", undefined, "bar-fill");
        caption.append(
          node("strong", `${row.intent} · ${row.requests} 次请求`),
          node(
            "span",
            `${finite(row.token_share) ? percent(row.token_share) : "占比未知"} · ${tokens(row, "total_tokens")} Token` +
              ` · 单均 ${average(row)}${row.unobserved_requests ? ` · ${row.unobserved_requests} 次未采集` : ""}`,
          ),
        );
        fill.style.width = `${finite(row.token_share) ? row.token_share * 100 : 0}%`;
        track.setAttribute("aria-hidden", "true");
        track.append(fill);
        bar.append(caption, track);
        return bar;
      }),
    );
    byId("costs").replaceChildren(
      node("p", costs.scope, "muted"),
      semanticTable(
        [
          "业务意图",
          "请求数",
          "模型调用",
          "输入 Token",
          "输出 Token",
          "已知总 Token",
          "单均 / 占比",
          "失败 / 未上报",
          "无记录请求",
          "预估费用",
        ],
        costs.items.map((r) => [
          r.intent,
          r.requests,
          r.model_calls,
          tokens(r, "input_tokens"),
          tokens(r, "output_tokens"),
          tokens(r, "total_tokens"),
          `${average(r)} / ${finite(r.token_share) ? percent(r.token_share) : "未知"}`,
          `${r.failed_calls} / ${r.unknown_usage_calls}`,
          r.unobserved_requests || 0,
          r.estimated_cost == null ? "不可估算" : r.estimated_cost.toFixed(4),
        ]),
        "业务意图用量与成本明细",
      ),
    );
    const highest = costs.items[0];
    byId("costInterpretation").textContent = [
      costs.truncated ? "窗口请求超过 5000 次，当前展示最新 5000 条样本统计。" : "",
      s.excluded_unlinked_empty_requests
        ? `已自动过滤 ${s.excluded_unlinked_empty_requests} 条无会话关联且零用量的空记录；含真实消耗的历史记录完整保留。`
        : "",
      partial ? "部分调用缺少 Token 回传，占比仅基于已知消耗计算。" : "",
      s.total_tokens > 0
        ? `首要消耗来自「${highest.intent}」（占已知 Token ${percent(highest.token_share)}，单次均值 ${count(highest.average_tokens)} Token），可结合 Langfuse 排查提示词长度与多轮工具调用。`
        : s.requests
          ? "当前窗口暂无已知 Token 消耗，请核查模型响应是否回传 usage 元数据。"
          : "当前统计窗口暂无会话请求记录。",
    ]
      .filter(Boolean)
      .join("\n");
  }

  function renderCalibration(report) {
    byId("calibrationStatus").textContent = report.message;
    if (!report.available) {
      cards("calibrationSummary", [
        [
          "线上生效阈值",
          number(report.active_threshold),
          report.active ? "门控配置校验有效" : "尚未绑定有效校准快照",
          !report.active,
        ],
      ]);
      ["calibrationDistribution", "calibrationChart"].forEach((id) =>
        empty(id, "暂无可复现的标注快照数据。"),
      );
      byId("formula").textContent = "暂无已校验的评分公式";
      byId("calibrationReading").textContent = report.active_reason || report.message;
      byId("calibrationNote").textContent = "请先生成并校验离线相关性标注快照。";
      return;
    }
    const stats = report.validation_at_active || report.validation_at_recommended;
    cards("calibrationSummary", [
      [
        "线上生效阈值",
        report.active ? number(report.active_threshold) : "未生效",
        report.active ? "与当前知识文档及重排模型指纹一致" : "当前环境与快照指纹不一致",
        !report.active,
        Boolean(report.active),
      ],
      [
        "离线推荐阈值",
        number(report.recommended_threshold),
        `训练集 ${report.training_size} 题 · ${report.validation_passed ? "留出验证通过" : "留出验证未通过"}` +
          ` · 区间 ${report.optimal_range.map(number).join("–")}`,
        !report.validation_passed,
      ],
      [
        "验证集相关放行率",
        percent(stats.relevant_pass_rate),
        `独立验证集 ${report.validation_size} 题（按${report.active ? "生效" : "推荐"}阈值）`,
      ],
      [
        "验证集无关误放率",
        percent(stats.irrelevant_pass_rate),
        `误放 ${stats.unsafe_accepted} 条无关样本`,
        stats.unsafe_accepted > 0,
      ],
    ]);
    byId("calibrationDistribution").replaceChildren(
      semanticTable(
        ["样本分组", "题数", "最低分", "P25", "中位数", "P75", "最高分"],
        report.distributions.map((r) => [
          r.group,
          r.count,
          ...["min", "p25", "median", "p75", "max"].map((k) => number(r[k])),
        ]),
        "证据相关性得分分位数分布",
      ),
    );
    plot(
      "calibrationChart",
      report.scan,
      [
        ["relevant_pass_rate", COLORS.lime, "相关证据放行率", true],
        ["irrelevant_pass_rate", COLORS.pink, "无关证据误放率", true],
      ],
      {
        x: (r) => r.threshold,
        caption: "训练集证据相关性阈值扫描曲线（纵轴为样本比例，横轴为候选阈值）",
        ticks: [0, 0.2, 0.4, 0.6, 0.8, 1].map((t) => [t, `阈值 ${t.toFixed(1)}`]),
        markers: [
          [report.recommended_threshold, COLORS.ink, `推荐 ${number(report.recommended_threshold)}`],
          [report.active_threshold, COLORS.sky, `生效 ${number(report.active_threshold)}`],
        ],
      },
    );
    const train = report.training_at_active || report.training_at_recommended;
    byId("calibrationReading").textContent =
      `按${report.active ? "线上生效" : "离线推荐"}阈值统计：训练集相关放行 ${percent(train.relevant_pass_rate)}、` +
      `无关误放 ${percent(train.irrelevant_pass_rate)}（放行样本中无关占比 ${percent(train.unsafe_share)}）；` +
      `独立验证集相关放行 ${percent(stats.relevant_pass_rate)}、无关误放 ${percent(stats.irrelevant_pass_rate)}。`;
    byId("formula").textContent = report.formula;
    byId("calibrationNote").textContent = [
      report.active
        ? "门控指纹校验通过：当前知识文档与重排配置匹配。"
        : `门控未激活原因：${report.active_reason}`,
      `标注复核来源：${report.reviewer || "未记录"}`,
      report.scope_limit,
    ]
      .filter(Boolean)
      .join("\n");
  }

  function metricCell(value, delta) {
    const td = node("td", undefined, "metric");
    td.append(node("span", number(value)));
    if (finite(delta)) {
      td.append(
        node(
          "span",
          `${Math.abs(delta) < 1e-9 ? " →" : delta < 0 ? " ↓" : " ↑"} ${delta > 0 ? "+" : ""}${delta.toFixed(4)}`,
          delta < -1e-9 ? "delta-down" : delta > 1e-9 ? "delta-up" : "",
        ),
      );
    }
    return td;
  }

  function renderTrend() {
    const group = groups.find((g) => g.key === byId("group").value);
    if (!group) {
      cards("trendSummary", [["已记录轮次", "0", "尚无历史评估记录"]]);
      empty("trend", "完成至少两轮同配置完整评估后展示趋势曲线。");
      empty("runs", "暂无评估轮次记录。");
      byId("trendInterpretation").textContent = "暂无可对比的阶段评测记录。";
      return;
    }
    const latest = group.latest;
    cards("trendSummary", [
      [
        "本组记录轮次",
        String(group.rows.length),
        `含 ${group.complete_count} 轮完整评估（上限保留最近 100 轮）`,
      ],
      [
        "最新事实有据率",
        number(latest?.values.faithfulness),
        latest ? `轮次 #${latest.id} · ${time(latest.created_at)}` : "暂无完整轮次",
        false,
        Boolean(latest),
      ],
      [
        "最新排序 MRR",
        number(latest?.values.mrr),
        `Recall@K ${number(latest?.values.recall_at_k)}`,
      ],
      [
        "环比指标回落",
        latest?.previous_id ? `${group.declining.length} 项` : "首轮基线",
        latest?.previous_id ? `对比同配置轮次 #${latest.previous_id}` : "满两轮完整评估后计算环比",
        group.declining.length > 0,
      ],
    ]);
    const valid = group.rows.filter((r) => r.comparable).reverse();
    if (valid.length < 2) {
      empty("trend", "当前同配置完整轮次不足 2 轮，暂不连线（缺失指标不补零）。");
    } else {
      plot(
        "trend",
        valid.map((r) => r.values),
        [
          ["recall_at_k", COLORS.skyMuted, "Recall@K", false],
          ["mrr", COLORS.lime, "MRR", true],
          ["faithfulness", COLORS.pink, "事实有据率", true],
          ["refusal_accuracy", COLORS.amberMuted, "超纲正确拒答率", false],
        ],
        {
          x: (_, i) => i / (valid.length - 1),
          caption: "同配置完整评估轮次指标趋势曲线",
          ticks: valid
            .map((r, i) => [i / (valid.length - 1), `#${r.id}`])
            .filter((_, i) => i === 0 || i === valid.length - 1 || i % Math.ceil(valid.length / 8) === 0),
        },
      );
    }
    const t = semanticTable(
      ["轮次 / 时间", "触发方式", "题数", ...Object.values(labels), "完成状态"],
      [],
      "历史评测轮次明细",
    );
    const body = t.querySelector("tbody");
    group.rows.forEach((r) => {
      const tr = node("tr");
      tr.append(
        node("td", `#${r.id} · ${time(r.created_at)}`),
        node("td", r.triggered_by),
        node("td", r.dataset_size),
      );
      Object.keys(labels).forEach((k) => tr.append(metricCell(r.values[k], r.deltas[k])));
      tr.append(
        node(
          "td",
          r.comparable
            ? `完整${r.previous_id ? ` · 对比 #${r.previous_id}` : " · 首轮基线"}`
            : "部分完成（不参与环比）",
        ),
      );
      body.append(tr);
    });
    byId("runs").replaceChildren(t);
    byId("trendInterpretation").textContent = !latest?.previous_id
      ? "当前尚无可对比的前序同配置完整轮次。"
      : group.declining.length
        ? `轮次 #${latest.id} 对比 #${latest.previous_id}：${group.declining.map((k) => labels[k]).join("、")} 出现回落，建议结合逐题报告与近期知识文档变更排查。`
        : `轮次 #${latest.id} 对比 #${latest.previous_id}：各项已评估核心指标保持稳定或提升。`;
  }

  async function loadObservations() {
    const version = ++loadVersion;
    const calVersion = ++calibrationVersion;
    const result = await api(`/api/observability?days=${byId("days").value || 7}`);
    if (version !== loadVersion) return;
    const c = result.configuration;
    byId("configuration").textContent =
      `Langfuse 追踪：${result.costs.langfuse_enabled ? "已启用" : "未启用"}` +
      ` · 知识归纳周期：${c.flywheel_interval_seconds ? `${c.flywheel_interval_seconds}s` : "手动"}` +
      ` · 自动评估周期：${c.evaluation_interval_seconds ? `${c.evaluation_interval_seconds}s` : "手动"}` +
      ` · 抽样每桶 ${c.sample_per_bucket} 题（单轮上限 ${c.max_model_calls} 次调用）`;
    try {
      const url = new URL(c.langfuse_url);
      if (["http:", "https:"].includes(url.protocol)) byId("langfuse").href = url.href;
    } catch {
      /* Keep the safe default link. */
    }
    renderCosts(result.costs);
    if (calVersion === calibrationVersion) renderCalibration(result.calibration);
    const oldKey = byId("group").value;
    groups = result.trend.groups;
    byId("group").replaceChildren(
      ...groups.map((g, i) => {
        const option = node(
          "option",
          `配置组 ${i + 1} · ${g.rows[0].dataset_size} 题 · 指纹 ${g.key.slice(0, 8)} · ${g.complete_count} 轮完整`,
        );
        option.value = g.key;
        return option;
      }),
    );
    if (groups.some((g) => g.key === oldKey)) byId("group").value = oldKey;
    renderTrend();
    byId("status").textContent =
      result.last_evaluation_error ||
      `数据已同步 · ${time(result.costs.generated_at)} · 累计记录 ${result.trend.total_runs} 轮评估`;
  }

  byId("load").addEventListener("click", () => busy(byId("load"), loadObservations));
  byId("days").addEventListener("change", () => busy(byId("load"), loadObservations));
  byId("group").addEventListener("change", renderTrend);
  byId("analyzeCalibration").addEventListener("click", () =>
    busy(byId("analyzeCalibration"), async () => {
      const version = ++calibrationVersion;
      byId("calibrationStatus").textContent = "正在重算本地标注快照…";
      try {
        const report = await api("/api/observability/calibration/analyze", "POST");
        if (version === calibrationVersion) renderCalibration(report);
      } catch (error) {
        if (version === calibrationVersion) {
          byId("calibrationStatus").textContent = `重算失败：${error.message}`;
        }
        throw error;
      }
    }),
  );
  byId("evaluate").addEventListener("click", () => {
    if (
      window.confirm(
        "小批量回归评估将调用外部模型并产生 Token 费用，结果写入独立评估轮次且不覆盖主报告。是否继续？",
      )
    ) {
      busy(byId("evaluate"), async () => {
        await api("/api/observability/evaluate", "POST");
        await loadObservations();
      });
    }
  });
  busy(byId("load"), loadObservations);
})();
