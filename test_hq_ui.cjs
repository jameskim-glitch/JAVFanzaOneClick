// Synthetic UI only; no real library, external image or GraphQL writes.
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
const url = "https://stashdb.org/images/69eb6b4b-d3a9-4fdf-b603-410c10379a00";
const image = { data: "data:image/png;base64,YWJj", width: 800, height: 1200 };
async function scenario(kind) {
  const body = new Element("body");
  const document = { body, createElement: t => new Element(t), getElementById: id => body.all().find(e => e.id === id) };
  const calls = []; let reloads = 0;
  const window = { location: { pathname: kind === "studio" ? "/studios/9" : "/performers/1", reload: () => reloads++ }, addEventListener() {}, removeEventListener() {} };
  const ctx = { document, window, console: { info() {}, error() {} }, setInterval() {}, clearInterval() {}, setTimeout() {}, clearTimeout() {},
    fetch: async (target, options) => {
      assert.equal(target, "/graphql"); const args = JSON.parse(options.body).variables.args; calls.push(args);
      let output;
      switch (args.mode) {
        case "images_catalog": output = { ok: true, total: 1, linked: 1, items: [{ id: kind === "studio" ? "9" : "1", name: "<script>literal name</script>", linked: true }] }; break;
        case "images_candidates": output = { ok: true, name: "Local", source_name: "Source", urls: [url, "http://127.0.0.1/private"], ticket: "ticket" }; break;
        case "images_preview": output = { ok: true, token: "preview", old: image, new: image, smaller: kind === "smaller", same: kind === "same" }; break;
        case "images_apply":
          assert.equal(args.confirmed, true);
          if (kind === "unknown") throw new Error("response lost");
          output = { ok: true, status: "updated", name: "Local", width: 800, height: 1200, history: "C:/saved/history.json", restore_token: "restore" }; break;
        case "images_restore": assert.equal(args.confirmed, true); output = { ok: true, message: "복원했습니다." }; break;
        case "name_preview": output = { ok: true, token: "name", before: "닮은 햇살", original: "似鳥（似鳥日菜）", options: ["니토리", "니토리 히나"] }; break;
        case "name_apply": assert.equal(args.name, "니토리 히나"); assert.equal(args.confirmed, true); output = { ok: true, before: "닮은 햇살", name: args.name }; break;
        default: throw new Error(args.mode);
      }
      return { ok: true, status: 200, text: async () => JSON.stringify({ data: { runPluginOperation: { output } } }) };
    } };
  vm.runInNewContext(fs.readFileSync(__dirname + "/jav_fanza_oneclick.js", "utf8"), ctx);
  const panel = document.getElementById("jav-fanza-oneclick-panel");
  if (kind !== "studio") {
    const visible = panel.children.filter(e => e.tagName === "button" && e.style.display !== "none").sort((a, b) => Number(a.style.order) - Number(b.style.order));
    assert.deepEqual(visible.map(e => e.textContent), ["이미지 전체", "미처리 전체", "사진·로고 업데이트"]);
    assert.equal(panel.querySelector('[data-role="auto-indicator"]').style.display, "none");
  }
  assert.ok(document.getElementById("jav-fanza-oneclick-original-fit").textContent.includes("object-fit: contain"));
  await body.querySelector('[data-role="hq"]').events.click();
  const dialog = document.getElementById("jav-fanza-oneclick-hq"); assert.ok(dialog);
  const find = role => dialog.querySelector(`[data-role="${role}"]`);
  assert.deepEqual(calls.map(c => c.mode), ["images_catalog"]);
  if (kind === "studio") { assert.equal(calls[0].kind, "studio"); assert.equal(find("hq-name-preview").style.display, "none"); }
  if (kind === "name") {
    await find("hq-name-preview").events.click();
    const apply = find("hq-name-apply"); await apply.events.click(); assert.equal(calls.length, 2);
    dialog.all().find(e => e["aria-label"] === "확인된 배우 이름").value = "니토리 히나";
    const agree = find("hq-name-agree"); agree.checked = true; agree.events.change(); await apply.events.click();
    assert.equal(calls.length, 3); await apply.events.click(); assert.equal(calls.length, 3); return;
  }
  await find("hq-candidates").events.click();
  assert.equal(dialog.all().filter(e => e.tagName === "img").length, 1, "Block non-StashDB image URLs in the DOM too");
  assert.equal(dialog.all().find(e => e.tagName === "img").style.objectFit, "contain");
  assert.equal(dialog.all().find(e => e.tagName === "img").referrerPolicy, "no-referrer");
  await find("hq-preview").events.click();
  assert.equal(calls.length, 3, "Catalog/candidates/preview never apply anything");
  if (kind === "same") { assert.equal(find("hq-apply"), undefined); return; }
  const commit = find("hq-apply"); const agree = find("hq-agree");
  assert.equal(commit.disabled, true); await commit.events.click(); assert.equal(calls.length, 3);
  agree.checked = true; agree.events.change();
  if (kind === "smaller") {
    await commit.events.click(); assert.equal(calls.length, 3);
    const small = find("hq-smaller"); small.checked = true; small.events.change();
  }
  if (kind === "change_entity") {
    const select = dialog.all().find(e => e["aria-label"] === "사진을 바꿀 항목"); select.value = "2"; select.events.change();
    await commit.events.click(); assert.equal(calls.length, 3); return;
  }
  await commit.events.click(); assert.equal(calls.length, 4); await commit.events.click(); assert.equal(calls.length, 4);
  assert.equal(reloads, 0);
  if (kind !== "unknown") {
    const undo = find("hq-restore"); await undo.events.click(); assert.equal(calls.length, 4);
    find("hq-restore-agree").checked = true; await undo.events.click(); assert.equal(calls.length, 5);
    await undo.events.click(); assert.equal(calls.length, 5);
  }
  window.__javFanzaOneClick.dispose();
  assert.equal(document.getElementById("jav-fanza-oneclick-hq"), undefined);
  assert.equal(document.getElementById("jav-fanza-oneclick-original-fit"), undefined);
}
(async () => {
  const scenarios = ["success", "studio", "smaller", "same", "change_entity", "unknown", "name"];
  for (const kind of scenarios) await scenario(kind);
  console.log(`HQ UI: ${scenarios.length} scenarios passed; preview, exact consent, uncropped images, restore and name selection checked`);
})().catch(error => { console.error(error); process.exitCode = 1; });
