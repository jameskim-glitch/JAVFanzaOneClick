// Verify one-click alias promotion dispatch without using a real library.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.style = {}; this.dataset = {}; this.events = {}; this.textContent = ""; this.value = ""; }
  appendChild(e) { this.children.push(e); e.parent = this; return e; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(key, fn) { this.events[key] = fn; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(e => e !== this); }
  all() { return [this, ...this.children.flatMap(e => e.all())]; }
  querySelectorAll(selector) { const role = selector.match(/data-role=['"]([^'"]+)['"]/); return this.all().filter(e => role && e.dataset.role === role[1]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}
async function scenario(kind) {
  const body = new Element("body");
  const document = { body, createElement: t => new Element(t), getElementById: id => body.all().find(e => e.id === id) };
  const calls = []; let reloads = 0;
  const window = { location: { pathname: kind === "single" ? "/performers/42" : "/performers", reload: () => reloads++ }, addEventListener() {}, removeEventListener() {} };
  const push = function (state, title, path) { window.location.pathname = path; };
  window.history = { pushState: push, replaceState: push };
  const ctx = { document, window, console: { info() {}, error() {} }, setInterval() {}, clearInterval() {}, setTimeout() {}, clearTimeout() {},
    fetch: async (url, options) => {
      assert.equal(url, "/graphql"); const request = JSON.parse(options.body); calls.push(request);
      if (request.query.includes("jobQueue")) return { ok: true, text: async () => JSON.stringify({ data: { jobQueue: kind === "running" ? [{id:"900",status:"RUNNING",description:"영문 별명으로 이름 변경 후 StashDB 재스크래핑"}] : [] } }) };
      if (request.query.includes("runPluginTask")) {
        assert.match(request.query, /args_map: \$args/);
        if (kind === "uncertain") throw new Error("response lost");
        return { ok: true, text: async () => JSON.stringify({ data: { runPluginTask: "901" } }) };
      }
      assert.equal(request.variables.args.mode, "stashdb_alias_scan");
      const output = { ok: true, total: 1, eligible: kind === "no_alias" ? 0 : 1, items: [{ id: "42", name: "한국어 이름", english_aliases: kind === "no_alias" ? [] : ["Sakura Momo"] }] };
      return { ok: true, text: async () => JSON.stringify({ data: { runPluginOperation: { output } } }) };
    } };
  vm.runInNewContext(fs.readFileSync(__dirname + "/jav_fanza_oneclick.js", "utf8"), ctx);
  if (kind === "all") {
    window.history.pushState({}, "", "/scenes/77");
    assert.equal(body.querySelector('[data-role="images"]').textContent, "사진·로고");
    window.history.replaceState({}, "", "/performers");
    assert.equal(body.querySelector('[data-role="images"]').textContent, "이미지 전체");
  }
  await body.querySelector('[data-role="alias-stashdb"]').events.click();
  assert.equal(calls.length, 1, "Opening dialog only inventories aliases");
  const dialog = document.getElementById("jav-fanza-oneclick-alias-stashdb");
  const start = dialog.querySelector('[data-role="alias-start"]');
  if (kind === "no_alias") { assert.equal(start.disabled, true); await start.events.click(); assert.equal(calls.length, 1); return; }
  if (kind === "upgrade") dialog.querySelector('[data-role="alias-upgrade"]').checked = true;
  await start.events.click();
  if (kind === "running") { assert.equal(calls.length, 2); assert.equal(calls.some(c => c.query.includes("runPluginTask")), false); return; }
  assert.equal(calls.length, 3);
  const args = calls[2].variables.args;
  assert.equal(args.mode, "stashdb_alias_run"); assert.equal(args.confirmed, true);
  assert.equal(args.photo_policy, kind === "upgrade" ? "upgrade" : "missing");
  if (kind === "single") assert.equal(args.id, "42"); else assert.equal(args.id, undefined);
  await start.events.click(); assert.equal(calls.length, 3, "No double submission or automatic retry after response loss");
  assert.equal(reloads, 0);
  window.__javFanzaOneClick.dispose();
  assert.equal(window.history.pushState, push, "Dispose restores navigation hooks");
  assert.equal(document.getElementById("jav-fanza-oneclick-alias-stashdb"), undefined);
}
(async () => {
  for (const kind of ["all", "single", "upgrade", "no_alias", "uncertain", "running"]) await scenario(kind);
  console.log("Alias UI: 6 scenarios passed; includes reuse of already running task");
})().catch(error => { console.error(error); process.exitCode = 1; });
