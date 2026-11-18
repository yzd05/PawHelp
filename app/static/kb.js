"use strict";
(() => {
  const adminHeaders = (extra = {}) =>
    window.PawHelpOperations
      ? window.PawHelpOperations.adminHeaders(extra)
      : { "X-PawHelp-Local-Admin": "1", ...extra };

  const CONTENT_TYPE_LABELS = {
    policy: "政策条款",
    faq: "商品 FAQ",
    manual: "售后手册",
    conversation: "会话问答",
  };

  const STATUS_META = {
    done: { label: "已就绪", tone: "done" },
    pending: { label: "待建索引", tone: "pending" },
    failed: { label: "索引异常", tone: "failed" },
    deleting: { label: "清理中", tone: "deleting" },
  };

  const STRATEGY_LABELS = {
    hybrid_rerank: "混合 + Rerank",
    hybrid: "混合 RRF",
    dense: "向量检索",
    bm25: "BM25 关键词",
  };

  function resolveSourceMeta(row) {
    const sourcePath = String(row.source_path || "");
    if (sourcePath.startsWith("review:") || row.source_kind === "review") {
      return { label: "审核入库", tone: "done" };
    }
    switch (row.source_kind) {
      case "markdown":
        return { label: "官方文档", tone: "sky" };
      case "conversation":
        return { label: "对话提取", tone: "brand" };
      case "manual":
      case "manual_markdown":
      case "batch":
      case "legacy":
        return { label: "人工录入", tone: "" };
      case "faq_table":
        return { label: "历史导入", tone: "" };
      default:
        return row.source_kind ? { label: "补充知识", tone: "" } : null;
    }
  }

  function wireRovingToolbar(toolbar) {
    if (!toolbar || "focusGroup" in HTMLElement.prototype) return;
    const items = Array.from(toolbar.querySelectorAll("button"));
    if (!items.length) return;
    items.forEach((btn, index) => {
      btn.tabIndex = index === 0 ? 0 : -1;
    });
    toolbar.addEventListener("focusin", ({ target }) => {
      if (items.includes(target)) {
        items.forEach((btn) => {
          btn.tabIndex = btn === target ? 0 : -1;
        });
      }
    });
    toolbar.addEventListener("keydown", (event) => {
      const current = items.indexOf(document.activeElement);
      if (current === -1) return;
      const rtl = getComputedStyle(toolbar).direction === "rtl";
      const steps = {
        ArrowRight: rtl ? -1 : 1,
        ArrowLeft: rtl ? 1 : -1,
      };
      let nextIndex = null;
      if (event.key in steps) {
        nextIndex = (current + steps[event.key] + items.length) % items.length;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = items.length - 1;
      }
      if (nextIndex !== null) {
        event.preventDefault();
        items[nextIndex].focus();
      }
    });
  }

  const materialList = document.querySelector("#materialList");
  const previewDrawer = document.querySelector("#previewDrawer");
  const previewList = document.querySelector("#previewList");
  const previewCount = document.querySelector("#previewCount");
  const chunkList = document.querySelector("#chunkList");
  const chunkKeyword = document.querySelector("#chunkKeyword");
  const searchResults = document.querySelector("#searchResults");
  const searchMeta = document.querySelector("#searchMeta");
  const markdownSummary = document.querySelector("#markdownSummary");
  const markdownPreview = document.querySelector("#markdownPreview");
  const statusFilter = document.querySelector("#statusFilter");
  const statusButtons = Array.from(document.querySelectorAll("[data-chunk-status]"));
  const pageSize = document.querySelector("#pageSize");
  const pageSummary = document.querySelector("#pageSummary");
  const prevPage = document.querySelector("#prevPage");
  const nextPage = document.querySelector("#nextPage");
  const toast = document.querySelector("#toast");

  let chunks = [];
  let chunkPage = 1;
  let chunkTotalPages = 1;
  let editingId = null;
  let toastTimer = null;

  wireRovingToolbar(document.querySelector(".segmented-toolbar"));

  function showToast(message, error = false, duration = 4200) {
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.classList.remove("hidden");
    toastTimer = setTimeout(() => toast.classList.add("hidden"), duration);
  }

  async function api(path, options = {}) {
    const { headers, ...rest } = options;
    const response = await fetch(path, {
      cache: "no-store",
      headers: adminHeaders({ "Content-Type": "application/json", ...(headers || {}) }),
      ...rest,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      /* keep null */
    }
    if (!response.ok) {
      const detail = payload?.detail;
      throw new Error(
        typeof detail === "string"
          ? detail
          : detail?.message || `请求失败（${response.status}）`,
      );
    }
    return payload;
  }

  function empty(target, message) {
    target.replaceChildren(
      Object.assign(document.createElement("div"), { className: "empty", textContent: message }),
    );
  }

  function tableMessage(target, message, colspan) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = colspan;
    cell.textContent = message;
    row.append(cell);
    target.replaceChildren(row);
  }

  function pill(text, className = "") {
    const item = document.createElement("span");
    item.className = `pill ${className}`.trim();
    item.textContent = text;
    return item;
  }

  function renderKnowledgeCard(row, kind = "chunk-card", editable = false) {
    const card = document.createElement("article");
    card.className = kind;
    if (row.is_key_clause) card.classList.add("is-key-clause");

    const topRow = document.createElement("div");
    topRow.className = "card-header-row";

    const meta = document.createElement("div");
    meta.className = "card-meta";
    if (row.id) meta.append(pill(`#${row.id}`, "id-pill"));
    meta.append(pill(row.category || "未分类"));
    if (row.content_type) {
      meta.append(pill(CONTENT_TYPE_LABELS[row.content_type] || "客服知识"));
    }
    const sourceMeta = resolveSourceMeta(row);
    if (sourceMeta) meta.append(pill(sourceMeta.label, sourceMeta.tone));
    if (row.vectorize_status) {
      const statusInfo = STATUS_META[row.vectorize_status] || {
        label: "状态未知",
        tone: "",
      };
      meta.append(pill(statusInfo.label, statusInfo.tone));
    }
    if (row.is_key_clause) meta.append(pill("关键条款", "pending"));
    if (typeof row.score === "number") {
      meta.append(pill(`相关度 ${row.score.toFixed(4)}`, "done"));
    }
    topRow.append(meta);

    if (row.section_path) {
      const sectionNote = document.createElement("span");
      sectionNote.className = "card-section-path";
      sectionNote.textContent = row.section_path;
      topRow.append(sectionNote);
    }

    const title = document.createElement("h3");
    title.className = "card-title";
    title.textContent = row.questions;

    const answer = document.createElement("p");
    answer.className = "card-answer";
    answer.textContent = row.answer;

    card.append(topRow, title, answer);

    if (editable && row.id) {
      const actions = document.createElement("div");
      actions.className = "card-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "编辑";
      edit.addEventListener("click", () => startEdit(row));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "删除";
      remove.addEventListener("click", () => deleteChunk(row));
      actions.append(edit, remove);
      card.append(actions);
    }
    return card;
  }

  function resetManualForm() {
    const form = document.querySelector("#manualForm");
    form.reset();
    editingId = null;
    document.querySelector("#manualSubmit").textContent = "＋ 保存知识条目";
    const badge = document.querySelector("#editorModeBadge");
    badge.textContent = "单条录入";
    badge.className = "tag";
    document.querySelector("#cancelEdit").classList.add("hidden");
  }

  function startEdit(row) {
    const form = document.querySelector("#manualForm");
    editingId = row.id;
    form.elements.category.value = row.category || "";
    form.elements.content_type.value = row.content_type || "manual";
    form.elements.questions.value = row.questions || "";
    form.elements.answer.value = row.answer || "";
    form.elements.section_path.value = row.section_path || "";
    form.elements.is_key_clause.checked = Boolean(row.is_key_clause);
    document.querySelector("#manualSubmit").textContent = `保存修改并更新索引 #${row.id}`;
    const badge = document.querySelector("#editorModeBadge");
    badge.textContent = `正在编辑 #${row.id}`;
    badge.className = "tag tag-pink";
    document.querySelector("#cancelEdit").classList.remove("hidden");
    form.scrollIntoView({ behavior: "smooth", block: "center" });
    form.elements.questions.focus();
  }

  async function deleteChunk(row) {
    if (!confirm(`确认删除知识条目 #${row.id}？原文与向量索引将一并移除。`)) return;
    try {
      await api(`/v1/knowledge/chunks/${row.id}`, { method: "DELETE" });
      if (editingId === row.id) resetManualForm();
      showToast(`知识条目 #${row.id} 已删除`);
      await loadChunks();
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function loadMaterials() {
    try {
      const materials = await api("/v1/knowledge/materials");
      const totalChunks = materials.reduce((sum, row) => sum + row.chunk_count, 0);
      document.querySelector("#materialCount").textContent = materials.length;
      document.querySelector("#materialSummary").textContent =
        `${materials.length} 篇知识文档 · 共 ${totalChunks} 条知识块`;
      materialList.replaceChildren();
      if (!materials.length) {
        return tableMessage(materialList, "文档目录下暂无可导入的官方知识文档。", 8);
      }
      materials.forEach((material) => {
        const row = document.createElement("tr");

        const pathCell = document.createElement("td");
        pathCell.className = "doc-path";
        pathCell.textContent = material.path;
        row.append(pathCell);

        const typeCell = document.createElement("td");
        typeCell.append(pill(CONTENT_TYPE_LABELS[material.content_type] || "知识文档", "sky"));
        row.append(typeCell);

        [
          material.character_count.toLocaleString("zh-CN"),
          material.line_count.toLocaleString("zh-CN"),
          String(material.chunk_count),
          String(material.key_clause_count),
        ].forEach((value) => {
          const cell = document.createElement("td");
          cell.className = "number";
          cell.textContent = value;
          row.append(cell);
        });

        const featureCell = document.createElement("td");
        featureCell.textContent = material.table_chunk_count
          ? `表格块 ${material.table_chunk_count}`
          : `${material.chunk_count} 节`;
        row.append(featureCell);

        const action = document.createElement("td");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "预览切块";
        button.addEventListener("click", () => previewMaterial(material.path));
        action.append(button);
        row.append(action);
        materialList.append(row);
      });
    } catch (error) {
      tableMessage(materialList, error.message, 8);
      showToast(error.message, true);
    }
  }

  async function previewMaterial(path) {
    if (previewDrawer) previewDrawer.open = true;
    previewCount.textContent = `正在读取 ${path}`;
    previewCount.className = "tag tag-amber";
    empty(previewList, "正在按章节结构切分知识块…");
    previewDrawer?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    try {
      const drafts = await api("/v1/knowledge/preview", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      previewCount.textContent = `${path} · ${drafts.length} 条知识块`;
      previewCount.className = "tag tag-sky";
      previewList.replaceChildren();
      if (!drafts.length) return empty(previewList, "该知识文档未生成知识块。");
      drafts.forEach((draft) => {
        const card = renderKnowledgeCard(draft, "preview-card");
        if (draft.is_key_clause) card.classList.add("key");
        previewList.append(card);
      });
    } catch (error) {
      previewCount.textContent = "预览失败";
      previewCount.className = "tag tag-pink";
      empty(previewList, error.message);
      showToast(error.message, true);
    }
  }

  async function loadOverview() {
    try {
      const result = await api("/v1/knowledge/overview");
      document.querySelector("#materialCount").textContent = result.materials;
      document.querySelector("#chunkCount").textContent = result.mysql_total;
      document.querySelector("#pendingCount").textContent = result.pending_total;
      document.querySelector("#milvusCount").textContent = result.milvus_total ?? "离线";

      const consistencyEl = document.querySelector("#consistencyCount");
      const consistencyCard = consistencyEl.closest(".metric");
      const dualTag = document.querySelector("#dualWriteTag");
      consistencyCard?.classList.remove("state-ok", "state-warn");

      if (result.dual_write_consistent === null) {
        consistencyEl.textContent = "向量服务离线";
        dualTag.textContent = "索引服务未连接";
        dualTag.className = "tag";
      } else if (result.dual_write_consistent) {
        consistencyEl.textContent = "索引状态正常";
        consistencyCard?.classList.add("state-ok");
        dualTag.textContent = "索引状态正常";
        dualTag.className = "tag tag-lime";
      } else {
        consistencyEl.textContent = result.pending_total > 0 ? "待建立索引" : "索引需同步";
        consistencyCard?.classList.add("state-warn");
        dualTag.textContent =
          result.pending_total > 0 ? `${result.pending_total} 条待建索引` : "索引需同步";
        dualTag.className = "tag tag-amber";
      }

      document.querySelector("#vectorPending").textContent = result.pending_total;
      document.querySelector("#vectorDone").textContent = result.done_total;
      document.querySelector("#vectorMilvus").textContent = result.milvus_total ?? "—";
      document.querySelector("#vectorCollection").textContent = result.milvus_collection;

      const drift = document.querySelector("#calibrationDrift");
      drift.textContent =
        "上次门控校准后，人工录入、审核入库或对话提取的知识发生变更，相关性分布可能偏移，建议重新核对校准。";
      drift.classList.toggle("hidden", result.calibration_entries_changed !== true);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function loadMiningStats() {
    try {
      const result = await api("/v1/knowledge/mining/stats");
      document.querySelector("#miningUnprocessed").textContent = result.unprocessed_conversations;
      document.querySelector("#miningExtracted").textContent = result.extracted;
      document.querySelector("#miningKept").textContent = result.kept;
      document.querySelector("#miningDiscarded").textContent = result.discarded;
      document.querySelector("#miningBatches").textContent = result.batches;
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function markdownPayload() {
    const form = document.querySelector("#markdownForm");
    const values = new FormData(form);
    return {
      markdown: values.get("markdown"),
      content_type: values.get("content_type"),
      vectorize_after: values.get("vectorize_after") === "on",
    };
  }

  function renderMarkdownPreview(result) {
    markdownSummary.replaceChildren(
      pill(`共 ${result.total} 条知识块`, "sky"),
      pill(`表格块 ${result.table_block_count}`),
      pill(`关键条款 ${result.key_clause_count}`, result.key_clause_count ? "pending" : ""),
      pill(`库内重复 ${result.duplicate_count}`, result.duplicate_count ? "pending" : "done"),
      pill(`可入库 ${result.ingestible_count}`, "done"),
    );
    if (!result.items.length) return empty(markdownPreview, "没有生成可导入的知识块。");
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["序号", "章节路径", "真实问法", "标准答案", "字数", "核验状态"].forEach((text, idx) => {
      const cell = document.createElement("th");
      cell.scope = "col";
      if ([0, 4].includes(idx)) cell.className = "num-col";
      cell.textContent = text;
      headRow.append(cell);
    });
    head.append(headRow);
    const body = document.createElement("tbody");
    result.items.forEach((item, index) => {
      const row = document.createElement("tr");
      if (item.duplicate) row.className = "duplicate";
      const marks = [
        item.is_key_clause ? "关键条款" : "",
        item.is_table_block ? "表格块" : "",
        item.duplicate ? "重复跳过" : "可入库",
      ]
        .filter(Boolean)
        .join(" · ");
      [
        String(index + 1),
        item.section_path || "未标章节",
        item.questions,
        item.answer,
        String(item.answer.length),
        marks,
      ].forEach((value, cellIndex) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        if ([0, 4].includes(cellIndex)) cell.className = "number";
        row.append(cell);
      });
      body.append(row);
    });
    table.append(head, body);
    markdownPreview.replaceChildren(table);
  }

  async function previewManualMarkdown() {
    const payload = markdownPayload();
    if (!String(payload.markdown || "").trim()) return showToast("请先粘贴 Markdown 正文", true);
    empty(markdownPreview, "正在切分知识块并核验重复…");
    try {
      const result = await api("/v1/knowledge/preview-markdown", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      renderMarkdownPreview(result);
    } catch (error) {
      empty(markdownPreview, error.message);
      showToast(error.message, true);
    }
  }

  function renderChunks() {
    chunkList.replaceChildren();
    const keyword = (chunkKeyword?.value || "").trim().toLocaleLowerCase();
    const visible = keyword
      ? chunks.filter((row) =>
          [row.category, row.questions, row.answer, row.section_path]
            .filter(Boolean)
            .some((field) => String(field).toLocaleLowerCase().includes(keyword)),
        )
      : chunks;
    if (!visible.length) {
      return empty(
        chunkList,
        keyword
          ? "本页没有匹配该关键词的知识条目。"
          : "当前筛选条件下暂无知识条目。",
      );
    }
    visible.forEach((row) => chunkList.append(renderKnowledgeCard(row, "chunk-card", true)));
  }

  function syncStatusToolbar(value) {
    statusFilter.value = value;
    statusButtons.forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        String((button.dataset.chunkStatus || "") === value),
      );
    });
  }

  async function loadChunks(requestedPage = chunkPage) {
    try {
      const params = new URLSearchParams({
        page: String(requestedPage),
        page_size: pageSize.value,
      });
      if (statusFilter.value) params.set("status", statusFilter.value);
      const result = await api(`/v1/knowledge/chunks?${params}`);
      if (requestedPage > result.total_pages) {
        await loadChunks(result.total_pages);
        return;
      }
      chunks = result.items;
      chunkPage = result.page;
      chunkTotalPages = result.total_pages;
      document.querySelector("#chunkCount").textContent = result.all_total;
      document.querySelector("#pendingCount").textContent = result.pending_total;
      document.querySelector("#vectorDone").textContent = result.done_total;
      document.querySelector("#vectorPending").textContent = result.pending_total;
      pageSummary.textContent = `第 ${chunkPage} / ${chunkTotalPages} 页 · 共 ${result.total} 条`;
      prevPage.disabled = chunkPage <= 1;
      nextPage.disabled = chunkPage >= chunkTotalPages;
      renderChunks();
    } catch (error) {
      empty(chunkList, error.message);
      showToast(error.message, true);
    }
  }

  document.querySelector("#jumpToCreate")?.addEventListener("click", () => {
    document.querySelector("#manualEditor")?.scrollIntoView({ behavior: "smooth", block: "start" });
    document.querySelector("#manualCategory")?.focus();
  });

  document.querySelector("#manualForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector("button[type=submit]");
    const values = new FormData(form);
    submit.disabled = true;
    try {
      const target = editingId ? `/v1/knowledge/chunks/${editingId}` : "/v1/knowledge/chunks";
      const method = editingId ? "PUT" : "POST";
      const row = await api(target, {
        method,
        body: JSON.stringify({
          category: values.get("category"),
          questions: values.get("questions"),
          answer: values.get("answer"),
          section_path: values.get("section_path") || null,
          content_type: values.get("content_type"),
          is_key_clause: values.get("is_key_clause") === "on",
        }),
      });
      const wasEditing = editingId !== null;
      const statusLabel = STATUS_META[row.vectorize_status]?.label || "已保存";
      resetManualForm();
      showToast(
        wasEditing
          ? `知识条目 #${row.id} 已保存并更新索引`
          : `知识条目 #${row.id} 已入库 · ${statusLabel}`,
      );
      await loadChunks(wasEditing ? chunkPage : 1);
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  document.querySelector("#cancelEdit").addEventListener("click", resetManualForm);

  document.querySelector("#fillMarkdownExample").addEventListener("click", () => {
    document.querySelector("#markdownInput").value =
      "# 发票与开票\n\n## 开票时效\n\n订单完成后 30 天内可申请开票，平台在收到申请后 3 个工作日内开出电子发票。\n\n> [!IMPORTANT]\n> 超过申请期限请联系人工客服处理。\n\n## 可开票类型\n\n| 类型 | 开票内容 |\n|---|---|\n| 电子普票 | 商品明细或办公用品 |\n| 电子专票 | 需补齐纳税人识别号 |";
  });
  document.querySelector("#clearMarkdown").addEventListener("click", () => {
    document.querySelector("#markdownForm").reset();
    markdownSummary.replaceChildren();
    markdownPreview.replaceChildren();
  });
  document.querySelector("#previewMarkdown").addEventListener("click", previewManualMarkdown);
  document.querySelector("#markdownForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.querySelector("#ingestMarkdown");
    const payload = markdownPayload();
    button.disabled = true;
    button.textContent = "正在查重并导入…";
    try {
      const result = await api("/v1/knowledge/ingest-markdown", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast(
        `入库 ${result.inserted} 条知识块，跳过重复 ${result.skipped_duplicates} 条，` +
          `拦截冲突 ${result.skipped_conflicts} 条，建立索引 ${result.vectorized} 条`,
      );
      await previewManualMarkdown();
      await loadChunks(1);
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "确认导入";
    }
  });

  document.querySelector("#mineConversationsButton").addEventListener("click", async (event) => {
    if (!confirm("将调用上游模型从已结束的客服会话中抽取一批知识候选并核验去重，是否继续？")) return;
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在提取并去重入库…";
    try {
      const result = await api("/v1/knowledge/mining/run", {
        method: "POST",
        body: JSON.stringify({
          confirm_external_model_usage: true,
          vectorize_after: document.querySelector("#mineVectorize").checked,
        }),
      });
      showToast(
        `已分析 ${result.conversations} 段客服会话：提炼候选 ${result.extracted} 条，采纳 ${result.kept} 条，过滤 ${result.discarded} 条，建立索引 ${result.vectorized} 条`,
      );
      await loadMiningStats();
      await loadChunks(1);
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "提取下一批对话知识";
    }
  });

  document.querySelector("#syncMaterialsButton").addEventListener("click", async (event) => {
    if (
      !confirm(
        "确认同步当前官方知识文档？仅更新官方文档生成的知识块，人工录入、审核入库与对话提取知识将保留。",
      )
    ) {
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在同步文档并建立索引…";
    try {
      const result = await api("/v1/knowledge/sync-materials", { method: "POST", body: "{}" });
      const synced = `同步完成：替换 ${result.replaced} 条，写入 ${result.ingested} 条，建立索引 ${result.vectorized} 条`;
      if (result.recheck_unavailable) {
        showToast(
          `${synced}。保留的 ${result.rechecked} 条补充知识暂未完成冲突复核，请稍后重试。`,
          true,
          12000,
        );
      } else if (result.flagged.length) {
        const lines = result.flagged.map((flag) => `#${flag.chunk_id} ${flag.message}`);
        showToast(
          `${synced}。保留的知识中有 ${result.flagged.length} 条存在冲突需处理：\n${lines.join("\n")}`,
          true,
          20000,
        );
      } else {
        showToast(`${synced}，保留的 ${result.rechecked} 条补充知识复查无冲突`);
      }
      await loadChunks();
      await loadOverview();
      await loadMaterials();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "同步官方文档";
    }
  });

  document.querySelector("#vectorizeButton").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在补齐索引…";
    try {
      const result = await api("/v1/knowledge/vectorize-pending", { method: "POST", body: "{}" });
      showToast(
        result.vectorized
          ? `已为 ${result.vectorized} 条知识块建立向量索引`
          : "所有知识块索引均已就绪",
      );
      await loadChunks();
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "补齐待建索引";
    }
  });

  document.querySelector("#searchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    const values = new FormData(form);
    const query = values.get("query");
    button.disabled = true;
    searchMeta.textContent = "正在执行检索测试…";
    empty(searchResults, "正在召回相关知识块…");
    try {
      const result = await api("/v1/knowledge/search", {
        method: "POST",
        body: JSON.stringify({
          query,
          strategy: values.get("strategy"),
          top_k: Number(values.get("top_k")),
        }),
      });
      const strategyName = STRATEGY_LABELS[result.strategy] || result.strategy;
      const synonyms = result.synonyms.length ? ` · 同义词扩展：${result.synonyms.join("、")}` : "";
      searchMeta.textContent = `${strategyName} · 标准问法：${result.normalized_query}${synonyms} · ${result.confident ? "证据置信度通过" : "未达采信阈值"}`;
      searchResults.replaceChildren();
      if (!result.matches.length) return empty(searchResults, "未召回到达到相似度阈值的知识块。");
      result.matches.forEach((row) => searchResults.append(renderKnowledgeCard(row, "result-card")));
    } catch (error) {
      empty(searchResults, error.message);
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  document.querySelectorAll(".query-examples button").forEach((button) => {
    button.addEventListener("click", () => {
      const form = document.querySelector("#searchForm");
      form.elements.query.value = button.textContent;
      form.requestSubmit();
    });
  });

  statusButtons.forEach((button) => {
    button.addEventListener("click", () => {
      syncStatusToolbar(button.dataset.chunkStatus || "");
      loadChunks(1);
    });
  });

  statusFilter.addEventListener("change", () => {
    syncStatusToolbar(statusFilter.value);
    loadChunks(1);
  });
  chunkKeyword?.addEventListener("input", renderChunks);
  pageSize.addEventListener("change", () => loadChunks(1));
  prevPage.addEventListener("click", () => loadChunks(chunkPage - 1));
  nextPage.addEventListener("click", () => loadChunks(chunkPage + 1));
  document.querySelector("#refreshChunks").addEventListener("click", () => loadChunks());
  document.querySelector("#refreshMaterials").addEventListener("click", loadMaterials);

  loadMaterials();
  loadOverview();
  loadMiningStats();
  loadChunks();
})();
