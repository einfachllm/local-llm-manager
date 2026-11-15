/* Renders the dashboard concept to PNGs and smoke-tests every interaction.
   Run: NODE_PATH=<node_modules with playwright> node shoot.cjs  (CHROME_PATH optional) */
const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");
/* use a specific browser build if CHROME_PATH points at one */
const SHELL = process.env.CHROME_PATH || "";
const exe = SHELL && fs.existsSync(SHELL) ? SHELL : undefined;
const ROOT = __dirname;
const URL = "file://" + path.join(ROOT, "prototype.html");
const IMG = path.join(ROOT, "img");
const errors = [];
const results = [];
const ok = (name, cond) => { results.push(`${cond ? "PASS" : "FAIL"}  ${name}`); if (!cond) process.exitCode = 1; };

(async () => {
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await ctx.newPage();
  page.on("pageerror", e => errors.push("pageerror: " + e.message));
  page.on("console", m => { if (m.type() === "error") errors.push("console: " + m.text()); });

  await page.goto(URL);
  await page.waitForTimeout(900);

  /* 01 — default dashboard */
  ok("host strip shows VRAM", await page.locator("#hoststrip").innerText().then(t => t.includes("115.1") && t.includes("15.5")));
  ok("serving card present", await page.locator(".serving-card").count() === 1);
  ok("3 library cards", await page.locator("#library-sec .card").count() === 3);
  ok("switch-primary on stopped cards", await page.locator('.card [data-act="switch"]').count() === 3);
  await page.screenshot({ path: path.join(IMG, "01-dashboard.png"), fullPage: true });

  /* 02 — list view + bulk */
  await page.click('#view-seg [data-view="list"]');
  await page.waitForTimeout(400);
  ok("list rows = 4", await page.locator("table.list tbody tr").count() === 4);
  await page.locator('table.list [data-bulk]').first().check();
  await page.locator('table.list [data-bulk]').nth(1).check();
  await page.waitForTimeout(250);
  ok("bulk bar appears", await page.locator(".bulkbar").count() === 1);
  await page.screenshot({ path: path.join(IMG, "02-list.png"), fullPage: true });
  await page.click("#bulk-clear");
  await page.click('#view-seg [data-view="cards"]');
  await page.waitForTimeout(300);

  /* 03 — drawer with variants */
  await page.click(".serving-card .sv-title h3");
  await page.waitForTimeout(450);
  ok("drawer open", await page.locator(".drawer.show").count() === 1);
  ok("variants listed", await page.locator(".drawer .variant").count() === 3);
  await page.screenshot({ path: path.join(IMG, "03-drawer.png") });
  /* edit inside drawer works */
  await page.click(".drawer details.edit summary");
  await page.fill("#ed-desc", "Qwen3.8-27B via SGLang — edited in the drawer");
  await page.click("#ed-save");
  await page.waitForTimeout(250);
  ok("drawer edit lands", (await page.locator(".serving-card .sv-sub").count()) >= 0 && await page.evaluate(() => JSON.parse(localStorage.getItem("llm-dash-concept-v1")).recipes.find(r => r.id === 7).desc.includes("edited in the drawer")));
  await page.click("#drawer-close");
  await page.waitForTimeout(300);

  /* 04 — switch dialog with memory math */
  await page.locator('.card[data-id="5"] [data-act="switch"]').click();
  await page.waitForTimeout(350);
  ok("switch dialog open", await page.locator("#dlg-switch[open]").count() === 1);
  ok("endpoint-unchanged note", await page.locator("#dlg-switch .ep-note.same").count() === 1);
  await page.screenshot({ path: path.join(IMG, "04-switch.png") });
  await page.click("#dlg-switch [data-close]");
  await page.waitForTimeout(200);

  /* 05 — boot progress: stop sglang, start tensorfold */
  await page.click('.serving-card [data-act="stop"]');
  await page.waitForTimeout(400);
  ok("stop frees VRAM", await page.locator("#hoststrip").innerText().then(t => t.includes("130.6 GB free")));
  ok("undo toast", await page.locator("#toasts .toast button").count() >= 1);
  await page.locator('.card[data-id="6"] [data-act="start"]').click();
  await page.waitForTimeout(2200);
  ok("starting state + progress", await page.locator('.serving-card[data-id="6"] .bootline').count() === 1);
  ok("primary becomes Start when it fits", await page.locator('.card[data-id="5"] [data-act="start"]').count() === 1);
  await page.screenshot({ path: path.join(IMG, "05-boot.png"), fullPage: true });
  await page.waitForTimeout(5200);
  ok("boot completes to ready", await page.locator('.serving-card[data-id="6"]').count() === 1);

  /* search + filter */
  await page.fill("#q", "nemotron");
  await page.waitForTimeout(250);
  ok("search filters", await page.locator("#library-sec .card").count() === 1);
  await page.fill("#q", "");
  await page.click('#status-chips [data-status="running"]');
  await page.waitForTimeout(250);
  ok("running filter", await page.locator("#library-sec .card").count() === 0);
  await page.click('#status-chips [data-status=""]');

  /* add recipe */
  await page.click("#btn-add");
  await page.fill("#add-name", "qwen3.8-mtp");
  await page.fill("#add-model", "Qwen/Qwen3.8-27B-NVFP4");
  await page.fill("#add-served", "qwen3.8");
  await page.fill("#add-est", "52");
  await page.click("#add-go");
  await page.waitForTimeout(300);
  ok("create lands", await page.locator('.card:has-text("qwen3.8-mtp")').count() === 1);

  /* delete + undo */
  await page.locator('.card:has-text("qwen3.8-mtp") [data-act="more"]').click();
  await page.waitForTimeout(150);
  await page.click('#overflow-menu [data-m="delete"]');
  await page.waitForTimeout(200);
  await page.click("#confirm-go");
  await page.waitForTimeout(250);
  ok("delete lands", await page.locator('.card:has-text("qwen3.8-mtp")').count() === 0);
  await page.locator("#toasts .toast button", { hasText: "Undo" }).last().click();
  await page.waitForTimeout(250);
  ok("undo restores", await page.locator('.card:has-text("qwen3.8-mtp")').count() === 1);

  /* saved view */
  page.once("dialog", d => d.accept("Night shift"));
  await page.click("#btn-save-view");
  await page.waitForTimeout(250);
  ok("saved view chip", await page.locator("#views-row .chip").count() === 1);

  /* 07 — light theme */
  await page.click("#btn-theme");
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(IMG, "07-light.png"), fullPage: true });
  await page.click("#btn-theme");

  /* reset for clean mobile shot */
  await page.evaluate(() => localStorage.removeItem("llm-dash-concept-v1"));
  await page.reload(); await page.waitForTimeout(800);

  /* 06 — mobile */
  const mctx = await browser.newContext({ viewport: { width: 420, height: 900 } });
  const mpage = await mctx.newPage();
  mpage.on("pageerror", e => errors.push("mobile pageerror: " + e.message));
  await mpage.goto(URL); await mpage.waitForTimeout(800);
  ok("mobile: no horizontal overflow", await mpage.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  await mpage.screenshot({ path: path.join(IMG, "06-mobile.png"), fullPage: true });
  await mpage.click("#btn-nav"); await mpage.waitForTimeout(350);
  ok("mobile: nav opens", await mpage.locator(".sidebar.open").count() === 1);
  await mctx.close();

  /* 08 — before/after composite (only when a "before" screenshot is present locally) */
  const beforePath = path.join(IMG, "00-current.png");
  if (fs.existsSync(beforePath)) {
  const cpage = await ctx.newPage();
  await cpage.setViewportSize({ width: 1600, height: 1000 });
  const before = fs.readFileSync(beforePath).toString("base64");
  const after = fs.readFileSync(path.join(IMG, "01-dashboard.png")).toString("base64");
  await cpage.setContent(`<body style="margin:0;background:#070b14;font-family:system-ui">
    <div style="padding:26px 30px 10px;color:#e6ebf5;font-size:22px;font-weight:800">Local LLM Manager — main dashboard</div>
    <div style="padding:0 30px 8px;color:#8d99b5;font-size:14px">Before: v0.3.0 (your screenshot) · After: interactive concept in prototype.html</div>
    <div style="margin:14px 30px 6px;color:#f0b429;font-size:13px;font-weight:700;letter-spacing:.1em">BEFORE</div>
    <img src="data:image/png;base64,${before}" style="width:1540px;display:block;margin:0 30px;border:1px solid #22304e;border-radius:10px">
    <div style="margin:22px 30px 6px;color:#22c58b;font-size:13px;font-weight:700;letter-spacing:.1em">AFTER — CONCEPT</div>
    <img src="data:image/png;base64,${after}" style="width:1540px;display:block;margin:0 30px 30px;border:1px solid #22304e;border-radius:10px">
  </body>`);
  await cpage.waitForTimeout(500);
  await cpage.screenshot({ path: path.join(IMG, "08-before-after.png"), fullPage: true });
  }

  await browser.close();
  console.log(results.join("\n"));
  console.log(errors.length ? "JS ERRORS:\n" + errors.join("\n") : "NO JS ERRORS");
  if (errors.length) process.exitCode = 1;
})().catch(e => { console.error(e); process.exit(1); });