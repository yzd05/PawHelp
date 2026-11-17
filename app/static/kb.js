"use strict";
(() => {
  const adminHeaders = (extra = {}) =>
    window.PawHelpOperations
      ? window.PawHelpOperations.adminHeaders(extra)
      : { "X-PawHelp-Local-Admin": "1", ...extra };

  const materialList = document.querySelector("#materialList");
  const previewList = document.querySelector("#previewList");
  const previewCount = document.querySelector("#previewCount");
  const chunkList = document.querySelector("#chunkList");
  const searchResults = document.querySelector("#searchResults");
  const searchMeta = document.querySelector("#searchMeta");
  const markdownSummary = document.querySelector("#markdownSummary");
  const markdownPreview = document.querySelector("#markdownPreview");
  const statusFilter = document.querySelector("#statusFilter");
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
    const meta = document.createElement("div");
    meta.className = "card-meta";
    if (row.id) meta.append(pill(`#${row.id}`));
    meta.append(pill(row.category || "未分类"));
    if (row.content_type) meta.append(pill(row.content_type));
    if (row.source_kind) meta.append(pill(row.source_kind));
    if (row.vectorize_status) meta.append(pill(row.vectorize_status, row.vectorize_status));
    if (row.is_key_clause) meta.append(pill("关键条款", "pending"));
    if (typeof row.score === "number") meta.append(pill(`score ${row.score.toFixed(4)}`, "done"));
    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = row.questions;
    const answer = document.createElement("p");
    answer.className = "card-answer";
    answer.textContent = row.answer;
    card.append(meta, title, answer);
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
    document.querySelector("#manualSubmit").textContent = "＋ 写入待向量化知识";
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
    document.querySelector("#manualSubmit").textContent = `保存并更新向量 #${row.id}`;
    document.querySelector("#cancelEdit").classList.remove("hidden");
    form.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function deleteChunk(row) {
    if (!confirm(`确定删除知识 #${row.id}？MySQL 原文和 Milvus 向量都会删除。`)) return;
    try {
      await api(`/v1/knowledge/chunks/${row.id}`, { method: "DELETE" });
      if (editingId === row.id) resetManualForm();
      showToast(`知识 #${row.id} 已删除`);
      await loadChunks();
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function loadMaterials() {
    try {
      const materials = await api("/v1/knowledge/materials");
      document.querySelector("#materialCount").textContent = materials.length;
      document.querySelector("#materialSummary").textContent = `${materials.length} 份材料 / 共 ${materials.reduce((sum, row) => sum + row.chunk_count, 0)} 块`;
      materialList.replaceChildren();
      if (!materials.length) return tableMessage(materialList, "knowledge/ 下还没有支持的 Markdown 材料。", 8);
      materials.forEach((material) => {
        const row = document.createElement("tr");
        const values = [
          material.path,
          material.content_type,
          String(material.character_count),
          String(material.line_count),
          String(material.chunk_count),
          String(material.key_clause_count),
          material.table_chunk_count ? `表格块 ${material.table_chunk_count}` : `${material.chunk_count} 节`,
        ];
        values.forEach((value, index) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          if ([2, 3, 4, 5].includes(index)) cell.className = "number";
          row.append(cell);
        });
        const action = document.createElement("td");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "看切块";
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
    empty(previewList, "正在进行结构感知切块…");
    try {
      const drafts = await api("/v1/knowledge/preview", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      previewCount.textContent = `${drafts.length} 个知识块`;
      previewList.replaceChildren();
      if (!drafts.length) return empty(previewList, "这个材料没有生成知识块。");
      drafts.forEach((draft) => {
        const card = renderKnowledgeCard(draft, "preview-card");
        if (draft.is_key_clause) card.classList.add("key");
        previewList.append(card);
      });
    } catch (error) {
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
      document.querySelector("#milvusCount").textContent = result.milvus_total ?? "不可用";
      const state =
        result.dual_write_consistent === null
          ? "无法检查"
          : result.dual_write_consistent
            ? "一致"
            : "需补齐";
      document.querySelector("#consistencyCount").textContent = state;
      document.querySelector("#vectorPending").textContent = result.pending_total;
      document.querySelector("#vectorDone").textContent = result.done_total;
      document.querySelector("#vectorMilvus").textContent = result.milvus_total ?? "—";
      document.querySelector("#vectorCollection").textContent = result.milvus_collection;
      document.querySelector("#dualWriteTag").textContent = `双写 ${state}`;
      const drift = document.querySelector("#calibrationDrift");
      drift.textContent =
        "证据门控校准之后，人工、审核或挖掘的知识有增删改，相关性分数可能偏移，建议重新校准。";
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
      pill(`共 ${result.total} 块 / ${result.total} 节`),
      pill(`表格块 ${result.table_block_count}`),
      pill(`关键条款 ${result.key_clause_count}`, result.key_clause_count ? "pending" : ""),
      pill(`重复 ${result.duplicate_count}`, result.duplicate_count ? "pending" : "done"),
      pill(`可入库 ${result.ingestible_count}`, "done"),
    );
    if (!result.items.length) return empty(markdownPreview, "没有生成可入库的知识块。");
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["#", "节（section_path）", "问法 questions", "正文 answer", "字数", "标记"].forEach((text) => {
      const cell = document.createElement("th");
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
        item.duplicate ? "重复，跳过" : "可入库",
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
    empty(markdownPreview, "正在切块并检查重复…");
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
    if (!chunks.length) return empty(chunkList, "当前筛选条件下没有知识块。");
    chunks.forEach((row) => chunkList.append(renderKnowledgeCard(row, "chunk-card", true)));
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
      resetManualForm();
      showToast(
        wasEditing ? `知识 #${row.id} 已更新并重新向量化` : `知识 #${row.id} 已写入，状态为 pending`,
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
    button.textContent = "正在查重并入库…";
    try {
      const result = await api("/v1/knowledge/ingest-markdown", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast(
        `入库 ${result.inserted} 块，跳过重复 ${result.skipped_duplicates} 块，` +
          `跳过与已有知识重复或冲突 ${result.skipped_conflicts} 块，向量化 ${result.vectorized} 块`,
      );
      await previewManualMarkdown();
      await loadChunks(1);
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "录入入库";
    }
  });

  document.querySelector("#mineConversationsButton").addEventListener("click", async (event) => {
    if (!confirm("该操作会调用当前配置的上游模型抽取一批历史对话，是否继续？")) return;
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在抽取、去重并入库…";
    try {
      const result = await api("/v1/knowledge/mining/run", {
        method: "POST",
        body: JSON.stringify({
          confirm_external_model_usage: true,
          vectorize_after: document.querySelector("#mineVectorize").checked,
        }),
      });
      showToast(
        `处理 ${result.conversations} 个会话，抽取 ${result.extracted} 条，保留 ${result.kept} 条，丢弃 ${result.discarded} 条，向量化 ${result.vectorized} 条`,
      );
      await loadMiningStats();
      await loadChunks(1);
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "抽取下一批并去重入库";
    }
  });

  document.querySelector("#syncMaterialsButton").addEventListener("click", async (event) => {
    if (
      !confirm(
        "确定用服务器上当前的 Markdown 重新同步知识库？旧 FAQ 表迁移数据会被清理，手工知识和历史对话知识会保留。",
      )
    ) {
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在切块、同步并生成向量…";
    try {
      const result = await api("/v1/knowledge/sync-materials", { method: "POST", body: "{}" });
      const synced = `同步完成：替换 ${result.replaced} 条，写入 ${result.ingested} 条，向量化 ${result.vectorized} 条`;
      if (result.recheck_unavailable) {
        showToast(
          `${synced}。保留的 ${result.rechecked} 条非文档知识未能复查（冲突检查不可用），请稍后重建再查`,
          true,
          12000,
        );
      } else if (result.flagged.length) {
        const lines = result.flagged.map((flag) => `#${flag.chunk_id} ${flag.message}`);
        showToast(
          `${synced}。保留的知识中有 ${result.flagged.length} 条需处理：\n${lines.join("\n")}`,
          true,
          20000,
        );
      } else {
        showToast(`${synced}，保留的 ${result.rechecked} 条非文档知识复查无冲突`);
      }
      await loadChunks();
      await loadOverview();
      await loadMaterials();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "重建离线库（文档切块 → pending → Milvus）";
    }
  });

  document.querySelector("#vectorizeButton").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在加载模型并写入 Milvus…";
    try {
      const result = await api("/v1/knowledge/vectorize-pending", { method: "POST", body: "{}" });
      showToast(
        result.vectorized ? `已补齐 ${result.vectorized} 个知识向量` : "没有需要补齐的 pending 知识",
      );
      await loadChunks();
      await loadOverview();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "补齐 pending 向量";
    }
  });

  document.querySelector("#searchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    const values = new FormData(form);
    const query = values.get("query");
    button.disabled = true;
    searchMeta.textContent = "正在执行检索…";
    empty(searchResults, "正在检索并从 MySQL 回表…");
    try {
      const result = await api("/v1/knowledge/search", {
        method: "POST",
        body: JSON.stringify({
          query,
          strategy: values.get("strategy"),
          top_k: Number(values.get("top_k")),
        }),
      });
      const synonyms = result.synonyms.length ? ` · 扩展：${result.synonyms.join("、")}` : "";
      searchMeta.textContent = `路线 ${result.strategy} · 标准问法：${result.normalized_query}${synonyms} · ${result.confident ? "证据置信度通过" : "证据置信度不足"}`;
      searchResults.replaceChildren();
      if (!result.matches.length) return empty(searchResults, "没有达到相似度阈值的结果。");
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

  statusFilter.addEventListener("change", () => loadChunks(1));
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
