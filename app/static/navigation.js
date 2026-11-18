"use strict";
// Separate customer and operator navigation; authorization is enforced on the server.
(() => {
  const ADMIN_TOKEN_KEY = "pawhelp.adminToken";
  const customerPage = location.pathname === "/";
  const pages = customerPage
    ? [["/admin", "管理后台"]]
    : [
        ["/kb", "知识库"],
        ["/review", "知识审核"],
        ["/topics", "主题分布"],
        ["/rag-eval", "检索评测"],
        ["/observability", "用量与成本"],
        ["/acceptance", "分类模型"],
      ];

  function readToken() {
    try {
      return (sessionStorage.getItem(ADMIN_TOKEN_KEY) || "").trim();
    } catch {
      return "";
    }
  }

  function writeToken(value) {
    const normalized = String(value || "").trim();
    try {
      if (normalized) sessionStorage.setItem(ADMIN_TOKEN_KEY, normalized);
      else sessionStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch {
      /* sessionStorage unavailable */
    }
  }

  function attachDialogLightDismiss(dialog) {
    if ("closedBy" in HTMLDialogElement.prototype) return;
    const outside = (event) => {
      const rect = dialog.getBoundingClientRect();
      return (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      );
    };
    let pressedOutside = false;
    dialog.addEventListener("pointerdown", (event) => {
      pressedOutside = outside(event);
    });
    dialog.addEventListener("pointerup", (event) => {
      if (pressedOutside && outside(event)) dialog.close();
    });
  }

  function buildTokenDialog(updateTrigger) {
    const dialog = document.createElement("dialog");
    dialog.className = "console-token-dialog";
    dialog.setAttribute("closedby", "any");
    dialog.setAttribute("aria-labelledby", "consoleTokenHeading");
    const form = document.createElement("form");
    form.method = "dialog";
    const heading = document.createElement("h2");
    heading.id = "consoleTokenHeading";
    heading.textContent = "管理后台访问凭证";
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.textContent =
      "本机同源访问免密直连；局域网或远程访问请输入管理员访问密钥（仅保存在当前标签页会话）。";
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.setAttribute("aria-label", "管理员访问密钥");
    input.placeholder = "请输入管理员访问密钥";
    const actions = document.createElement("div");
    actions.className = "console-token-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => dialog.close());
    const clear = document.createElement("button");
    clear.type = "button";
    clear.textContent = "清除凭证";
    clear.addEventListener("click", () => {
      writeToken("");
      updateTrigger();
      dialog.close();
      location.reload();
    });
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "primary";
    save.textContent = "保存并刷新";
    actions.append(cancel, clear, save);
    form.append(heading, hint, input, actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      writeToken(input.value);
      updateTrigger();
      dialog.close();
      location.reload();
    });
    dialog.append(form);
    attachDialogLightDismiss(dialog);
    document.body.append(dialog);
    return {
      open() {
        input.value = readToken();
        dialog.showModal();
        input.focus();
      },
    };
  }

  document.body.classList.add(customerPage ? "customer-page" : "console-page");
  document.querySelectorAll("[data-app-navigation]").forEach((container) => {
    container.classList.add("app-nav");
    container.setAttribute("aria-label", "全局页面导航");
    const links = pages.map(([path, label]) => {
      const link = document.createElement("a");
      link.href = path;
      link.textContent = label;
      if (
        location.pathname.replace(/\/$/, "") === path.replace(/\/$/, "") ||
        (path !== "/" && location.pathname.startsWith(path + "/"))
      ) {
        link.setAttribute("aria-current", "page");
      }
      return link;
    });
    container.replaceChildren(...links);
    if (!customerPage) {
      const sidebar = document.createElement("aside");
      sidebar.className = "console-sidebar";
      sidebar.setAttribute("aria-label", "PawHelp 管理后台");
      const brand = document.createElement("a");
      brand.className = "console-brand";
      brand.href = "/admin";
      brand.textContent = "PawHelp";
      const caption = document.createElement("p");
      caption.className = "console-caption";
      caption.textContent = "客户服务与知识运营中心";
      const bottom = document.createElement("div");
      bottom.className = "console-sidebar-bottom";
      const chat = document.createElement("a");
      chat.href = "/";
      chat.textContent = "返回智能客服前台 ↗";
      const mode = document.createElement("small");
      mode.textContent = "官方客服运营环境";
      const tokenTrigger = document.createElement("button");
      tokenTrigger.type = "button";
      tokenTrigger.className = "console-token-button";
      const refreshLabel = () => {
        tokenTrigger.textContent = readToken() ? "已配置访问凭证" : "设置访问凭证";
      };
      refreshLabel();
      let modal = null;
      tokenTrigger.addEventListener("click", () => {
        if (!modal) modal = buildTokenDialog(refreshLabel);
        modal.open();
      });
      bottom.append(chat, mode, tokenTrigger);
      sidebar.append(brand, caption, container, bottom);
      document.body.prepend(sidebar);
    }
  });
})();
