/*
 * spatialdata dashboard.
 *
 * Plain JS on purpose: the whole UI is one form, one list and one map, which
 * does not earn a framework or a build step. Everything is driven off
 * /api/meta so the worker stays the single source of truth for which voices,
 * layers and styles actually exist on this machine.
 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  let META = null;
  let map = null;
  let bbox = [7.30, 45.85, 8.05, 46.25];   // w, s, e, n
  let pollTimer = null;
  let searchTimer = null;

  // ------------------------------------------------------------ helpers
  async function api(path, options) {
    const res = await fetch("/api" + path, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, options || {}));
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* text body */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined) continue;
      node.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function fillSelect(select, items, selected) {
    select.innerHTML = "";
    for (const item of items) {
      const value = typeof item === "string" ? item : item.id;
      const label = typeof item === "string" ? item : (item.label || item.id);
      const opt = el("option", { value }, label);
      if (value === selected) opt.selected = true;
      select.append(opt);
    }
  }

  function fmtDuration(s) {
    if (s === null || s === undefined) return "-";
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${Math.round(s % 60)}s`;
  }

  // ------------------------------------------------------- region picker
  function bboxPolygon(b) {
    const [w, s, e, n] = b;
    return {
      type: "Feature", properties: {},
      geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] }
    };
  }

  function syncBboxInputs() {
    const ids = ["#bb-w", "#bb-s", "#bb-e", "#bb-n"];
    ids.forEach((id, i) => { $(id).value = bbox[i].toFixed(4); });
    const [w, s, e, n] = bbox;
    const kmWide = Math.round((e - w) * 111 * Math.cos((s + n) / 2 * Math.PI / 180));
    const kmTall = Math.round((n - s) * 111);
    $("#bbox-readout").textContent = `${kmWide} x ${kmTall} km`;
  }

  function setBbox(next, fit) {
    // Keep the box the right way round however the user dragged it.
    bbox = [Math.min(next[0], next[2]), Math.min(next[1], next[3]),
            Math.max(next[0], next[2]), Math.max(next[1], next[3])];
    syncBboxInputs();
    if (map && map.getSource("bbox")) {
      map.getSource("bbox").setData(bboxPolygon(bbox));
    }
    if (fit && map) {
      map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
                    { padding: 60, duration: 600 });
    }
  }

  function initMap() {
    map = new maplibregl.Map({
      container: "picker-map",
      style: "https://tiles.openfreemap.org/styles/dark",
      center: [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2],
      zoom: 6,
      attributionControl: { compact: true }
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    map.on("load", () => {
      map.addSource("bbox", { type: "geojson", data: bboxPolygon(bbox) });
      map.addLayer({
        id: "bbox-fill", type: "fill", source: "bbox",
        paint: { "fill-color": "#ffd166", "fill-opacity": 0.12 }
      });
      map.addLayer({
        id: "bbox-line", type: "line", source: "bbox",
        paint: { "line-color": "#ffd166", "line-width": 2 }
      });
      map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: 60, duration: 0 });
    });

    // Shift-drag draws a new box; that keeps ordinary panning intact.
    let dragStart = null;
    map.getCanvas().addEventListener("mousedown", (ev) => {
      if (!ev.shiftKey) return;
      ev.preventDefault();
      map.dragPan.disable();
      const p = map.unproject([ev.offsetX, ev.offsetY]);
      dragStart = [p.lng, p.lat];
    });
    map.getCanvas().addEventListener("mousemove", (ev) => {
      if (!dragStart) return;
      const p = map.unproject([ev.offsetX, ev.offsetY]);
      setBbox([dragStart[0], dragStart[1], p.lng, p.lat], false);
    });
    window.addEventListener("mouseup", () => {
      if (!dragStart) return;
      dragStart = null;
      map.dragPan.enable();
    });

    $("#picker-map").insertAdjacentHTML("beforeend",
      '<div style="position:absolute;bottom:8px;left:10px;font-size:11px;' +
      'color:#8496ab;background:#0a0e14bb;padding:3px 8px;border-radius:5px;' +
      'pointer-events:none">shift + drag to draw a region</div>');

    ["#bb-w", "#bb-s", "#bb-e", "#bb-n"].forEach((id, i) => {
      $(id).addEventListener("change", () => {
        const next = bbox.slice();
        next[i] = parseFloat($(id).value);
        if (!Number.isNaN(next[i])) setBbox(next, true);
      });
    });
  }

  /** Trailing part of the full display name, i.e. region and country. */
  function contextOf(r) {
    const parts = (r.name || "").split(",").map((p) => p.trim()).filter(Boolean);
    if (parts.length <= 1) return "";
    return parts.slice(1).slice(-2).join(", ");
  }

  /** Rough width of a bbox in km, so a tiny hamlet is obvious at a glance. */
  function spanKm(bbox) {
    if (!bbox) return "";
    const [w, s, e, n] = bbox;
    const km = (e - w) * 111 * Math.cos((s + n) / 2 * Math.PI / 180);
    return km >= 1 ? `${Math.round(km)} km` : "<1 km";
  }

  // --------------------------------------------------------- place search
  async function runSearch() {
    const q = $("#place-search").value.trim();
    const list = $("#search-results");
    if (q.length < 2) { list.hidden = true; return; }
    try {
      const data = await api("/geocode?q=" + encodeURIComponent(q));
      list.innerHTML = "";
      if (!data.results.length) {
        list.append(el("li", {}, "no matches"));
      }
      for (const r of data.results) {
        list.append(el("li", {
          onclick: () => {
            setBbox(r.bbox, true);
            list.hidden = true;
            $("#place-search").value = r.short;
            const form = $("#job-form");
            if (!form.place_name.value) form.place_name.value = r.short;
            if (!form.title.value) form.title.value = r.short;
          }
        },
          el("span", { class: "sr-name" }, r.short),
          el("span", { class: "sr-type" }, r.type || ""),
          // Full context matters: searching "Paki" returns a village in
          // Nigeria long before it returns Pakistan, and without the country
          // on screen there is no way to tell the two apart.
          el("span", { class: "sr-where" }, contextOf(r)),
          el("span", { class: "sr-size" }, spanKm(r.bbox))));
      }
      list.hidden = false;
    } catch (err) {
      list.innerHTML = "";
      list.append(el("li", {}, "search failed: " + err.message));
      list.hidden = false;
    }
  }

  // ---------------------------------------------------------- job form
  function buildOverlayCards() {
    const descriptions = {
      satellite: "NASA GIBS true-colour imagery for the chosen date",
      satellite_after: "Second date, for before/after — needs an end date",
      weather: "Cloud, storm, fire or snow layers from GIBS",
      population: "Kontur H3 population density, draped on the terrain",
      borders: "International boundaries, drawn as a glowing line"
    };
    const grid = $("#overlay-grid");
    grid.innerHTML = "";
    for (const name of META.overlays) {
      const input = el("input", { type: "checkbox", name: "overlay", value: name });
      const card = el("label", { class: "overlay-card" },
        el("div", { class: "oc-top" }, input,
           el("span", { class: "oc-name" }, name)),
        el("div", { class: "oc-desc" }, descriptions[name] || ""));
      input.addEventListener("change", () => {
        card.classList.toggle("on", input.checked);
        updateLayerSelects();
      });
      grid.append(card);
    }
  }

  function selectedOverlays() {
    return $$("input[name=overlay]:checked").map((i) => i.value);
  }

  function updateLayerSelects() {
    const chosen = selectedOverlays();
    const basemap = $("#basemap").value;
    $("#satellite_layer").closest(".field").style.display =
      (chosen.includes("satellite") || chosen.includes("satellite_after")
       || basemap === "satellite") ? "" : "none";
    // before/after is meaningless without the second date, so say so inline
    const needsEnd = chosen.includes("satellite_after");
    const endField = $("#job-form").date_end.closest(".field");
    endField.style.outline = needsEnd && !$("#job-form").date_end.value
      ? "1px solid var(--fail)" : "";
    endField.querySelector("span").innerHTML = needsEnd
      ? "End date <em>(required for before/after)</em>"
      : "End date <em>(optional range)</em>";
    $("#weather_layer").closest(".field").style.display =
      chosen.includes("weather") ? "" : "none";
    $("#style-field").style.display = basemap === "vector" ? "" : "none";
    updateEstimate();
  }

  function updateEstimate() {
    const form = $("#job-form");
    const [w, h] = form.resolution.value.split("x").map(Number);
    const fps = Number(form.fps.value);
    const seconds = Number(form.duration_s.value) || 0;
    const frames = Math.round(seconds * fps);
    // ~9 fps measured at 720p on this GPU; larger frames cost roughly with area.
    const rate = 9 * (1280 * 720) / (w * h);
    const mins = frames / Math.max(0.6, rate) / 60;
    $("#estimate").textContent =
      `${frames} frames - roughly ${mins < 1 ? "under a minute" : Math.round(mins) + " min"} of rendering` +
      (form.narration.value.trim() ? ", plus narration (the script may extend the length)" : "");
  }

  function providerVoices(id) {
    const p = (META.tts_providers || []).find((x) => x.id === id);
    return p ? p.voices : [];
  }

  function populateForm() {
    fillSelect($("#basemap"), META.basemaps, "vector");
    fillSelect($("#vector_style"), META.vector_styles, "fiord");
    fillSelect($("#label_density"), META.label_densities, "balanced");
    fillSelect($("#satellite_layer"), META.satellite_layers, "truecolor_viirs");
    fillSelect($("#weather_layer"), META.weather_layers);

    // Paid providers say so in the dropdown itself: picking one spends money,
    // and that should never be a surprise found later in the stats.
    const providers = (META.tts_providers || []).map((p) => ({
      id: p.id,
      label: p.id + (p.paid ? " (paid)" : "") + (p.available ? "" : " (not configured)")
    }));
    fillSelect($("#tts_provider"), providers, "kokoro");
    fillSelect($("#tts_voice"), providerVoices("kokoro"), "af_heart");
    fillSelect($("#music"), [{ id: "", label: "no music" }].concat(
      (META.music || []).map((m) => ({ id: m.id, label: m.label }))));

    $("#tts_provider").addEventListener("change", (e) => {
      fillSelect($("#tts_voice"), providerVoices(e.target.value));
    });
    $("#basemap").addEventListener("change", updateLayerSelects);
    ["resolution", "fps", "duration_s", "narration"].forEach((n) => {
      $("#job-form")[n].addEventListener("input", updateEstimate);
    });

    const d = META.defaults || {};
    $("#job-form").resolution.value = `${d.width || 1920}x${d.height || 1080}`;
    $("#job-form").fps.value = String(d.fps || 30);

    buildOverlayCards();
    updateLayerSelects();
    $("#attribution-text").textContent = META.attribution || "";
  }

  function formToSpec() {
    const form = $("#job-form");
    const [width, height] = form.resolution.value.split("x").map(Number);
    const spec = {
      bbox: bbox,
      place_name: form.place_name.value.trim() || "Untitled region",
      title: form.title.value.trim(),
      date: form.date.value || null,
      date_end: form.date_end.value || null,
      basemap: form.basemap.value,
      vector_style: form.vector_style.value,
      label_density: form.label_density.value,
      terrain: form.terrain.checked,
      hillshade: form.hillshade.checked,
      overlays: selectedOverlays(),
      satellite_layer: form.satellite_layer.value,
      weather_layer: selectedOverlays().includes("weather")
        ? form.weather_layer.value : null,
      narration: form.narration.value.trim(),
      tts_provider: form.tts_provider.value,
      tts_voice: form.tts_voice.value,
      music: form.music.value || null,
      duration_s: Number(form.duration_s.value),
      width, height,
      fps: Number(form.fps.value)
    };
    return spec;
  }

  // ------------------------------------------------------------- queue
  function stageClass(stage) {
    if (stage === "done") return "done";
    if (stage === "failed") return "failed";
    return "busy";
  }

  function renderJobs(jobs) {
    const list = $("#job-list");
    list.innerHTML = "";
    const active = jobs.filter((j) => !["done", "failed"].includes(j.stage)).length;
    $("#queue-count").textContent = String(active);

    if (!jobs.length) {
      list.append(el("div", { class: "empty" },
        "No jobs yet. Pick a region and hit Render."));
      return;
    }
    for (const job of jobs) {
      const cls = stageClass(job.stage);
      const pct = Math.round((job.progress || 0) * 100);
      list.append(el("div", { class: "job", onclick: () => openDrawer(job.id) },
        el("div", { class: "job-top" },
          el("span", { class: "job-title" }, job.spec.title || job.spec.place_name),
          el("span", { class: "job-id" }, job.id),
          el("span", { class: "job-stage" },
            el("span", { class: "chip " + cls }, job.stage.replace(/_/g, " ")))),
        el("div", { class: "bar" }, el("i", {
          class: cls, style: `width:${job.stage === "done" ? 100 : pct}%`
        })),
        el("div", { class: "job-meta" },
          el("span", {}, job.message || ""),
          el("span", {}, `elapsed ${fmtDuration(job.elapsed_s)}`),
          job.eta_s ? el("span", {}, `eta ${fmtDuration(job.eta_s)}`) : null)));
    }
  }

  async function refreshJobs() {
    try {
      const data = await api("/jobs?limit=60");
      renderJobs(data.jobs);
      const busy = data.jobs.some((j) => !["done", "failed"].includes(j.stage));
      setStatus(busy ? "busy" : "ok", busy ? "rendering" : "idle");
      if (drawerJobId) refreshDrawer();
    } catch (err) {
      setStatus("fail", "offline");
    }
  }

  function setStatus(kind, label) {
    const node = $("#worker-status");
    node.className = "status " + kind;
    node.querySelector(".label").textContent = label;
  }

  // ------------------------------------------------------------ drawer
  let drawerJobId = null;

  function openDrawer(id) {
    drawerJobId = id;
    $("#job-drawer").hidden = false;
    refreshDrawer();
  }

  function closeDrawer() {
    drawerJobId = null;
    $("#job-drawer").hidden = true;
  }

  async function refreshDrawer() {
    let job;
    try { job = await api("/jobs/" + drawerJobId); }
    catch (err) { return; }

    $("#drawer-title").textContent = job.spec.title || job.spec.place_name;
    const body = $("#drawer-body");
    body.innerHTML = "";

    const idx = job.stages.indexOf(job.stage);
    body.append(el("div", { class: "stage-track" },
      job.stages.map((s, i) => el("div", {
        class: "stage-pip " + (job.stage === "done" || i < idx ? "done" : i === idx ? "current" : "")
      }))));
    body.append(el("div", { class: "stage-names" },
      job.stages.map((s) => el("span", {}, s.split("_")[0]))));

    if (job.error) {
      body.append(el("p", { class: "form-error", style: "margin-top:14px" }, job.error));
    }

    const previews = Number(job.stats?.render?.frames ? 5 : 0);
    if (previews) {
      const grid = el("div", { class: "previews" });
      for (let i = 0; i < previews; i++) {
        grid.append(el("img", {
          src: `/api/jobs/${job.id}/preview/${i}`, loading: "lazy",
          onerror: (e) => e.target.remove()
        }));
      }
      body.append(el("h3", { style: "font-size:12px;color:#8496ab;margin:18px 0 0" },
                     "Preview frames"), grid);
    }

    if (job.artifacts && job.artifacts.video) {
      body.append(el("h3", { style: "font-size:12px;color:#8496ab;margin:18px 0 0" }, "Output"));
      body.append(el("video", { controls: "", src: `/api/jobs/${job.id}/video` }));
      body.append(el("div", { class: "actions", style: "margin-top:10px" },
        el("a", { class: "btn-primary", href: `/api/jobs/${job.id}/video`,
                  download: "", style: "text-decoration:none" }, "Download MP4")));
    }

    const s = job.stats || {};
    const kv = el("dl", { class: "kv" });
    const rows = [
      ["region", job.spec.bbox.map((v) => v.toFixed(3)).join(", ")],
      ["overlays", job.spec.overlays.join(", ") || "none"],
      ["frames", s.frames],
      ["video length", s.duration_s ? s.duration_s + "s" : null],
      ["render speed", s.render?.render_fps ? s.render.render_fps + " fps" : null],
      ["renderer", s.render?.renderer],
      ["tiles planned", s.prefetch_plan?.total],
      ["downloaded", s.prefetch ? s.prefetch.mb_downloaded + " MB" : null],
      ["cache misses", s.render_misses?.misses],
      ["voice", s.voice ? `${s.voice.provider}/${s.voice.voice}, ${s.voice.words} words` : null],
      ["alignment", s.voice?.aligned_with],
      ["file size", s.video ? s.video.size_mb + " MB" : null],
      ["total time", s.total_s ? fmtDuration(s.total_s) : null]
    ];
    for (const [k, v] of rows) {
      if (v === null || v === undefined || v === "") continue;
      kv.append(el("dt", {}, k), el("dd", {}, String(v)));
    }
    body.append(kv);
  }

  // ---------------------------------------------------------- settings
  async function loadSettings() {
    const { config } = await api("/settings");
    const d = META.defaults || {};
    $("#set-fps").value = config.render?.fps ?? d.fps ?? 30;
    $("#set-width").value = config.render?.width ?? d.width ?? 1920;
    $("#set-height").value = config.render?.height ?? d.height ?? 1080;
    $("#set-terrain").value = config.render?.terrain_exaggeration
      ?? d.terrain_exaggeration ?? 1.3;
    $("#set-miss").value = config.render?.miss_policy ?? "warn";
    $("#set-music-db").value = config.audio?.music_gain_db ?? -22;
    $("#set-duck-db").value = config.audio?.duck_gain_db ?? -14;
    fillSelect($("#set-tts"), (META.tts_providers || []).map((p) => p.id),
               config.tts?.provider ?? "kokoro");

    const scale = config.overlays?.population?.color_scale
      || d.population_color_scale || [];
    const holder = $("#set-scale");
    holder.innerHTML = "";
    scale.forEach((c) => holder.append(el("input", { type: "color", value: c })));
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const payload = {
      render: {
        fps: Number($("#set-fps").value),
        width: Number($("#set-width").value),
        height: Number($("#set-height").value),
        terrain_exaggeration: Number($("#set-terrain").value),
        miss_policy: $("#set-miss").value
      },
      tts: { provider: $("#set-tts").value },
      audio: {
        music_gain_db: Number($("#set-music-db").value),
        duck_gain_db: Number($("#set-duck-db").value)
      },
      overlays: {
        population: {
          color_scale: $$("#set-scale input").map((i) => i.value)
        }
      }
    };
    try {
      const res = await api("/settings", { method: "PUT", body: JSON.stringify(payload) });
      $("#settings-msg").style.color = "var(--ok)";
      $("#settings-msg").textContent = res.note || "saved";
    } catch (err) {
      $("#settings-msg").style.color = "var(--fail)";
      $("#settings-msg").textContent = err.message;
    }
  }

  // -------------------------------------------------------------- boot
  function wireTabs() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        $$(".tab").forEach((t) => t.classList.remove("is-active"));
        $$(".view").forEach((v) => v.classList.remove("is-active"));
        tab.classList.add("is-active");
        $("#view-" + tab.dataset.view).classList.add("is-active");
        if (tab.dataset.view === "queue") refreshJobs();
        if (tab.dataset.view === "new" && map) map.resize();
      });
    });
  }

  async function boot() {
    wireTabs();
    try {
      META = await api("/meta");
    } catch (err) {
      setStatus("fail", "cannot reach worker");
      return;
    }
    populateForm();
    initMap();
    syncBboxInputs();
    await loadSettings();
    setStatus(META.worker_enabled ? "ok" : "busy",
              META.worker_enabled ? "idle" : "UI only");

    $("#search-btn").addEventListener("click", runSearch);
    $("#place-search").addEventListener("input", () => {
      clearTimeout(searchTimer);
      // Nominatim allows one request a second; debounce well inside that.
      searchTimer = setTimeout(runSearch, 500);
    });
    $("#place-search").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); runSearch(); }
    });

    $("#job-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = $("#submit-btn");
      btn.disabled = true;
      $("#form-error").textContent = "";
      try {
        await api("/jobs", { method: "POST", body: JSON.stringify(formToSpec()) });
        $$(".tab").find((t) => t.dataset.view === "queue").click();
      } catch (err) {
        $("#form-error").textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });

    $("#refresh-jobs").addEventListener("click", refreshJobs);
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#job-drawer").addEventListener("click", (e) => {
      if (e.target.id === "job-drawer") closeDrawer();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && drawerJobId) closeDrawer();
    });
    $("#settings-form").addEventListener("submit", saveSettings);

    refreshJobs();
    pollTimer = setInterval(refreshJobs, 2000);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
