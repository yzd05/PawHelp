"use strict";
(() => {
  const { byId, node, api, table } = window.PawHelpOperations;
  const content = byId("content");
  const path = location.pathname;
  const params = new URLSearchParams(location.search);

  const subpages = [
    ["/acceptance", "运行概览"],
    ["/acceptance/eval", "评估报告"],
    ["/acceptance/errors", "偏差复核"],
  ];

  const sources = {
    low_confidence: "未命中高置信度依据",
    user_feedback: "买家反馈未解决",
    negative_feedback: "买家反馈未解决",
    retrieval_low_confidence: "未命中高置信度依据",
    retrieval_low_conf: "未命中高置信度依据",
    self_check: "现有知识不足以解答",
    synthetic: "扩充测试样本",
    real_pool: "真实咨询样本",
  };

  const reviews = {
    pending: "待审核",
    approved: "已采纳",
    rejected: "已忽略",
    published: "已发布",
    待审: "待审核",
    通过: "已采纳",
    驳回: "已忽略",
  };

  const score = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : "—";
  const percent = (value) =>
    typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
  const date = (value) =>
    value && Number.isFinite(Date.parse(value))
      ? new Date(value).toLocaleString("zh-CN", { hour12: false })
      : "—";
  const body = (report, key) => (report[key]?.present ? report[key].data : null);
  const currentEvaluation = (report) =>
    report.evaluation?.present && !report.evaluation.stale
      ? report.evaluation.data
      : null;

  function heading(title, subtitle) {
    byId("title").textContent = title;
    document.title = `${title} · PawHelp`;
    byId("subtitle").textContent = subtitle;
  }

  function link(href, text, cls) {
    const a = node("a", text, cls);
    a.href = href;
    return a;
  }

  function section(title, note) {
    const box = node("section");
    box.append(node("h2", title));
    if (note) box.append(node("p", note, "muted"));
    content.append(box);
    return box;
  }

  function notice(box, text, tone = "") {
    box.append(node("p", text, `topic-notice ${tone}`.trim()));
  }

  function empty(box, text) {
    box.append(node("p", text, "empty-list"));
  }

  function badge(text, tone = "") {
    return node("span", text, `topic-tag ${tone}`.trim());
  }

  function cards(values, target = content) {
    const box = node("div", undefined, "topic-cards");
    for (const [label, value, note] of values) {
      const card = node("article");
      card.append(node("div", label, "muted"), node("p", value ?? "—", "topic-value"));
      if (note) card.append(node("small", note, "muted"));
      box.append(card);
    }
    target.append(box);
    return box;
  }

  function tags(labels, classify = () => "") {
    const box = node("span", undefined, "topic-tags");
    for (const label of labels || []) {
      box.append(badge(label, classify(label)));
    }
    return box;
  }

  function tabular(box, headers, rows, regionLabel) {
    const wrap = node("div", undefined, "table-wrap");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", regionLabel || box.querySelector("h2")?.textContent || "数据表格");
    wrap.append(table(headers, rows));
    box.append(wrap);
  }

  function details(box, title, text) {
    const d = node("details", undefined, "inline-note-fold");
    d.append(node("summary", title), node("p", text, "muted"));
    box.append(d);
    return d;
  }

  function pageLink(label, page, overrides = {}) {
    const query = new URLSearchParams({ label, page });
    if (path === "/topics/questions") {
      for (const key of ["q", "source", "review_status"]) {
        const value = overrides[key] ?? params.get(key);
        if (value && value !== "all") query.set(key, value);
      }
    }
    return `/topics/questions?${query}`;
  }

  function foldSection(box, title) {
    const fold = node("details", undefined, "topic-technical");
    fold.append(node("summary", title || box.querySelector("h2")?.textContent || "详细信息"));
    box.before(fold);
    fold.append(box);
    box.querySelector("h2")?.remove();
    box.classList.add("topic-fold-body");
    return fold;
  }

  function selection(label, name, options, value) {
    const field = node("label", label, "topic-field");
    const select = node("select");
    select.name = name;
    for (const [key, text] of options) {
      const o = node("option", text);
      o.value = key;
      select.append(o);
    }
    select.value = value || "all";
    field.append(select);
    return field;
  }

  function pager(target, page, pages, total, change) {
    const nav = node("nav", undefined, "topic-pagination");
    nav.setAttribute("aria-label", "分页");
    for (const [label, next, disabled] of [
      ["← 上一页", page - 1, page <= 1],
      ["下一页 →", page + 1, page >= pages],
    ]) {
      const button = node("button", label);
      button.type = "button";
      button.disabled = disabled;
      button.addEventListener("click", () => change(next));
      nav.append(button);
    }
    nav.append(
      node(
        "span",
        `第 ${page} / ${Math.max(1, pages)} 页 · 共 ${total} 条`,
        "muted",
      ),
    );
    target.append(nav);
  }

  async function topics() {
    heading("买家咨询热点", "洞察买家高频咨询与知识盲区分布，指导知识库优先补充。");
    const report = await api("/api/topics/distribution");
    cards([
      ["已归类咨询", report.total, "已完成主题归类的买家提问"],
      ["独立诉求数", report.unique_gaps_total, "同义问法合并去重后"],
      ["命中业务主题", `${report.hit_topics} / ${report.topics.length}`, "已有咨询记录的主题类目"],
      ["待归类候选", report.pending_merged, "已完成问法归并、等待批量分类"],
    ]);

    const toolbar = node("div", undefined, "topic-page-actions");
    toolbar.append(
      node(
        "p",
        report.latest ? `最近归类时间：${date(report.latest)}` : "暂无历史归类记录",
        "muted",
      ),
      link("/review", "前往知识审核 →", "topic-action primary"),
    );
    content.append(toolbar);

    const box = section("各业务主题咨询占比");
    const searchRegion = node("search");
    searchRegion.setAttribute("aria-label", "业务主题筛选");
    const filters = node("div", undefined, "topic-filterbar");
    const searchLabel = node("label", "搜索主题", "topic-field");
    const search = node("input");
    search.type = "search";
    search.autocorrect = false;
    search.setAttribute("autocorrect", "off");
    search.setAttribute("autocapitalize", "none");
    search.spellcheck = false;
    search.placeholder = "输入业务主题名称";
    searchLabel.append(search);

    const sortField = selection(
      "排序方式",
      "sort",
      [
        ["count", "咨询量从高到低"],
        ["unique_gaps", "独立诉求从高到低"],
      ],
      "count",
    );
    const toggleLabel = node("label", undefined, "topic-checkbox");
    const toggle = node("input");
    toggle.type = "checkbox";
    toggleLabel.append(toggle, node("span", "显示暂无记录的主题"));
    filters.append(searchLabel, sortField, toggleLabel);
    searchRegion.append(filters);
    box.append(searchRegion);

    const results = node("div");
    box.append(results);

    function render() {
      results.replaceChildren();
      const key = sortField.querySelector("select").value;
      const visible = [...report.topics]
        .filter(
          (item) =>
            (toggle.checked || item.count > 0) &&
            item.label.includes(search.value.trim()),
        )
        .sort((a, b) => b[key] - a[key] || b.count - a.count);

      if (!visible.length) {
        empty(
          results,
          report.total
            ? "未找到匹配的业务主题，请调整筛选条件。"
            : "暂无归类记录，完成提问归并与批量分类后将展示于此。",
        );
      }

      const max = Math.max(1, ...report.topics.map((item) => item.count));
      const totalRecords = Math.max(1, report.total || 0);
      for (const item of visible) {
        const d = node(
          "details",
          undefined,
          `topic-distribution-row ${item.count ? "" : "is-zero"}`.trim(),
        );
        const row = node("summary", undefined, "topic-overview-row");
        const name = node("span", undefined, "topic-name");
        name.append(
          node("strong", item.label),
          node("small", `${item.unique_gaps} 个独立诉求`, "muted"),
        );

        const track = node("span", undefined, "topic-track");
        const fill = node("span", undefined, "topic-fill");
        fill.style.width = `${(item.count / max) * 100}%`;
        track.append(fill);
        track.setAttribute("aria-hidden", "true");

        const shareText =
          report.total > 0
            ? `${item.count} 条 · ${((item.count / totalRecords) * 100).toFixed(0)}%`
            : `${item.count} 条`;

        row.append(
          name,
          track,
          node("strong", shareText, "topic-record-count"),
          node("span", "原问示例", "topic-expand-label"),
        );
        d.append(row);

        const samples = node("div", undefined, "topic-samples");
        const list = node("ul");
        for (const text of item.samples) list.append(node("li", text));
        if (!item.samples.length) {
          samples.append(node("p", "该业务主题下暂无买家提问示例。", "muted"));
        }
        samples.append(
          list,
          link(pageLink(item.label, 1), "查看买家提问明细 →", "topic-action"),
        );
        d.append(samples);
        results.append(d);
      }
      results.append(
        node(
          "p",
          `当前展示 ${visible.length} / ${report.topics.length} 个业务主题 · 多诉求咨询分别计入各相关主题`,
          "muted",
        ),
      );
    }

    search.addEventListener("input", render);
    sortField.querySelector("select").addEventListener("change", render);
    toggle.addEventListener("change", render);
    render();
  }

  async function questions() {
    const label = params.get("label") || "商品信息";
    const requestedPage = Number(params.get("page"));
    const page = Number.isFinite(requestedPage)
      ? Math.max(1, Math.floor(requestedPage))
      : 1;
    const request = new URLSearchParams({ label, page });
    for (const key of ["q", "source", "review_status"]) {
      if (params.get(key)) request.set(key, params.get(key));
    }
    const [report, distribution] = await Promise.all([
      api(`/api/topics/questions?${request}`),
      api("/api/topics/distribution"),
    ]);

    heading(`${label} · 买家提问明细`, "检视已脱敏的买家真实问法、触发来源与知识审核进度。");
    const categories = node("nav", undefined, "topic-category-tabs");
    categories.setAttribute("aria-label", "切换业务主题");
    for (const item of [...distribution.topics].sort((a, b) => b.count - a.count)) {
      const a = link(
        pageLink(item.label, 1),
        `${item.label} ${item.count}`,
        item.count ? "" : "is-zero",
      );
      if (item.label === label) a.setAttribute("aria-current", "page");
      categories.append(a);
    }
    content.append(categories);

    const box = section("脱敏买家提问列表");
    const top = node("div", undefined, "topic-page-actions");
    top.append(
      link("/topics", "← 返回咨询热点", "topic-action"),
      link("/review", "前往知识审核 →", "topic-action"),
    );
    box.append(top);

    const searchRegion = node("search");
    searchRegion.setAttribute("aria-label", "筛选买家提问");
    const form = node("form", undefined, "topic-filterbar");
    form.method = "get";
    form.action = "/topics/questions";
    const hidden = node("input");
    hidden.type = "hidden";
    hidden.name = "label";
    hidden.value = label;

    const textLabel = node("label", "搜索提问", "topic-field");
    const search = node("input");
    search.type = "search";
    search.name = "q";
    search.maxLength = 200;
    search.setAttribute("autocorrect", "off");
    search.setAttribute("autocapitalize", "none");
    search.spellcheck = false;
    search.placeholder = "输入提问关键词";
    search.value = params.get("q") || "";
    textLabel.append(search);

    const sourceField = selection(
      "触发来源",
      "source",
      [
        ["all", "全部来源"],
        ["retrieval_low_conf", "未命中高置信度依据"],
        ["self_check", "现有知识不足以解答"],
        ["user_feedback", "买家反馈未解决"],
      ],
      params.get("source"),
    );
    const reviewField = selection(
      "知识审核进度",
      "review_status",
      [
        ["all", "全部状态"],
        ["待审", "待审核"],
        ["通过", "已采纳"],
        ["驳回", "已忽略"],
        ["unmerged", "待归并"],
      ],
      params.get("review_status"),
    );
    const submit = node("button", "筛选");
    submit.type = "submit";
    form.append(
      hidden,
      textLabel,
      sourceField,
      reviewField,
      submit,
      link(
        pageLink(label, 1, { q: "", source: "all", review_status: "all" }),
        "重置",
        "topic-reset",
      ),
    );
    searchRegion.append(form);
    box.append(searchRegion, node("p", `共匹配 ${report.total} 条买家提问`, "muted"));

    if (!report.items.length) {
      empty(
        box,
        report.total
          ? "当前页暂无记录，请返回上一页或切换其他主题。"
          : "该业务主题下暂无匹配的买家提问。",
      );
    }

    for (const item of report.items) {
      const card = node("article", undefined, "topic-question");
      card.append(node("h3", item.text), tags(item.labels));
      const meta = node("div", undefined, "topic-meta");
      for (const text of [
        `触发来源：${sources[item.source] || item.source || "未记录"}`,
        `归并频次：${item.occurrence_count == null ? "待归并" : `累计 ${item.occurrence_count} 次提问`}`,
        `审核状态：${reviews[item.review_status] || item.review_status || "待归并"}`,
        `归类时间：${date(item.classified_at)}`,
      ]) {
        meta.append(node("span", text));
      }
      card.append(meta);
      details(card, "查看脱敏买家原话", item.raw_text);
      if (!item.normalized) {
        card.append(node("p", "该提问尚未归并至审核工单，当前展示脱敏原话。", "muted"));
      }
      box.append(card);
    }
    pager(box, report.page, report.pages, report.total, (next) => {
      location.href = pageLink(label, next);
    });
    details(
      box,
      "字段说明",
      "所有手机号、订单号与联系方式均已自动脱敏；审核状态表示知识库补充进度。",
    );
  }

  function policyFor(report, label) {
    const policy = body(report, "acceptance_policy");
    for (const [key, title] of [
      ["strict", "核心保障"],
      ["standard", "常规保障"],
      ["observe", "长尾观察"],
    ]) {
      if (policy?.[key]?.labels?.includes(label)) {
        return { title, minimum: policy[key].minimum_f1 };
      }
    }
    return { title: "未配置", minimum: null };
  }

  function grade(row, report) {
    const policy = policyFor(report, row.label);
    if (policy.minimum == null) return { text: "观察项", tone: "neutral" };
    if (!row.support) return { text: "缺少样本", tone: "warning" };
    return row.f1 >= policy.minimum
      ? { text: `≥ ${policy.minimum.toFixed(2)} 达标`, tone: "good" }
      : { text: `< ${policy.minimum.toFixed(2)} 待优化`, tone: "bad" };
  }

  function invalidReport(box, report, key, name) {
    if (!report[key]?.present) {
      empty(box, `${name}尚未生成${report[key]?.error ? `：${report[key].error}` : "。"}`);
      return true;
    }
    if (report[key].stale) {
      notice(
        box,
        `${name}与当前模型或数据集版本不一致，暂不作为有效评估依据。${(report[key].stale_reasons || []).join("；")}`,
        "warning",
      );
      return true;
    }
    return false;
  }

  function metric(value) {
    const box = node("div", undefined, "topic-metric");
    box.append(node("span", score(value)));
    const track = node("span", undefined, "topic-mini-track");
    const fill = node("span");
    fill.style.width = `${Math.max(0, Math.min(1, value || 0)) * 100}%`;
    track.append(fill);
    box.append(track);
    return box;
  }

  function evaluations(report) {
    heading("分类模型评估报告", "基于独立测试集评估各业务主题的识别准确率与召回率。");
    const data = currentEvaluation(report);
    const summary = section("综合识别表现");
    if (!invalidReport(summary, report, "evaluation", "测试集评估报告")) {
      const grid = node("div", undefined, "topic-two-columns");
      for (const [key, title, explanation] of [
        ["micro", "微平均综合表现", "汇总全部测试样本的识别结果，反映高频主流主题的整体准确度。"],
        ["macro", "宏平均均衡表现", "对全部业务主题等权取平均，反映低频长尾主题的均衡识别能力。"],
      ]) {
        const card = node("article");
        card.append(node("h3", title), node("p", explanation, "muted"));
        const values = node("div", undefined, "topic-metric-values");
        for (const [k, name] of [
          ["precision", "准确率"],
          ["recall", "召回率"],
          ["f1", "综合 F1"],
        ]) {
          const v = node("div");
          v.append(node("strong", score(data[key][k])), node("small", name));
          values.append(v);
        }
        card.append(values);
        grid.append(card);
      }
      summary.append(grid);
      cards(
        [
          ["测试集样本量", data.size],
          ["全标签完全一致率", percent(data.exact_match)],
          ["当前分类阈值", data.threshold],
          ["存在偏差样本", data.errors.wrong_samples],
        ],
        summary,
      );
      notice(
        summary,
        `测试样本构成：${
          Object.entries(data.source_counts || {})
            .map(([key, count]) => `${sources[key] || key} ${count} 条`)
            .join("、") || "未记录"
        }。建议结合下方分主题明细逐项核查长尾类目表现。`,
      );

      const s = section(
        "各业务主题指标与达标线",
        "样本数为测试集中包含该主题的标准样本量；保障等级仅用于质量核验，不改变线上推理阈值。",
      );
      const t = node("table");
      const head = node("thead");
      const tr = node("tr");
      for (const text of ["业务主题", "保障等级", "准确率", "召回率", "综合 F1", "样本数", "评估结论"]) {
        const th = node("th", text);
        th.scope = "col";
        tr.append(th);
      }
      head.append(tr);
      t.append(head);
      const tbody = node("tbody");
      for (const row of data.per_class) {
        const r = node("tr");
        const g = grade(row, report);
        for (const value of [row.label, policyFor(report, row.label).title]) {
          r.append(node("td", value));
        }
        for (const key of ["precision", "recall", "f1"]) {
          const cell = node("td");
          cell.append(metric(row[key]));
          r.append(cell);
        }
        r.append(node("td", row.support));
        const status = node("td");
        status.append(badge(g.text, g.tone));
        r.append(status);
        tbody.append(r);
      }
      t.append(tbody);
      const wrap = node("div", undefined, "table-wrap topic-evaluation-table");
      wrap.tabIndex = 0;
      wrap.setAttribute("role", "region");
      wrap.setAttribute("aria-label", "各业务主题分类评估指标表");
      wrap.append(t);
      s.append(wrap);

      const policy = body(report, "acceptance_policy");
      notice(
        s,
        policy
          ? `达标要求：核心保障类目 F1 ≥ ${score(policy.strict.minimum_f1)}，常规保障类目 F1 ≥ ${score(policy.standard.minimum_f1)}，长尾观察类目不设硬性门槛。`
          : "当前未配置分主题 F1 达标线，仅展示实测指标。",
      );

      const matrices = section(
        "各主题分类判定统计",
        "每个业务主题独立执行二分类判定；单条问题发生主题错位时会同时记入一次误判归入与一次遗漏未标。",
      );
      const fp = data.errors.false_positive_labels;
      const fn = data.errors.false_negative_labels;
      notice(
        matrices,
        `共完成 ${data.size * data.per_class.length} 次主题判定：误判归入 ${fp} 次，遗漏未标 ${fn} 次，涉及 ${data.errors.wrong_samples} 条测试提问。`,
      );
      const matrixGrid = node("div", undefined, "topic-matrices");
      for (const row of data.per_class) {
        const c = node("article");
        c.append(node("h3", row.label));
        const cells = node("div", undefined, "topic-matrix-cells");
        for (const [value, title, tone] of [
          [row.matrix[0][0], "正确排除", "neutral"],
          [row.matrix[0][1], "误判归入", "bad"],
          [row.matrix[1][0], "遗漏未标", "bad"],
          [row.matrix[1][1], "准确命中", "good"],
        ]) {
          const cell = node("div", undefined, tone);
          cell.append(node("strong", value), node("small", title));
          cells.append(cell);
        }
        c.append(cells);
        matrixGrid.append(c);
      }
      matrices.append(
        matrixGrid,
        link("/acceptance/errors", "查看偏差样本复核 →", "topic-action"),
      );
      foldSection(matrices, "各主题识别明细与混淆统计");
    }
    thresholdScan(report);
  }

  function thresholdScan(report) {
    const box = section(
      "验证集阈值优选",
      "分类阈值完全在独立验证集上选定，不接触测试集样本。",
    );
    foldSection(box, "分类阈值优选记录");
    if (!body(report, "threshold")) {
      empty(box, "暂无验证集阈值扫描记录。");
      return;
    }
    const data = report.threshold.data;
    const model = body(report, "model");
    const chosen = data.chosen;
    if (!chosen || !Array.isArray(data.trials)) {
      empty(box, "阈值扫描记录字段不完整。");
      return;
    }
    for (const trial of data.trials) {
      const row = node(
        "div",
        undefined,
        `topic-scan-row ${trial.threshold === chosen.threshold ? "selected" : ""}`.trim(),
      );
      const track = node("span", undefined, "topic-track");
      const fill = node("span", undefined, "topic-fill");
      fill.style.width = `${Math.max(0, Math.min(1, trial.micro_f1)) * 100}%`;
      track.append(fill);
      row.append(
        node("strong", trial.threshold.toFixed(2)),
        track,
        node("span", score(trial.micro_f1)),
        node(
          "small",
          `${trial.threshold === chosen.threshold ? "已选用 · " : ""}误判 ${trial.fp} / 漏标 ${trial.fn}`,
          "muted",
        ),
      );
      box.append(row);
    }
    const matches = chosen.threshold === model?.threshold;
    notice(
      box,
      `验证集样本 ${report.threshold.validation_size ?? "未记录"} 条 · 优选阈值 ${chosen.threshold.toFixed(2)} · ` +
        `当前模型配置阈值 ${model?.threshold ?? "未记录"}，${matches ? "二者配置一致" : "配置不一致，请核对模型文件"}。`,
      matches ? "good" : "warning",
    );
  }

  function errors(report) {
    heading("偏差样本复核", "对比人工标准主题与模型预测结果，定位易混淆问法并指导语料补充。");
    const box = section("测试集偏差样本明细");
    if (invalidReport(box, report, "errors", "偏差样本记录")) return;
    const rows = report.errors.data;
    const missed = rows.reduce((n, row) => n + row.missed.length, 0);
    const extra = rows.reduce((n, row) => n + row.extra.length, 0);
    cards(
      [
        ["偏差提问数", rows.length],
        ["遗漏主题数", missed],
        ["误判主题数", extra],
        ["累计标签偏差", missed + extra],
      ],
      box,
    );
    notice(
      box,
      "以下记录用于人工复盘主题边界与补充易混淆问法；所有买家隐私字段均已完成脱敏处理。",
      "warning",
    );

    const searchRegion = node("search");
    searchRegion.setAttribute("aria-label", "筛选偏差样本");
    const toolbar = node("div", undefined, "topic-filterbar");
    const label = node("label", "偏差类型", "topic-field");
    const select = node("select");
    select.setAttribute("aria-label", "按偏差类型筛选");
    for (const [value, title] of [
      ["all", "全部偏差"],
      ["missed", "存在遗漏标签"],
      ["extra", "存在误判标签"],
      ["mixed", "主题错位"],
    ]) {
      const o = node("option", title);
      o.value = value;
      select.append(o);
    }
    const searchLabel = node("label", "搜索提问", "topic-field");
    const search = node("input");
    search.type = "search";
    search.setAttribute("autocorrect", "off");
    search.setAttribute("autocapitalize", "none");
    search.spellcheck = false;
    search.placeholder = "输入提问关键词";
    searchLabel.append(search);
    label.append(select);
    toolbar.append(searchLabel, label);
    searchRegion.append(toolbar);
    box.append(searchRegion);

    const list = node("div");
    box.append(list);

    function render(page = 1) {
      list.replaceChildren();
      const type = (row) =>
        row.missed.length && row.extra.length
          ? "mixed"
          : row.missed.length
            ? "missed"
            : "extra";
      const filtered = rows.filter(
        (row) =>
          (select.value === "all" || type(row) === select.value) &&
          row.text.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()),
      );
      if (!filtered.length) empty(list, "当前筛选条件下暂无偏差样本。");
      for (const row of filtered.slice((page - 1) * 10, page * 10)) {
        const card = node("article", undefined, "topic-error-card");
        const top = node("div", undefined, "topic-meta");
        top.append(
          badge(
            { missed: "遗漏标签", extra: "误判标签", mixed: "主题错位" }[type(row)],
            type(row) === "mixed" ? "bad" : "warning",
          ),
          node("span", `共 ${row.missed.length + row.extra.length} 处标签差异`, "muted"),
        );
        card.append(top, node("h3", row.text));

        const expected = node("div", undefined, "topic-label-row");
        expected.append(
          node("span", "标准主题"),
          tags(row.expected, (x) => (row.missed.includes(x) ? "missed" : "good")),
        );
        const predicted = node("div", undefined, "topic-label-row");
        predicted.append(
          node("span", "模型预测"),
          tags(row.predicted, (x) => (row.extra.includes(x) ? "extra" : "good")),
        );
        card.append(
          expected,
          predicted,
          node(
            "p",
            `遗漏：${row.missed.join("、") || "无"} · 误判：${row.extra.join("、") || "无"}`,
            "muted",
          ),
          node(
            "p",
            `样本来源：${sources[row.source] || row.source || "未记录"} · 复核进度：${reviews[row.review_status] || row.review_status || "未记录"}`,
            "muted",
          ),
        );
        details(
          card,
          "查看模型预测得分与样本编号",
          `${row.id} · ${[...new Set([...row.expected, ...row.predicted])]
            .map((x) => `${x} ${score(row.scores?.[x])}`)
            .join(" · ")}`,
        );
        list.append(card);
      }
      pager(list, page, Math.ceil(filtered.length / 10), filtered.length, render);
    }

    select.addEventListener("change", () => render());
    search.addEventListener("input", () => render());
    render();
  }

  function size(bytes) {
    if (typeof bytes !== "number") return "—";
    if (bytes < 1024) return `${bytes} B`;
    return bytes < 1024 * 1024
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function inventory(box, rows, regionLabel) {
    if (!rows) {
      empty(box, "当前暂无本地产物清单记录。");
      return;
    }
    tabular(
      box,
      ["产物路径", "记录数", "文件大小", "最后更新时间", "状态"],
      rows.map((row) => [
        row.path,
        row.rows ?? "—",
        size(row.size_bytes),
        date(row.modified_at),
        row.error || (row.present ? "已就绪" : "未生成"),
      ]),
      regionLabel,
    );
  }

  function dataArtifacts(report) {
    heading("模型与数据档案", "追溯分类器样本构成、数据集划分及训练配置参数。");
    const intro = section("只读运维档案", "本页仅展示本地模型与数据集元数据，不会触发模型训练或外部调用。");
    intro.append(link("/acceptance", "← 返回运行概览", "topic-action"));
    const inv = report.inventory;
    const dataset = body(report, "dataset");
    const model = body(report, "model");

    const lineage = section("样本流转规模", "按真实问题快照、预标注审定与近义分组顺序构建数据集，数据增强仅作用于训练集。");
    const count = (name) => inv?.corpus?.find((row) => row.path === name)?.rows;
    cards(
      [
        ["脱敏问题快照", count("pool-20261013.jsonl"), "导出自历史未解提问池"],
        [
          "预标注 / 扩充样本",
          `${count("pool-prelabels.jsonl") ?? "—"} / ${count("synthetic-v1.jsonl") ?? "—"}`,
          "经抽样核验后冻结入库",
        ],
        [
          "审定 / 近义分组",
          `${count("corpus-approved-v1.jsonl") ?? "—"} / ${count("corpus-grouped-v2.jsonl") ?? "—"}`,
          "按近义组隔离防止跨集泄漏",
        ],
        [
          "训练 / 验证 / 测试集",
          dataset
            ? ["train", "val", "test"]
                .map((k) => dataset.after_augmentation?.[k]?.size ?? "—")
                .join(" / ")
            : "—",
          "按整组切分后仅扩充训练集",
        ],
      ],
      lineage,
    );
    if (report.review_summary?.present) {
      notice(
        lineage,
        `人工抽审样本：${report.review_summary.samples} 条 · 批次状态：${report.review_summary.approved ? "已审定通过" : "待审定"}。`,
      );
    }
    inventory(lineage, inv?.corpus, "语料样本文件清单");

    const split = section("数据集划分与扩充明细");
    if (dataset) {
      tabular(
        split,
        ["数据分区", "扩充前条数", "扩充后条数", "处理策略"],
        [
          ["train", "训练集"],
          ["val", "验证集"],
          ["test", "测试集"],
        ].map(([key, title]) => [
          title,
          dataset.before_augmentation?.[key]?.size ?? "—",
          dataset.after_augmentation?.[key]?.size ?? "—",
          key === "train" ? "生成同义问法变体" : "严格冻结，不参与扩充",
        ]),
        "数据集划分明细",
      );
      const overlap = Object.entries(dataset.overlaps || {})
        .map(([key, value]) => `${key}：文本 ${value.texts} / 分组 ${value.groups}`)
        .join("；");
      notice(split, `跨集重复核查结果：${overlap || "零重叠"}。`);
    } else {
      empty(split, "暂无数据集划分清单。");
    }
    inventory(split, inv?.dataset, "数据集分区文件清单");

    const artifacts = section("模型架构与推理参数", "模型权重、分词器、17 类主题词表与判定阈值共同构成线上分类配置。");
    if (model) {
      cards(
        [
          ["模型版本", model.model_version],
          ["判定阈值", model.threshold],
          ["可训练参数量", model.trainable_parameters?.toLocaleString("zh-CN")],
          ["最大序列长度", model.max_length],
        ],
        artifacts,
      );
      notice(
        artifacts,
        `底座模型：${model.base_model} · 共 ${model.labels?.length ?? "—"} 个业务主题标签；无具体主题达标时自动归入“其他”兜底。`,
      );
    } else {
      empty(artifacts, "尚未生成本地分类模型配置。");
    }
    inventory(artifacts, inv?.model, "模型权重与配置文件清单");

    const train = section("训练轮次记录");
    const training = body(report, "training");
    if (training) {
      const epochs = (training.history || []).filter(
        (row) => typeof row.eval_micro_f1 === "number",
      );
      tabular(
        train,
        ["训练轮次", "验证集微平均 F1", "验证集宏平均 F1", "验证损失"],
        epochs.map((row) => [
          row.epoch,
          score(row.eval_micro_f1),
          score(row.eval_macro_f1),
          score(row.eval_loss),
        ]),
        "训练轮次验证指标",
      );
      notice(
        train,
        `最佳验证微平均 F1：${score(training.best_metric)} · 选用检查点：${training.best_checkpoint || "未记录"}。`,
      );
    } else {
      empty(train, "暂无模型训练历史记录。");
    }

    const exports = section("推理运行环境");
    const environment = body(report, "environment");
    if (environment) {
      tabular(
        exports,
        ["训练计算设备", "环境标识", "记录时间"],
        [[environment.device, environment.witness, date(environment.timestamp)]],
        "训练环境记录",
      );
    }
    notice(exports, "分类服务基于 PyTorch / Transformers 提供本地 HTTP 推理接口。");

    const reports = section("评估与批次归档文件");
    inventory(reports, inv?.reports, "评估报告文件清单");
    for (const box of [lineage, split, artifacts, train, exports, reports]) {
      foldSection(box);
    }
  }

  function overview(report, service, distribution) {
    heading("分类模型运行概览", "查看买家问题自动归类服务状态、批量处理进度与离线评估结论。");
    const model = body(report, "model");
    const evaluation = currentEvaluation(report);
    const sameVersion =
      service.online &&
      model?.model_version &&
      service.model_version === model.model_version;

    cards([
      [
        "分类推理服务",
        service.online ? (sameVersion ? "在线就绪" : "版本待核对") : "服务离线",
        "用于离线热点归类，不影响实时客服对话",
      ],
      ["累计已归类", distribution?.total ?? "—", "已写入主题标签的买家提问"],
      ["待批量归类", distribution?.pending_merged ?? "—", "已完成同义归并的待分类问题"],
      [
        "测试集微平均 F1",
        evaluation ? score(evaluation.micro.f1) : "待评估",
        "独立离线测试集综合得分",
      ],
    ]);

    const quality = section("分类质量核验");
    if (service.online && !sameVersion) {
      notice(
        quality,
        "当前在线分类服务版本与本地模型配置版本不一致，建议先核对版本后再执行新批次归类。",
        "warning",
      );
    }
    if (!service.online) {
      notice(
        quality,
        "分类推理服务当前未启动，新增提问暂不执行自动归类；历史咨询热点数据仍可正常查阅。",
        "warning",
      );
    }
    const statuses = evaluation?.per_class.map((row) => grade(row, report)) || [];
    const failed = statuses.filter((x) => x.tone === "bad" || x.tone === "warning").length;
    const checked = statuses.filter((x) => x.tone !== "neutral").length;
    if (evaluation) {
      quality.append(
        badge(
          checked
            ? failed
              ? "部分类目待优化"
              : "设线类目全部达标"
            : "未配置达标线",
          checked ? (failed ? "warning" : "good") : "neutral",
        ),
      );
      quality.append(
        node(
          "p",
          `共核验 ${checked} 个核心与常规业务主题，其中 ${checked - failed} 个已达标，${failed} 个待补充训练样本或复核边界。`,
        ),
      );
      notice(
        quality,
        `独立测试集共 ${evaluation.size} 条样本：${
          Object.entries(evaluation.source_counts || {})
            .map(([key, count]) => `${sources[key] || key} ${count} 条`)
            .join("、") || "未记录构成"
        }。`,
      );
    } else {
      invalidReport(quality, report, "evaluation", "模型评估报告");
    }
    const actions = node("div", undefined, "topic-page-actions");
    actions.append(
      link("/acceptance/eval", "查看完整评估报告 →", "topic-action"),
      link("/acceptance/errors", "复核偏差样本 →", "topic-action"),
    );
    quality.append(actions);

    const batch = section("最近一次批量归类");
    const last = report.batch;
    if (last?.present) {
      const names = {
        completed: "已完成",
        complete: "已完成",
        no_pending: "无待归类提问",
        waiting_for_batch: "等待积累批次",
        failed: "执行失败",
        started: "正在执行",
      };
      const failure =
        last.status === "complete" || last.status === "completed" || last.failed > 0
          ? last.failed
          : ["no_pending", "waiting_for_batch"].includes(last.status)
            ? "未触发"
            : "待确认";
      tabular(
        batch,
        ["批次状态", "成功归类", "失败条数", "启动时间", "完成时间"],
        [
          [
            names[last.status] || "未知状态",
            last.saved,
            failure,
            date(last.started_at),
            last.finished_at ? date(last.finished_at) : "未记录",
          ],
        ],
        "最近批量归类记录",
      );
      if (last.status === "started") {
        notice(
          batch,
          "最近一次批量归类尚未写入完成时间，请先核查后台任务状态，避免重复触发。",
          "warning",
        );
      }
      if (last.model_version && last.model_version !== model?.model_version) {
        notice(
          batch,
          "最近批次使用的模型版本与当前本地配置不同；已入库的历史归类标签保持不变。",
          "warning",
        );
      }
    } else {
      empty(batch, "暂无批量归类执行记录。");
    }
    if (distribution) {
      notice(
        batch,
        `当前已归并待分类：${distribution.pending_merged} 条 · 累计未分类原始记录：${distribution.unclassified} 条。`,
      );
    }
    const results = node("div", undefined, "topic-page-actions");
    results.append(
      link("/topics", "查看买家咨询热点 →", "topic-action primary"),
      link("/review", "前往知识审核 →", "topic-action"),
    );
    batch.append(results);

    const diagnostics = section("版本与阈值配置");
    tabular(
      diagnostics,
      ["配置项", "当前值"],
      [
        ["在线服务模型版本", service.model_version || "未连接"],
        ["本地模型配置版本", model?.model_version || "未生成"],
        ["多标签判定阈值", model?.threshold ?? "未生成"],
      ],
      "模型版本与阈值配置",
    );
    diagnostics.append(link("/acceptance/data", "查看模型与数据档案 →", "topic-action"));
    foldSection(diagnostics);
  }

  async function refresh() {
    content.replaceChildren();
    byId("status").textContent = "正在同步最新数据…";
    byId("refresh").disabled = true;
    try {
      if (path === "/topics") await topics();
      else if (path === "/topics/questions") await questions();
      else {
        const report = await api("/api/acceptance/reports");
        if (path === "/acceptance/eval") evaluations(report);
        else if (path === "/acceptance/data") dataArtifacts(report);
        else if (path === "/acceptance/errors") errors(report);
        else {
          const [service, distribution] = await Promise.all([
            api("/api/acceptance/service"),
            api("/api/topics/distribution").catch(() => null),
          ]);
          overview(report, service, distribution);
        }
      }
      byId("status").textContent = `数据已更新 · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    } catch (e) {
      byId("status").textContent = `读取失败：${e.message}`;
    } finally {
      byId("refresh").disabled = false;
    }
  }

  for (const [href, title] of path.startsWith("/acceptance")
    ? subpages
    : [
        ["/topics", "咨询热点分布"],
        ["/acceptance", "分类模型评估"],
      ]) {
    const a = link(href, title);
    if (href === path || (href === "/topics" && path === "/topics/questions")) {
      a.setAttribute("aria-current", "page");
    }
    byId("subnav").append(a);
  }
  if (path.startsWith("/acceptance")) {
    const tech = link("/acceptance/data", "模型与数据档案", "topic-secondary-link");
    if (path === "/acceptance/data") tech.setAttribute("aria-current", "page");
    byId("subnav").append(tech);
    byId("countPolicy").hidden = true;
  }
  byId("refresh").addEventListener("click", refresh);
  refresh();
})();
