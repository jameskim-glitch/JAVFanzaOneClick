// Synthetic DOM and GraphQL only. Never talks to a real Stash library.
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
  const document = { body, createElement: tag => new Element(tag), getElementById: id => body.all().find(e => e.id === id) };
  let reloads = 0;
  const window = { location: { pathname: "/performers", reload: () => reloads++ }, addEventListener() {}, removeEventListener() {} };
  const calls = [];
  const people = [{ id: "1", name: "<script>Literal name</script>", alias_list: ["Old Name"], image_path: "http://untrusted.example/image", scene_count: 4 },
    { id: "2", name: "Second Name", alias_list: ["Old Name"], scene_count: 1 }];
  const candidate = { ids: ["1", "2"], confidence: kind === "blocked" ? "blocked" : "review", evidence: ["별칭 일치"], conflicts: [], people, recommended_id: "1" };
  const preview = { ok: true, ids: candidate.ids, source: "2", destination: "1", name: people[0].name, token: "a".repeat(64), field_conflicts: [{ field: "height_cm", kept: 160, other: 165 }] };
  const context = { document, window, console: { info() {}, error() {} }, setInterval() { return 1; }, clearInterval() {}, clearTimeout() {}, setTimeout() { return 1; },
    async fetch(url, options) {
      assert.equal(url, "/graphql"); const data = JSON.parse(options.body); const args = data.variables.args; calls.push(args);
      let output;
      if (args.mode === "duplicates_scan") output = { ok: true, checked: 2, total_candidates: 1, identifier_matches: 0, review_matches: 1, blocked_matches: 0, candidates: [candidate] };
      else if (args.mode === "duplicates_prepare") output = preview;
      else if (args.mode === "duplicates_merge") {
        assert.equal(args.confirmed, true); assert.equal(args.reviewed_identity, true); assert.equal(args.token, preview.token);
        if (kind === "network_error") throw new Error("connection lost");
        output = kind === "unknown" ? { ok: false, status: "needs_check", message: "결과 확인 필요", backup: "C:/safe/backup.zip" }
          : kind === "not_merged" ? { ok: false, status: "not_merged", message: "병합하지 않았습니다. backup failed", backup_status: "unconfirmed", backup: null }
          : { ok: true, status: "merged", destination: "1", source: "2", name: people[0].name,
            backup_status: "completed", backup: kind === "success_null_backup" ? null : "C:/safe/backup.zip" };
      } else throw new Error("Unexpected operation " + args.mode);
      return { ok: true, status: 200, text: async () => JSON.stringify({ data: { runPluginOperation: { output } } }) };
    } };
  vm.runInNewContext(fs.readFileSync(__dirname + "/jav_fanza_oneclick.js", "utf8"), context);
  const button = body.querySelector('[data-role="duplicates"]');
  assert.equal(button.style.display, "none"); await button.events.click();
  const dialog = document.getElementById("jav-fanza-oneclick-duplicates"); assert.ok(dialog);
  assert.equal(calls.length, 1, "Scanning must not prepare/merge anything");
  assert.ok(dialog.all().some(e => e.textContent.includes("<script>Literal name</script>")));
  assert.equal(dialog.all().some(e => e.innerHTML), false);
  assert.equal(dialog.all().find(e => e.tagName === "img").src, "/performer/1/image", "Never use external image URL");
  if (kind === "blocked") { assert.equal(dialog.querySelector('[data-role="duplicates-prepare"]'), undefined); return; }
  const inspect = dialog.querySelector('[data-role="duplicates-prepare"]'); await inspect.events.click();
  assert.equal(calls.length, 2); const merge = dialog.querySelector('[data-role="duplicates-merge"]');
  const agree = dialog.querySelector('[data-role="duplicates-agree"]');
  assert.equal(merge.disabled, true); assert.equal(agree.checked, false);
  await merge.events.click(); assert.equal(calls.length, 2, "No operation without explicit confirmation");
  agree.checked = true; agree.events.change(); assert.equal(merge.disabled, false);
  if (kind === "change_destination") {
    const select = dialog.all().find(e => e["aria-label"] === "남길 배우 항목"); select.value = "2"; select.events.change();
    await merge.events.click(); assert.equal(calls.length, 2, "Changing destination invalidates preview"); return;
  }
  await merge.events.click(); assert.equal(calls.length, 3); assert.equal(reloads, 0);
  assert.equal(inspect.disabled, true); assert.equal(merge.disabled, true);
  await merge.events.click(); assert.equal(calls.length, 3, "No automatic or repeated merge after an outcome");
  if (kind === "network_error") assert.ok(dialog.all().some(e => e.textContent.includes("바로 재시도하지")));
  else if (kind === "not_merged") {
    assert.ok(dialog.all().some(e => e.textContent.includes("병합하지 않았습니다.")));
    assert.equal(dialog.all().some(e => e.textContent.includes("병합 완료 여부를 확인하지 못했습니다")), false);
  }
  else if (kind === "success_null_backup") {
    assert.ok(dialog.all().some(e => e.textContent.includes("Successfully backed up database to")));
    assert.equal(dialog.all().some(e => e.textContent.includes("완료 여부 확인 필요")), false);
  }
  else assert.ok(dialog.all().some(e => e.textContent.includes("C:/safe/backup.zip")));
  window.__javFanzaOneClick.dispose(); assert.equal(document.getElementById("jav-fanza-oneclick-duplicates"), undefined);
}

(async () => {
  const scenarios = ["success", "success_null_backup", "blocked", "change_destination", "unknown", "not_merged", "network_error"];
  for (const kind of scenarios) await scenario(kind);
  console.log(`Duplicate UI: ${scenarios.length} scenarios passed; null-backup success, definite non-merge, explicit consent and no retry verified`);
})().catch(error => { console.error(error); process.exitCode = 1; });
