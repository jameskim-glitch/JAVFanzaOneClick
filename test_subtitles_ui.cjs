// Exercise UI wiring in a CommonJS harness, without touching a user library.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.style = {}; this.dataset = {};
    this.events = {}; this.attributes = {}; this.textContent = ""; this.value = "";
  }
  appendChild(element) { this.children.push(element); element.parent = this; return element; }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, callback) { this.events[key] = callback; }
  focus() {}
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((item) => item !== this); }
  all() { return [this, ...this.children.flatMap((child) => child.all())]; }
  querySelectorAll(selector) {
    const role = selector.match(/data-role=['"]([^'"]+)['"]/);
    return this.all().filter((item) => role && item.dataset.role === role[1]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}

async function runScenario(kind, nested = true) {
  const body = new Element("body");
  const document = {
    body, createElement: (tag) => new Element(tag),
    getElementById: (id) => body.all().find((element) => element.id === id),
  };
  const calls = [];
  let reloads = 0;
  let activeIntervals = 0;
  const window = { location: { pathname: ["images_bulk", "names_bulk"].includes(kind) ? "/performers" : "/scenes/77", reload: () => { reloads += 1; } }, addEventListener() {}, removeEventListener() {} };
  const result = {
    ok: true, code: "SONE-458", status: "search",
    files: [{ index: 0, path: "D:/SONE-458_1080p.mp4", basename: "SONE-458_1080p.mp4", existing: [] }],
    candidates: kind === "empty" ? [] : [{ title: "SONE-458 <unsafe literal>", page_url: "https://www.subtitlecat.com/subs/1/SONE-458.html" }],
  };
  if (kind === "existing") {
    result.status = "existing";
    result.candidates = [];
    result.files[0].existing = ["D:/SONE-458_1080p.ko.srt"];
    result.files[0].rescan_paths = ["D:/SONE-458_1080p.ko.srt"];
  }
  const context = {
    document, window, console: { info() {}, error() {} },
    setInterval: () => { activeIntervals += 1; return activeIntervals; }, clearInterval() { activeIntervals -= 1; }, clearTimeout() {},
    setTimeout: (callback) => { callback(); return 1; },
    async fetch(url, options) {
      assert.equal(url, "/graphql");
      const payload = JSON.parse(options.body);
      calls.push(payload);
      let data;
      if (payload.query.includes("jobQueue")) {
        data = { jobQueue: [] };
      } else if (payload.query.includes("runPluginTask")) {
        assert.match(payload.variables.name, kind === "names_bulk" ? /배우 이름 발음 기준 정리/ : /배우 사진·제작사 로고 빈 항목 채우기/);
        data = { runPluginTask: "901" };
      } else if (payload.query.includes("findJob")) {
        if (kind === "scan_error") throw new Error("connection lost");
        data = { findJob: { status: "FINISHED", error: null } };
      } else if (payload.query.includes("findScene")) data = { findScene: {
        files: kind === "multi_file" ? [{ path: "D:/first.mp4" }, { path: "D:/second.mp4" }] : [{ path: "D:/SONE-458_1080p.mp4" }],
        captions: [{ language_code: "ko" }] } };
      else {
        const args = payload.variables.args;
        let output = result;
        if (args.mode === "images_scene") {
          assert.equal(args.scene_id, "77");
          output = { ok: kind !== "images_failed", checked: kind === "images_empty" ? 0 : 3,
            filled: kind === "images_empty" ? 0 : 2, portraits: 1, logos: 1, existing: 1,
            failed: kind === "images_failed" ? 1 : 0, errors: [{ error: "download unavailable" }] };
        } else if (args.mode === "subtitle_download") {
          assert.equal(args.scene_id, "77");
          assert.equal(args.file_index, 0);
          assert.equal(args.file_path, "D:/SONE-458_1080p.mp4");
          output = kind === "no_ko" ? { ok: true, status: "not_found", message: "기존 한국어 자막 없음" }
            : { ok: true, status: "downloaded", path: "D:/SONE-458_1080p.ko.srt", video_path: "D:/SONE-458_1080p.mp4", scan_job_id: "123" };
        } else if (args.mode === "subtitle_rescan") {
          assert.equal(args.file_path, "D:/SONE-458_1080p.mp4");
          output = { ok: true, status: "rescanned", video_path: args.file_path, scan_job_ids: ["123"] };
        }
        data = { runPluginOperation: nested ? { output } : output };
      }
      return { ok: true, status: 200, text: async () => JSON.stringify({ data }) };
    },
  };
  vm.runInNewContext(fs.readFileSync(__dirname + "/jav_fanza_oneclick.js", "utf8"), context);
  vm.runInNewContext(fs.readFileSync(__dirname + "/jav_fanza_oneclick.js", "utf8"), context);
  assert.equal(activeIntervals, 0, "SPA navigation should not require polling");
  if (kind === "names_bulk") {
    const button = body.querySelector('[data-role="names"]');
    assert.equal(button.style.display, "none");
    await button.events.click();
    assert.equal(calls.length, 2);
    assert.equal(reloads, 0);
    assert.match(document.getElementById("jav-fanza-oneclick-status").textContent, /직접 수정한 이름은 보존/);
    assert.equal(button.disabled, false);
    return;
  }
  if (kind.startsWith("images_")) {
    const button = body.querySelector('[data-role="images"]');
    assert.equal(button.textContent, kind === "images_bulk" ? "이미지 전체" : "사진·로고");
    await button.events.click();
    assert.equal(calls.length, kind === "images_bulk" ? 2 : 1);
    assert.equal(reloads, 0, "Image update must not interrupt playback");
    const status = document.getElementById("jav-fanza-oneclick-status").textContent;
    if (kind === "images_bulk") assert.match(status, /작업 ID: 901/);
    else if (kind === "images_empty") assert.match(status, /등록된 배우·제작사가 없습니다/);
    else {
      assert.match(status, /배우 사진 1개 · 제작사 로고 1개 저장/);
      if (kind === "images_failed") assert.match(status, /실패 1개: download unavailable/);
      await body.querySelector('[data-role="image-refresh"]').events.click();
      assert.equal(reloads, 1);
    }
    assert.equal(button.disabled, false);
    return;
  }
  const search = body.querySelector('[data-role="subtitles"]');
  assert.ok(search);
  await search.events.click();
  assert.equal(calls[0].variables.args.mode, "subtitle_search");
  const dialog = document.getElementById("jav-fanza-oneclick-subtitles");
  if (kind === "empty") {
    assert.equal(dialog, undefined);
    assert.equal(calls.length, 1);
    assert.equal(reloads, 0);
  } else {
    assert.ok(dialog);
    if (kind === "existing") {
      const retry = dialog.querySelector('[data-role="subtitle-rescan"]');
      assert.equal(retry.disabled, false);
      await retry.events.click();
      assert.equal(calls.length, 4);
      assert.equal(reloads, 0);
      assert.equal(search.disabled, false);
      return;
    }
    assert.ok(dialog.all().some((item) => item.textContent === "SONE-458 <unsafe literal>"));
    assert.equal(dialog.all().some((item) => item.innerHTML), false);
    const download = dialog.querySelector('[data-role="download"]');
    assert.equal(download.disabled, false, "Search completion must enable download");
    await download.events.click();
    if (kind === "no_ko") {
      assert.equal(calls.length, 2);
      assert.equal(reloads, 0);
      assert.ok(document.getElementById("jav-fanza-oneclick-subtitles"));
    } else if (kind === "scan_error") {
      assert.equal(reloads, 0);
      assert.match(document.getElementById("jav-fanza-oneclick-status").textContent, /자막은 저장됐습니다/);
    } else {
      assert.equal(calls.length, 4);
      assert.equal(reloads, 0, "A completed scan must not interrupt playback");
      assert.equal(document.getElementById("jav-fanza-oneclick-subtitles"), undefined);
      if (kind === "multi_file") {
        assert.match(document.getElementById("jav-fanza-oneclick-status").textContent, /여러 영상/);
      }
      await body.querySelector('[data-role="subtitle-refresh"]').events.click();
      assert.equal(reloads, 1);
    }
  }
  assert.equal(search.disabled, false);
}

(async () => {
  await runScenario("download");
  await runScenario("download", false);
  await runScenario("empty");
  await runScenario("no_ko");
  await runScenario("scan_error");
  await runScenario("multi_file");
  await runScenario("existing");
  await runScenario("images_success");
  await runScenario("images_success", false);
  await runScenario("images_failed");
  await runScenario("images_empty");
  await runScenario("images_bulk");
  await runScenario("names_bulk");
  console.log("UI: 13 scenarios passed; subtitles, images, name repair, bulk dispatch and playback protection checked");
})().catch((error) => { console.error(error); process.exitCode = 1; });
