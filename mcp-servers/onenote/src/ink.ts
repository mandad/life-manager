/**
 * OneNote ink → PNG rasterizer.
 *
 * OneNote returns ink via Graph `GET /pages/{id}/content?includeinkML=true` as a
 * multipart/mixed body with an `application/inkml+xml` part. The InkML traceFormat is
 * channels X Y F (force), absolute coordinates, points comma-separated, channel values
 * space-separated. We take X,Y per point, scale the global bbox to a target width, and
 * render strokes to one or more PNG tiles (kept ≤ Claude's ~1568px vision sweet spot so
 * handwriting stays legible for OCR on tall pages).
 */

import { createCanvas } from "@napi-rs/canvas";

export interface Point {
  x: number;
  y: number;
}
export interface Stroke {
  pts: Point[];
}

/** Pull every application/inkml+xml part out of a multipart/mixed body. */
export function extractInkml(body: string, contentType: string): string[] {
  const m = contentType.match(/boundary=("?)([^";]+)\1/i);
  if (!m) return [];
  const parts = body.split("--" + m[2]);
  const out: string[] = [];
  for (const p of parts) {
    if (/content-type:\s*application\/inkml\+xml/i.test(p)) {
      out.push(p.replace(/^[\s\S]*?\r?\n\r?\n/, "").trim()); // drop part headers
    }
  }
  return out;
}

/** Parse <trace> elements into strokes (first two numeric channels = X, Y). */
export function parseTraces(xml: string): Stroke[] {
  const strokes: Stroke[] = [];
  const re = /<(?:inkml:)?trace\b[^>]*>([\s\S]*?)<\/(?:inkml:)?trace>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(xml))) {
    const pts: Point[] = [];
    for (const grp of m[1].split(",")) {
      const nums = grp.trim().match(/-?\d+(?:\.\d+)?/g);
      if (nums && nums.length >= 2) pts.push({ x: parseFloat(nums[0]), y: parseFloat(nums[1]) });
    }
    if (pts.length) strokes.push({ pts });
  }
  return strokes;
}

export interface RenderOpts {
  targetWidth?: number; // px the ink bbox width maps to
  maxTileHeight?: number; // px; tall pages split into vertical tiles
  overlap?: number; // px vertical overlap between tiles (so lines aren't cut)
  pad?: number; // px border
  lineWidth?: number; // px stroke width
}

export interface RenderResult {
  tiles: Buffer[]; // PNG buffers, top→bottom
  width: number;
  height: number; // full (untiled) height
  strokeCount: number;
}

export function renderStrokesToPng(strokes: Stroke[], opts: RenderOpts = {}): RenderResult {
  const targetWidth = opts.targetWidth ?? 1500;
  const maxTileHeight = opts.maxTileHeight ?? 1500;
  const overlap = opts.overlap ?? 120;
  const pad = opts.pad ?? 28;
  const lineWidth = opts.lineWidth ?? 2.2;

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const s of strokes)
    for (const p of s.pts) {
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    }
  if (!isFinite(minX)) return { tiles: [], width: 0, height: 0, strokeCount: 0 };

  const wUnits = maxX - minX || 1;
  const hUnits = maxY - minY || 1;
  const scale = (targetWidth - pad * 2) / wUnits;
  const fullW = targetWidth;
  const fullH = Math.ceil(hUnits * scale + pad * 2);
  const tx = (x: number) => (x - minX) * scale + pad;
  const ty = (y: number) => (y - minY) * scale + pad;

  const tiles: Buffer[] = [];
  const step = maxTileHeight - overlap;
  for (let y0 = 0; y0 < fullH; y0 += step) {
    const h = Math.min(maxTileHeight, fullH - y0);
    const canvas = createCanvas(fullW, h);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, fullW, h);
    ctx.strokeStyle = "#111111";
    ctx.lineWidth = lineWidth;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    const top = y0;
    const bot = y0 + h;
    for (const s of strokes) {
      // cheap cull: skip strokes whose Y is entirely outside this band
      let sMin = Infinity, sMax = -Infinity;
      for (const p of s.pts) {
        const Y = ty(p.y);
        if (Y < sMin) sMin = Y;
        if (Y > sMax) sMax = Y;
      }
      if (sMax < top - 2 || sMin > bot + 2) continue;
      ctx.beginPath();
      let started = false;
      for (const p of s.pts) {
        const X = tx(p.x);
        const Y = ty(p.y) - y0;
        if (!started) {
          ctx.moveTo(X, Y);
          started = true;
        } else ctx.lineTo(X, Y);
      }
      ctx.stroke();
    }
    tiles.push(canvas.toBuffer("image/png"));
    if (h < maxTileHeight) break;
  }
  return { tiles, width: fullW, height: fullH, strokeCount: strokes.length };
}
