/* State-driven, non-destructive verification of the redesign against a running manager.
 * Expectations are derived from /api/recipes + /api/hosts, so it works whether models are
 * running or stopped. Opens dialogs and cancels them; never confirms start/stop/switch/delete.
 * Run: NODE_PATH=<node_modules with playwright> node verify-live.cjs
 * Env: DGXM_URL (default http://localhost:8090), DGXM_USER / DGXM_PASSWORD (or a ../.env file). */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const SHELL = process.env.CHROME_PATH || "";
const env = { DGXM_USER: process.env.DGXM_USER, DGXM_PASSWORD: process.env.DGXM_PASSWORD };
try {
  for (const line of fs.readFileSync(path.join(__dirname, "..", ".env"), "utf8").split("\n")) {
    const t = line.trim();
    if (t && !t.startsWith("#") && t.includes("=")) {
      const i = t.indexOf("=");
      const k = t.slice(0, i).trim();
      if (env[k] == null) env[k] = t.slice(i + 1).trim().replace(/^["']|["']$/g, "");
    }
  }
} catch { /* no .env: rely on the process environment */ }
const BASE = process.env.DGXM_URL || "http://localhost:8090";
const SHOT_BLUR = process.env.DGXM_SHOT_BLUR === "1";  // keep blur ON for publishable screenshots
const AUTH = "Basic " + Buffer.from(`${env.DGXM_USER || "admin"}:${env.DGXM_PASSWORD || ""}`).toString("base64");
const api = async (p) => (await fetch(BASE + p, { headers: { Authorization: AUTH } })).json();
const IMG = path.join(__dirname, "img");
const errors = [];
const results = [];
const ok = (name, cond) => { results.push(`${cond ? "PASS" : "FAIL"}  ${name}`); if (!cond) process.exitCode = 1; };
const RUNNING = ["ready", "loading", "starting"];

(async () => {
  const recipes = await api("/api/recipes");
  const hosts = await api("/api/hosts");
  const running = recipes.filter((r) => RUNNING.includes(r.status));
  const pinned = recipes.filter((r) => r.pinned);
  const stopped = recipes.filter((r) => !RUNNING.includes(r.status));
  const libExpected = stopped.filter((r) => !r.pinned).length;
  const host = hosts[0];
  const total = host?.system?.memory?.total_gb ?? 0;
  console.log(`live state: ${recipes.length} recipes · ${running.length} running · ${pinned.length} pinned · ${total} GB total`);

  const browser = await chromium.launch(fs.existsSync(SHELL) ? { executablePath: SHELL } : {});
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, httpCredentials: { username: env.DGXM_USER || "admin", password: env.DGXM_PASSWORD || "" } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  // Screenshots should show real names (the user's own setup): ensure blur is OFF first.
  if (!SHOT_BLUR && (await page.getAttribute("#btn-blur", "aria-pressed")) === "true") {
    await page.click("#btn-blur");
    await page.waitForTimeout(400);
  }

  /* structure */
  ok("host strip renders", await page.locator("#host-strip .hs-card").count() >= 1);
  ok("VRAM numbers visible", await page.locator("#host-strip").innerText().then((t) => /GB free/.test(t) && t.includes(String(total))));
  ok("toolbar present", await page.locator(".dash-toolbar #recipe-search").count() === 1);
  ok("no filter rail left", await page.locator(".filter-rail").count() === 0);
  ok("library cards match state", await page.locator("#recipes .lib-card").count() === libExpected);
  ok("pinned section matches state", await page.locator("#pinned-list .card").count() === pinned.length);
  const servingExpected = running.filter((r) => !r.pinned && r.kind !== "agent").length;
  ok("serving rows match state", await page.locator("#serving-sec .serving-card").count() === servingExpected);
  if (running.length) {
    ok("fit lines offer switch", await page.locator("#recipes .fit-line.swap").count() >= 1);
  } else {
    ok("fit lines say fits-now", await page.locator("#recipes .fit-line.ok").count() >= 1);
    ok("primary start buttons", await page.locator('#recipes .lib-card [data-act="start"]').count() >= 1);
  }
  ok("activity strip", await page.locator("#dash-activity li").count() >= 1);
  await page.screenshot({ path: path.join(IMG, "live-01-dashboard.png"), fullPage: true });

  /* search + status filter */
  const target = stopped[0] ?? recipes[0];
  await page.fill("#recipe-search", target.name);
  await page.waitForTimeout(300);
  ok("search filters to 1", await page.locator("#recipes .lib-card, #pinned-list .card").count() === 1);
  await page.fill("#recipe-search", "");
  await page.click('#status-filter [data-status="running"]');
  await page.waitForTimeout(300);
  ok("running filter hides stopped", await page.locator("#recipes .lib-card").count() === 0);
  await page.click('#status-filter [data-status=""]');
  await page.waitForTimeout(300);

  /* list view + bulk (select only, no actions) */
  await page.click('#layout-seg [data-layout="list"]');
  await page.waitForTimeout(400);
  ok("list rows = all recipes", await page.locator(".list-view tbody tr").count() === recipes.length);
  await page.locator(".list-view input[data-bulk]").first().check();
  await page.waitForTimeout(250);
  ok("bulk bar appears", await page.locator("#bulk-bar:not(.hidden)").count() === 1);
  await page.screenshot({ path: path.join(IMG, "live-02-list.png"), fullPage: true });
  await page.click('[data-bulk-act="clear"]');
  await page.click('#layout-seg [data-layout="cards"]');
  await page.waitForTimeout(300);

  /* sort + group selects */
  await page.selectOption("#recipe-sort", "vram");
  await page.waitForTimeout(250);
  ok("vram sort applied", await page.evaluate(() => JSON.parse(localStorage.getItem("dgxm.recipeView")).sort === "vram"));
  await page.selectOption("#recipe-group", "engine");
  await page.waitForTimeout(250);
  ok("engine groups render", await page.locator("#recipes .group").count() >= 1);
  await page.selectOption("#recipe-group", "");
  await page.selectOption("#recipe-sort", "name");
  await page.waitForTimeout(250);

  /* drawer on a model with variants (served name shared) */
  const withVariants = recipes.find((r) => r.kind !== "agent" && recipes.some((x) => x.id !== r.id && x.host_id === r.host_id && (x.args || "").includes(`--served-model-name ${((r.args || "").match(/--served-model-name(?:=|\s+)(\S+)/) || [])[1] ?? "§"}`)));
  const drawerTarget = withVariants ?? target;
  await page.locator(`.card[data-id="${drawerTarget.id}"] .card-title`).first().click();
  await page.waitForTimeout(450);
  ok("drawer opens", await page.locator("#drawer.show").count() === 1);
  ok("drawer facts", await page.locator("#drawer .kv dt").count() >= 5);
  if (withVariants) ok("drawer variants listed", await page.locator("#drawer .variant-row").count() >= 2);
  await page.screenshot({ path: path.join(IMG, "live-03-drawer.png") });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  ok("Esc closes drawer", await page.locator("#drawer.show").count() === 0);

  /* switch dialog — open, verify, CANCEL (never confirm on the live machine) */
  const stoppedRecipes = recipes.filter((r) => !RUNNING.includes(r.status));
  const switchTarget = stoppedRecipes[0] ?? drawerTarget;
  const switchBtn = page.locator(`.card[data-id="${switchTarget.id}"] [data-act="switch"], .card[data-id="${switchTarget.id}"] [data-act="start"]`).first();
  const isSwitch = (await switchBtn.count()) > 0 && (await switchBtn.getAttribute("data-act")) === "switch";
  if (isSwitch) {
    await switchBtn.click();
    await page.waitForTimeout(350);
    ok("switch dialog opens", await page.locator("#dlg-switch[open]").count() === 1);
    ok("memory compare shown", await page.locator("#dlg-switch .mem-compare").count() === 1);
    ok("endpoint note shown", await page.locator("#dlg-switch .ep-note").count() === 1);
    await page.screenshot({ path: path.join(IMG, "live-04-switch.png") });
    await page.click("#dlg-switch .dialog-head [data-close]");
    await page.waitForTimeout(250);
    ok("switch cancelled", await page.locator("#dlg-switch[open]").count() === 0);
  } else {
    // no primary button on the target (e.g. it is booting): verify the dialog builder directly
    const built = await page.evaluate((id) => { openSwitchDialog(state.recipes.find((r) => r.id === id)); return document.querySelector("#dlg-switch").open; }, switchTarget.id);
    ok("switch dialog builds (no primary button)", built === true);
    ok("endpoint note shown", await page.locator("#dlg-switch .ep-note").count() === 1);
    await page.screenshot({ path: path.join(IMG, "live-04-switch.png") });
    await page.click("#dlg-switch .dialog-head [data-close]");
    await page.waitForTimeout(250);
  }

  /* overflow menu -> editor opens -> cancel */
  await page.locator(`.card[data-id="${target.id}"] [data-act="more"]`).first().click();
  await page.waitForTimeout(200);
  ok("overflow menu opens", await page.locator("#overflow-menu").count() === 1);
  await page.click('#overflow-menu [data-m="edit"]');
  await page.waitForTimeout(400);
  ok("recipe editor opens from menu", await page.locator("#dlg-recipe[open]").count() === 1);
  await page.click("#dlg-recipe .dialog-head [data-close]");
  await page.waitForTimeout(250);
  ok("editor cancelled", await page.locator("#dlg-recipe[open]").count() === 0);

  /* guided wizard: opens from Add recipe, walks steps, cancels without saving */
  await page.click("#btn-add-recipe");
  await page.waitForTimeout(500);
  ok("wizard opens", await page.locator("#dlg-wizard[open]").count() === 1);
  ok("wizard step 1 shows weights or model choice", await page.locator("#dlg-wizard .wiz-body").innerText().then((t) => /weight|model/i.test(t)));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  // Esc on a pristine wizard closes directly; if a discard confirm appears, accept it.
  if (await page.locator("#dlg-wizard[open]").count()) {
    page.once("dialog", (d) => d.accept());
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
  }
  ok("wizard cancelled", await page.locator("#dlg-wizard[open]").count() === 0);

  /* filters popover + saved view */
  await page.click("#btn-filters");
  await page.waitForTimeout(200);
  ok("filter popover opens", await page.locator("#filter-pop:not(.hidden)").count() === 1);
  await page.click("#btn-filters");
  page.once("dialog", (d) => d.accept("Live test view"));
  await page.click("#btn-save-view");
  await page.waitForTimeout(250);
  ok("saved view chip", await page.locator("#saved-views .chip").count() === 1);
  await page.click("#saved-views .sv-x");
  await page.waitForTimeout(200);
  ok("saved view removed", await page.locator("#saved-views .chip").count() === 0);

  /* blur + theme */
  const blurOn = async () => (await page.getAttribute("#btn-blur", "aria-pressed")) === "true";
  if (!(await blurOn())) { await page.click("#btn-blur"); await page.waitForTimeout(400); }  // ensure ON
  ok("blur masks host name", !(await page.locator("#host-strip").innerText()).includes(host.name));
  await page.click("#btn-blur");  // OFF
  await page.waitForTimeout(400);
  ok("show reveals host name", (await page.locator("#host-strip").innerText()).includes(host.name));
  if (SHOT_BLUR) { await page.click("#btn-blur"); await page.waitForTimeout(400); }  // back ON for the screenshots
  await page.click("#btn-theme");
  await page.waitForTimeout(400);
  ok("light theme applies", await page.evaluate(() => document.documentElement.dataset.theme === "light"));
  await page.screenshot({ path: path.join(IMG, "live-05-light.png"), fullPage: true });
  await page.click("#btn-theme");
  await page.click("#btn-theme");  // auto -> dark
  await page.waitForTimeout(300);

  /* other tabs still work */
  await page.click('.tab[data-tab="hosts"]');
  await page.waitForTimeout(700);
  ok("hosts topology renders", await page.locator("#topology .topo-node").count() >= 1);
  await page.click('.tab[data-tab="schedules"]');
  await page.waitForTimeout(700);
  ok("schedules tab renders", await page.locator("#tab-schedules:not(.hidden)").count() === 1);
  await page.click('.tab[data-tab="activity"]');
  await page.waitForTimeout(700);
  ok("activity tab renders", await page.locator("#events tr").count() >= 1);
  await page.click('.tab[data-tab="recipes"]');
  await page.waitForTimeout(400);

  /* mobile */
  const mctx = await browser.newContext({ viewport: { width: 420, height: 900 }, httpCredentials: { username: env.DGXM_USER || "admin", password: env.DGXM_PASSWORD || "" } });
  const mpage = await mctx.newPage();
  mpage.on("pageerror", (e) => errors.push("mobile pageerror: " + e.message));
  await mpage.addInitScript((blur) => { try { localStorage.setItem("llm.blur", blur); } catch { /* ignore */ } }, SHOT_BLUR ? "1" : "0");
  await mpage.goto(BASE + "/", { waitUntil: "networkidle" });
  await mpage.waitForTimeout(1000);
  ok("mobile: no horizontal overflow", await mpage.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  await mpage.screenshot({ path: path.join(IMG, "live-06-mobile.png"), fullPage: true });
  // Rack chrome: the nav is a horizontal tab bar on mobile — no hamburger drawer anymore.
  ok("mobile: rack nav visible", await mpage.locator('.tab[data-tab="recipes"]').isVisible());
  await mpage.locator('.tab[data-tab="hosts"]').evaluate((el) => el.scrollIntoView({ inline: "center", block: "nearest" }));
  await mpage.click('.tab[data-tab="hosts"]');
  await mpage.waitForTimeout(500);
  ok("mobile: tab switch works", await mpage.locator("#tab-hosts:not(.hidden)").count() === 1);
  await mctx.close();

  await browser.close();
  console.log(results.join("\n"));
  console.log(errors.length ? "JS ERRORS:\n" + errors.join("\n") : "NO JS ERRORS");
  if (errors.length) process.exitCode = 1;
})().catch((e) => { console.error(e); process.exit(1); });