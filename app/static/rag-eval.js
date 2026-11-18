"use strict";
(() => {
  const adminHeaders = (extra = {}) =>
    window.PawHelpOperations
      ? window.PawHelpOperations.adminHeaders(extra)
      : { "X-PawHelp-Local-Admin": "1", ...extra };

  async function responseError(response, fallback) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      /* keep fallback */
    }
    const detail = payload?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message;
    return fallback;
  }

  const STRATEGIES = ["dense", "bm25", "hybrid", "hybrid_rerank"];
  const STRATEGY_LABELS = {
    dense: "纯向量",
    bm25: "纯 BM25",
    hybrid: "混合 RRF",
    hybrid_rerank: "混合 + Rerank",
  };
  const STRATEGY_SWATCHES = {
    dense: "swatch-dense",
    bm25: "swatch-bm25",
    hybrid: "swatch-hybrid",
    hybrid_rerank: "swatch-rerank",
  };
  const BUCKETS = [
    ["A_policy", "售后与退换政策"],
    ["B_model", "商品型号与规格"],
    ["C_colloquial", "口语化提问"],
    ["E_multi", "多要点组合"],
    ["F_identifier", "故障码与配件号"],
    ["overall", "全部可答题"],
  ];
  const QUERY_TYPE_LABELS = {
    A_policy: "售后与退换政策",
    B_model: "商品型号与规格",
    C_colloquial: "口语化提问",
    D_absent: "超纲拒答题",
    E_multi: "多要点组合",
    F_identifier: "故障码与配件号",
    overall: "全部可答题",
  };
  const DIFFICULTY_LABELS = {
    easy: "基础",
    medium: "中等",
    hard: "困难",
  };
  const ANSWERABLE_BUCKETS = BUCKETS.filter(([bucket]) => bucket !== "overall");

  const statusBox = document.querySelector("#status");
  const emptyState = document.querySelector("#emptyState");
  const rerunCopy = document.querySelector("#rerunCopy");
  const runMeta = document.querySelector("#runMeta");
  const metrics = document.querySelector("#metrics");
  const strategyLegend = document.querySelector("#strategyLegend");
  const bucketChart = document.querySelector("#bucketChart");
  const retrievalReading = document.querySelector("#retrievalReading");
  const retrievalMetricHelp = document.querySelector("#retrievalMetricHelp");
  const faithBars = document.querySelector("#faithBars");
  const productionBreakdown = document.querySelector("#productionBreakdown");
  const productionBreakdownTitle = document.querySelector("#productionBreakdownTitle");
  const generationMetricHelp = document.querySelector("#generationMetricHelp");
  const generationLegend = document.querySelector("#generationLegend");
  const generationChart = document.querySelector("#generationChart");
  const fullGeneration = document.querySelector("#fullGeneration");
  const issueFilters = document.querySelector("#issueFilters");
  const reviewNotice = document.querySelector("#reviewNotice");
  const issueList = document.querySelector("#issueList");
  const issuePagination = document.querySelector("#issuePagination");
  const issueCount = document.querySelector("#issueCount");
  const summaryTable = document.querySelector("#summaryTable");
  const tableReading = document.querySelector("#tableReading");
  const casePicker = document.querySelector("#casePicker");
  const caseSummary = document.querySelector("#caseSummary");
  const strategyGrid = document.querySelector("#strategyGrid");
  const rerunButton = document.querySelector("#rerunButton");
  const rerunButtons = [rerunButton, ...document.querySelectorAll(".rerun-trigger")];

  let report = null;
  let counts = null;
  let selectedMetric = "mrr";
  let selectedGenerationMetric = "answer_coverage";
  let selectedIssueFilter = "all";
  let selectedIssuePage = 1;
  let faithReviews = new Map();
  let reviewLoadError = "正在读取人工复核记录…";
  const ISSUE_PAGE_SIZE = 10;

  const percent = (value) => (value == null ? "未评测" : `${Math.round(Number(value) * 100)}%`);
  const METRIC_NAMES = {
    mrr: "MRR",
    recall_at_k: "Recall@5",
    evidence_coverage: "要点命中率",
    answer_coverage: "要点覆盖率",
    faithfulness: "事实有据率",
  };
  const RETRIEVAL_HELP = {
    mrr: "标准相关知识块的平均倒数排名，命中位置越靠前得分越高（满分 1.00）。",
    recall_at_k: "前 5 条召回知识块对标准相关依据组的覆盖率，越高越不易漏召回。",
    evidence_coverage: "前 10 条召回知识块原文对标准答案核心要点的包含率，用于定位检索侧缺失。",
  };
  const GENERATION_HELP = {
    answer_coverage: "裁判模型逐项核对最终回答对标准答案要点的覆盖比例，仅统计成功生成的可答题。",
    faithfulness: "裁判模型核查混合 + Rerank 最终回答的事实陈述是否均有当轮召回知识块支撑（合规拒答记 1.00）。",
  };

  function caseCounts() {
    if (counts) return counts;
    const bucketOf = new Map();
    const answerableIds = new Set();
    const refusalIds = new Set();
    for (const row of report?.cases || []) {
      bucketOf.set(row.case_id, row.query_type);
      (row.expected_answerable === false ? refusalIds : answerableIds).add(row.case_id);
    }
    const byBucket = {};
    for (const bucket of bucketOf.values()) byBucket[bucket] = (byBucket[bucket] || 0) + 1;
    const dataset = report?.dataset || {};
    const answerable = dataset.answerable_cases ?? answerableIds.size;
    const refusal = dataset.refusal_cases ?? refusalIds.size;
    byBucket.overall = answerable;
    counts = { byBucket, answerable, refusal, total: dataset.total_cases ?? answerable + refusal };
    return counts;
  }

  function bucketCaption(bucket, label) {
    const caption = element("div", "bucket-label", label);
    const total = caseCounts().byBucket[bucket];
    if (total != null) caption.appendChild(element("small", "", `${total} 题`));
    return caption;
  }

  function scoredMetric(strategy, bucket, metric) {
    const values =
      bucket === "overall"
        ? report.strategies?.[strategy]
        : report.by_query_type?.[`${strategy}:${bucket}`];
    return values?.[metric] == null ? null : Number(values[metric]);
  }

  function setStatus(message, error = false, baseline = false) {
    statusBox.textContent = message;
    statusBox.className = `notice${error ? " error" : baseline ? " baseline" : ""}`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function buildLegendNodes() {
    return STRATEGIES.map((strategy) => {
      const item = element("span", strategy === "hybrid_rerank" ? "is-primary" : "");
      const swatch = element("i", STRATEGY_SWATCHES[strategy]);
      swatch.setAttribute("aria-hidden", "true");
      item.append(swatch, document.createTextNode(STRATEGY_LABELS[strategy]));
      return item;
    });
  }

  function buildChartCanvas(metricKey) {
    const canvas = element("div", "chart-canvas");
    const grid = element("div", "chart-grid");
    grid.setAttribute("aria-hidden", "true");
    for (const tick of ["1.00", "0.75", "0.50", "0.25", "0.00"]) {
      const line = element("div", `chart-grid-line${tick === "0.00" ? " is-zero" : ""}`);
      line.appendChild(element("span", "", tick));
      grid.appendChild(line);
    }
    const groups = element("div", "bucket-groups");
    for (const [bucket, label] of BUCKETS) {
      const group = element("div", `bucket-group${bucket === "overall" ? " is-overall" : ""}`);
      const bars = element("div", "bucket-bars");
      for (const strategy of STRATEGIES) {
        const value = scoredMetric(strategy, bucket, metricKey);
        const bar = element("div", "bucket-bar");
        bar.style.height = `${Math.max(1.5, (value ?? 0) * 100)}%`;
        bar.title =
          `${STRATEGY_LABELS[strategy]} · ${label} · ${METRIC_NAMES[metricKey]}：` +
          `${value == null ? "未评测" : value.toFixed(3)}`;
        bar.appendChild(element("b", "", value == null ? "—" : value.toFixed(2)));
        bars.appendChild(bar);
      }
      group.append(bars, bucketCaption(bucket, label));
      groups.appendChild(group);
    }
    canvas.append(grid, groups);
    return canvas;
  }

  function reportGeneratedStrategies() {
    const recorded = report.run?.generation_strategies;
    return new Set(recorded === "all" ? STRATEGIES : Array.isArray(recorded) ? recorded : []);
  }

  function hasSemanticCoverage() {
    return (report.cases || []).some((row) =>
      Object.prototype.hasOwnProperty.call(row, "answer_coverage"),
    );
  }

  function renderMeta() {
    const dataset = report.dataset || {};
    const run = report.run || {};
    const reportedCases = new Set((report.cases || []).map((row) => row.case_id)).size;
    const resultCount = (report.cases || []).length;
    const judgedRows = (report.cases || []).filter(
      (row) =>
        row.strategy === "hybrid_rerank" &&
        (row.faithfulness_status === "completed" ||
          (row.faithfulness_status == null && row.faithful != null)),
    );
    const suspectedCases = judgedRows.filter((row) => row.faithful === false).length;
    const totalStepErrors =
      Number(run.generation_failed ?? 0) +
      Number(run.coverage_failed ?? 0) +
      Number(run.faithfulness_failed ?? 0);
    const failedSteps =
      run.generation_failed == null
        ? "旧报告未记录"
        : run.generation_enabled === false
          ? "仅运行检索评估"
          : totalStepErrors === 0
            ? "0 题报错（全流程正常）"
            : `生成回答：${run.generation_failed} 题\n要点核对：${run.coverage_failed ?? 0} 题\n` +
              `事实核查：${run.faithfulness_failed ?? 0} 题`;
    const savedIssues =
      run.ledger_failed == null
        ? "旧报告未记录"
        : run.generation_enabled === false || !judgedRows.length
          ? "本轮未运行事实核查"
          : Number(run.ledger_failed) > 0
            ? `检出 ${suspectedCases} 题 · 写入异常 ${run.ledger_failed} 题`
            : `检出 ${suspectedCases} 题（支持人工复核）`;
    const values = [
      ["评测题集", `${dataset.total_cases || reportedCases} 题`, `${caseCounts().answerable} 道可答题 · ${caseCounts().refusal} 道超纲题`],
      ["策略结果", `${resultCount.toLocaleString("zh-CN")} 条`, "4 条检索策略同题对照"],
      ["评测语料范围", `${run.knowledge_chunks ?? "—"} 个知识块`, run.corpus_sources?.length ? `${run.corpus_sources.length} 篇知识文档同步切分` : undefined],
      ["向量 / 重排模型", `${run.embedding_model || "未记录"} · ${run.reranker_model || "未记录"}`],
      ["裁判模型", run.judge_model || (run.generation_enabled === false ? "未运行" : "未记录")],
      ["评估步骤报错", failedSteps, "统计执行异常题数，非答错题数"],
      ["疑似编造回答", savedIssues, "仅登记待复核的事实争议样本"],
      ["报告生成时间", run.generated_at ? new Date(run.generated_at).toLocaleString("zh-CN") : "历史报告"],
    ];
    runMeta.replaceChildren(
      ...values.map(([label, value, help]) => {
        const card = element("article", "run-card");
        card.append(element("span", "", label), element("strong", "", value));
        if (help) card.appendChild(element("small", "", help));
        return card;
      }),
    );
  }

  function renderBucketChart() {
    retrievalMetricHelp.textContent = RETRIEVAL_HELP[selectedMetric];
    bucketChart.replaceChildren(buildChartCanvas(selectedMetric));
    const ranked = STRATEGIES.filter(
      (strategy) => scoredMetric(strategy, "overall", selectedMetric) != null,
    ).sort(
      (left, right) =>
        scoredMetric(right, "overall", selectedMetric) -
        scoredMetric(left, "overall", selectedMetric),
    );
    retrievalReading.replaceChildren();
    if (ranked.length) {
      const winner = ranked[0];
      const score = scoredMetric(winner, "overall", selectedMetric);
      retrievalReading.append(
        element("span", "winner-tag", "最优策略"),
        document.createTextNode(
          ` ${STRATEGY_LABELS[winner]} · 总体 ${METRIC_NAMES[selectedMetric]} ${score.toFixed(3)}` +
            `（共 ${caseCounts().answerable} 道可答题）`,
        ),
      );
    } else {
      retrievalReading.textContent = "本轮未记录该指标；空值不计 0 分。";
    }
    document.querySelectorAll("#metricTabs [data-metric]").forEach((button) => {
      const active = button.dataset.metric === selectedMetric;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function renderGenerationQuality() {
    const generationEnabled = report.run?.generation_enabled !== false;
    const generated = reportGeneratedStrategies();
    const allStrategiesGenerated = STRATEGIES.every((strategy) => generated.has(strategy));
    const showComparison =
      generationEnabled &&
      allStrategiesGenerated &&
      hasSemanticCoverage() &&
      selectedGenerationMetric === "answer_coverage";
    generationChart.hidden = !showComparison;
    productionBreakdown.hidden = !generationEnabled || showComparison;
    generationMetricHelp.textContent = GENERATION_HELP[selectedGenerationMetric];
    if (showComparison) {
      generationLegend.replaceChildren(...buildLegendNodes());
    } else {
      const overallVal = scoredMetric("hybrid_rerank", "overall", selectedGenerationMetric);
      generationLegend.textContent = !generationEnabled
        ? "本轮仅运行检索评估，未生成回答。"
        : selectedGenerationMetric === "faithfulness"
          ? `评估范围：混合 + Rerank 线上路线 · 总体事实有据率 ${overallVal == null ? "—" : overallVal.toFixed(3)}`
          : hasSemanticCoverage()
            ? `评估范围：混合 + Rerank 线上路线 · 总体要点覆盖率 ${overallVal == null ? "—" : overallVal.toFixed(3)}`
            : "旧口径报告未包含要点覆盖率，缺失项不计 0 分。";
    }
    if (!generationEnabled) {
      generationChart.replaceChildren();
      faithBars.replaceChildren();
      document.querySelectorAll("[data-generation-metric]").forEach((button) => {
        const active = button.dataset.generationMetric === selectedGenerationMetric;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      return;
    }
    if (showComparison) {
      generationChart.replaceChildren(buildChartCanvas("answer_coverage"));
    }
    productionBreakdownTitle.textContent = `混合 + Rerank · 各题型${METRIC_NAMES[selectedGenerationMetric]}`;
    faithBars.replaceChildren(
      ...BUCKETS.map(([bucket, label]) => {
        const value = scoredMetric("hybrid_rerank", bucket, selectedGenerationMetric);
        const row = element("div", `faith-row${bucket === "overall" ? " is-overall" : ""}`);
        const track = element("div", "faith-track");
        track.setAttribute("aria-hidden", "true");
        const fill = element("i");
        if (selectedGenerationMetric === "answer_coverage") fill.className = "answer-coverage";
        fill.style.width = value == null ? "0%" : `${Math.max(0, Math.min(100, Number(value) * 100))}%`;
        track.appendChild(fill);
        row.append(
          element("span", "", `${label}（${caseCounts().byBucket[bucket] || 0} 题）`),
          track,
          element("span", "", value == null ? "—" : value.toFixed(3)),
        );
        return row;
      }),
    );
    document.querySelectorAll("[data-generation-metric]").forEach((button) => {
      const active = button.dataset.generationMetric === selectedGenerationMetric;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function issueKinds(row) {
    const generationEnabled = report.run?.generation_enabled !== false;
    const kinds = [];
    if (row.faithful === false) kinds.push("hallucination");
    if (row.expected_answerable !== false && Number(row.evidence_coverage ?? 1) < 1) kinds.push("evidence");
    const answerCoverage = row.coverage_status == null ? row.answer_completeness : row.answer_coverage;
    if (
      generationEnabled &&
      row.generation_status !== "failed" &&
      row.answer &&
      row.expected_answerable !== false &&
      !row.refused &&
      Number(answerCoverage ?? 1) < 1
    ) {
      kinds.push("completeness");
    }
    if (
      generationEnabled &&
      row.refused != null &&
      ((row.expected_answerable !== false && row.refused) ||
        (row.expected_answerable === false && !row.refused))
    ) {
      kinds.push("refusal");
    }
    return kinds;
  }

  function issueRows() {
    return (report.cases || []).filter((row) => row.strategy === "hybrid_rerank" && issueKinds(row).length);
  }

  function faithReviewBox(row) {
    const box = element("div", "review-box");
    const review = faithReviews.get(row.case_id);
    if (!review?.available) {
      box.append(element("p", "", `人工复核：暂不可提交。${review?.message || reviewLoadError}`));
      return box;
    }
    box.append(element("p", "", `人工复核状态：${review.status}（独立存档，不改动模型评估分数）`));
    box.append(
      element(
        "p",
        "",
        "已解决：问题已实际修复；无需解决：人工确认属于裁判误判或不影响回答。请在理由中说明依据。",
      ),
    );
    if (review.resolution) box.append(element("p", "", `上次处置理由：${review.resolution}`));
    const form = element("form", "review-form");
    form.dataset.reviewCase = row.case_id;
    const label = element("label", "", "处置理由（已解决／无需解决必填，最多 300 字）");
    const reason = element("textarea", "review-reason");
    reason.name = "resolution";
    reason.maxLength = 300;
    reason.value = review.resolution || "";
    label.appendChild(reason);
    form.appendChild(label);
    const buttons = element("div", "review-buttons");
    for (const status of ["已解决", "无需解决", ...(review.status === "未解决" ? [] : ["未解决"])]) {
      const button = element("button", "mini-button", status === "未解决" ? "退回未解决" : status);
      button.type = "submit";
      button.dataset.reviewStatus = status;
      buttons.appendChild(button);
    }
    form.appendChild(buttons);
    form.appendChild(element("p", "review-feedback"));
    box.appendChild(form);
    return box;
  }

  function renderIssues(expandedCaseId = null) {
    const allIssues = issueRows();
    const filters = [
      ["all", "全部"],
      ["hallucination", "事实无依据"],
      ["evidence", "检索要点缺失"],
      ["completeness", "回答要点遗漏"],
      ["refusal", "拒答边界偏差"],
    ];
    issueFilters.replaceChildren(
      ...filters.map(([key, label]) => {
        const count =
          key === "all"
            ? allIssues.length
            : allIssues.filter((row) => issueKinds(row).includes(key)).length;
        const active = selectedIssueFilter === key;
        const button = element(
          "button",
          `issue-filter${active ? " active" : ""}`,
          `${label} ${count}`,
        );
        button.type = "button";
        button.dataset.issueFilter = key;
        button.setAttribute("aria-pressed", String(active));
        return button;
      }),
    );
    const issues =
      selectedIssueFilter === "all"
        ? allIssues
        : allIssues.filter((row) => issueKinds(row).includes(selectedIssueFilter));
    const totalPages = Math.max(1, Math.ceil(issues.length / ISSUE_PAGE_SIZE));
    selectedIssuePage = Math.min(Math.max(selectedIssuePage, 1), totalPages);
    const pageStart = (selectedIssuePage - 1) * ISSUE_PAGE_SIZE;
    const pageIssues = issues.slice(pageStart, pageStart + ISSUE_PAGE_SIZE);
    issueCount.textContent = `当前 ${issues.length} 题 / 共 ${allIssues.length} 题`;
    const flagged = allIssues.filter((row) => row.faithful === false);
    const reviewed = flagged
      .map((row) => faithReviews.get(row.case_id))
      .filter((state) => state?.available);
    const statusCount = (status) => reviewed.filter((state) => state.status === status).length;
    reviewNotice.textContent =
      reviewLoadError ||
      `共 ${flagged.length} 道事实争议题支持人工处置：未解决 ${statusCount("未解决")}、` +
        `已解决 ${statusCount("已解决")}、无需解决 ${statusCount("无需解决")}` +
        `${reviewed.length < flagged.length ? `；另有 ${flagged.length - reviewed.length} 道记录与当前快照未对齐，暂不可提交` : ""}。`;
    issueList.replaceChildren(
      ...pageIssues.map((row) => {
        const card = element("details", "issue");
        card.open = row.case_id === expandedCaseId;
        const summary = element("summary");
        summary.append(element("span", "issue-title", `${row.case_id} · ${row.query}`));
        for (const kind of issueKinds(row)) {
          const labels = {
            hallucination: "疑似编造",
            evidence: "检索要点缺失",
            completeness: "回答要点遗漏",
            refusal: "拒答偏差",
          };
          summary.append(element("span", `chip${kind === "hallucination" ? " bad" : ""}`, labels[kind]));
        }
        const body = element("div", "issue-body");
        body.append(
          element(
            "p",
            "",
            `${METRIC_NAMES.evidence_coverage}：${percent(row.evidence_coverage)} · ` +
              `${METRIC_NAMES.answer_coverage}：${percent(row.answer_coverage)} · ` +
              `${METRIC_NAMES.faithfulness}：${percent(row.faithfulness)}`,
          ),
          element(
            "p",
            "",
            `事实核查说明：${row.faithfulness_reason || "本题未返回核查说明"}`,
          ),
          element("div", "answer", row.answer || (row.refused ? "本题已触发拒答" : "未生成回答")),
        );
        const citationCount = (row.citations || []).length;
        body.appendChild(
          element(
            "p",
            "",
            `召回链路：共 ${citationCount} 个知识块 · 标准相关知识块：${(row.relevant_chunk_ids || []).join(", ") || "无（超纲拒答题）"}`,
          ),
        );
        if (row.faithful === false) body.appendChild(faithReviewBox(row));
        const actions = element("div", "issue-actions");
        const inspect = element("button", "mini-button", "查看四路证据");
        inspect.type = "button";
        inspect.dataset.inspectCase = row.case_id;
        actions.appendChild(inspect);
        body.appendChild(actions);
        card.append(summary, body);
        return card;
      }),
    );
    if (!issues.length) issueList.appendChild(element("div", "empty", "当前筛选条件下暂无待排查样本。"));
    if (!issues.length) {
      issuePagination.replaceChildren();
      return;
    }
    const status = element(
      "span",
      "page-status",
      `第 ${selectedIssuePage} / ${totalPages} 页 · ` +
        `${pageStart + 1}-${Math.min(pageStart + ISSUE_PAGE_SIZE, issues.length)} / ${issues.length} 条`,
    );
    const buttons = element("div", "page-buttons");
    const previous = element("button", "page-button", "上一页");
    previous.type = "button";
    previous.dataset.issuePage = String(selectedIssuePage - 1);
    previous.disabled = selectedIssuePage === 1;
    buttons.appendChild(previous);
    for (let page = 1; page <= totalPages; page += 1) {
      const button = element(
        "button",
        `page-button${page === selectedIssuePage ? " active" : ""}`,
        String(page),
      );
      button.type = "button";
      button.dataset.issuePage = String(page);
      button.setAttribute("aria-label", `第 ${page} 页`);
      button.setAttribute("aria-current", page === selectedIssuePage ? "page" : "false");
      buttons.appendChild(button);
    }
    const next = element("button", "page-button", "下一页");
    next.type = "button";
    next.dataset.issuePage = String(selectedIssuePage + 1);
    next.disabled = selectedIssuePage === totalPages;
    buttons.appendChild(next);
    issuePagination.replaceChildren(status, buttons);
  }

  function renderFullTable() {
    const bestMrr = Math.max(...STRATEGIES.map((strategy) => Number(report.strategies?.[strategy]?.mrr || 0)));
    const generatedStrategies = reportGeneratedStrategies();
    const metricCell = (value, blank = false, asPercent = false) =>
      element(
        "td",
        "table-number",
        blank || value == null
          ? "—"
          : asPercent
            ? `${(Number(value) * 100).toFixed(2)}%`
            : Number(value).toFixed(3),
      );
    summaryTable.replaceChildren(
      ...STRATEGIES.map((strategy) => {
        const values = report.strategies?.[strategy] || {};
        const generated = generatedStrategies.has(strategy);
        const isBest = Number(values.mrr || 0) === bestMrr;
        const row = element("tr", isBest ? "best-row" : "");
        const rowHead = element("th", "", STRATEGY_LABELS[strategy]);
        rowHead.scope = "row";
        row.append(
          rowHead,
          metricCell(scoredMetric(strategy, "A_policy", "mrr")),
          metricCell(scoredMetric(strategy, "B_model", "mrr")),
          metricCell(scoredMetric(strategy, "C_colloquial", "mrr")),
          metricCell(scoredMetric(strategy, "E_multi", "mrr")),
          metricCell(scoredMetric(strategy, "F_identifier", "mrr")),
          metricCell(values.mrr),
          metricCell(values.recall_at_k),
          metricCell(values.evidence_coverage),
          metricCell(values.answer_coverage, !generated),
          metricCell(values.faithfulness, !generated || strategy !== "hybrid_rerank"),
          metricCell(values.refusal_accuracy, !generated, true),
          metricCell(values.false_refusal_rate, !generated, true),
        );
        return row;
      }),
    );
    const productionBuckets = ANSWERABLE_BUCKETS.map(([bucket, label]) => [
      label,
      scoredMetric("hybrid_rerank", bucket, "mrr"),
    ]).filter(([, value]) => value != null);
    const lowest = productionBuckets.sort((left, right) => left[1] - right[1])[0];
    tableReading.textContent =
      `浅绿高亮行标记总体 MRR 最优路线。` +
      `${lowest ? `混合 + Rerank 当前最低分桶为「${lowest[0]}」（MRR ${lowest[1].toFixed(3)}）。` : ""}` +
      `超纲正确拒答率分母为 ${caseCounts().refusal} 道库外题，库内误拒率分母为 ${caseCounts().answerable} 道可答题。`;
  }

  function renderSummary() {
    const production = report.strategies?.hybrid_rerank || {};
    const ranked = STRATEGIES.filter((strategy) => report.strategies?.[strategy])
      .map((strategy) => [strategy, Number(report.strategies[strategy].mrr || 0)])
      .sort((left, right) => right[1] - left[1]);
    const [bestStrategy, bestMrr] = ranked[0] || ["hybrid_rerank", 0];
    const generationEnabled = report.run?.generation_enabled !== false;
    const answerableRows = (report.cases || []).filter(
      (row) =>
        row.strategy === "hybrid_rerank" && row.expected_answerable !== false && row.refused != null,
    );
    const refusedAnswerable = answerableRows.filter((row) => row.refused).length;
    const refusalRows = (report.cases || []).filter(
      (row) =>
        row.strategy === "hybrid_rerank" && row.expected_answerable === false && row.refused != null,
    );
    const correctlyRefused = refusalRows.filter((row) => row.refused).length;
    const falseRefusalRate =
      production.false_refusal_rate == null
        ? "未评测"
        : `${(Number(production.false_refusal_rate) * 100).toFixed(2)}%`;
    const cards = [
      [
        `最佳整体 MRR · ${STRATEGY_LABELS[bestStrategy]}`,
        bestMrr.toFixed(3),
        "四路策略中标准相关知识块综合排序最高。",
      ],
      [
        "库内误拒率",
        generationEnabled ? falseRefusalRate : "未运行",
        answerableRows.length
          ? `${refusedAnswerable}/${answerableRows.length} 道可答题触发保守拒答，越低越好。`
          : "库内可答题被误判拒答的比例，越低越好。",
      ],
      [
        "超纲正确拒答率",
        generationEnabled ? percent(production.refusal_accuracy) : "未运行",
        refusalRows.length
          ? `${correctlyRefused}/${refusalRows.length} 道超纲无依据题准确拒答，越高越好。`
          : "超出知识库范围的问题准确拒答比例，越高越好。",
      ],
    ];
    metrics.replaceChildren(
      ...cards.map(([name, value, hint]) => {
        const card = element("article", "metric");
        card.append(
          element("span", "metric-name", name),
          element("strong", "metric-value", value),
          element("small", "metric-hint", hint),
        );
        return card;
      }),
    );

    strategyLegend.replaceChildren(...buildLegendNodes());
    renderBucketChart();
    renderGenerationQuality();
    renderIssues();
    renderFullTable();
  }

  function groupedCases() {
    const groups = new Map();
    for (const item of report.cases || []) {
      if (!groups.has(item.case_id)) groups.set(item.case_id, []);
      groups.get(item.case_id).push(item);
    }
    return groups;
  }

  function renderCase(caseId) {
    const rows = groupedCases().get(caseId) || [];
    if (!rows.length) {
      strategyGrid.replaceChildren(element("div", "empty", "暂无该题的评估结果"));
      return;
    }
    const base = rows.find((item) => item.query) || rows[0];
    caseSummary.replaceChildren();
    const title = element("h3", "", `${base.case_id} · ${base.query || ""}`);
    const meta = element("div", "case-meta");
    const typeLabel = QUERY_TYPE_LABELS[base.query_type] || base.query_type || "未分类";
    const diffLabel = DIFFICULTY_LABELS[base.difficulty] || base.difficulty || "常规";
    const relevantIds = base.relevant_chunk_ids || [];
    meta.append(
      element("span", "chip", `题型：${typeLabel}`),
      element("span", "chip", `难度：${diffLabel}`),
      element(
        "span",
        "chip",
        `标准相关知识块 ${relevantIds.length} 块：${relevantIds.join(", ") || "无（超纲拒答题）"}`,
      ),
    );
    caseSummary.append(title, meta);

    const byStrategy = Object.fromEntries(rows.map((row) => [row.strategy, row]));
    strategyGrid.replaceChildren(
      ...STRATEGIES.map((strategy) => {
        const row = byStrategy[strategy];
        const card = element("article", "strategy-card");
        card.appendChild(element("h3", "", STRATEGY_LABELS[strategy]));
        const body = element("div", "card-body");
        if (!row) {
          body.appendChild(element("div", "empty", "无数据"));
          card.appendChild(body);
          return card;
        }
        const generatedAnswer = Boolean(row.answer);
        const rowMetrics = element("div", "coverage");
        const answerCoverage = !generatedAnswer
          ? "未运行"
          : hasSemanticCoverage()
            ? percent(row.answer_coverage)
            : "旧报告未评测";
        const factChecked =
          row.faithfulness_status === "completed" ||
          (row.faithfulness_status == null && row.faithful != null && generatedAnswer);
        const metricLines = [
          `Recall@5：${percent(row.recall_at_k)}`,
          `倒数排名 (RR)：${row.reciprocal_rank == null ? "未评测" : Number(row.reciprocal_rank).toFixed(3)}`,
          `${METRIC_NAMES.evidence_coverage}：${percent(row.evidence_coverage)}`,
          `${METRIC_NAMES.answer_coverage}：${answerCoverage}`,
          `${METRIC_NAMES.faithfulness}：` +
            `${factChecked ? percent(row.faithfulness) : row.faithfulness_status === "failed" ? "核查失败" : "未运行"}`,
        ];
        rowMetrics.replaceChildren(...metricLines.map((line) => element("span", "", line)));
        body.appendChild(rowMetrics);
        const list = element("ol", "ranking");
        const relevant = new Set(row.relevant_chunk_ids || []);
        for (const source of row.citations || []) {
          const isHit = relevant.has(source.chunk_id);
          const item = element("li", `evidence${isHit ? " hit" : ""}`);
          const chunkAnswer = element("p", "evidence-answer");
          chunkAnswer.append(
            element("strong", "", "知识块回答"),
            document.createTextNode(String(source.answer || "").trim() || "该知识块无正文内容"),
          );
          item.append(
            element(
              "span",
              "evidence-rank",
              `#${source.n} · 知识块 #${source.chunk_id}${isHit ? " · 命中标准依据" : ""}`,
            ),
            element("strong", "evidence-title", source.question || "未标问题"),
            element("span", "evidence-path", source.section_path || "未标章节"),
            chunkAnswer,
          );
          list.appendChild(item);
        }
        if (!list.children.length) list.appendChild(element("li", "evidence", "未召回知识块"));
        body.appendChild(list);
        const answer = element(
          "div",
          "answer",
          row.answer ||
            (row.generation_status === "failed"
              ? `生成回答异常：${row.generation_error || "原因未记录"}`
              : "本路线未生成回答"),
        );
        answer.prepend(element("strong", "", row.refused ? "明确拒答\n" : "最终回答\n"));
        body.appendChild(answer);
        body.appendChild(
          element(
            "div",
            "case-meta",
            factChecked
              ? `事实依据核查：${row.faithful ? "有据通过" : "疑似编造，待人工复核"} · ${row.faithfulness_reason || "未附说明"}`
              : `事实依据核查：${row.faithfulness_status === "failed" ? "核查失败" : "未运行"}`,
          ),
        );
        card.appendChild(body);
        return card;
      }),
    );
  }

  function renderReport(payload) {
    report = payload;
    counts = null;
    document.body.classList.remove("report-missing");
    emptyState.hidden = true;
    rerunCopy.textContent =
      `运行 ${caseCounts().total} 道评测题 × 4 条检索路线；默认四路生成并评估要点覆盖率，` +
      "取消顶部勾选可仅生成混合 + Rerank 回答以节省模型调用。";
    faithReviews = new Map();
    reviewLoadError = "正在读取人工复核记录…";
    renderMeta();
    renderSummary();
    const groups = groupedCases();
    casePicker.replaceChildren(
      ...Array.from(groups.entries()).map(([caseId, rows]) => {
        const option = document.createElement("option");
        option.value = caseId;
        const queryText = rows.find((item) => item.query)?.query || "";
        option.textContent = `${caseId} · ${queryText}`;
        return option;
      }),
    );
    if (casePicker.value) renderCase(casePicker.value);
    const caseCount = report.dataset?.total_cases || groups.size;
    if (report.dataset_alignment?.status === "stale") {
      setStatus(
        `当前评测题已修改，页面显示的是旧题目的历史分数（至少 ${report.dataset_alignment.changed_cases} 题标注已变）；请重新运行评估后再据此验收。`,
        true,
      );
    } else if (report.is_baseline_snapshot) {
      setStatus(
        `当前展示内置基准评测快照（${caseCount} 题 · ${Object.keys(report.strategies || {}).length} 路检索对照），绿色知识块代表命中标准相关知识块。`,
        false,
        true,
      );
    } else {
      const base =
        `已加载最新评测报告（${caseCount} 题 · ${report.cases?.length || 0} 条策略结果）。`;
      setStatus(
        hasSemanticCoverage()
          ? `${base}绿色知识块代表命中标准相关知识块。`
          : `${base}这是旧口径报告，不含答案语义覆盖度；新评估完成前请勿将缺失指标当成 0 分。`,
      );
    }
  }

  async function loadReport() {
    const response = await fetch("/v1/rag-eval/report", {
      cache: "no-store",
      headers: adminHeaders(),
    });
    if (response.status === 404) {
      document.body.classList.add("report-missing");
      emptyState.hidden = false;
      setStatus("尚未生成评估报告。");
      return;
    }
    if (!response.ok) {
      throw new Error(await responseError(response, `读取报告失败（${response.status}）`));
    }
    renderReport(await response.json());
    await loadFaithReviews();
  }

  async function loadFaithReviews() {
    const sourceReport = report;
    try {
      const response = await fetch("/v1/rag-eval/faith-cases", {
        cache: "no-store",
        headers: adminHeaders(),
      });
      if (!response.ok) {
        throw new Error(await responseError(response, `人工复核记录暂不可用（${response.status}）`));
      }
      const payload = await response.json();
      if (report !== sourceReport) return;
      faithReviews = new Map(Object.entries(payload.cases || {}));
      reviewLoadError = "";
    } catch (error) {
      if (report !== sourceReport) return;
      faithReviews = new Map();
      reviewLoadError = `${error.message || "人工复核记录暂不可用"}；报告仍可查看，暂不能提交处置。`;
    }
    renderIssues();
  }

  casePicker.addEventListener("change", () => renderCase(casePicker.value));
  document.querySelector("#metricTabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-metric]");
    if (!button) return;
    selectedMetric = button.dataset.metric;
    renderBucketChart();
  });
  document.querySelector("#generationTabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-generation-metric]");
    if (!button) return;
    selectedGenerationMetric = button.dataset.generationMetric;
    renderGenerationQuality();
  });
  issueFilters.addEventListener("click", (event) => {
    const button = event.target.closest("[data-issue-filter]");
    if (!button) return;
    selectedIssueFilter = button.dataset.issueFilter;
    selectedIssuePage = 1;
    renderIssues();
  });
  issuePagination.addEventListener("click", (event) => {
    const button = event.target.closest("[data-issue-page]");
    if (!button || button.disabled) return;
    selectedIssuePage = Number(button.dataset.issuePage);
    renderIssues();
    issueFilters.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  issueList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-inspect-case]");
    if (!button) return;
    casePicker.value = button.dataset.inspectCase;
    renderCase(casePicker.value);
    caseSummary.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  issueList.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-review-case]");
    if (!form) return;
    event.preventDefault();
    const evalId = form.dataset.reviewCase;
    const review = faithReviews.get(evalId);
    const status = event.submitter?.dataset.reviewStatus;
    const resolution = form.elements.resolution.value.trim();
    const feedback = form.querySelector(".review-feedback");
    if (!review?.available || !status) return;
    if (status !== "未解决" && !resolution) {
      feedback.textContent = "请先填写处置理由。";
      form.elements.resolution.focus();
      return;
    }
    form.querySelectorAll("button").forEach((button) => {
      button.disabled = true;
    });
    feedback.textContent = "正在保存…";
    try {
      const response = await fetch(`/v1/rag-eval/faith-cases/${encodeURIComponent(evalId)}/status`, {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          status,
          resolution: status === "未解决" ? null : resolution,
          expected_seen_count: review.seen_count,
          expected_status: review.status,
          expected_resolution: review.resolution,
        }),
      });
      if (!response.ok) {
        throw new Error(
          response.status === 409
            ? "记录已被更新或评估已重跑，请刷新页面再复核。"
            : await responseError(response, `保存失败（${response.status}），请检查后重试。`),
        );
      }
      faithReviews.set(evalId, await response.json());
      renderIssues(evalId);
    } catch (error) {
      feedback.textContent = error.message || "保存失败，请重试。";
      form.querySelectorAll("button").forEach((button) => {
        button.disabled = false;
      });
    }
  });

  async function rerunEvaluation() {
    const runFullGeneration = fullGeneration.checked;
    const generationMode = runFullGeneration
      ? "四条路线均执行回答生成与裁判评分，模型调用量较高"
      : "仅对混合 + Rerank 路线执行回答生成与裁判评分";
    const scope = report ? `${caseCounts().total} 题` : "全部评测题";
    if (!window.confirm(`将对${scope}运行四种检索策略，${generationMode}。确定继续吗？`)) return;
    rerunButtons.forEach((button) => {
      button.disabled = true;
      button.textContent = "评估运行中…";
    });
    setStatus(`正在运行${scope}评测：四路检索 + ${generationMode}，请保持页面打开。`, false);
    try {
      const response = await fetch("/v1/rag-eval/run", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          confirm_external_model_usage: true,
          full_generation: runFullGeneration,
        }),
      });
      if (!response.ok) {
        throw new Error(await responseError(response, `评估失败（${response.status}）`));
      }
      renderReport(await response.json());
      await loadFaithReviews();
    } catch (error) {
      setStatus(error.message || "评估失败，请检查服务日志。", true);
    } finally {
      rerunButtons.forEach((button) => {
        button.disabled = false;
        button.textContent = "重新运行评估";
      });
    }
  }
  rerunButtons.forEach((button) => button.addEventListener("click", rerunEvaluation));

  loadReport().catch((error) => setStatus(error.message || "读取报告失败", true));
})();
