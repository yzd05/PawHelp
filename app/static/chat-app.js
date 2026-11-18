"use strict";
(async function startChat() {
  await window.PawHelpStorage.prepare();
  const pageMain = document.querySelector("#chatPageMain");
  const messages = document.querySelector("#messages");
  const form = document.querySelector("#chatForm");
  const input = document.querySelector("#messageInput");
  const sendButton = document.querySelector("#sendButton");
  const newChatButton = document.querySelector("#newChatButton");
  const agentStatus = document.querySelector("#agentStatus");
  const conversationPreview = document.querySelector("#conversationPreview");
  const sourceModal = document.querySelector("#sourceModal");
  const sourceClose = document.querySelector("#sourceClose");
  const sourceMeta = document.querySelector("#sourceMeta");
  const sourceQuestion = document.querySelector("#sourceQuestion");
  const sourceAnswer = document.querySelector("#sourceAnswer");
  const ticketModal = document.querySelector("#ticketModal");
  const ticketForm = document.querySelector("#ticketForm");
  const ticketType = document.querySelector("#ticketType");
  const ticketDescription = document.querySelector("#ticketDescription");
  const ticketError = document.querySelector("#ticketError");
  const ticketCancel = document.querySelector("#ticketCancel");
  const ticketSubmit = document.querySelector("#ticketSubmit");
  const refundModal = document.querySelector("#refundModal");
  const refundForm = document.querySelector("#refundForm");
  const refundOrder = document.querySelector("#refundOrder");
  const refundReason = document.querySelector("#refundReason");
  const refundDescription = document.querySelector("#refundDescription");
  const refundError = document.querySelector("#refundError");
  const refundCancel = document.querySelector("#refundCancel");
  const refundSubmit = document.querySelector("#refundSubmit");

  const DEFAULT_AGENT_STATUS = "官方宠物客服 · 7×24 小时实时响应";
  const USER_ID_KEY = "pawhelp.userId";
  const CONVERSATION_ID_KEY = "pawhelp.conversationId";
  const FEEDBACK_KEY = "pawhelp.messageFeedback.server";
  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const SUPPORTS_FIELD_SIZING =
    typeof window.CSS !== "undefined" &&
    typeof window.CSS.supports === "function" &&
    window.CSS.supports("field-sizing", "content");

  const QUICK_PROMPTS = [
    {
      tag: "📦 订单物流",
      title: "查询我的订单与物流进度",
      desc: "追踪包裹动态与异常滞留催件",
      prompt: "帮我查询一下我的订单和最新物流进度",
    },
    {
      tag: "🔄 退换政策",
      title: "7天无理由退换货规则",
      desc: "拆封要求、适用范围与运费承担说明",
      prompt: "7天无理由退换货有什么条件和运费规则？",
    },
    {
      tag: "🛠️ 保修寄修",
      title: "智能喂食器/猫砂盆保修与寄修",
      desc: "整机质保期限、故障自查与寄修指引",
      prompt: "智能猫砂盆和喂食器保修期多久？故障怎么寄修？",
    },
    {
      tag: "🧾 发票运费",
      title: "电子发票开具与运费说明",
      desc: "开票入口、抬头修改及包邮门槛",
      prompt: "电子发票怎么申请开具？包邮和运费怎么算？",
    },
  ];

  const TOOL_ACTION_COPY = {
    query_order: {
      running: "正在核对订单信息…",
      succeeded: "已核对订单信息",
      failed: "订单信息暂未同步",
    },
    查询订单: {
      running: "正在核对订单信息…",
      succeeded: "已核对订单信息",
      failed: "订单信息暂未同步",
    },
    查询订单详情: {
      running: "正在核对订单信息…",
      succeeded: "已核对订单信息",
      failed: "订单信息暂未同步",
    },
    query_logistics: {
      running: "正在同步实时物流…",
      succeeded: "已同步实时物流",
      failed: "物流轨迹暂未获取",
    },
    查询物流: {
      running: "正在同步实时物流…",
      succeeded: "已同步实时物流",
      failed: "物流轨迹暂未获取",
    },
    查询物流轨迹: {
      running: "正在同步实时物流…",
      succeeded: "已同步实时物流",
      failed: "物流轨迹暂未获取",
    },
    query_faq: {
      running: "正在查阅服务指南…",
      succeeded: "已查阅服务指南",
      failed: "服务指南暂未命中",
    },
    查询常见问题: {
      running: "正在查阅服务指南…",
      succeeded: "已查阅服务指南",
      failed: "服务指南暂未命中",
    },
    query_product: {
      running: "正在核实商品库存…",
      succeeded: "已核实商品库存",
      failed: "商品信息暂未同步",
    },
    查询商品: {
      running: "正在核实商品库存…",
      succeeded: "已核实商品库存",
      failed: "商品信息暂未同步",
    },
    query_warranty: {
      running: "正在核查保修权益…",
      succeeded: "已核查保修权益",
      failed: "保修信息暂未同步",
    },
    查询保修: {
      running: "正在核查保修权益…",
      succeeded: "已核查保修权益",
      failed: "保修信息暂未同步",
    },
    query_return_progress: {
      running: "正在查询退换进度…",
      succeeded: "已同步退换货进度",
      failed: "退换进度暂未同步",
    },
    查询退货进度: {
      running: "正在查询退换进度…",
      succeeded: "已同步退换货进度",
      failed: "退换进度暂未同步",
    },
    create_ticket: {
      running: "正在登记服务工单…",
      succeeded: "已登记专属工单",
      failed: "工单登记未完成",
    },
    创建人工工单: {
      running: "正在登记服务工单…",
      succeeded: "已登记专属工单",
      failed: "工单登记未完成",
    },
  };

  function readStoredUuid(key) {
    const value = localStorage.getItem(key);
    return value && UUID_PATTERN.test(value) ? value : null;
  }

  function readStoredConversationId() {
    const value = localStorage.getItem(CONVERSATION_ID_KEY);
    if (!value || !/^\d+$/.test(value)) return null;
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
  }

  const userId = readStoredUuid(USER_ID_KEY) || crypto.randomUUID();
  let conversationId = readStoredConversationId();
  let isStreaming = false;
  let activeController = null;
  let conversationEpoch = 0;
  let pendingTicket = null;
  let ticketSubmitting = false;
  let pendingRefund = null;
  let refundSubmitting = false;
  let activeTicketPreview = null;
  let sourceTrigger = null;

  localStorage.setItem(USER_ID_KEY, userId);

  const contextStatus = window.PawHelpContextStatus.create({
    element: document.querySelector("#contextCompressionStatus"),
    userId,
    getContext: () => ({ id: conversationId, epoch: conversationEpoch }),
  });
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) contextStatus.stop();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) contextStatus.refresh();
  });

  class PublicFacingError extends Error {}

  function syncBackgroundInert() {
    if (!pageMain) return;
    pageMain.inert = !sourceModal.hidden || !ticketModal.hidden || !refundModal.hidden;
  }

  function scrollToLatest() {
    messages.scrollTop = messages.scrollHeight;
  }

  function resizeInput() {
    if (!SUPPORTS_FIELD_SIZING) {
      input.style.overflowY = "hidden";
      input.style.height = "auto";
      const nextHeight = Math.min(Math.max(input.scrollHeight, 50), 126);
      input.style.height = `${nextHeight}px`;
      input.style.overflowY = input.scrollHeight > 126 ? "auto" : "hidden";
    }
    sendButton.disabled = isStreaming || !input.value.trim();
  }

  function addDayLabel() {
    const label = document.createElement("p");
    label.className = "day-label";
    label.textContent = "今天";
    messages.appendChild(label);
  }

  function addMessage(role, content = "", state = "normal") {
    const row = document.createElement("div");
    row.className = `message-row ${role}${state === "error" ? " error" : ""}`;

    if (role === "assistant") {
      const avatar = document.createElement("span");
      avatar.className = "mini-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = "喵";
      row.appendChild(avatar);
    }

    const bubble = document.createElement("div");
    bubble.className = "message";
    const copy = document.createElement("div");
    copy.className = "message-copy";
    copy.textContent = content;
    bubble.appendChild(copy);

    const traces = document.createElement("div");
    traces.className = "tool-traces";
    bubble.appendChild(traces);

    const feedback = document.createElement("div");
    feedback.className = "message-feedback";
    bubble.appendChild(feedback);

    const actions = document.createElement("div");
    actions.className = "suggested-actions";
    bubble.appendChild(actions);

    row.appendChild(bubble);
    messages.appendChild(row);
    scrollToLatest();
    return { row, bubble, copy, traces, feedback, actions, badges: new Map() };
  }

  function readFeedback() {
    try {
      const value = JSON.parse(localStorage.getItem(FEEDBACK_KEY) || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch (_) {
      return {};
    }
  }

  function renderFeedback(assistant, messageId) {
    if (!Number.isSafeInteger(messageId) || messageId < 1) return;
    const stored = readFeedback();
    const current = stored[String(messageId)];
    assistant.feedback.replaceChildren();
    for (const [value, label] of [
      ["up", "👍"],
      ["down", "👎"],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `feedback-button${current === value ? " selected" : ""}`;
      button.textContent = label;
      button.setAttribute("aria-label", value === "up" ? "解答有帮助" : "解答没有帮助");
      button.disabled = Boolean(current);
      button.addEventListener("click", async () => {
        const latest = readFeedback();
        if (latest[String(messageId)]) return;
        const cid = conversationId;
        for (const item of assistant.feedback.querySelectorAll("button")) item.disabled = true;
        try {
          const response = await fetch(
            `/v1/conversations/${encodeURIComponent(cid)}/messages/${encodeURIComponent(messageId)}/feedback`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-Demo-User-Id": userId },
              body: JSON.stringify({ vote: value }),
            },
          );
          if (!response.ok) throw new Error("feedback failed");
          const result = await response.json();
          const updated = readFeedback();
          updated[String(messageId)] = result.feedback;
          localStorage.setItem(FEEDBACK_KEY, JSON.stringify(updated));
          renderFeedback(assistant, messageId);
        } catch (_) {
          renderFeedback(assistant, messageId);
          const error = document.createElement("span");
          error.className = "feedback-label";
          error.textContent = "评价暂未保存，请稍后重试";
          assistant.feedback.appendChild(error);
        }
      });
      assistant.feedback.appendChild(button);
    }
    if (current) {
      const label = document.createElement("span");
      label.className = "feedback-label";
      label.textContent = current === "down" ? "已记录您的反馈，我们会尽快跟进完善" : "感谢您的认可";
      assistant.feedback.appendChild(label);
    }
  }

  function closeSourceModal() {
    sourceModal.hidden = true;
    syncBackgroundInert();
    if (sourceTrigger?.isConnected) sourceTrigger.focus();
    sourceTrigger = null;
  }

  async function openSource(source) {
    sourceTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    sourceModal.hidden = false;
    syncBackgroundInert();
    sourceMeta.textContent = `参考依据 [${source.number}] · 正在加载…`;
    sourceQuestion.textContent = "";
    sourceAnswer.textContent = "";
    sourceClose.focus();
    try {
      const response = await fetch(`/v1/knowledge/chunks/${encodeURIComponent(source.chunk_id)}`);
      if (!response.ok) throw new Error("source unavailable");
      const chunk = await response.json();
      sourceMeta.textContent = chunk.section_path
        ? `${chunk.category} · ${chunk.section_path}`
        : chunk.category || `参考依据 [${source.number}]`;
      sourceQuestion.textContent = chunk.questions || "";
      sourceAnswer.textContent = chunk.answer || "";
    } catch (_) {
      sourceMeta.textContent = `参考依据 [${source.number}]`;
      sourceAnswer.textContent = "参考内容暂时无法读取，请稍后重试。";
    }
  }

  function renderFinalAnswer(assistant, text, sources, messageId) {
    if (window.PawHelpMarkdown) window.PawHelpMarkdown.render(assistant.copy, text, sources, openSource);
    else assistant.copy.textContent = String(text || "");
    renderFeedback(assistant, messageId);
  }

  function errorMessage(payload, fallback) {
    const detail = payload?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message;
    if (detail?.code === "context_budget_exceeded") {
      return "当前消息内容较长，建议精简描述或点击右上角“新建会话”继续提问，原有历史记录仍会为您保留。";
    }
    return fallback;
  }

  function openTicketForm(messageId, button) {
    if (!Number.isSafeInteger(conversationId) || conversationId < 1) return;
    pendingTicket = { conversationId, messageId, button };
    ticketForm.reset();
    ticketError.hidden = true;
    ticketModal.hidden = false;
    syncBackgroundInert();
    ticketType.focus();
  }

  function closeTicketForm() {
    if (ticketSubmitting) return;
    ticketModal.hidden = true;
    syncBackgroundInert();
    ticketForm.reset();
    ticketError.hidden = true;
    const button = pendingTicket?.button;
    pendingTicket = null;
    if (button?.isConnected && !button.disabled) button.focus();
  }

  function formatTicketTime(utcValue) {
    const date = new Date(utcValue);
    if (Number.isNaN(date.getTime())) return "刚刚";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Hong_Kong",
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function formatSessionTime(utcValue) {
    if (!utcValue) return "";
    const date = new Date(utcValue);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Hong_Kong",
      hourCycle: "h23",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  async function submitTicketForm(event) {
    event.preventDefault();
    if (!pendingTicket || ticketSubmitting) return;
    const description = ticketDescription.value.trim();
    if (!ticketType.value || !description) {
      ticketError.textContent = "请选择反馈类别并填写具体问题描述。";
      ticketError.hidden = false;
      (ticketType.value ? ticketDescription : ticketType).focus();
      return;
    }
    const action = pendingTicket;
    const fallback = "工单暂时无法提交，请稍后重试；您填写的内容已为您保留。";
    ticketSubmitting = true;
    ticketSubmit.disabled = true;
    ticketCancel.disabled = true;
    ticketError.hidden = true;
    try {
      const response = await fetch(`/v1/conversations/${encodeURIComponent(action.conversationId)}/tickets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: userId,
          message_id: action.messageId,
          ticket_type: ticketType.value,
          description,
        }),
      });
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (_) {
          /* keep fallback */
        }
        throw new PublicFacingError(errorMessage(payload, fallback));
      }
      const ticket = await response.json();
      ticketSubmitting = false;
      action.button.disabled = true;
      closeTicketForm();
      addMessage(
        "assistant",
        `已为您登记服务工单：${ticket.ticket_no}（${ticket.status}），提交时间：${formatTicketTime(ticket.created_at)}。专属客服将尽快为您跟进。`,
      );
    } catch (error) {
      ticketError.textContent = error instanceof PublicFacingError ? error.message : fallback;
      ticketError.hidden = false;
    } finally {
      ticketSubmitting = false;
      ticketSubmit.disabled = false;
      ticketCancel.disabled = false;
    }
  }

  function openRefundForm(messageId, orderId, button) {
    if (!Number.isSafeInteger(conversationId) || !/^\d{4,20}$/.test(String(orderId))) return;
    pendingRefund = { conversationId, messageId, orderId, button };
    refundForm.reset();
    refundOrder.value = `订单 ${orderId}`;
    refundError.hidden = true;
    refundModal.hidden = false;
    syncBackgroundInert();
    refundReason.focus();
  }

  function closeRefundForm() {
    if (refundSubmitting) return;
    refundModal.hidden = true;
    syncBackgroundInert();
    refundForm.reset();
    refundError.hidden = true;
    const button = pendingRefund?.button;
    pendingRefund = null;
    if (button?.isConnected && !button.disabled) button.focus();
  }

  async function submitRefundForm(event) {
    event.preventDefault();
    if (!pendingRefund || refundSubmitting) return;
    if (!refundReason.value) {
      refundError.textContent = "请选择退款原因。";
      refundError.hidden = false;
      refundReason.focus();
      return;
    }
    const action = pendingRefund;
    const fallback = "退款申请暂时无法提交，请稍后重试；您填写的说明已为您保留。";
    refundSubmitting = true;
    refundSubmit.disabled = true;
    refundCancel.disabled = true;
    refundError.hidden = true;
    try {
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(action.conversationId)}/refund-requests`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: userId,
            message_id: action.messageId,
            order_id: action.orderId,
            reason: refundReason.value,
            description: refundDescription.value.trim() || null,
          }),
        },
      );
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (_) {
          /* keep fallback */
        }
        throw new PublicFacingError(errorMessage(payload, fallback));
      }
      const result = await response.json();
      refundSubmitting = false;
      action.button.disabled = true;
      closeRefundForm();
      addMessage(
        "assistant",
        `退款申请已提交：${result.request_no}（${result.status}），提交时间：${formatTicketTime(result.created_at)}。售后专员审核通过后将为您原路退回。`,
      );
    } catch (error) {
      refundError.textContent = error instanceof PublicFacingError ? error.message : fallback;
      refundError.hidden = false;
    } finally {
      refundSubmitting = false;
      refundSubmit.disabled = false;
      refundCancel.disabled = false;
    }
  }

  function renderSuggestedActions(assistant, actions, messageId, relatedOrderId = null) {
    assistant.actions.replaceChildren();
    if (!Array.isArray(actions) || !Number.isSafeInteger(messageId)) return;
    for (const action of ["转人工", "建工单", "申请退款"]) {
      if (!actions.includes(action)) continue;
      if (action === "申请退款" && !/^\d{4,20}$/.test(String(relatedOrderId))) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "suggested-action";
      button.textContent = action;
      button.addEventListener("click", async () => {
        if (action === "转人工") {
          if (!window.confirm("当前环境将为您展示模拟人工客服接入效果，是否继续？")) return;
          button.disabled = true;
          addMessage("assistant", "已为您转接在线客服专席（演示模式）");
          addMessage("assistant", "您好，这里是模拟人工客服专席。正式环境中可直接接入真人售后顾问为您跟进。");
          return;
        }
        if (action === "申请退款") {
          openRefundForm(messageId, relatedOrderId, button);
          return;
        }
        openTicketForm(messageId, button);
      });
      assistant.actions.appendChild(button);
    }
  }

  function showTyping(assistant) {
    const typing = document.createElement("span");
    typing.className = "typing-dots";
    typing.setAttribute("aria-label", "正在输入");
    for (let index = 0; index < 3; index += 1) {
      typing.appendChild(document.createElement("i"));
    }
    assistant.copy.replaceChildren(typing);
  }

  function renderStreamingText(assistant, text) {
    if (window.PawHelpMarkdown) window.PawHelpMarkdown.render(assistant.copy, text);
    else assistant.copy.textContent = text;
    const caret = document.createElement("span");
    caret.className = "streaming-caret";
    caret.setAttribute("aria-hidden", "true");
    assistant.copy.appendChild(caret);
    scrollToLatest();
  }

  function normalizeToolKey(label, name) {
    if (typeof name === "string" && TOOL_ACTION_COPY[name]) return name;
    const clean = String(label || "")
      .replace(/（MCP.*?）|\(MCP.*?\)/g, "")
      .trim();
    return clean;
  }

  function toolBadgeText(label, status, name) {
    const key = normalizeToolKey(label, name);
    const preset = TOOL_ACTION_COPY[key];
    if (preset) {
      if (status === "succeeded") return `✓ ${preset.succeeded}`;
      if (status === "failed") return `！${preset.failed}`;
      return `↻ ${preset.running}`;
    }
    const cleanLabel = key || "服务查询";
    if (status === "succeeded") return `✓ 已完成${cleanLabel}`;
    if (status === "failed") return `！${cleanLabel}暂未完成`;
    return `↻ 正在${cleanLabel}…`;
  }

  function updateToolTracePresentation(assistant) {
    const count = assistant.badges.size;
    if (!count) {
      assistant.traces.classList.remove("reported");
      assistant.traces.replaceChildren();
      return;
    }
    assistant.traces.classList.add("reported");

    let toggle = assistant.traces.querySelector(".tool-trace-toggle");
    if (count > 3 && !toggle) {
      toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "tool-trace-toggle";
      toggle.addEventListener("click", () => {
        assistant.traceExpanded = !assistant.traceExpanded;
        updateToolTracePresentation(assistant);
        scrollToLatest();
      });
      assistant.traces.appendChild(toggle);
    }
    if (toggle) {
      assistant.traces.appendChild(toggle);
      toggle.hidden = count <= 3;
      toggle.textContent = assistant.traceExpanded ? "收起服务记录" : `查看全部 ${count} 项服务记录`;
    }
    let index = 0;
    for (const badge of assistant.badges.values()) {
      badge.hidden = count > 3 && !assistant.traceExpanded && index >= 3;
      index += 1;
    }
  }

  function upsertToolBadge(assistant, callId, label, status, name = "") {
    if (typeof callId !== "string" || !callId) return;
    if (typeof label !== "string" || !label) return;
    const safeStatus = ["running", "succeeded", "failed"].includes(status) ? status : "running";
    let badge = assistant.badges.get(callId);
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "tool-badge";
      assistant.badges.set(callId, badge);
      assistant.traces.appendChild(badge);
    }
    badge.dataset.label = label;
    badge.dataset.name = name || "";
    badge.className = `tool-badge ${safeStatus}`;
    badge.textContent = toolBadgeText(label, safeStatus, name);
    updateToolTracePresentation(assistant);
    scrollToLatest();
  }

  function markRunningBadgesFailed(assistant) {
    for (const badge of assistant.badges.values()) {
      if (badge.classList.contains("running")) {
        badge.className = "tool-badge failed";
        badge.textContent = toolBadgeText(badge.dataset.label, "failed", badge.dataset.name);
      }
    }
  }

  function addSuggestions() {
    const suggestions = document.createElement("div");
    suggestions.className = "suggestions";
    suggestions.id = "suggestions";
    for (const item of QUICK_PROMPTS) {
      const button = document.createElement("button");
      button.className = "suggestion";
      button.type = "button";

      const tag = document.createElement("span");
      tag.className = "suggestion-tag";
      tag.textContent = item.tag;

      const title = document.createElement("strong");
      title.className = "suggestion-title";
      title.textContent = item.title;

      const desc = document.createElement("span");
      desc.className = "suggestion-desc";
      desc.textContent = item.desc;

      button.append(tag, title, desc);
      button.addEventListener("click", () => sendMessage(item.prompt));
      suggestions.appendChild(button);
    }
    messages.appendChild(suggestions);
  }

  function renderWelcome() {
    activeTicketPreview = null;
    messages.replaceChildren();
    addDayLabel();
    const welcome = addMessage(
      "assistant",
      "你好呀，我是 PawHelp 客服小喵 🐾\n订单物流追踪、智能宠物设备排障、退换货或发票咨询都可以交给我，点下方快捷贴纸即可一键提问：",
    );
    welcome.row.id = "welcomeGreeting";
    addSuggestions();
    scrollToLatest();
  }

  function parseEventBlock(block) {
    let eventName = "message";
    const dataLines = [];

    for (const line of block.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") eventName = value;
      if (field === "data") dataLines.push(value);
    }

    if (!dataLines.length) return null;
    try {
      return { event: eventName, data: JSON.parse(dataLines.join("\n")) };
    } catch (_) {
      throw new PublicFacingError("网络响应出现波动，请重新发送试试。");
    }
  }

  async function readEventStream(response, onEvent) {
    if (!response.body) throw new PublicFacingError("当前环境暂不支持实时消息流，请刷新重试。");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const drain = (flush = false) => {
      if (flush) buffer += decoder.decode();
      let match;
      while ((match = buffer.match(/\r?\n\r?\n/))) {
        const block = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const parsed = parseEventBlock(block);
        if (parsed) onEvent(parsed.event, parsed.data);
      }
      if (flush && buffer.trim()) {
        const parsed = parseEventBlock(buffer);
        if (parsed) onEvent(parsed.event, parsed.data);
        buffer = "";
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      drain();
    }
    drain(true);
  }

  function startOperation() {
    if (activeController) activeController.abort();
    const operation = {
      controller: new AbortController(),
      epoch: conversationEpoch,
    };
    activeController = operation.controller;
    return operation;
  }

  function isCurrentOperation(operation) {
    return (
      operation.epoch === conversationEpoch &&
      activeController === operation.controller &&
      !operation.controller.signal.aborted
    );
  }

  function finishOperation(operation) {
    if (activeController === operation.controller) activeController = null;
  }

  function applyToolTrace(assistant, trace) {
    for (const item of Array.isArray(trace) ? trace : []) {
      if (!item || typeof item !== "object") continue;
      upsertToolBadge(
        assistant,
        item.call_id || item.tool_call_id,
        item.label,
        item.status,
        item.name || "",
      );
    }
    updateToolTracePresentation(assistant);
  }

  function renderHistory(history) {
    messages.replaceChildren();
    addDayLabel();
    let latestUserText = "";
    for (const item of history) {
      if (!item || !["user", "assistant"].includes(item.role)) continue;
      const rendered = addMessage(
        item.role,
        typeof item.content === "string" ? item.content : "",
      );
      if (item.role === "user" && typeof item.content === "string") {
        latestUserText = item.content;
      }
      if (item.role === "assistant") {
        applyToolTrace(rendered, item.tool_trace);
        renderFinalAnswer(rendered, item.content, item.sources, item.id);
        renderSuggestedActions(rendered, item.suggested_actions, item.id, item.related_order_id);
      }
    }
    if (!history.length) {
      renderWelcome();
      return;
    }
    conversationPreview.textContent = latestUserText || "已恢复当前对话";
    scrollToLatest();
  }

  let conversationListEpoch = 0;
  async function refreshConversationList() {
    const epoch = ++conversationListEpoch;
    try {
      const response = await fetch("/api/conversations", { headers: { "X-Demo-User-Id": userId } });
      if (!response.ok) return;
      const rows = await response.json();
      if (epoch !== conversationListEpoch || !Array.isArray(rows)) return;
      renderConversationList(rows);
      return rows;
    } catch (_) {
      /* silent degradation, current conversation stays usable */
    }
  }

  async function refreshConversationTitle() {
    const rows = await refreshConversationList();
    if (rows?.some((row) => row.id === conversationId && !row.title)) {
      setTimeout(refreshConversationList, 3000);
    }
  }

  function renderConversationList(rows) {
    const list = document.querySelector("#conversationList");
    list.replaceChildren();
    for (const row of rows) {
      if (!Number.isSafeInteger(row.id) || row.id < 1) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "session-button";
      button.setAttribute("aria-current", row.id === conversationId ? "true" : "false");
      const label = document.createElement("span");
      label.className = "session-title";
      label.textContent = row.preview || "新咨询";
      button.appendChild(label);
      const badge = document.createElement("small");
      const timeLabel = formatSessionTime(row.created_at);
      badge.textContent = timeLabel ? `${timeLabel} · ${row.status || "咨询中"}` : `服务单 #${row.id}`;
      button.appendChild(badge);
      button.addEventListener("click", () => switchConversation(row.id));
      list.appendChild(button);
    }
  }

  async function switchConversation(id) {
    if (ticketSubmitting || refundSubmitting || isStreaming || id === conversationId) return;
    activeTicketPreview = null;
    closeTicketForm();
    closeRefundForm();
    closeSourceModal();
    conversationEpoch += 1;
    if (activeController) activeController.abort();
    activeController = null;
    conversationId = id;
    contextStatus.refresh();
    localStorage.setItem(CONVERSATION_ID_KEY, String(id));
    input.value = "";
    messages.replaceChildren();
    await hydrateConversation();
    refreshConversationList();
  }

  async function hydrateConversation() {
    if (!conversationId) {
      renderWelcome();
      return;
    }

    const requestedConversationId = conversationId;
    const operation = startOperation();
    isStreaming = true;
    agentStatus.textContent = "正在同步对话记录…";
    resizeInput();

    try {
      const response = await fetch(
        `/v1/conversations/${encodeURIComponent(requestedConversationId)}/messages`,
        {
          headers: { "X-Demo-User-Id": userId },
          signal: operation.controller.signal,
        },
      );
      if (!isCurrentOperation(operation)) return;

      if (response.status === 404) {
        renderWelcome();
        return;
      }
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (_) {
          /* keep fallback */
        }
        throw new PublicFacingError(errorMessage(payload, `加载对话记录失败（${response.status}）`));
      }

      let history;
      try {
        history = await response.json();
      } catch (_) {
        throw new PublicFacingError("对话记录加载异常，请刷新重试。");
      }
      if (!Array.isArray(history)) {
        throw new PublicFacingError("对话记录加载异常，请刷新重试。");
      }
      if (isCurrentOperation(operation)) {
        const savedFeedback = readFeedback();
        for (const message of history) {
          if (message.feedback) savedFeedback[String(message.id)] = message.feedback;
        }
        localStorage.setItem(FEEDBACK_KEY, JSON.stringify(savedFeedback));
        renderHistory(history);
        activeTicketPreview = null;
        const pendingResponse = await fetch(
          `/v1/conversations/${encodeURIComponent(requestedConversationId)}/pending-order`,
          { headers: { "X-Demo-User-Id": userId }, signal: operation.controller.signal },
        );
        if (pendingResponse.ok && isCurrentOperation(operation)) {
          const pending = await pendingResponse.json();
          if (pending.pending && pending.selection) {
            renderOrderSelection(addMessage("assistant"), pending.selection);
          }
        }
        const ticketResponse = await fetch(
          `/v1/conversations/${encodeURIComponent(requestedConversationId)}/pending-ticket`,
          { headers: { "X-Demo-User-Id": userId }, signal: operation.controller.signal },
        );
        if (ticketResponse.ok && isCurrentOperation(operation)) {
          const pending = await ticketResponse.json();
          if (pending.pending && pending.confirmation) {
            renderTicketConfirmation(addMessage("assistant"), pending.confirmation);
          }
        }
      }
    } catch (error) {
      if (error.name === "AbortError" || !isCurrentOperation(operation)) return;
      renderWelcome();
      const failure = addMessage("assistant", "", "error");
      failure.copy.textContent =
        error instanceof PublicFacingError ? error.message : "对话暂时无法恢复，请稍后刷新重试。";
    } finally {
      if (isCurrentOperation(operation)) {
        finishOperation(operation);
        isStreaming = false;
        agentStatus.textContent = DEFAULT_AGENT_STATUS;
        resizeInput();
        input.focus();
        scrollToLatest();
      }
    }
  }

  function orderStatusModifier(statusText) {
    const value = String(statusText || "");
    if (value.includes("签收") || value.includes("完成")) return " delivered";
    if (value.includes("待") || value.includes("处理")) return " pending";
    return "";
  }

  function renderOrderSelection(assistant, payload) {
    assistant.copy.textContent = payload.message || "为您找到以下订单，请点选需要处理的订单小票：";
    assistant.actions.replaceChildren();
    const choices = document.createElement("div");
    choices.className = "order-choice-list";
    const orders = Array.isArray(payload.orders) ? payload.orders : [];
    for (const order of orders) {
      if (!order || !/^\d{4,20}$/.test(String(order.order_id))) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "order-choice";

      const head = document.createElement("span");
      head.className = "order-receipt-head";
      const idSpan = document.createElement("span");
      idSpan.className = "order-receipt-id";
      idSpan.textContent = order.label || `订单号 #${order.order_id}`;
      const stamp = document.createElement("span");
      const statusText = order.status_label || "处理中";
      stamp.className = `order-status-stamp${orderStatusModifier(statusText)}`;
      stamp.textContent = statusText;
      head.append(idSpan, stamp);

      const body = document.createElement("span");
      body.className = "order-receipt-body";
      const product = document.createElement("strong");
      product.className = "order-product-name";
      product.textContent = order.item || order.product || "PawHelp 官方宠物商品";
      const amount = document.createElement("span");
      amount.className = "order-amount";
      amount.textContent = `¥${order.amount || "—"}`;
      body.append(product, amount);

      const foot = document.createElement("span");
      foot.className = "order-receipt-foot";
      const timeInfo = document.createElement("small");
      timeInfo.className = "order-receipt-time";
      if (order.delivered_at) {
        timeInfo.textContent = `签收时间：${formatTicketTime(order.delivered_at)}`;
      } else if (order.created_at) {
        timeInfo.textContent = `下单时间：${formatTicketTime(order.created_at)}`;
      } else {
        timeInfo.textContent = `单号 ${order.order_id}`;
      }
      const cta = document.createElement("span");
      cta.className = "order-receipt-cta";
      cta.textContent = "选择此订单 →";
      foot.append(timeInfo, cta);

      button.append(head, body, foot);
      button.addEventListener("click", () =>
        resumeSelectedOrder(order.order_id, payload.message_id, choices),
      );
      choices.appendChild(button);
    }
    assistant.actions.appendChild(choices);
    scrollToLatest();
  }

  function renderTicketConfirmation(assistant, payload) {
    activeTicketPreview = payload;
    assistant.copy.textContent = "已为您整理好服务工单小票，核对无误后点击“确认提交”即可生效：";
    assistant.actions.replaceChildren();
    const card = document.createElement("div");
    card.className = "order-choice-list";
    card.dataset.ticketPreview = payload.proposal_id;

    const head = document.createElement("div");
    head.className = "ticket-receipt-head";
    const headTitle = document.createElement("span");
    headTitle.textContent = "📋 服务工单确认小票";
    const tag = document.createElement("span");
    tag.className = "ticket-receipt-tag";
    tag.textContent = `${payload.ticket_type || "售后"}工单`;
    head.append(headTitle, tag);

    const description = document.createElement("p");
    description.className = "ticket-preview-description";
    description.textContent = `诉求详情：${payload.description}`;
    card.append(head, description);

    for (const [text, confirmed] of [
      ["确认提交", true],
      ["取消", false],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "order-choice";
      button.textContent = text;
      button.addEventListener("click", () => resumeTicketConfirmation(payload, confirmed, card));
      card.appendChild(button);
    }
    assistant.actions.appendChild(card);
    scrollToLatest();
  }

  async function resumeTicketConfirmation(payload, confirmed, card) {
    if (isStreaming || !conversationId || activeTicketPreview?.proposal_id !== payload.proposal_id) {
      return;
    }
    for (const button of card.querySelectorAll("button")) button.disabled = true;
    isStreaming = true;
    const assistant = addMessage("assistant");
    showTyping(assistant);
    const operation = startOperation();
    try {
      const response = await fetch(`/v1/conversations/${encodeURIComponent(conversationId)}/confirm-ticket`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({
          user_id: userId,
          message_id: payload.message_id,
          proposal_id: payload.proposal_id,
          confirmed,
        }),
        signal: operation.controller.signal,
      });
      if (!response.ok) throw new PublicFacingError("该工单确认小票已处理或已过期，请刷新核对。");
      await consumeChatResponse(response, assistant, operation);
      if (activeTicketPreview?.proposal_id === payload.proposal_id) activeTicketPreview = null;
      const result = document.createElement("p");
      result.textContent = confirmed ? "✓ 已提交工单确认，处理结果请见上方回复。" : "已取消本次工单登记。";
      card.replaceChildren(result);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrentOperation(operation)) return;
      markRunningBadgesFailed(assistant);
      assistant.copy.textContent =
        error instanceof PublicFacingError
          ? error.message
          : "确认请求未成功送达，请刷新页面核对，系统不会重复建单。";
      for (const button of card.querySelectorAll("button")) button.disabled = false;
    } finally {
      if (isCurrentOperation(operation)) {
        finishOperation(operation);
        isStreaming = false;
        resizeInput();
      }
    }
  }

  async function consumeChatResponse(response, assistant, operation) {
    let assistantText = "";
    let completed = false;
    await readEventStream(response, (event, data) => {
      if (!isCurrentOperation(operation)) return;
      if (event === "conversation" && Number.isSafeInteger(data.conversation_id) && data.conversation_id > 0) {
        const changed = conversationId !== data.conversation_id;
        conversationId = data.conversation_id;
        if (changed) contextStatus.refresh();
        localStorage.setItem(CONVERSATION_ID_KEY, String(conversationId));
        refreshConversationList();
      } else if (event === "tool_status") {
        upsertToolBadge(
          assistant,
          data.call_id,
          data.label,
          data.status,
          data.name || "",
        );
      } else if (event === "token") {
        assistantText += data.content ?? "";
        renderStreamingText(assistant, assistantText);
      } else if (event === "order_selection") {
        renderOrderSelection(assistant, data);
        completed = true;
      } else if (event === "ticket_confirmation") {
        renderTicketConfirmation(assistant, data);
        completed = true;
      } else if (event === "done") {
        assistantText = data.content ?? assistantText;
        renderFinalAnswer(assistant, assistantText, data.sources, data.message_id);
        applyToolTrace(assistant, data.tool_trace);
        renderSuggestedActions(assistant, data.suggested_actions, data.message_id, data.related_order_id);
        completed = true;
        refreshConversationTitle();
      } else if (event === "error") {
        throw new PublicFacingError(data.message || "客服小喵刚刚忙线了，请稍后再试一下。");
      }
    });
    if (!completed) throw new PublicFacingError("消息传输中断，请重新发送试试。");
  }

  async function resumeSelectedOrder(orderId, messageId, choices) {
    if (isStreaming || !conversationId) return;
    for (const button of choices.querySelectorAll("button")) button.disabled = true;
    addMessage("user", `已选择订单 ${orderId}`);
    isStreaming = true;
    agentStatus.textContent = "正在核对订单与物流状态…";
    const assistant = addMessage("assistant");
    showTyping(assistant);
    const operation = startOperation();
    try {
      const response = await fetch(`/v1/conversations/${encodeURIComponent(conversationId)}/select-order`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({ user_id: userId, order_id: orderId, message_id: messageId }),
        signal: operation.controller.signal,
      });
      if (!response.ok) throw new PublicFacingError("该订单选择已过期，请重新告诉我您的诉求。");
      await consumeChatResponse(response, assistant, operation);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrentOperation(operation)) return;
      markRunningBadgesFailed(assistant);
      assistant.row.classList.add("error");
      assistant.copy.textContent =
        error instanceof PublicFacingError ? error.message : "订单查询暂时遇到问题，请重试。";
      for (const button of choices.querySelectorAll("button")) button.disabled = false;
    } finally {
      if (isCurrentOperation(operation)) {
        finishOperation(operation);
        isStreaming = false;
        agentStatus.textContent = DEFAULT_AGENT_STATUS;
        input.focus();
      }
    }
  }

  async function sendMessage(text) {
    if (isStreaming || !text.trim()) return;
    if (activeTicketPreview) {
      addMessage("assistant", "请先在上方工单确认小票中点击“确认提交”或“取消”，再继续发送新问题哦。");
      return;
    }

    const welcomeGreeting = document.querySelector("#welcomeGreeting");
    if (welcomeGreeting) welcomeGreeting.remove();
    const suggestions = document.querySelector("#suggestions");
    if (suggestions) suggestions.remove();

    const normalizedText = text.trim();
    addMessage("user", normalizedText);
    conversationPreview.textContent = normalizedText;

    input.value = "";
    isStreaming = true;
    resizeInput();
    agentStatus.textContent = "正在为您查询解答…";

    const assistant = addMessage("assistant");
    showTyping(assistant);
    const operation = startOperation();

    try {
      const response = await fetch("/v1/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({
          user_id: userId,
          conversation_id: conversationId,
          message: normalizedText,
        }),
        signal: operation.controller.signal,
      });

      if (!isCurrentOperation(operation)) return;

      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (_) {
          /* keep fallback */
        }
        throw new PublicFacingError(errorMessage(payload, `服务请求失败（${response.status}）`));
      }

      await consumeChatResponse(response, assistant, operation);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrentOperation(operation)) return;
      markRunningBadgesFailed(assistant);
      assistant.row.classList.add("error");
      assistant.copy.textContent =
        error instanceof PublicFacingError ? error.message : "网络连接异常，请稍后再试。";
    } finally {
      if (isCurrentOperation(operation)) {
        finishOperation(operation);
        isStreaming = false;
        agentStatus.textContent = DEFAULT_AGENT_STATUS;
        resizeInput();
        input.focus();
        scrollToLatest();
      }
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage(input.value);
  });

  input.addEventListener("input", resizeInput);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      // Guard Chinese/CJK IME candidate confirmation across Chromium, Firefox, and Safari.
      if (event.isComposing || event.keyCode === 229) {
        return;
      }
      form.requestSubmit();
    }
  });

  newChatButton.addEventListener("click", () => {
    if (ticketSubmitting || refundSubmitting) return;
    closeTicketForm();
    closeRefundForm();
    closeSourceModal();
    conversationEpoch += 1;
    if (activeController) activeController.abort();
    activeController = null;
    conversationId = null;
    contextStatus.refresh();
    localStorage.removeItem(CONVERSATION_ID_KEY);
    renderWelcome();
    conversationPreview.textContent = "随时为您解答订单与售后问题";
    agentStatus.textContent = DEFAULT_AGENT_STATUS;
    isStreaming = false;
    resizeInput();
    input.focus();
    refreshConversationList();
  });

  sourceClose.addEventListener("click", closeSourceModal);
  sourceModal.addEventListener("click", (event) => {
    if (event.target === sourceModal) closeSourceModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !sourceModal.hidden) closeSourceModal();
    if (event.key === "Escape" && !ticketModal.hidden) closeTicketForm();
    if (event.key === "Escape" && !refundModal.hidden) closeRefundForm();
  });
  ticketForm.addEventListener("submit", submitTicketForm);
  ticketCancel.addEventListener("click", closeTicketForm);
  ticketModal.addEventListener("click", (event) => {
    if (event.target === ticketModal) closeTicketForm();
  });
  refundForm.addEventListener("submit", submitRefundForm);
  refundCancel.addEventListener("click", closeRefundForm);
  refundModal.addEventListener("click", (event) => {
    if (event.target === refundModal) closeRefundForm();
  });

  resizeInput();
  hydrateConversation();
  refreshConversationList();
})().catch(() => {
  const warning = document.createElement("p");
  warning.className = "init-warning";
  warning.setAttribute("role", "alert");
  warning.textContent = "页面服务初始化未完成，请刷新页面后重试。";
  document.body.prepend(warning);
  document
    .querySelectorAll("form input, form textarea, form select, form button")
    .forEach((element) => {
      element.disabled = true;
    });
});
