"use strict";
(() => {
  const ADMIN_TOKEN_KEY = "pawhelp.adminToken";
  const byId = (id) => document.getElementById(id);
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  };

  function getAdminToken() {
    try {
      return (sessionStorage.getItem(ADMIN_TOKEN_KEY) || "").trim();
    } catch {
      return "";
    }
  }

  function setAdminToken(token) {
    const normalized = String(token || "").trim();
    try {
      if (normalized) sessionStorage.setItem(ADMIN_TOKEN_KEY, normalized);
      else sessionStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch {
      /* sessionStorage unavailable */
    }
  }

  function adminHeaders(extra = {}) {
    const headers = { "X-PawHelp-Local-Admin": "1", ...extra };
    const token = getAdminToken();
    if (token) headers["X-Admin-Token"] = token;
    return headers;
  }

  async function api(path, method = "GET", body) {
    const response = await fetch(path, {
      method,
      cache: "no-store",
      headers: adminHeaders({ "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      /* keep null */
    }
    if (!response.ok) {
      const detail = data?.detail;
      throw new Error(
        typeof detail === "string"
          ? detail
          : detail?.message || `服务请求异常（状态码 ${response.status}）`,
      );
    }
    return data;
  }

  async function busy(button, work) {
    button.disabled = true;
    try {
      await work();
    } catch (error) {
      if (byId("status")) byId("status").textContent = error.message;
      if (byId("detailStatus")) byId("detailStatus").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  function table(headers, rows) {
    const t = node("table");
    const h = node("tr");
    headers.forEach((x) => {
      const th = node("th", x);
      th.scope = "col";
      h.append(th);
    });
    const head = node("thead");
    head.append(h);
    t.append(head);
    const body = node("tbody");
    rows.forEach((row) => {
      const r = node("tr");
      row.forEach((x) => r.append(node("td", x ?? "—")));
      body.append(r);
    });
    t.append(body);
    return t;
  }

  const number = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : "—";

  window.PawHelpOperations = {
    byId,
    node,
    api,
    busy,
    table,
    number,
    adminHeaders,
    getAdminToken,
    setAdminToken,
  };
})();
