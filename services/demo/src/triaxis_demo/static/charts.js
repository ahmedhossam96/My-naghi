/* Minimal SVG chart helpers (no external libraries; the demo runs offline). */
"use strict";

(function () {
  const NS = "http://www.w3.org/2000/svg";

  function svg(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
    return node;
  }

  /** Donut chart. segments: [{label, value, color}] */
  function donut(segments, opts) {
    const size = (opts && opts.size) || 112;
    const stroke = (opts && opts.stroke) || 16;
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    const total = segments.reduce((s, x) => s + x.value, 0);
    const root = svg("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}`, role: "img" });
    const title = svg("title");
    title.textContent = segments.map((s) => `${s.label}: ${s.value}`).join(", ");
    root.appendChild(title);
    root.appendChild(svg("circle", {
      cx: size / 2, cy: size / 2, r, fill: "none", stroke: "#eaf1fc", "stroke-width": stroke,
    }));
    let offset = 0;
    for (const s of segments) {
      if (!s.value) continue;
      const len = (s.value / total) * c;
      root.appendChild(svg("circle", {
        cx: size / 2, cy: size / 2, r, fill: "none", stroke: s.color, "stroke-width": stroke,
        "stroke-dasharray": `${len} ${c - len}`, "stroke-dashoffset": -offset,
        transform: `rotate(-90 ${size / 2} ${size / 2})`,
      }));
      offset += len;
    }
    const label = svg("text", {
      x: "50%", y: "50%", "text-anchor": "middle", "dominant-baseline": "central",
      "font-size": 22, "font-weight": "bold", fill: "#033495", "font-family": "Helvetica, Arial, sans-serif",
    });
    label.textContent = String((opts && opts.center) !== undefined ? opts.center : total);
    root.appendChild(label);
    return root;
  }

  /** Horizontal stacked bar as a div. segments: [{label, value, color}] */
  function stack(segments) {
    const total = segments.reduce((s, x) => s + x.value, 0) || 1;
    const bar = document.createElement("div");
    bar.className = "stack";
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", segments.map((s) => `${s.label}: ${s.value}`).join(", "));
    for (const s of segments) {
      const part = document.createElement("span");
      part.style.width = `${(s.value / total) * 100}%`;
      part.style.background = s.color;
      part.title = `${s.label}: ${s.value}`;
      bar.appendChild(part);
    }
    return bar;
  }

  window.TriaxisCharts = { donut, stack };
})();
