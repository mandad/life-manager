/**
 * OneNote embedded raster images (screenshots, pasted pictures) → vision-OCR pipeline.
 *
 * OneNote does OCR images server-side, but that recognized text is NOT returned in the
 * page HTML (img tags carry only id/width/height/src/data-*-src/-type — no alt, no OCR
 * text) and `$search` is rejected on consumer Graph, so the OCR text is unreachable via
 * any page query. The only way to read a screenshot's text is to fetch the image binary
 * and OCR it with vision — this module extracts the image refs and downscales big PNGs so
 * the base64 payload stays within Claude's ~1568px vision sweet spot.
 */

import { createCanvas, loadImage } from "@napi-rs/canvas";

export interface PageImage {
  ord: number;
  url: string; // Graph resources/{id}/$value
  resourceId: string;
  mime: string;
  width: number | null; // declared display dims from the <img> tag (may be fractional)
  height: number | null;
}

/** Parse every <img> tag out of OneNote page HTML, newest-first in document order. */
export function extractImages(html: string): PageImage[] {
  const out: PageImage[] = [];
  const tags = html.match(/<img\b[^>]*>/gi) || [];
  let ord = 0;
  for (const tag of tags) {
    const attr = (n: string) => (tag.match(new RegExp(`\\b${n}="([^"]*)"`, "i")) || [])[1];
    // fullres is the original paste; src is a (sometimes resized) render — prefer fullres.
    const url = attr("data-fullres-src") || attr("src");
    if (!url) continue;
    const mime = attr("data-fullres-src-type") || attr("data-src-type") || "image/png";
    const rid = (url.match(/resources\/([^/]+)\//) || [])[1] || "";
    const w = parseFloat(attr("width") || "");
    const h = parseFloat(attr("height") || "");
    out.push({
      ord: ord++,
      url,
      resourceId: rid,
      mime,
      width: Number.isFinite(w) ? w : null,
      height: Number.isFinite(h) ? h : null,
    });
  }
  return out;
}

export interface PreparedImage {
  buf: Buffer;
  mime: string;
  width: number;
  height: number;
  downscaled: boolean;
}

/**
 * Decode an image buffer; if wider than maxWidth, downscale (preserves aspect) and re-encode
 * to PNG. Otherwise return the original bytes untouched (no quality loss). Non-decodable
 * buffers (rare/odd formats) are passed through as-is with unknown dims.
 */
export async function prepareImage(
  buf: Buffer,
  mime: string,
  maxWidth = 1568,
): Promise<PreparedImage> {
  try {
    const img = await loadImage(buf);
    const w = img.width;
    const h = img.height;
    if (w <= maxWidth) {
      return { buf, mime, width: w, height: h, downscaled: false };
    }
    const scale = maxWidth / w;
    const nw = Math.round(w * scale);
    const nh = Math.round(h * scale);
    const canvas = createCanvas(nw, nh);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, nw, nh);
    return { buf: canvas.toBuffer("image/png"), mime: "image/png", width: nw, height: nh, downscaled: true };
  } catch {
    return { buf, mime, width: 0, height: 0, downscaled: false };
  }
}
