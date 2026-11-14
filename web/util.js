// DOM helper, formatters, labels and JWS verification. No dependencies.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "html") el.innerHTML = v;
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c == null || c === false || c === "") continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
}

// ------------------------------------------------------------------ formatting
const TZ = "Europe/Helsinki";
const eurFmt = new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR" });
const numFmt = new Intl.NumberFormat("en-IE", { maximumFractionDigits: 2 });

export const eur = (n) => (n == null || isNaN(n) ? "–" : eurFmt.format(n));
export const num = (n, d = 2) =>
  n == null || isNaN(n) ? "–" : new Intl.NumberFormat("en-IE", { maximumFractionDigits: d }).format(n);
export const pct = (x) => (x == null ? "no data" : `${numFmt.format(Math.round(x * 1000) / 10)}%`);

const dt = (opts) => new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour12: false, ...opts });
const fTime = dt({ hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fHm = dt({ hour: "2-digit", minute: "2-digit" });
const fDay = dt({ weekday: "short", day: "numeric", month: "short" });
const fFull = dt({ weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

const toDate = (v) => (v instanceof Date ? v : new Date(v));
export const time = (v) => (v ? fTime.format(toDate(v)) : "");
export const hm = (v) => (v ? fHm.format(toDate(v)) : "");
// Dates like "2026-11-16" are calendar dates, not instants: format without TZ shift.
export const day = (v) => {
  if (!v) return "";
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) {
    const [y, m, d] = v.split("-").map(Number);
    return new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" }).format(new Date(Date.UTC(y, m - 1, d)));
  }
  return fDay.format(toDate(v));
};
export const dayTime = (v) => (v ? `${fDay.format(toDate(v))} ${fHm.format(toDate(v))}` : "");
export const full = (v) => (v ? fFull.format(toDate(v)) : "");

export function countdown(expiresAt) {
  const ms = toDate(expiresAt) - Date.now();
  if (ms <= 0) return { text: "expired", cls: "gone" };
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return { text: `${m}m ${String(s % 60).padStart(2, "0")}s`, cls: m < 3 ? "low" : "" };
}

export const shortHash = (h, n = 12) => (h ? String(h).replace(/^sha256:/, "").slice(0, n) : "–");
export function groupedHash(h, groups = 4) {
  const s = shortHash(h, groups * 4);
  return Array.from({ length: Math.ceil(s.length / 4) }, (_, i) => s.slice(i * 4, i * 4 + 4));
}

export function relTime(v) {
  const s = Math.round((Date.now() - toDate(v)) / 1000);
  if (s < 60) return `${Math.max(s, 0)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return day(v);
}

// ------------------------------------------------------------------ labels
const CLAIMS = {
  FREE_FROM_LACTOSE: "Lactose-free", LACTOSE_FREE: "Lactose-free", LOW_LACTOSE: "Low lactose",
  FREE_FROM_GLUTEN: "Gluten-free", GLUTEN_FREE: "Gluten-free", FREE_FROM_MILK: "Milk-free",
  ORGANIC: "Organic", VEGAN: "Vegan", VEGETARIAN: "Vegetarian",
};
export const claimLabel = (c) => CLAIMS[c] || titleCase(String(c).replace(/^FREE_FROM_/, "free from "));

export function titleCase(s) {
  const t = String(s).replace(/[_-]+/g, " ").toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export const roleLabel = (r) => (r ? titleCase(r).replace(/\b\w/g, (m) => m.toUpperCase()) : "");

// Flags and exception codes: label + severity. Unknown codes fall back to neutral.
const FLAGS = {
  cutoff_passed: ["Order cut-off passed", "bad"],
  partial_stock: ["Partial stock", "warn"],
  out_of_stock: ["Out of stock", "bad"],
  allergen_unknown: ["Allergen data missing", "warn"],
  allergen_contains: ["Contains excluded allergen", "bad"],
  contains_allergen: ["Contains excluded allergen", "bad"],
  allergen_may_contain: ["May contain excluded allergen", "bad"],
  outside_preference: ["Outside standing preference", "warn"],
  pack_substitution: ["Pack change", "warn"],
  brand_substitution: ["Brand change", "warn"],
  spec_substitution: ["Spec change", "warn"],
  injection_flagged: ["Prompt injection flagged", "bad"],
  injection_suspected: ["Prompt injection flagged", "bad"],
  suspicious_text: ["Prompt injection flagged", "bad"],
  sales_block: ["Sales block", "bad"],
  price_error: ["Price unavailable", "bad"],
  missing_claim: ["Required claim missing", "bad"],
  reliability_unknown: ["No delivery reliability data", "neutral"],
  volume_anomaly: ["Unusual volume", "warn"],
  retry: ["Retried after API error", "warn"],
  rate_limited: ["Rate limited, retried", "warn"],
  terminal: ["Terminal item", "neutral"],
};
export function flagInfo(code) {
  const k = String(code).toLowerCase();
  if (FLAGS[k]) return { label: FLAGS[k][0], sev: FLAGS[k][1] };
  if (k.startsWith("contains_")) return { label: `Contains ${k.slice(9)}`, sev: "bad" };
  if (k.includes("injection")) return { label: "Prompt injection flagged", sev: "bad" };
  return { label: titleCase(k), sev: "neutral" };
}
export const flagEl = (code) => {
  const f = flagInfo(code);
  return h("span", { class: `flag ${f.sev}` }, f.label);
};

export const TIER_LABEL = { preferred: "Preferred", best: "Best", better: "Better", good: "Good", rejected: "Rejected" };
export const TIER_HINT = {
  preferred: "Your usual item",
  best: "Same brand and spec, different pack",
  better: "Same spec, another brand",
  good: "Meets the hard constraints only",
  rejected: "Not usable for this order",
};

export const VERDICT = {
  auto_approve: { title: "Auto-approved", icon: "✓" },
  needs_approval: { title: "Needs approval", icon: "!" },
  reject: { title: "Rejected by policy", icon: "×" },
};

export function actorKind(actor) {
  const a = String(actor || "");
  if (a.startsWith("human")) return "human";
  if (a.startsWith("agent")) return "agent";
  return "system";
}

export const pretty = (o) => JSON.stringify(o, null, 2);

export function download(filename, text, type = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h("a", { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// ------------------------------------------------------------------ crypto (JWS EdDSA)
export function b64urlToBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}
export function bytesToB64url(bytes) {
  let bin = "";
  new Uint8Array(bytes).forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
export const strToB64url = (s) => bytesToB64url(new TextEncoder().encode(s));

export async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Verify a compact JWS (EdDSA / Ed25519) against a JWKS and check that it covers `expected`.
 *  Returns {state: "verified"|"invalid"|"unverifiable", detail}. */
export async function verifyJws(jws, jwks, expected) {
  try {
    if (!jws || typeof jws !== "string") return { state: "unverifiable", detail: "no signature" };
    const parts = jws.split(".");
    if (parts.length !== 3) return { state: "unverifiable", detail: "not a compact JWS" };
    const [hdrB64, payloadB64, sigB64] = parts;
    const header = JSON.parse(new TextDecoder().decode(b64urlToBytes(hdrB64)));
    const keys = (jwks && jwks.keys) || [];
    const jwk = keys.find((k) => !header.kid || k.kid === header.kid) || keys[0];
    if (!jwk) return { state: "unverifiable", detail: "no public key in JWKS" };
    let key;
    try {
      key = await crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", x: jwk.x }, { name: "Ed25519" }, false, ["verify"]);
    } catch {
      return { state: "unverifiable", detail: "browser lacks Ed25519 WebCrypto" };
    }
    // Detached payload (header..sig): the payload is the expected value itself.
    const pB64 = payloadB64 || strToB64url(expected || "");
    const ok = await crypto.subtle.verify({ name: "Ed25519" }, key, b64urlToBytes(sigB64), new TextEncoder().encode(`${hdrB64}.${pB64}`));
    if (!ok) return { state: "invalid", detail: "signature does not match key " + (jwk.kid || "") };
    if (expected) {
      const payload = new TextDecoder().decode(b64urlToBytes(pB64));
      const bare = String(expected).replace(/^sha256:/, "");
      if (!payload.includes(bare)) return { state: "invalid", detail: "signature is valid but covers a different hash" };
    }
    return { state: "verified", detail: `EdDSA · kid ${jwk.kid || header.kid || "?"}` };
  } catch (e) {
    return { state: "unverifiable", detail: String(e.message || e) };
  }
}
