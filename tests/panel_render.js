// Headless render of static/index.html against /api/state snapshots (jsdom).
// Usage: node panel_render.js <index.html> <snapshots.json>  -> JSON on stdout.
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");

const html = fs.readFileSync(process.argv[2], "utf8").replace(/__INGRESS__/g, "");
const snaps = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const IDS = ["balance", "equity", "pnl", "unreal", "posCount", "winrate", "tradeCount", "bestLong", "bestShort",
  "modeBadge", "modeSubtitle", "statusBadge", "heartbeat", "scanStatus", "posSummary", "tradingAlerts",
  "openTrades", "historyTrades", "scanTable", "entryRule", "balanceLabel", "log"];

async function renderOne(name, snap) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e.message || e)));
  vc.on("error", (...a) => errors.push("console.error: " + a.map(String).join(" ")));
  const dom = new JSDOM(html, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/",
    beforeParse(window) {
      window.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(snap)) });
      window.setInterval = () => 0;
      window.confirm = () => true;
      window.addEventListener("error", (ev) => errors.push("window.error: " + ev.message));
    },
  });
  const w = dom.window;
  await new Promise((r) => setTimeout(r, 30));
  try { w.render(JSON.parse(JSON.stringify(snap))); } catch (e) { errors.push("render: " + e.stack); }
  const d = w.document;
  const texts = {};
  for (const id of IDS) { const el = d.getElementById(id); texts[id] = el ? el.textContent.replace(/\s+/g, " ").trim() : null; }
  const out = {
    errors, texts,
    safeBadgeHidden: d.getElementById("safeBadge").classList.contains("hidden"),
    alertsHidden: d.getElementById("tradingAlerts").classList.contains("hidden"),
    resetDisabled: d.getElementById("btnReset").disabled,
    initBalanceDisabled: d.getElementById("initBalance").disabled,
    startDisabled: d.getElementById("btnStart").disabled,
    stopDisabled: d.getElementById("btnStop").disabled,
  };
  w.close();
  return out;
}

(async () => {
  const result = {};
  for (const [name, snap] of Object.entries(snaps)) result[name] = await renderOne(name, snap);
  process.stdout.write(JSON.stringify(result));
})().catch((e) => { console.error(e); process.exit(2); });
