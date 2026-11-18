// Offline headless browser runner for DOMPurify XSS, streaming Markdown, and strict CSP3 assertions.
"use strict";

const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const SERVER_SCRIPT = path.join(ROOT, "tests", "browser", "markdown_server.cjs");
const STATIC_ASSERTIONS = path.join(ROOT, "tests", "browser", "markdown_assertions.js");
const STREAM_ASSERTIONS = path.join(ROOT, "tests", "browser", "markdown_stream_assertions.js");
const UPSTREAM_PORT = 8879;

const STRICT_CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; " +
  "img-src 'self' data:; font-src 'self'; connect-src 'self'; " +
  "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'";

function findBrowserBinary() {
  const envCandidates = [
    process.env.BROWSER_BIN,
    process.env.CHROME_BIN,
    process.env.EDGE_BIN,
  ].filter(Boolean);
  for (const candidate of envCandidates) {
    if (fs.existsSync(candidate)) return candidate;
  }

  const commandNames = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "msedge",
  ];
  const locator = process.platform === "win32" ? "where.exe" : "which";
  for (const cmd of commandNames) {
    try {
      const out = execFileSync(locator, [cmd], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      });
      const first = out
        .split(/\r?\n/)
        .map((line) => line.trim())
        .find((line) => line && fs.existsSync(line));
      if (first) return first;
    } catch (_) {
      // Try next candidate.
    }
  }

  const knownPaths =
    process.platform === "win32"
      ? [
          "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
          "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
          "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
          "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        ]
      : process.platform === "darwin"
        ? [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
          ]
        : [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
          ];
  for (const candidate of knownPaths) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(
    "No headless Chromium/Chrome/Edge binary found. Set CHROME_BIN or BROWSER_BIN.",
  );
}

function startFixtureServer() {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SERVER_SCRIPT], {
      cwd: ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`Timed out starting markdown_server.cjs: ${stderr}`));
    }, 10000);

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      if (chunk.includes("Markdown browser fixture")) {
        clearTimeout(timer);
        resolve(child);
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`markdown_server.cjs exited early (${code}): ${stderr}`));
    });
  });
}

function startCspProxy() {
  return new Promise((resolve, reject) => {
    const proxy = http.createServer((clientReq, clientRes) => {
      const upstreamReq = http.request(
        {
          hostname: "127.0.0.1",
          port: UPSTREAM_PORT,
          path: clientReq.url,
          method: clientReq.method,
          headers: { ...clientReq.headers, host: `127.0.0.1:${UPSTREAM_PORT}` },
        },
        (upstreamRes) => {
          const headers = {
            ...upstreamRes.headers,
            "content-security-policy": STRICT_CSP,
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "same-origin",
          };
          if ((clientReq.url || "").split("?")[0] === "/static/chat-app.js") {
            const chunks = [];
            upstreamRes.on("data", (chunk) => chunks.push(chunk));
            upstreamRes.on("end", () => {
              const original = Buffer.concat(chunks).toString("utf8");
              const target = "})().catch(() => {";
              const hook =
                "  Object.assign(window, { addMessage, renderFinalAnswer, renderStreamingText, renderHistory });\n" +
                "  window.__pawhelpChatReady = true;\n})().catch(() => {";
              if (!original.includes(target)) {
                throw new Error("Expected startChat IIFE tail not found in chat-app.js");
              }
              const instrumented = original.replace(target, hook);
              delete headers["content-length"];
              delete headers["transfer-encoding"];
              clientRes.writeHead(upstreamRes.statusCode || 200, headers);
              clientRes.end(instrumented, "utf8");
            });
            return;
          }
          clientRes.writeHead(upstreamRes.statusCode || 200, headers);
          upstreamRes.pipe(clientRes);
        },
      );
      upstreamReq.on("error", (err) => {
        clientRes.writeHead(502);
        clientRes.end(String(err));
      });
      clientReq.pipe(upstreamReq);
    });
    proxy.once("error", reject);
    proxy.listen(0, "127.0.0.1", () => {
      const addr = proxy.address();
      resolve({ proxy, url: `http://127.0.0.1:${addr.port}` });
    });
  });
}

function launchHeadlessBrowser(binaryPath, userDataDir) {
  return new Promise((resolve, reject) => {
    const args = [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--disable-extensions",
      "--no-first-run",
      "--no-default-browser-check",
      "--window-size=1280,900",
      `--user-data-dir=${userDataDir}`,
      "--remote-debugging-port=0",
      "about:blank",
    ];
    const proc = spawn(binaryPath, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    const timer = setTimeout(() => {
      proc.kill();
      reject(new Error(`Timed out waiting for DevTools port: ${stderr}`));
    }, 15000);

    proc.stderr.setEncoding("utf8");
    proc.stderr.on("data", (chunk) => {
      stderr += chunk;
      const match = stderr.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//);
      if (match) {
        clearTimeout(timer);
        resolve({ proc, port: Number(match[1]) });
      }
    });
    proc.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`Browser exited before DevTools ready (${code}): ${stderr}`));
    });
  });
}

