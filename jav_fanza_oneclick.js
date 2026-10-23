(() => {
  "use strict";

  const VERSION = "1.12.0";
  window.__javFanzaOneClick?.dispose();
  let disposed = false;
  const PLUGIN_ID = "JAVFanzaOneClick";
  const PANEL_ID = "jav-fanza-oneclick-panel";
  const STATUS_ID = "jav-fanza-oneclick-status";
  const TASK_NAME = "전체 미처리 장면 채우기";
  const IMAGE_TASK_NAME = "배우 사진·제작사 로고 빈 항목 채우기";
  const NAME_TASK_NAME = "배우 이름 발음 기준 정리";
  const SUBTITLE_DIALOG_ID = "jav-fanza-oneclick-subtitles";
  const DUPLICATE_DIALOG_ID = "jav-fanza-oneclick-duplicates";
  const HQ_DIALOG_ID = "jav-fanza-oneclick-hq";
  const PHOTO_STYLE_ID = "jav-fanza-oneclick-original-fit";
  const ALIAS_DIALOG_ID = "jav-fanza-oneclick-alias-stashdb";
  let busy = false;

  const RUN_CURRENT = `
    mutation JAVFanzaOneClickRun($args: Map) {
      runPluginOperation(plugin_id: "${PLUGIN_ID}", args: $args)
    }
  `;
  const RUN_BULK = `
    mutation JAVFanzaOneClickBulk {
      runPluginTask(plugin_id: "${PLUGIN_ID}", task_name: "${TASK_NAME}")
    }
  `;

  function sceneIdFromLocation() {
    const match = window.location.pathname.match(/\/scenes\/(\d+)(?:\/|$)/);
    return match ? match[1] : null;
  }

  async function startTask(name, args) {
    const queued = await graphql("query OneClickActiveTasks { jobQueue { id status description } }");
    const existing = (queued.jobQueue || []).find(job => !["FINISHED", "CANCELLED", "FAILED"].includes(job.status) && (job.description || "").includes(name));
    if (existing) return existing.id;
    const data = args
      ? await graphql(`mutation OneClickTask($name: String!, $args: Map) { runPluginTask(plugin_id: "${PLUGIN_ID}", description: $name, args_map: $args) }`, { name, args })
      : await graphql(`mutation OneClickTask($name: String!) { runPluginTask(plugin_id: "${PLUGIN_ID}", task_name: $name) }`, { name });
    if (!data.runPluginTask) throw new Error("작업 등록 결과를 확인하지 못했습니다. Tasks에서 확인하세요.");
    return data.runPluginTask;
  }

  async function graphql(query, variables = {}) {
    const response = await fetch("/graphql", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/graphql-response+json, application/json",
      },
      body: JSON.stringify({ query, variables }),
    });
    const raw = await response.text();
    let payload;
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch {
      throw new Error(`Stash가 JSON이 아닌 응답을 반환했습니다. (HTTP ${response.status})`);
    }
    if (!response.ok) {
      throw new Error(payload?.errors?.[0]?.message || `Stash HTTP ${response.status}`);
    }
    if (payload.errors?.length) {
      throw new Error(payload.errors.map((item) => item.message || String(item)).join("\n"));
    }
    return payload.data || {};
  }

  function showStatus(message, kind = "info", timeout = 8000) {
    if (disposed) return;
    let box = document.getElementById(STATUS_ID);
    if (!box) {
      box = document.createElement("div");
      box.id = STATUS_ID;
      Object.assign(box.style, {
        position: "fixed",
        right: "22px",
        bottom: "78px",
        zIndex: "2147483647",
        width: "min(420px, calc(100vw - 44px))",
        padding: "12px 14px",
        borderRadius: "9px",
        boxShadow: "0 5px 22px rgba(0,0,0,.5)",
        color: "#fff",
        whiteSpace: "pre-wrap",
        fontSize: "13px",
        lineHeight: "1.45",
      });
      document.body.appendChild(box);
    }
    const colors = {
      info: "rgba(40, 47, 60, .97)",
      success: "rgba(31, 112, 72, .97)",
      error: "rgba(161, 49, 49, .97)",
    };
    box.style.background = colors[kind] || colors.info;
    box.textContent = message;
    box.style.display = "block";
    clearTimeout(box._hideTimer);
    if (timeout > 0) {
      box._hideTimer = setTimeout(() => { box.style.display = "none"; }, timeout);
    }
  }

  function resultText(result) {
    if (!result) return "처리가 끝났습니다.";
    if (result.skipped) return `${result.code || "장면"}: 이미 처리되어 건너뛰었습니다.`;
    const changes = Array.isArray(result.changes) && result.changes.length
      ? result.changes.join(", ")
      : "변경 없음";
    const warning = result.warning ? `\n${result.warning}` : "";
    return `${result.code || "장면"} 처리 완료\n${changes}${warning}`;
  }

  async function runCurrent() {
    if (busy) return;
    const sceneId = sceneIdFromLocation();
    if (!sceneId) {
      showStatus("장면 상세 화면에서 사용할 수 있습니다.", "error");
      return;
    }
    busy = true;
    refreshPanel();
    showStatus("FANZA 표지와 메타데이터를 가져오고 한국어 내용을 만드는 중입니다…", "info", 0);
    try {
      const data = await graphql(RUN_CURRENT, {
        args: { mode: "single", scene_id: sceneId, force: true },
      });
      const value = data.runPluginOperation;
      const result = value?.output ?? value;
      if (value?.error) throw new Error(value.error);
      if (result?.ok === false && !result?.skipped) throw new Error(result.error || "처리에 실패했습니다.");
      showStatus(resultText(result), result?.complete === false ? "info" : "success", 10000);
      if (result?.updated) setTimeout(() => {
        if (!disposed && sceneIdFromLocation() === sceneId) window.location.reload();
      }, 900);
    } catch (error) {
      console.error(`[JAV FANZA OneClick ${VERSION}]`, error);
      showStatus(`처리 실패\n${error?.message || error}`, "error", 12000);
    } finally {
      busy = false;
      refreshPanel();
    }
  }

  async function runBulk() {
    if (busy) return;
    busy = true;
    refreshPanel();
    showStatus("전체 미처리 장면 작업을 시작하는 중입니다…", "info", 0);
    try {
      const jobId = await startTask(TASK_NAME);
      showStatus(`백그라운드 작업을 시작했습니다.${jobId ? `\n작업 ID: ${jobId}` : ""}\n진행 상황은 작업(Tasks) 화면에서 확인할 수 있습니다.`, "success", 12000);
    } catch (error) {
      console.error(`[JAV FANZA OneClick ${VERSION}] bulk`, error);
      showStatus(`일괄 작업 시작 실패\n${error?.message || error}`, "error", 12000);
    } finally {
      busy = false;
      refreshPanel();
    }
  }

  async function runImages() {
    if (busy) return;
    const sceneId = sceneIdFromLocation();
    busy = true;
    refreshPanel();
    showStatus(sceneId ? "현재 장면의 배우 사진·제작사 로고를 확인하는 중입니다…" : "사진·로고 전체 작업을 시작하는 중입니다…", "info", 0);
    try {
      if (!sceneId) {
        const jobId = await startTask(IMAGE_TASK_NAME);
        showStatus(`사진·로고 전체 작업이 등록되어 있습니다.\n작업 ID: ${jobId}\n진행·결과는 작업(Tasks)과 로그에서 확인할 수 있습니다. 기존 이미지는 보존합니다.`, "success", 15000);
        return;
      }
      const data = await graphql(RUN_CURRENT, { args: { mode: "images_scene", scene_id: sceneId } });
      const value = data.runPluginOperation;
      if (value?.error) throw new Error(value.error);
      const result = value?.output ?? value;
      if (!result || typeof result.filled !== "number") throw new Error("사진·로고 작업 결과를 확인하지 못했습니다.");
      const parts = [`배우 사진 ${result.portraits || 0}개 · 제작사 로고 ${result.logos || 0}개 저장`,
        `기존 이미지 ${result.existing || 0}개 보존`];
      if (result.unmatched) parts.push(`출처를 확정하지 못한 항목 ${result.unmatched}개는 건너뛰었습니다.`);
      if (result.no_image) parts.push(`FANZA에 이미지가 없는 항목 ${result.no_image}개`);
      if (result.unknown) parts.push(`기존 이미지 상태 확인 불가 ${result.unknown}개`);
      if (result.failed) parts.push(`실패 ${result.failed}개: ${result.errors?.[0]?.error || "나중에 다시 시도하세요."}`);
      if (!result.checked) parts.push("등록된 배우·제작사가 없습니다. 먼저 현재 장면 채우기를 실행하세요.");
      showStatus(parts.join("\n"), result.failed || result.unmatched || result.unknown ? "info" : "success", 0);
      // Do not interrupt playback after an image-only update.
      if (result.filled && !disposed && sceneIdFromLocation() === sceneId) {
        const refresh = makeButton("화면 새로고침", () => {
          if (sceneIdFromLocation() === sceneId) window.location.reload();
        }, "image-refresh");
        refresh.style.marginTop = "10px";
        refresh.style.display = "block";
        document.getElementById(STATUS_ID)?.appendChild(refresh);
      }
    } catch (error) {
      showStatus(`사진·로고 작업 실패\n${error?.message || error}`, "error", 15000);
    } finally {
      busy = false;
      refreshPanel();
    }
  }

  async function runNames() {
    if (busy) return;
    busy = true;
    refreshPanel();
    showStatus("배우 이름 정리 작업을 시작하는 중입니다…", "info", 0);
    try {
      const data = { runPluginTask: await startTask(NAME_TASK_NAME) };
      showStatus(`배우 이름 정리를 시작했습니다.\n작업 ID: ${data.runPluginTask}\n공식 발음을 확인한 뒤 이전 자동 번역 이름을 교정합니다. 직접 수정한 이름은 보존합니다.\n작업 완료 후 화면을 새로고침하세요. 결과는 로그에서 확인할 수 있습니다.`, "success", 20000);
    } catch (error) {
      showStatus(`이름 정리 시작 실패\n${error?.message || error}`, "error", 15000);
    } finally {
      busy = false;
      refreshPanel();
    }
  }

  async function runAliasStashDB() {
    if (busy) return;
    document.getElementById(ALIAS_DIALOG_ID)?.remove();
    const overlay = document.createElement("div"); overlay.id = ALIAS_DIALOG_ID;
    Object.assign(overlay.style, { position: "fixed", inset: "0", zIndex: "2147483646", background: "rgba(0,0,0,.75)", padding: "24px", overflowY: "auto", color: "#f5f5f5" });
    const box = document.createElement("section"); overlay.appendChild(box);
    Object.assign(box.style, { maxWidth: "660px", margin: "8vh auto", padding: "22px", background: "#202630", borderRadius: "12px" });
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true"); box.setAttribute("aria-label", "영문 별명으로 이름 변경 후 StashDB 재스크래핑");
    const paragraph = text => { const p = document.createElement("p"); p.textContent = text; p.style.whiteSpace = "pre-wrap"; box.appendChild(p); return p; };
    paragraph("영문 별명 → 이름 변경 → StashDB 재스크래핑");
    paragraph("기존 영문 별명을 이름으로 올립니다. StashDB에서 이름·성 순서를 확인하고, 원래 한국어 이름은 별명으로 남깁니다. 프로필의 빈 정보와 사진을 가져오며, 시작 전에 Stash 백업을 만듭니다.");
    const currentId = window.location.pathname.match(/^\/performers\/(\d+)(?:\/|$)/)?.[1];
    const scope = document.createElement("select"); scope.setAttribute("aria-label", "재스크래핑 범위");
    for (const [value, text] of [["all", "전체 배우"], ...(currentId ? [[currentId, "현재 배우 한 명"]] : [])]) { const option = document.createElement("option"); option.value = value; option.textContent = text; scope.appendChild(option); }
    scope.value = currentId || "all"; box.appendChild(scope);
    const label = document.createElement("label"); label.style.display = "block"; label.style.marginTop = "12px";
    const upgrade = document.createElement("input"); upgrade.type = "checkbox"; upgrade.checked = false; upgrade.dataset.role = "alias-upgrade"; label.appendChild(upgrade);
    const text = document.createElement("span"); text.textContent = " 기존 사진도 더 큰 원본이 있으면 갱신"; label.appendChild(text); box.appendChild(label);
    const status = paragraph("영문 별명을 확인하는 중입니다…");
    const preview = paragraph(""); let eligible = 0; let submitted = false;
    const load = async () => {
      if (busy) return;
      busy = true; scope.disabled = true; start.disabled = true; refreshPanel();
      try {
        const args = { mode: "stashdb_alias_scan", ...(scope.value === "all" ? {} : { id: scope.value }) };
        const result = await duplicateOperation(args);
        if (!result.ok) throw new Error(result.error || "영문 별명 조회 실패");
        eligible = result.eligible;
        status.textContent = `대상 ${result.total}명 · 영문 별명 또는 영문 이름이 있는 배우 ${eligible}명\n여러 배우가 일치하거나 영문 별명이 없는 항목은 결과 로그에 표시합니다.`;
        preview.textContent = result.items.filter(item => item.english_aliases.length).slice(0, 8).map(item => `${item.name} → ${item.english_aliases.join(" / ")}`).join("\n") + (eligible > 8 ? "\n…" : "");
      } catch (error) { eligible = 0; status.textContent = `조회 실패: ${error?.message || error}`; }
      finally { busy = false; scope.disabled = submitted; start.disabled = !eligible || submitted; refreshPanel(); }
    };
    const start = makeButton("영문명으로 바꾸고 재스크래핑", async () => {
      if (busy || submitted || !eligible) return;
      submitted = true; busy = true; scope.disabled = true; start.disabled = true; refreshPanel();
      try {
        const args = { mode: "stashdb_alias_run", confirmed: true, photo_policy: upgrade.checked ? "upgrade" : "missing", ...(scope.value === "all" ? {} : { id: scope.value }) };
        const data = { runPluginTask: await startTask("영문 별명으로 이름 변경 후 StashDB 재스크래핑", args) };
        if (!data.runPluginTask) throw new Error("작업 시작 응답을 확인하지 못했습니다.");
        status.textContent = `재스크래핑 작업을 시작했습니다.\n작업 ID: ${data.runPluginTask}\nTasks와 로그에서 진행·결과를 확인하세요. 완료 후 Stash 화면을 새로고침하면 영문 이름과 새 정보를 볼 수 있습니다.`;
        preview.textContent = "";
      } catch (error) { status.textContent = `작업 시작 결과 확인 필요: ${error?.message || error}\nTasks에서 시작 여부를 확인하세요.`; }
      finally { busy = false; refreshPanel(); }
    }, "alias-start"); start.disabled = true; box.appendChild(start);
    box.appendChild(makeButton("닫기", () => { if (!busy) overlay.remove(); }, "alias-close"));
    scope.addEventListener("change", load);
    document.body.appendChild(overlay); await load();
  }

  async function subtitleOperation(args) {
    const data = await graphql(RUN_CURRENT, { args });
    const value = data.runPluginOperation;
    if (value?.error) throw new Error(value.error);
    const result = value?.output ?? value;
    if (!result || result.ok === false) throw new Error(result?.error || "자막 작업에 실패했습니다.");
    return result;
  }

  function showSubtitleRefresh(sceneId, message, kind = "success") {
    showStatus(message, kind, 0);
    if (disposed || sceneIdFromLocation() !== sceneId) return;
    const button = makeButton("화면 새로고침", () => {
      if (sceneIdFromLocation() === sceneId) window.location.reload();
    }, "subtitle-refresh");
    button.style.marginTop = "10px";
    button.style.display = "block";
    document.getElementById(STATUS_ID)?.appendChild(button);
  }

  async function watchSubtitleScan(sceneId, jobId, videoPath) {
    // Wait for the queued scan, not a fixed reload delay that races the job queue.
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      if (disposed) return;
      const data = await graphql(`query OneClickSubtitleJob($input: FindJobInput!) {
        findJob(input: $input) { status error }
      }`, { input: { id: jobId } });
      const job = data.findJob;
      if (job?.error || ["FAILED", "CANCELLED"].includes(job?.status)) {
        throw new Error(job?.error || "자막 연결 스캔이 취소되었습니다. 해당 폴더를 선택 스캔하세요.");
      }
      if (!job || job.status === "FINISHED") {
        const captions = await graphql(`query OneClickSubtitleCheck($id: ID!) {
          findScene(id: $id) { files { path } captions { language_code } }
        }`, { id: sceneId });
        const files = captions.findScene?.files || [];
        if (files.length > 1) {
          showSubtitleRefresh(sceneId, "자막 스캔이 끝났습니다. 여러 영상이 있는 장면이므로 선택한 영상의 자막 메뉴를 확인해주세요.", "info");
        } else if (files.length === 1 && files[0].path === videoPath
            && captions.findScene?.captions?.some((caption) => caption.language_code === "ko")) {
          showSubtitleRefresh(sceneId, "한국어 자막이 Stash에 연결되었습니다. 재생을 방해하지 않도록 새로고침은 직접 누를 수 있습니다.");
        } else {
          showStatus("자막은 저장됐지만 Stash 연결을 확인하지 못했습니다. 해당 폴더를 선택 스캔해주세요.", "info", 18000);
        }
        return;
      }
    }
    showStatus("자막은 저장됐습니다. 연결 스캔이 대기 중입니다. 작업 완료 후 화면을 새로고침해주세요.", "info", 18000);
  }

  function subtitleDialog(result, sceneId) {
    if (disposed) return;
    document.getElementById(SUBTITLE_DIALOG_ID)?.remove();
    const overlay = document.createElement("div");
    overlay.id = SUBTITLE_DIALOG_ID;
    Object.assign(overlay.style, { position: "fixed", inset: "0", zIndex: "2147483645",
      background: "rgba(0,0,0,.65)", display: "flex", alignItems: "center", justifyContent: "center", padding: "20px" });
    const box = document.createElement("section");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "한국어 자막 검색 결과");
    Object.assign(box.style, { background: "#24242b", color: "white", borderRadius: "12px", padding: "20px",
      width: "min(640px, 100%)", maxHeight: "80vh", overflowY: "auto", boxShadow: "0 8px 30px #000" });
    const heading = document.createElement("h3");
    heading.textContent = `${result.code} 자막`;
    box.appendChild(heading);
    const note = document.createElement("p");
    note.textContent = "선택한 페이지에 기존 한국어 Download 파일이 있을 때만 저장합니다. 번역·제작하지 않으며, 원본 영상에 따른 싱크 차이는 있을 수 있습니다.";
    Object.assign(note.style, { fontSize: "13px", lineHeight: "1.5", color: "#c7c7d0" });
    box.appendChild(note);
    const label = document.createElement("label");
    label.textContent = "적용할 영상: ";
    const select = document.createElement("select");
    Object.assign(select.style, { maxWidth: "100%", padding: "7px", background: "#353540", color: "white" });
    for (const file of result.files || []) {
      const option = document.createElement("option");
      option.value = String(file.index);
      option.textContent = file.basename + (file.existing.length ? " (기존 자막 있음)" : "");
      select.appendChild(option);
    }
    const first = (result.files || []).find((file) => !file.existing.length);
    if (first) select.value = String(first.index);
    select.setAttribute("aria-label", "적용할 영상");
    label.appendChild(select);
    box.appendChild(label);
    const localInfo = document.createElement("p");
    Object.assign(localInfo.style, { fontSize: "12px", whiteSpace: "pre-wrap", overflowWrap: "anywhere" });
    box.appendChild(localInfo);
    const rescan = makeButton("저장된 한국어 자막 다시 연결", async () => {
      if (busy || sceneIdFromLocation() !== sceneId) return;
      busy = true;
      select.disabled = true;
      refreshActions();
      refreshPanel();
      try {
        const file = result.files.find((item) => item.index === Number(select.value));
        const scanned = await subtitleOperation({ mode: "subtitle_rescan", scene_id: sceneId,
          file_index: file.index, file_path: file.path });
        showStatus(scanned.warning || "저장된 자막의 연결 스캔을 요청했습니다.", scanned.warning ? "info" : "success", 14000);
        for (const jobId of scanned.scan_job_ids || []) await watchSubtitleScan(sceneId, jobId, scanned.video_path);
      } catch (error) {
        showStatus(`기존 자막을 보존했습니다. 연결 확인 오류\n${error?.message || error}`, "error", 16000);
      } finally {
        busy = false;
        select.disabled = false;
        refreshActions();
        refreshPanel();
      }
    }, "subtitle-rescan");
    box.appendChild(rescan);
    function refreshActions() {
      const file = result.files.find((item) => item.index === Number(select.value));
      localInfo.textContent = file?.existing?.length
        ? `기존 자막을 보존합니다.\n${file.existing.join("\n")}\n한국어 표시를 위해서는 영상 이름 뒤에 .ko.srt 또는 .ko.vtt가 필요합니다.`
        : file?.other?.length ? "기존 외국어 자막과 별도로 한국어 자막을 저장합니다." : "";
      rescan.style.display = file?.rescan_paths?.length ? "inline-block" : "none";
      rescan.disabled = busy;
      box.querySelectorAll("button[data-role='download']").forEach((item) => {
        item.disabled = busy || Boolean(file?.existing?.length);
      });
    }
    select.addEventListener("change", refreshActions);
    for (const candidate of result.candidates || []) {
      const row = document.createElement("div");
      Object.assign(row.style, { marginTop: "12px", padding: "12px", border: "1px solid #484854", borderRadius: "8px" });
      const title = document.createElement("div");
      title.textContent = candidate.title;
      row.appendChild(title);
      const link = document.createElement("a");
      link.textContent = "사이트에서 보기";
      link.href = candidate.page_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      Object.assign(link.style, { color: "#bfaaff", marginRight: "12px", fontSize: "12px" });
      row.appendChild(link);
      const button = makeButton("한국어 자막 받기", async () => {
        if (busy) return;
        if (sceneIdFromLocation() !== sceneId) {
          overlay.remove();
          showStatus("장면이 바뀌었습니다. 현재 장면에서 다시 검색해주세요.", "info");
          return;
        }
        busy = true;
        select.disabled = true;
        box.querySelectorAll("button[data-role='download']").forEach((item) => { item.disabled = true; });
        refreshPanel();
        showStatus("기존 한국어 자막을 확인하고 내려받는 중입니다…", "info", 0);
        let savedPath = null;
        try {
          const downloaded = await subtitleOperation({ mode: "subtitle_download", scene_id: sceneId,
            page_url: candidate.page_url, file_index: Number(select.value),
            file_path: result.files.find((file) => file.index === Number(select.value))?.path });
          if (downloaded.status === "not_found") {
            showStatus(downloaded.message, "info", 12000);
          } else if (downloaded.status === "existing") {
            overlay.remove();
            showStatus("기존 자막이 있어 덮어쓰지 않았습니다.\n" + downloaded.existing.join("\n"), "info", 14000);
          } else {
            savedPath = downloaded.path;
            overlay.remove();
            showStatus(`자막 저장 완료\n${downloaded.path}\n${downloaded.warning || "Stash 연결 스캔을 요청했습니다…"}`, downloaded.warning ? "info" : "success", 15000);
            if (downloaded.scan_job_id) await watchSubtitleScan(sceneId, downloaded.scan_job_id, downloaded.video_path);
            if (downloaded.warning) showSubtitleRefresh(sceneId, `자막은 저장됐습니다.\n${downloaded.warning}`, "info");
          }
        } catch (error) {
          showStatus(`${savedPath ? `자막은 저장됐습니다: ${savedPath}\n연결 확인 오류` : "자막 작업 오류"}\n${error?.message || error}`, "error", 18000);
        } finally {
          busy = false;
          select.disabled = false;
          refreshActions();
          refreshPanel();
        }
      }, "download");
      row.appendChild(button);
      box.appendChild(row);
    }
    if (result.truncated) {
      const more = document.createElement("p");
      more.textContent = "후보가 많아 처음 30개를 표시합니다.";
      box.appendChild(more);
    }
    const close = makeButton("닫기", () => overlay.remove(), "close");
    close.style.marginTop = "16px";
    box.appendChild(close);
    refreshActions();
    overlay.appendChild(box);
    overlay.addEventListener("click", (event) => { if (event.target === overlay) overlay.remove(); });
    overlay.addEventListener("keydown", (event) => { if (event.key === "Escape") overlay.remove(); });
    document.body.appendChild(overlay);
    close.focus();
  }

  async function runSubtitleSearch() {
    if (busy) return;
    const sceneId = sceneIdFromLocation();
    if (!sceneId) return;
    busy = true;
    refreshPanel();
    showStatus("SubtitleCat에서 현재 품번의 자막을 검색하는 중입니다…", "info", 0);
    try {
      const result = await subtitleOperation({ mode: "subtitle_search", scene_id: sceneId });
      if (disposed || sceneIdFromLocation() !== sceneId) return;
      busy = false;
      if (result.status === "existing") {
        showStatus("기존 자막을 찾았습니다. 파일명을 확인하거나 다시 연결할 수 있습니다.", "info", 14000);
        subtitleDialog(result, sceneId);
      } else if (!result.candidates?.length) {
        showStatus(`${result.code}: 사이트에 해당 품번의 자막이 없습니다. 자막은 제작하지 않습니다.`, "info", 12000);
      } else {
        showStatus("자막 후보를 찾았습니다. 적용할 영상과 후보를 선택해주세요.", "success");
        subtitleDialog(result, sceneId);
      }
    } catch (error) {
      showStatus(`자막 검색 실패\n${error?.message || error}`, "error", 16000);
    } finally {
      busy = false;
      refreshPanel();
    }
  }

  async function duplicateOperation(args) {
    const data = await graphql(RUN_CURRENT, { args });
    const value = data.runPluginOperation;
    if (value?.error) throw new Error(value.error);
    const result = value?.output ?? value;
    if (!result || typeof result.ok !== "boolean") throw new Error("중복 배우 작업 결과를 확인하지 못했습니다.");
    return result;
  }

  function duplicateDialog(result) {
    document.getElementById(DUPLICATE_DIALOG_ID)?.remove();
    const overlay = document.createElement("div");
    overlay.id = DUPLICATE_DIALOG_ID;
    Object.assign(overlay.style, { position: "fixed", inset: "0", zIndex: "2147483646", background: "rgba(0,0,0,.75)",
      overflowY: "auto", padding: "24px", color: "#f5f5f5" });
    const box = document.createElement("div");
    Object.assign(box.style, { maxWidth: "980px", margin: "0 auto", background: "#202630", padding: "22px", borderRadius: "12px" });
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "중복 배우 검사");
    overlay.appendChild(box);
    const paragraph = (parent, text) => {
      const p = document.createElement("p");
      p.textContent = text;
      p.style.whiteSpace = "pre-wrap";
      parent.appendChild(p);
      return p;
    };
    paragraph(box, `중복 배우 검사 · ${result.checked}명\n식별번호 일치 ${result.identifier_matches}쌍 / 이름·별칭 확인 필요 ${result.review_matches}쌍 / 정보 충돌 ${result.blocked_matches}쌍`);
    paragraph(box, "검사만으로는 아무것도 바뀌지 않습니다. 이름·사진을 비교하고 남길 배우를 선택하세요.\n병합하면 다른 배우 항목은 제거되며 장면·이미지·갤러리·기존 태그 연결은 남길 배우로 모입니다. 영상 파일은 변경하지 않습니다.");
    if (result.truncated) paragraph(box, `후보 ${result.total_candidates}쌍 중 처음 500쌍만 표시합니다. 처리 후 다시 검사하세요.`);
    if (result.ignored_common_keys) paragraph(box, `너무 많은 배우가 공유하는 이름·식별번호 ${result.ignored_common_keys}개는 오인 방지를 위해 제외했습니다.`);
    const controls = document.createElement("div");
    Object.assign(controls.style, { display: "flex", gap: "8px", flexWrap: "wrap" });
    box.appendChild(controls);
    controls.appendChild(makeButton("다시 검사", () => { if (!busy) runDuplicates(); }, "duplicates-rescan"));
    controls.appendChild(makeButton("Stash 화면 새로고침", () => { if (!busy) window.location.reload(); }, "duplicates-refresh"));
    controls.appendChild(makeButton("닫기", () => { if (!busy) overlay.remove(); }, "duplicates-close"));
    const filter = document.createElement("select");
    for (const [value, title] of [["all", "모든 후보"], ["identifier", "식별번호 일치"], ["review", "이름·별칭 확인 필요"], ["blocked", "정보 충돌 (병합 차단)"]]) {
      const option = document.createElement("option"); option.value = value; option.textContent = title; filter.appendChild(option);
    }
    filter.value = "all";
    filter.setAttribute("aria-label", "중복 후보 필터");
    controls.appendChild(filter);
    const cards = [];
    const invalidated = new Set();
    for (const candidate of result.candidates || []) {
      const card = document.createElement("section");
      Object.assign(card.style, { marginTop: "18px", border: "1px solid #606775", padding: "14px", borderRadius: "8px" });
      card.dataset.confidence = candidate.confidence;
      cards.push(card); box.appendChild(card);
      paragraph(card, candidate.confidence === "identifier" ? "식별번호 일치 · 확인 후 병합 가능" : candidate.confidence === "blocked" ? "정보 충돌 · 병합 차단" : "이름·별칭만 일치 · 동일인 여부 직접 확인 필요");
      paragraph(card, [...candidate.evidence, ...candidate.conflicts].join("\n"));
      const people = document.createElement("div");
      Object.assign(people.style, { display: "flex", flexWrap: "wrap", gap: "18px" }); card.appendChild(people);
      for (const person of candidate.people) {
        const part = document.createElement("div"); part.style.flex = "1 1 250px"; people.appendChild(part);
        if (/^[0-9]+$/.test(person.id)) {
          const link = document.createElement("a"); link.href = `/performers/${person.id}`; link.target = "_blank"; link.rel = "noopener noreferrer";
          link.textContent = `#${person.id} · ${person.name}`; link.style.color = "#beaaff"; part.appendChild(link);
          if (person.image_path && !/[?&]default=true(?:&|$)/.test(person.image_path)) {
            const photo = document.createElement("img"); photo.src = `/performer/${person.id}/image`;
            photo.alt = `${person.name} 등록 사진`; photo.loading = "lazy";
            Object.assign(photo.style, { display: "block", width: "120px", height: "150px", objectFit: "contain", marginTop: "8px" }); part.appendChild(photo);
          }
        }
        paragraph(part, `생년월일: ${person.birthdate || "미등록"} · 장면 ${person.scene_count || 0}개\n별칭: ${(person.alias_list || []).join(", ") || "없음"}`);
      }
      if (candidate.confidence === "blocked") continue;
      const destination = document.createElement("select");
      destination.setAttribute("aria-label", "남길 배우 항목");
      for (const person of candidate.people) {
        const option = document.createElement("option"); option.value = person.id;
        option.textContent = `남길 배우: #${person.id} ${person.name}${person.id === candidate.recommended_id ? " (정보량 기준 추천)" : ""}`;
        destination.appendChild(option);
      }
      destination.value = candidate.recommended_id;
      card.appendChild(destination);
      const area = document.createElement("div");
      let currentPreview = null;
      destination.addEventListener("change", () => { currentPreview = null; area.textContent = ""; });
      const inspect = makeButton("병합 미리보기", async () => {
        if (busy) return;
        if (candidate.ids.some((id) => invalidated.has(id))) { area.textContent = "이 배우가 다른 병합에 포함됐습니다. 다시 검사하세요."; return; }
        busy = true; refreshPanel(); currentPreview = null; area.textContent = "최신 정보를 비교하는 중입니다…";
        try {
          const preview = await duplicateOperation({ mode: "duplicates_prepare", ids: candidate.ids, destination: destination.value });
          if (disposed || destination.value !== preview.destination) return;
          if (!preview.ok) throw new Error(preview.message || preview.error || "미리보기 실패");
          currentPreview = preview; area.textContent = "";
          paragraph(area, `#${preview.source} → #${preview.destination} ${preview.name}\n남길 항목의 이름은 바꾸지 않습니다. 별칭·출처 링크·설명은 모으고 빈 프로필 정보는 채웁니다.\n사진: ${preview.image_from ? "합쳐질 배우의 사진을 복사" : "남길 배우의 사진 유지"}`);
          if (preview.field_conflicts?.length) paragraph(area, "서로 다른 값은 남길 배우의 값을 유지합니다:\n" + preview.field_conflicts.map((c) => `${c.field}: 유지 ${JSON.stringify(c.kept)} / 다른 값 ${JSON.stringify(c.other)}`).join("\n"));
          paragraph(area, "병합 전에 Stash 전체 DB와 저장된 이미지 백업을 만듭니다. 용량에 따라 시간이 걸릴 수 있습니다.\n개별 병합 취소 버튼은 없습니다. 복구하려면 Stash 백업 복원이 필요하며, 백업 이후 다른 변경도 되돌아갈 수 있습니다.");
          const label = document.createElement("label"); const agree = document.createElement("input"); agree.type = "checkbox"; agree.checked = false;
          agree.dataset.role = "duplicates-agree"; label.appendChild(agree);
          const text = document.createElement("span"); text.textContent = " 두 항목이 같은 배우임을 확인했으며, 백업 후 병합에 동의합니다."; label.appendChild(text); area.appendChild(label);
          const commit = makeButton("백업 후 이 두 배우 병합", async () => {
            if (busy || !agree.checked || currentPreview !== preview) return;
            busy = true; commit.disabled = true; destination.disabled = true; refreshPanel();
            showStatus("Stash 백업 후 선택한 두 배우를 병합합니다. 다른 정리 작업은 잠시 멈춰 주세요…", "info", 0);
            try {
              const merged = await duplicateOperation({ mode: "duplicates_merge", ids: candidate.ids, destination: preview.destination,
                token: preview.token, confirmed: true, reviewed_identity: true });
              currentPreview = null; candidate.ids.forEach((id) => invalidated.add(id));
              const backupInfo = merged.backup_status === "completed"
                ? (merged.backup || "완료 — Stash에 설정된 백업 폴더에 저장했습니다. 정확한 경로는 Stash 로그의 ‘Successfully backed up database to’에서 확인하세요.")
                : (merged.backup_status === "not_requested" ? "요청하지 않음" : merged.backup || "완료 여부 확인 필요");
              area.textContent = `${merged.ok ? `병합 완료: #${merged.destination} ${merged.name}` : merged.message}\n백업: ${backupInfo}\n${merged.warning || ""}\n다른 후보를 처리하려면 다시 검사하세요.`;
              inspect.disabled = true;
              showStatus(merged.ok ? "배우 병합이 완료됐습니다. 결과 화면의 백업 안내를 확인하세요." : merged.message,
                merged.ok ? "success" : "error", 15000);
            } catch (error) {
              // The browser may lose a response after the server has committed: no retry here.
              currentPreview = null; candidate.ids.forEach((id) => invalidated.add(id));
              area.textContent = `병합 완료 여부를 확인하지 못했습니다: ${error?.message || error}\n바로 재시도하지 말고 Stash의 두 배우를 확인한 뒤 다시 검사하세요.`;
              inspect.disabled = true; showStatus(area.textContent, "error", 0);
            } finally { busy = false; refreshPanel(); }
          }, "duplicates-merge");
          commit.disabled = true;
          agree.addEventListener("change", () => { commit.disabled = !agree.checked || busy || currentPreview !== preview; });
          area.appendChild(commit);
        } catch (error) { area.textContent = `미리보기 실패: ${error?.message || error}`; }
        finally { busy = false; refreshPanel(); }
      }, "duplicates-prepare");
      card.appendChild(inspect); card.appendChild(area);
    }
    filter.addEventListener("change", () => { cards.forEach((card) => { card.style.display = filter.value === "all" || filter.value === card.dataset.confidence ? "block" : "none"; }); });
    if (!cards.length) paragraph(box, "현재 기준으로 일치하는 중복 후보가 없습니다. 이름이 완전히 다르고 공통 식별번호·별칭도 없으면 찾지 못할 수 있습니다.");
    document.body.appendChild(overlay);
  }

  async function runDuplicates() {
    if (busy) return;
    busy = true; refreshPanel();
    showStatus("등록된 배우의 식별번호·이름·별칭을 비교합니다. 이 검사는 데이터를 변경하지 않습니다…", "info", 0);
    try {
      const result = await duplicateOperation({ mode: "duplicates_scan" });
      if (!result.ok) throw new Error(result.error || "중복 검사 실패");
      if (!disposed) duplicateDialog(result);
      showStatus(`중복 배우 검사 완료: ${result.total_candidates}쌍. 비교 화면에서 확인하세요.`, "success", 5000);
    } catch (error) { showStatus(`중복 배우 검사 실패\n${error?.message || error}`, "error", 12000); }
    finally { busy = false; refreshPanel(); }
  }

  function originalPhotoFit(enabled) {
    document.getElementById(PHOTO_STYLE_ID)?.remove();
    if (enabled) {
      const style = document.createElement("style"); style.id = PHOTO_STYLE_ID;
      style.textContent = ".performer-card-image { object-fit: contain !important; object-position: center !important; }";
      (document.head || document.body).appendChild(style);
    }
    try { window.localStorage?.setItem("jav-fanza-original-fit", enabled ? "1" : "0"); } catch (_) {}
  }

  async function runHQ() {
    if (busy) return;
    document.getElementById(HQ_DIALOG_ID)?.remove();
    const overlay = document.createElement("div"); overlay.id = HQ_DIALOG_ID;
    Object.assign(overlay.style, { position: "fixed", inset: "0", zIndex: "2147483646", background: "rgba(0,0,0,.8)", overflowY: "auto", padding: "24px", color: "#f5f5f5" });
    const box = document.createElement("div");
    Object.assign(box.style, { maxWidth: "1060px", margin: "0 auto", background: "#202630", padding: "22px", borderRadius: "12px" });
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true"); box.setAttribute("aria-label", "원본 사진·로고 업데이트"); overlay.appendChild(box);
    const paragraph = (parent, text) => { const p = document.createElement("p"); p.textContent = text; p.style.whiteSpace = "pre-wrap"; parent.appendChild(p); return p; };
    const checkbox = (parent, text, role) => { const label = document.createElement("label"); label.style.display = "block";
      const input = document.createElement("input"); input.type = "checkbox"; input.checked = false; input.dataset.role = role;
      label.appendChild(input); const span = document.createElement("span"); span.textContent = " " + text; label.appendChild(span); parent.appendChild(label); return input; };
    const photo = (parent, src, alt) => { const img = document.createElement("img");
      if (!/^data:image\/(?:jpeg|png|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(src || "") && !/^https:\/\/stashdb\.org\/images\/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(src || "")) return;
      img.src = src; img.alt = alt; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
      Object.assign(img.style, { width: "100%", height: "300px", objectFit: "contain", background: "#141820" }); parent.appendChild(img); };
    paragraph(box, "원본 사진·제작사 로고 업데이트");
    paragraph(box, "연결된 StashDB 식별번호가 일치하는 원본만 조회합니다. 전신사진은 아래 후보에서 직접 확인해 선택하세요.\n사진을 자르거나 늘려 저장하지 않습니다. 기존 원본은 교체 전에 보관하며 이름·장면·태그는 바꾸지 않습니다.");
    const fit = checkbox(box, "배우 목록에서도 사진 전체 보기 (원본 구도 유지)", "hq-fit");
    fit.checked = Boolean(document.getElementById(PHOTO_STYLE_ID)); fit.addEventListener("change", () => originalPhotoFit(fit.checked));
    const controls = document.createElement("div"); Object.assign(controls.style, { display: "flex", gap: "8px", flexWrap: "wrap", marginTop: "14px" }); box.appendChild(controls);
    const kind = document.createElement("select"); kind.setAttribute("aria-label", "사진 종류");
    for (const [value, text] of [["performer", "배우 사진"], ["studio", "제작사 로고"]]) { const opt = document.createElement("option"); opt.value = value; opt.textContent = text; kind.appendChild(opt); }
    kind.value = window.location.pathname.startsWith("/studios") ? "studio" : "performer"; controls.appendChild(kind);
    const selector = document.createElement("select"); selector.setAttribute("aria-label", "사진을 바꿀 항목"); selector.style.maxWidth = "100%"; controls.appendChild(selector);
    const status = paragraph(box, "목록을 읽는 중입니다…");
    const gallery = document.createElement("div"); Object.assign(gallery.style, { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "12px" }); box.appendChild(gallery);
    const area = document.createElement("div"); area.style.marginTop = "18px"; box.appendChild(area);
    let activeToken = null;
    const request = async (args) => { const result = await duplicateOperation(args); if (!result.ok) throw new Error(result.message || result.error || "작업 결과를 확인하지 못했습니다."); return result; };
    const task = async (action) => {
      if (busy) return;
      busy = true; kind.disabled = true; selector.disabled = true; refreshPanel();
      try { await action(); }
      catch (error) { status.textContent = `작업 중지: ${error?.message || error}`; }
      finally { busy = false; kind.disabled = false; selector.disabled = false; refreshPanel(); }
    };
    const reset = () => { activeToken = null; area.textContent = ""; gallery.textContent = ""; };
    const loadCatalog = () => task(async () => {
      reset(); selector.textContent = "";
      const result = await request({ mode: "images_catalog", kind: kind.value }); if (disposed) return;
      const currentId = window.location.pathname.match(/^\/(?:performers|studios)\/(\d+)/)?.[1];
      for (const item of result.items) { const option = document.createElement("option"); option.value = item.id;
        option.textContent = `#${item.id} ${item.name}${item.linked ? "" : " — " + item.reason}`; selector.appendChild(option); }
      selector.value = result.items.some((item) => item.id === currentId) ? currentId : (result.items[0]?.id || "");
      status.textContent = `${result.total}개 중 StashDB 연결 ${result.linked}개. 항목을 고르고 ‘원본 후보 조회’를 누르세요.\n연결 없는 항목은 그대로 두며, 이름을 추측해 다른 사람 사진을 넣지 않습니다.`;
      nameButton.style.display = kind.value === "performer" ? "inline-block" : "none";
    });
    const showPreview = (ticket, url) => task(async () => {
      activeToken = null; area.textContent = "선택한 원본과 현재 사진의 실제 크기를 확인하는 중입니다…";
      const result = await request({ mode: "images_preview", ticket, url }); if (disposed) return;
      area.textContent = ""; activeToken = result.token;
      const compare = document.createElement("div"); Object.assign(compare.style, { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))", gap: "16px" }); area.appendChild(compare);
      for (const [label, image] of [["현재 사진", result.old], ["선택한 원본", result.new]]) {
        const part = document.createElement("div"); compare.appendChild(part); paragraph(part, image ? `${label} · ${image.width}×${image.height}` : `${label} · 없음`);
        if (image) photo(part, image.data, label);
      }
      if (result.same) { paragraph(area, "이미 같은 원본입니다. 교체하지 않습니다."); return; }
      const allowSmaller = result.smaller ? checkbox(area, "해상도는 작아지지만 이 구도의 사진을 사용하겠습니다.", "hq-smaller") : null;
      const agree = checkbox(area, "같은 배우·제작사의 원하는 사진임을 확인했고, 기존 원본 보관 후 교체합니다.", "hq-agree");
      const commit = makeButton("기존 원본 보관 후 교체", () => {
        if (busy || !agree.checked || (allowSmaller && !allowSmaller.checked) || activeToken !== result.token) return;
        activeToken = null; commit.disabled = true;
        return task(async () => {
          status.textContent = "원본 보관과 사진 교체를 진행합니다…";
          const saved = await request({ mode: "images_apply", token: result.token, confirmed: true, allow_smaller: Boolean(allowSmaller?.checked) }); if (disposed) return;
          area.textContent = "";
          paragraph(area, saved.status === "unchanged" ? saved.message : `${saved.name} · ${saved.width}×${saved.height} 원본 저장 완료\n교체 전 기록: ${saved.history}`);
          status.textContent = "처리했습니다. 목록 사진은 ‘Stash 화면 새로고침’으로 갱신하세요.";
          if (saved.restore_token) {
            const restoreAgree = checkbox(area, "방금 교체하기 전 사진으로 되돌리겠습니다.", "hq-restore-agree");
            const undo = makeButton("이전 원본 복원", () => {
              if (busy || !restoreAgree.checked || undo.disabled) return;
              undo.disabled = true;
              return task(async () => { const restored = await request({ mode: "images_restore", token: saved.restore_token, confirmed: true }); status.textContent = restored.message; });
            }, "hq-restore"); area.appendChild(undo);
          }
        });
      }, "hq-apply");
      commit.disabled = true;
      const updateConsent = () => { commit.disabled = !agree.checked || Boolean(allowSmaller && !allowSmaller.checked) || activeToken !== result.token; };
      agree.addEventListener("change", updateConsent); allowSmaller?.addEventListener("change", updateConsent); area.appendChild(commit);
      status.textContent = "원본 구도와 실제 해상도를 비교해 주세요. 전신 여부는 자동 판정하지 않습니다.";
    });
    controls.appendChild(makeButton("원본 후보 조회", () => task(async () => {
      reset(); status.textContent = "연결된 StashDB에서 같은 식별번호의 원본 후보를 조회합니다…";
      const found = await request({ mode: "images_candidates", kind: kind.value, id: selector.value }); if (disposed) return;
      status.textContent = `${found.name} / 출처: ${found.source_name}\n원본 후보 ${found.urls.length}개. 원하는 구도를 선택해 비교하세요.${found.truncated ? " (처음 24개 표시)" : ""}`;
      if (!found.urls.length) paragraph(gallery, "확인 가능한 원본 후보가 없어 현재 사진을 유지합니다.");
      for (const [index, url] of found.urls.entries()) { const card = document.createElement("div"); gallery.appendChild(card); photo(card, url, `${found.source_name} 원본 후보 ${index + 1}`);
        card.appendChild(makeButton(`원본 ${index + 1} 비교`, () => showPreview(found.ticket, url), "hq-preview")); }
    }), "hq-candidates"));
    const nameButton = makeButton("이 배우 이름 발음 확인", () => task(async () => {
      reset(); const result = await request({ mode: "name_preview", id: selector.value }); if (disposed) return;
      activeToken = result.token;
      paragraph(area, `현재: ${result.before}\nFANZA 원명: ${result.original}\n공식 발음으로 확인한 현재 이름·옛 예명 중 사용할 표기를 고르세요. 이전 이름은 별칭으로 보존합니다.`);
      const names = document.createElement("select"); names.setAttribute("aria-label", "확인된 배우 이름");
      for (const text of result.options) { const opt = document.createElement("option"); opt.value = text; opt.textContent = text; names.appendChild(opt); }
      names.value = result.options[0] || ""; area.appendChild(names);
      const agree = checkbox(area, "선택한 발음 표기로 이 배우 한 명의 이름을 변경합니다.", "hq-name-agree");
      const save = makeButton("선택한 이름 적용", () => {
        if (busy || !agree.checked || activeToken !== result.token) return;
        activeToken = null; save.disabled = true;
        return task(async () => { const saved = await request({ mode: "name_apply", token: result.token, name: names.value, confirmed: true });
          area.textContent = `${saved.before} → ${saved.name}\n이전 이름과 확인된 예명은 별칭에 보존했습니다.`; });
      }, "hq-name-apply"); save.disabled = true; agree.addEventListener("change", () => { save.disabled = !agree.checked; }); area.appendChild(save);
      status.textContent = "이름의 뜻은 번역하지 않습니다. 사진·장면·태그도 변경하지 않습니다.";
    }), "hq-name-preview"); controls.appendChild(nameButton);
    controls.appendChild(makeButton("Stash 화면 새로고침", () => { if (!busy) window.location.reload(); }, "hq-refresh"));
    const studios = makeButton("제작사 이름·StashDB 연결 정리", () => task(async () => {
      const job = await startTask("제작사 이름·StashDB 연결 정리");
      status.textContent = `제작사 정리 작업 ID: ${job}\n일본어 이름·별칭을 StashDB 목록과 대조해 연결합니다. 중복 병합은 설정 → 작업의 별도 병합 작업에서 실행하세요.`;
    }), "hq-studios"); controls.appendChild(studios);
    controls.appendChild(makeButton("닫기", () => { if (!busy) { activeToken = null; overlay.remove(); } }, "hq-close"));
    kind.addEventListener("change", loadCatalog); selector.addEventListener("change", reset);
    document.body.appendChild(overlay);
    await loadCatalog();
  }

  function makeButton(text, action, role) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.dataset.role = role;
    Object.assign(button.style, {
      border: "1px solid rgba(255,255,255,.22)",
      borderRadius: "7px",
      padding: "8px 11px",
      background: "#6741d9",
      color: "white",
      fontSize: "12px",
      fontWeight: "650",
      cursor: "pointer",
    });
    button.addEventListener("click", action);
    return button;
  }

  function mountPanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.title = "새로 스캔된 장면은 자동으로 처리됩니다.";
    Object.assign(panel.style, {
      position: "fixed",
      right: "22px",
      bottom: "22px",
      zIndex: "2147483646",
      display: "flex",
      flexWrap: "wrap",
      maxWidth: "calc(100vw - 44px)",
      gap: "6px",
      alignItems: "center",
      padding: "7px",
      borderRadius: "10px",
      background: "rgba(24,24,28,.95)",
      boxShadow: "0 4px 18px rgba(0,0,0,.45)",
    });
    panel.appendChild(makeButton("현재 장면 채우기", runCurrent, "current"));
    panel.appendChild(makeButton("사진·로고", runImages, "images"));
    panel.appendChild(makeButton("배우 이름 정리", runNames, "names"));
    panel.appendChild(makeButton("영문 별명 → StashDB", runAliasStashDB, "alias-stashdb"));
    panel.appendChild(makeButton("중복 배우 검사", runDuplicates, "duplicates"));
    panel.appendChild(makeButton("사진·로고 업데이트", runHQ, "hq"));
    panel.appendChild(makeButton("자막 찾기", runSubtitleSearch, "subtitles"));
    panel.appendChild(makeButton("미처리 전체", runBulk, "bulk"));
    const auto = document.createElement("span");
    auto.dataset.role = "auto-indicator";
    auto.textContent = "자동 ✓";
    auto.title = "Scene.Create.Post 훅으로 새 장면을 자동 처리합니다.";
    Object.assign(auto.style, {
      padding: "6px 7px",
      color: "#93e2b1",
      fontSize: "11px",
      fontWeight: "650",
      whiteSpace: "nowrap",
    });
    panel.appendChild(auto);
    document.body.appendChild(panel);
    return panel;
  }

  function refreshPanel() {
    if (disposed) return;
    const panel = mountPanel();
    const performerPage = /^\/performers(?:\/|$)/.test(window.location.pathname);
    const current = panel.querySelector('[data-role="current"]');
    const bulk = panel.querySelector('[data-role="bulk"]');
    const subtitles = panel.querySelector('[data-role="subtitles"]');
    const images = panel.querySelector('[data-role="images"]');
    const names = panel.querySelector('[data-role="names"]');
    const duplicates = panel.querySelector('[data-role="duplicates"]');
    const hq = panel.querySelector('[data-role="hq"]');
    const aliasStashDB = panel.querySelector('[data-role="alias-stashdb"]');
    if (aliasStashDB) {
      aliasStashDB.style.display = "none";
      aliasStashDB.disabled = busy;
      aliasStashDB.style.opacity = busy ? ".55" : "1";
    }
    if (hq) {
      hq.style.display = /^\/(?:performers|studios)(?:\/|$)/.test(window.location.pathname) ? "inline-block" : "none";
      hq.disabled = busy; hq.style.opacity = busy ? ".55" : "1";
    }
    if (duplicates) {
      duplicates.style.display = "none";
      duplicates.disabled = busy;
      duplicates.style.opacity = busy ? ".55" : "1";
    }
    if (names) {
      names.style.display = "none";
      names.disabled = busy;
      names.style.opacity = busy ? ".55" : "1";
    }
    if (images) {
      images.textContent = sceneIdFromLocation() ? "사진·로고" : "이미지 전체";
      images.title = sceneIdFromLocation() ? "현재 장면에 연결된 배우·제작사의 빈 이미지만 채웁니다." : "배우·제작사의 빈 이미지를 라이브러리 전체에서 채웁니다.";
      images.disabled = busy;
      images.style.opacity = busy ? ".55" : "1";
    }
    if (subtitles) {
      subtitles.style.display = sceneIdFromLocation() ? "inline-block" : "none";
      subtitles.disabled = busy;
      subtitles.style.opacity = busy ? ".55" : "1";
    }
    if (current) {
      current.style.display = sceneIdFromLocation() ? "inline-block" : "none";
      current.disabled = busy;
      current.style.opacity = busy ? ".55" : "1";
    }
    if (bulk) {
      bulk.disabled = busy;
      bulk.style.opacity = busy ? ".55" : "1";
    }
    // Actor pages have three controls, in the requested order.
    for (const [button, order] of [[images, 0], [bulk, 1], [hq, 2]]) {
      if (button) button.style.order = performerPage ? String(order) : "";
    }
    const autoIndicator = panel.querySelector('[data-role="auto-indicator"]');
    if (autoIndicator) autoIndicator.style.display = performerPage ? "none" : "inline";
  }

  let fitOriginals = true;
  try { fitOriginals = window.localStorage?.getItem("jav-fanza-original-fit") !== "0"; } catch (_) {}
  originalPhotoFit(fitOriginals);
  mountPanel();
  refreshPanel();
  window.addEventListener("popstate", refreshPanel);
  const historyHooks = [];
  for (const method of ["pushState", "replaceState"]) {
    if (typeof window.history?.[method] !== "function") continue;
    const previous = window.history[method];
    const wrapper = function (...args) { const result = previous.apply(this, args); if (!disposed) refreshPanel(); return result; };
    window.history[method] = wrapper;
    historyHooks.push({ method, previous, wrapper });
  }
  const dispose = () => {
    disposed = true;
    for (const { method, previous, wrapper } of historyHooks) {
      if (window.history[method] === wrapper) window.history[method] = previous;
    }
    window.removeEventListener("popstate", refreshPanel);
    window.removeEventListener("beforeunload", dispose);
    [PANEL_ID, STATUS_ID, SUBTITLE_DIALOG_ID, DUPLICATE_DIALOG_ID, HQ_DIALOG_ID, PHOTO_STYLE_ID, ALIAS_DIALOG_ID].forEach((id) => document.getElementById(id)?.remove());
  };
  window.__javFanzaOneClick = { dispose };
  window.addEventListener("beforeunload", dispose);
  console.info(`[JAV FANZA OneClick ${VERSION}] loaded`);
})();
