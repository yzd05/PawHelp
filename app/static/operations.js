"use strict";
(() => {
  const { byId, node, api, busy, number } = window.PawHelpOperations;
  let offset = 0;
  let selected = null;
  let reviewStatus = "待审";
  let listVersion = 0;
  let detailVersion = 0;
  let submitting = false;

  const sourceNames = {
    retrieval_low_conf: "未命中高置信度依据",
    self_check: "现有知识不足以完整解答",
    user_feedback: "买家反馈未解决",
  };

  const reviewStatusLabels = {
    待审: "待审核",
    通过: "已采纳",
    驳回: "已忽略",
    全部: "全部状态",
  };

  const publicationLabels = {
    已同步: "已发布入库",
    待同步: "待同步索引",
    未发布: "未发布",
  };

  function wireRovingToolbar(toolbar) {
    if (!toolbar || "focusGroup" in HTMLElement.prototype) return;
    const items = Array.from(toolbar.querySelectorAll("button"));
    if (!items.length) return;
    items.forEach((btn, idx) => {
      btn.tabIndex = idx === 0 ? 0 : -1;
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
      const steps = { ArrowRight: rtl ? -1 : 1, ArrowLeft: rtl ? 1 : -1 };
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

  function localTime(value) {
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.getTime())
      ? date.toLocaleString("zh-CN", { hour12: false })
      : "未记录时间";
  }

  function processingStatus(result) {
    const summary =
      `本次归并 ${result.processed} 条提问：新增待审工单 ${result.created ?? 0} 张，` +
      `合并至已有工单 ${result.merged ?? 0} 条；异常 ${result.failed} 条，队列变更延后 ${result.deferred} 条。`;
    if (!result.failed) return summary;
    const details = (result.errors || []).map(
      (error) => `记录 #${error.pool_id}：${error.message}`,
    );
    return [
      summary,
      "异常提问仍保留在待归并池中，请核实原因后再手动重试。",
      ...details,
    ].join("\n");
  }

  function originalEvidence(row) {
    const container = node("div", undefined, "originals");
    if (!row.originals.length) {
      container.append(node("p", "暂无关联的买家原始提问记录。", "muted"));
    }
    row.originals.forEach((original) => {
      const card = node("div", undefined, "original-card");
      const meta = node("div", undefined, "original-meta");
      meta.append(
        node("span", sourceNames[original.source] || "转审记录", "source-tag"),
        node("time", localTime(original.created_at)),
        node("span", `记录 #${original.id}`),
      );
      card.append(meta, node("p", original.raw_question, "raw-question"));
      if (original.reason) {
        card.append(node("p", `触发转审说明：${original.reason}`, "reason-line"));
      }
      const chunks = original.retrieved_chunks;
      if (chunks == null) {
        card.append(
          node(
            "p",
            "该轮对话未保存检索快照，请结合当前知识库内容直接核实。",
            "snapshot-empty",
          ),
        );
      } else if (!chunks.length) {
        card.append(
          node(
            "p",
            "当轮检索未召回任何知识块，请核实是否属于尚未覆盖的服务政策或新品信息。",
            "snapshot-empty",
          ),
        );
      } else {
        chunks.forEach((chunk) => {
          const evidence = node("div", undefined, "evidence-chunk");
          evidence.append(
            node(
              "h4",
              chunk.title ||
                chunk.question ||
                chunk.section_path ||
                `知识块 #${chunk.chunk_id ?? "未知"}`,
            ),
            node("pre", chunk.answer ?? "未保存原文"),
          );
          const score = node("div", undefined, "score-row");
          const track = node("span", undefined, "score-track");
          const fill = node("span", undefined, "score-fill");
          const numeric = typeof chunk.score === "number" && Number.isFinite(chunk.score);
          fill.style.width = `${numeric ? Math.min(1, Math.max(0, chunk.score)) * 100 : 0}%`;
          track.setAttribute("aria-hidden", "true");
          track.append(fill);
          score.append(
            node("span", `相关度 ${number(chunk.score)}`),
            track,
            node("span", chunk.section_path || "未标章节", "muted"),
          );
          evidence.append(score);
          card.append(evidence);
        });
      }
      container.append(card);
    });
    return container;
  }

  async function publishFromCard(rowId, answerText, statusNode, triggerButtons) {
    const trimmed = answerText.trim();
    if (!trimmed) {
      statusNode.textContent = "请先填写核实后的标准答案";
      return;
    }
    statusNode.textContent = "";
    triggerButtons.forEach((btn) => {
      btn.disabled = true;
    });
    try {
      const result = await api(`/api/review/${rowId}/approve`, "POST", {
        approved_answer: trimmed,
      });
      byId("list").querySelector(`[data-review-id="${rowId}"]`)?.remove();
      const outcome =
        result.publication_status === "已同步"
          ? "标准答案已采纳并发布至在线知识库。"
          : result.publication_error || "标准答案已核准，索引待同步；可在“已采纳”列表重试同步。";
      byId("status").textContent = outcome;
      try {
        await loadReviews();
        byId("status").textContent = outcome;
      } catch (error) {
        byId("status").textContent = `${outcome}\n刷新列表失败：${error.message}`;
      }
    } catch (error) {
      statusNode.textContent = error.message;
    } finally {
      triggerButtons.forEach((btn) => {
        btn.disabled = false;
      });
    }
  }

  function reviewCard(row) {
    const card = node("article", undefined, "review-card");
    card.dataset.reviewId = row.id;

    const head = node("div", undefined, "review-row");
    const badge = node(
      "span",
      `提问 ${row.occurrence_count} 次`,
      `frequency-badge${row.occurrence_count >= 3 ? " hot" : ""}`,
    );
    const body = node("div", undefined, "review-copy");
    const titleRow = node("div", undefined, "review-title-row");
    titleRow.append(node("h3", row.normalized_question));

    const statusTone =
      row.review_status === "通过"
        ? "approved"
        : row.review_status === "驳回"
          ? "rejected"
          : "";
    titleRow.append(
      node(
        "span",
        reviewStatusLabels[row.review_status] || row.review_status,
        `status-tag ${statusTone}`.trim(),
      ),
    );
    if (row.review_status === "通过") {
      const pubText = publicationLabels[row.publication_status] || row.publication_status;
      const pubClass =
        row.publication_status === "已同步"
          ? "publication-tag"
          : "publication-tag pending";
      titleRow.append(node("span", pubText, pubClass));
    }
    body.append(titleRow);

    const actions = node("div", undefined, "review-actions");

    if (row.review_status === "待审") {
      body.append(
        node(
          "p",
          "触发转审：客服对话未命中高置信度依据或买家反馈未解决，请核实下方标准答案后发布。",
          "trigger-hint",
        ),
      );
      const inspectBtn = node("button", "核对原问弹窗");
      inspectBtn.type = "button";
      inspectBtn.addEventListener("click", () => busy(inspectBtn, () => detail(row.id)));
      actions.append(inspectBtn);
      head.append(badge, body, actions);
      card.append(head);

      const editorBox = node("div", undefined, "inline-editor-block");
      const editorId = `inline-answer-${row.id}`;
      const label = node("label", "标准答案修订");
      label.setAttribute("for", editorId);
      const textarea = node("textarea");
      textarea.id = editorId;
      textarea.maxLength = 10000;
      textarea.placeholder = "填写经核实的标准客服答案，采纳后将写入知识库…";
      textarea.value = row.ai_suggested_answer || "";
      label.append(textarea);

      const footer = node("div", undefined, "inline-card-footer");
      const inlineStatus = node("p", "", "inline-editor-status");
      inlineStatus.setAttribute("role", "status");
      const btnGroup = node("div", undefined, "review-actions");

      const approveBtn = node("button", "采纳并发布到知识库", "approve");
      approveBtn.type = "button";
      approveBtn.dataset.action = "approve";

      const rejectBtn = node("button", "忽略", "danger");
      rejectBtn.type = "button";
      rejectBtn.dataset.action = "reject";

      approveBtn.addEventListener("click", () =>
        publishFromCard(row.id, textarea.value, inlineStatus, [
          approveBtn,
          rejectBtn,
          inspectBtn,
        ]),
      );
      rejectBtn.addEventListener("click", () =>
        busy(rejectBtn, async () => {
          await api(`/api/review/${row.id}/reject`, "POST");
          await loadReviews();
          byId("status").textContent =
            "已忽略该工单，不写入知识库；同类问法后续仅累计频次。";
        }),
      );

      btnGroup.append(approveBtn, rejectBtn);
      footer.append(inlineStatus, btnGroup);
      editorBox.append(label, footer);
      card.append(editorBox);
    } else {
      const previewText =
        row.approved_answer || row.ai_suggested_answer || "未填写答案内容。";
      body.append(node("p", previewText, "answer-preview"));
      if (row.review_status === "通过") {
        const viewBtn = node(
          "button",
          row.publication_status === "已同步" ? "查看发布内容" : "查看 / 重试同步",
        );
        viewBtn.type = "button";
        viewBtn.addEventListener("click", () => busy(viewBtn, () => detail(row.id)));
        actions.append(viewBtn);
      }
      head.append(badge, body, actions);
      card.append(head);
    }

    const expanded = node("details", undefined, "inline-evidence");
    const summary = node(
      "summary",
      `查看买家原问、触发原因与检索快照 · 累计 ${row.occurrence_count} 次提问`,
    );
    expanded.append(summary);
    let loaded = false;
    let loading = false;
    expanded.addEventListener("toggle", async () => {
      if (!expanded.open || loaded || loading) return;
      loading = true;
      expanded.querySelector(".evidence-slot")?.remove();
      const slot = node("div", "正在读取买家原话与检索快照…", "evidence-slot");
      expanded.append(slot);
      try {
        const fresh = await api(`/api/review/${row.id}`);
        if (!expanded.isConnected) return;
        slot.replaceChildren(originalEvidence(fresh));
        loaded = true;
      } catch (error) {
        slot.textContent = `加载失败：${error.message}。收起后重新展开可重试。`;
      } finally {
        loading = false;
      }
    });
    card.append(expanded);
    return card;
  }

  async function loadReviews() {
    const version = ++listVersion;
    const currentOffset = offset;
    const result = await api(
      `/api/review?status=${encodeURIComponent(reviewStatus)}&offset=${currentOffset}&sort=${byId("sort").value}`,
    );
    if (version !== listVersion) return;
    byId("list").replaceChildren(...result.items.map(reviewCard));
    if (!result.items.length) {
      byId("list").append(
        node(
          "p",
          "当前筛选下暂无审核工单；如有新收集的买家提问，可点击上方“归并新提问”生成候选。",
          "empty-list",
        ),
      );
    }
    const statusLabel = reviewStatusLabels[reviewStatus] || reviewStatus;
    byId("count").textContent =
      `待归并买家原问：${result.unprocessed} 条 · 当前显示 ${result.items.length} 张工单 · ${statusLabel}`;
    byId("page").textContent = `第 ${currentOffset / 50 + 1} 页`;
    byId("prev").disabled = currentOffset === 0;
    byId("next").disabled = result.items.length < 50;
    byId("status").textContent = "工单队列已同步。";
  }

  async function detail(id) {
    const version = ++detailVersion;
    const row = await api(`/api/review/${id}`);
    if (version !== detailVersion || submitting) return;
    selected = id;
    byId("question").textContent = row.normalized_question;
    const statusLabel = reviewStatusLabels[row.review_status] || row.review_status;
    const pubLabel = publicationLabels[row.publication_status] || row.publication_status;
    byId("detailMeta").textContent =
      `累计提问 ${row.occurrence_count} 次 · ${statusLabel} · ${pubLabel}` +
      `${row.published_chunk_id ? ` · 对应知识块 #${row.published_chunk_id}` : ""}`;
    byId("approved").value = row.approved_answer ?? row.ai_suggested_answer ?? "";
    byId("approved").readOnly = row.review_status !== "待审";
    byId("originals").replaceChildren(originalEvidence(row));
    byId("detailStatus").textContent = row.publication_error || "";
    byId("approve").hidden = row.review_status !== "待审";
    byId("publish").hidden =
      row.review_status !== "通过" || row.publication_status === "已同步";
    if (!byId("detail").open) byId("detail").showModal();
  }

  async function submitReview(action) {
    if (submitting || selected == null) return;
    if (action === "approve" && !byId("approved").value.trim()) {
      byId("detailStatus").textContent = "请填写核实后的标准答案";
      return;
    }
    const id = selected;
    const answer = byId("approved").value;
    submitting = true;
    ["approve", "publish", "close"].forEach((key) => {
      byId(key).disabled = true;
    });
    try {
      const result = await api(
        `/api/review/${id}/${action}`,
        "POST",
        action === "approve" ? { approved_answer: answer } : undefined,
      );
      byId("detail").close();
      ++detailVersion;
      byId("list").querySelector(`[data-review-id="${id}"]`)?.remove();
      const outcome =
        result.publication_status === "已同步"
          ? "标准答案已采纳并发布至在线知识库。"
          : result.publication_error ||
            "标准答案已核准，索引待同步；可在“已采纳”列表重试同步。";
      byId("status").textContent = outcome;
      try {
        await loadReviews();
        byId("status").textContent = outcome;
      } catch (error) {
        byId("status").textContent = `${outcome}\n刷新列表失败：${error.message}`;
      }
    } catch (error) {
      byId("detailStatus").textContent = error.message;
    } finally {
      submitting = false;
      ["approve", "publish", "close"].forEach((key) => {
        byId(key).disabled = false;
      });
    }
  }

  wireRovingToolbar(document.querySelector(".status-toolbar"));

  byId("load").addEventListener("click", () => busy(byId("load"), loadReviews));
  if (byId("list")) {
    document.querySelectorAll("[data-status]").forEach((button) => {
      button.addEventListener("click", () => {
        reviewStatus = button.dataset.status;
        offset = 0;
        document.querySelectorAll("[data-status]").forEach((tab) => {
          tab.setAttribute("aria-pressed", String(tab === button));
        });
        busy(byId("load"), loadReviews);
      });
    });
    byId("sort").addEventListener("change", () => {
      offset = 0;
      busy(byId("load"), loadReviews);
    });
    byId("prev").addEventListener("click", () => {
      offset = Math.max(0, offset - 50);
      busy(byId("load"), loadReviews);
    });
    byId("next").addEventListener("click", () => {
      offset += 50;
      busy(byId("load"), loadReviews);
    });
    byId("process").addEventListener("click", () => {
      if (confirm("将调用上游语言模型对本批未归并提问进行标准化查重，是否继续？")) {
        busy(byId("process"), async () => {
          const result = await api("/api/review/process", "POST");
          await loadReviews();
          byId("status").textContent = processingStatus(result);
        });
      }
    });
    byId("close").addEventListener("click", () => {
      if (!submitting) {
        ++detailVersion;
        byId("detail").close();
      }
    });
    byId("detail").addEventListener("cancel", (event) => {
      if (submitting) event.preventDefault();
      else ++detailVersion;
    });
    byId("approve").addEventListener("click", () => submitReview("approve"));
    byId("publish").addEventListener("click", () => submitReview("publish"));
  }

  busy(byId("load"), loadReviews);
})();