async function connectPageCdp(port) {
  const deadline = Date.now() + 10000;
  let wsUrl = null;
  while (Date.now() < deadline) {
    const listResp = await fetch(`http://127.0.0.1:${port}/json/list`);
    const targets = await listResp.json();
    const page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
    if (page) {
      wsUrl = page.webSocketDebuggerUrl;
      break;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  if (!wsUrl) throw new Error("No debuggable page target exposed by headless browser");

  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", () => resolve(), { once: true });
    ws.addEventListener("error", (err) => reject(err), { once: true });
  });

  let nextId = 1;
  const pending = new Map();
  const runtimeErrors = [];
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(String(event.data));
    if (msg.method === "Runtime.exceptionThrown") {
      runtimeErrors.push(
        msg.params?.exceptionDetails?.exception?.description ||
          msg.params?.exceptionDetails?.text ||
          JSON.stringify(msg.params),
      );
    }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject, timer } = pending.get(msg.id);
      clearTimeout(timer);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`CDP ${msg.error.message}`));
      else resolve(msg.result);
    }
  });

  function send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`CDP timeout calling ${method}`));
      }, 20000);
      pending.set(id, { resolve, reject, timer });
      ws.send(JSON.stringify({ id, method, params }));
    });
  }

  return { ws, send, runtimeErrors };
}

async function evaluateOrThrow(send, expression) {
  const response = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    const desc =
      response.exceptionDetails.exception?.description ||
      response.exceptionDetails.text ||
      "Unknown browser evaluation failure";
    throw new Error(desc);
  }
  return response.result?.value;
}

async function main() {
  const browserBin = findBrowserBinary();
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "pawhelp-browser-"));
  let fixtureChild = null;
  let proxyInfo = null;
  let browserInfo = null;
  let cdp = null;

  try {
    fixtureChild = await startFixtureServer();
    proxyInfo = await startCspProxy();

    const probe = await fetch(`${proxyInfo.url}/`);
    if (probe.headers.get("content-security-policy") !== STRICT_CSP) {
      throw new Error("CSP proxy failed to attach strict Content-Security-Policy");
    }

    browserInfo = await launchHeadlessBrowser(browserBin, userDataDir);
    cdp = await connectPageCdp(browserInfo.port);

    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
      source:
        "window.__cspViolations = [];" +
        "window.addEventListener('securitypolicyviolation', (e) => {" +
        "  window.__cspViolations.push(`${e.violatedDirective}:${e.blockedURI || 'inline'}`);" +
        "});",
    });

    await cdp.send("Page.navigate", { url: `${proxyInfo.url}/` });
    const readyDeadline = Date.now() + 10000;
    let chatReady = false;
    while (Date.now() < readyDeadline) {
      chatReady = Boolean(
        await evaluateOrThrow(
          cdp.send,
          "document.readyState === 'complete' && window.__pawhelpChatReady === true && typeof renderHistory === 'function'",
        ),
      );
      if (chatReady) break;
      await new Promise((r) => setTimeout(r, 50));
    }
    if (!chatReady) {
      const diag = await evaluateOrThrow(
        cdp.send,
        "JSON.stringify({ href: location.href, readyState: document.readyState, warning: document.querySelector('.init-warning')?.textContent, viol: window.__cspViolations })",
      );
      throw new Error(
        `Timed out waiting for chat-app.js initialization: ${diag}; errors=${JSON.stringify(cdp.runtimeErrors)}`,
      );
    }

    const staticScript = fs.readFileSync(STATIC_ASSERTIONS, "utf8");
    const staticResult = await evaluateOrThrow(cdp.send, staticScript);
    if (!staticResult || staticResult.passed !== 14) {
      throw new Error(
        `Unexpected markdown_assertions.js result: ${JSON.stringify(staticResult)}`,
      );
    }

    const streamScript = fs.readFileSync(STREAM_ASSERTIONS, "utf8");
    const streamResult = await evaluateOrThrow(cdp.send, streamScript);
    if (!streamResult || !Array.isArray(streamResult.passed) || streamResult.passed.length !== 3) {
      throw new Error(
        `Unexpected markdown_stream_assertions.js result: ${JSON.stringify(streamResult)}`,
      );
    }

    const violations = await evaluateOrThrow(cdp.send, "window.__cspViolations");
    if (!Array.isArray(violations) || violations.length > 0) {
      throw new Error(`CSP violations recorded during browser checks: ${JSON.stringify(violations)}`);
    }

    const total = staticResult.passed + streamResult.passed.length;
    console.log(
      `Browser Markdown/XSS/CSP checks: ${total} assertions passed (${staticResult.passed} static + ${streamResult.passed.length} stream), 0 CSP violations.`,
    );
  } finally {
    if (cdp?.ws) {
      try {
        cdp.ws.close();
      } catch (_) {}
    }
    if (browserInfo?.proc) {
      browserInfo.proc.kill();
    }
    if (proxyInfo?.proxy) {
      proxyInfo.proxy.close();
    }
    if (fixtureChild) {
      fixtureChild.kill();
    }
    await new Promise((r) => setTimeout(r, 150));
    try {
      fs.rmSync(userDataDir, { recursive: true, force: true });
    } catch (_) {}
  }
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
