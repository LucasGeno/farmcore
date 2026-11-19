// Driven by test_connect_agent_browser.py: sign in, create a token on "Connect your agent", copy
// the Codex snippet. Prints one JSON line: the clipboard text and the issued token.
const { chromium } = require("playwright");
const [base, login, password] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
    const page = await ctx.newPage();
    await page.goto(base + "/login");
    await page.fill('input[name="login"]', login);
    await page.fill('input[name="password"]', password);
    await Promise.all([page.waitForNavigation(), page.click('button:has-text("Sign in")')]);
    await page.goto(base + "/share/connect");
    await page.fill('input[name="name"]', "Codex");
    await page.fill('input[name="purpose"]', "my own agent");
    await Promise.all([page.waitForNavigation(), page.click('button:has-text("Create token")')]);
    const token = await page.inputValue("[data-token-value]");
    const copy = async (key) => {
      const box = `[data-snippet="${key}"]`;
      await page.click(`${box} [data-copy-snippet]`);
      await page.waitForFunction((sel) => document.querySelector(sel + " [data-copied]").textContent === "Copied", box);
      return page.evaluate(() => navigator.clipboard.readText());
    };
    const env = await copy("codex-env");
    const toml = await copy("codex-toml");
    if (process.env.CONNECT_SHOT) await page.screenshot({ path: process.env.CONNECT_SHOT, fullPage: true });
    console.log(JSON.stringify({ token, env, toml }));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
