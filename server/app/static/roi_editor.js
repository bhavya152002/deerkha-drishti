// Visual polygon editor for detection ROI (drawn at 1080x720, single polygon)
// and ignore zones (drawn at native camera res ~1920x1080, multiple polygons).
// Coordinates are stored in the layer's reference resolution, exactly matching
// how the edge reads ROI_POLYGONS_RAW_ORIGINAL and roi_zones.json.

const RoiEditor = (() => {
  let cv, ctx, cam = null, layer = "roi";
  let polys = [];          // array of polygons; each polygon = array of [x,y] in ref coords
  let refW = 1080, refH = 720;
  let img = null;
  let drag = null;         // {p: polyIdx, v: vertexIdx}
  const HIT = 9;           // px hit radius on canvas
  const DISP_W = 960;

  function scale() { return DISP_W / refW; }
  function toCanvas(pt) { const s = scale(); return [pt[0] * s, pt[1] * s]; }
  function toRef(cx, cy) { const s = scale(); return [Math.round(cx / s), Math.round(cy / s)]; }

  function setLayer(which) {
    layer = which;
    if (!cam) return;
    if (which === "roi") {
      refW = cam.roi_drawn_w || 1080; refH = cam.roi_drawn_h || 720;
      polys = cam.roi_polygon && cam.roi_polygon.length ? [cam.roi_polygon.map(p => [p[0], p[1]])] : [];
    } else {
      refW = cam.ignore_drawn_w || 1920; refH = cam.ignore_drawn_h || 1080;
      polys = (cam.ignore_zones || []).map(poly => poly.map(p => [p[0], p[1]]));
    }
    resize();
    draw();
    document.getElementById("layer_info").textContent =
      which === "roi"
        ? `Detection ROI · single polygon · reference ${refW}×${refH}`
        : `Ignore zones · multiple polygons · reference ${refW}×${refH}`;
    document.getElementById("btn_newpoly").style.display = which === "ignore" ? "" : "none";
  }

  function resize() {
    cv.width = DISP_W;
    cv.height = Math.round(DISP_W * refH / refW);
  }

  function loadImage(url) {
    img = null;
    if (!url) { draw(); return; }
    const im = new Image();
    im.onload = () => { img = im; draw(); };
    im.onerror = () => { img = null; draw(); };
    im.src = url;
  }

  function draw() {
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (img) ctx.drawImage(img, 0, 0, cv.width, cv.height);
    else {
      ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.strokeStyle = "#1c232d"; ctx.lineWidth = 1;
      for (let x = 0; x < cv.width; x += 40) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, cv.height); ctx.stroke(); }
      for (let y = 0; y < cv.height; y += 40) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cv.width, y); ctx.stroke(); }
    }
    const color = layer === "roi" ? "#f5a623" : "#f85149";
    polys.forEach(poly => {
      if (!poly.length) return;
      ctx.beginPath();
      poly.forEach((pt, i) => { const [x, y] = toCanvas(pt); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.closePath();
      ctx.fillStyle = color + "22"; ctx.fill();
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
      poly.forEach(pt => {
        const [x, y] = toCanvas(pt);
        ctx.beginPath(); ctx.arc(x, y, 4, 0, 7); ctx.fillStyle = "#fff"; ctx.fill();
        ctx.strokeStyle = color; ctx.stroke();
      });
    });
  }

  function hitTest(cx, cy) {
    for (let p = 0; p < polys.length; p++)
      for (let v = 0; v < polys[p].length; v++) {
        const [x, y] = toCanvas(polys[p][v]);
        if (Math.hypot(x - cx, y - cy) <= HIT) return { p, v };
      }
    return null;
  }

  function onDown(e) {
    const r = cv.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    const hit = hitTest(cx, cy);
    if (e.button === 2) { // right-click: delete vertex
      if (hit) { polys[hit.p].splice(hit.v, 1); if (!polys[hit.p].length) polys.splice(hit.p, 1); draw(); }
      e.preventDefault(); return;
    }
    if (hit) { drag = hit; return; }
    // add vertex to the active polygon (last one; create if none)
    if (!polys.length) polys.push([]);
    if (layer === "roi" && polys.length > 1) polys = [polys[0]]; // ROI is single-polygon
    polys[polys.length - 1].push(toRef(cx, cy));
    draw();
  }
  function onMove(e) {
    if (!drag) return;
    const r = cv.getBoundingClientRect();
    polys[drag.p][drag.v] = toRef(e.clientX - r.left, e.clientY - r.top);
    draw();
  }
  function onUp() { drag = null; }

  function newPolygon() { if (layer === "ignore") { polys.push([]); draw(); } }
  function clearLayer() { polys = layer === "roi" ? [] : []; draw(); }

  async function save() {
    if (layer === "roi") {
      const poly = polys[0] || [];
      await saved(apiPut(`/api/admin/cameras/${cam.id}/roi`,
        { roi_polygon: poly, roi_drawn_w: refW, roi_drawn_h: refH }), "ROI saved");
      cam.roi_polygon = poly;
    } else {
      const zones = polys.filter(p => p.length >= 3);
      await saved(apiPut(`/api/admin/cameras/${cam.id}/ignore-zones`,
        { ignore_zones: zones, ignore_drawn_w: refW, ignore_drawn_h: refH }), "Ignore zones saved");
      cam.ignore_zones = zones;
    }
  }

  // cfg_cameras.reference_frame_url stores a BARE FILENAME now. Frames used to
  // be served by an unauthenticated static mount, so the column held a public
  // URL path; they are now behind an admin-guarded route. Older rows may still
  // hold the legacy "/reference_frames/<name>" form, so normalise both.
  function frameUrl(v) {
    if (!v) return null;
    const name = v.split("/").pop();
    return name ? `/api/admin/reference-frame/${name}` : null;
  }

  function attach(camera) {
    cam = camera;
    loadImage(frameUrl(cam.reference_frame_url));
    setLayer(layer);
  }

  function init() {
    cv = document.getElementById("roi_canvas");
    ctx = cv.getContext("2d");
    cv.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    cv.addEventListener("contextmenu", e => e.preventDefault());
  }

  return { init, attach, setLayer, newPolygon, clearLayer, save, reloadImage: (u) => loadImage(u) };
})();
