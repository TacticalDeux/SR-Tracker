// Report proxy: public front door for bug reports.
//
// The app ships no secret. It POSTs either plain report JSON (recent
// activity / comment-only) or one multipart call (metadata JSON part
// plus the joined session file) here, and this worker writes one row
// to its bound database. Whole-file bytes go to a private file host
// under an unguessable key; the row keeps the capped excerpt plus
// that key. Replies never echo store detail; callers only ever see
// { ok, error? } with generic text, so nothing can leak through this
// front.

// Incoming field caps. Anything outside them is rejected unread.
// The log cap covers the ~1MB client slice with headroom; values
// arrive bound, so the statement itself stays tiny either way.
const MAX_COMMENT = 5000; // chars
const MAX_LOG = 1000 * 1024; // chars, best effort
const MAX_VERSION = 64; // chars

// File part ceiling: every part a session may hold, with headroom.
const MAX_FILE = 40 * 1024 * 1024; // bytes

// One send per caller address per window.
const RATE_WINDOW_MS = 5 * 60 * 1000;

// Best-effort per-address guard. Isolates restart fresh (which only
// loosens the limit); promote to KV when abuse needs a shared view.
const recentSends = new Map();

function reply(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function rejected() {
  return reply({ ok: false, error: "The report was rejected." }, 400);
}

function validMetadata(body) {
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return false;
  }
  const { comment, log, session_id, app_version, event_count } = body;
  if (typeof comment !== "string" || comment.length > MAX_COMMENT) {
    return false;
  }
  if (typeof log !== "string" || log.length > MAX_LOG) {
    return false;
  }
  if (!Number.isInteger(session_id) || session_id < 0) {
    return false;
  }
  if (
    typeof app_version !== "string" ||
    app_version.length === 0 ||
    app_version.length > MAX_VERSION
  ) {
    return false;
  }
  if (!Number.isInteger(event_count) || event_count < 0) {
    return false;
  }
  return true;
}

// Split one multipart body into its named parts. Manual parse, no
// deps: cut on the boundary, then split each section into headers
// and bytes at the blank line. Returns { metadata, file } as raw
// strings (logs are text; the caller re-encodes as needed).
function parseMultipart(raw, boundary) {
  let metadata = null;
  let file = null;
  const sections = raw.split(`--${boundary}`);
  for (let section of sections) {
    section = section.replace(/^\r\n/, "");
    if (!section || section.startsWith("--") || !section.includes("\r\n\r\n")) {
      continue;
    }
    const cut = section.indexOf("\r\n\r\n");
    const headers = section.slice(0, cut);
    let content = section.slice(cut + 4);
    if (content.endsWith("\r\n")) content = content.slice(0, -2);
    const match = /name="([^"]+)"/.exec(headers);
    if (!match) continue;
    if (match[1] === "metadata") metadata = content;
    else if (match[1] === "file") file = content;
  }
  return { metadata, file };
}

function throttled(addr, now) {
  if (
    recentSends.get(addr) !== undefined &&
    now - recentSends.get(addr) < RATE_WINDOW_MS
  ) {
    return true;
  }
  recentSends.set(addr, now);
  if (recentSends.size > 2000) {
    for (const [key, seen] of recentSends) {
      if (now - seen >= RATE_WINDOW_MS) recentSends.delete(key);
    }
  }
  return false;
}

async function storeIssue(env, fields, logKey) {
  const { app_version, comment, log, session_id, event_count } = fields;
  // Newer schema carries the file key; older ones predate the column,
  // so fall back to the keyless insert rather than failing the send.
  try {
    await env.DB.prepare(
      "INSERT INTO issues (app_version, comment, log, session_id, event_count, log_url) VALUES (?, ?, ?, ?, ?, ?)",
    )
      .bind(app_version, comment, log, session_id, event_count, logKey)
      .run();
  } catch {
    await env.DB.prepare(
      "INSERT INTO issues (app_version, comment, log, session_id, event_count) VALUES (?, ?, ?, ?, ?)",
    )
      .bind(app_version, comment, log, session_id, event_count)
      .run();
  }
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return reply({ ok: false, error: "Not found." }, 405);
    }

    const contentType = request.headers.get("Content-Type") || "";
    if (contentType.startsWith("multipart/form-data")) {
      const match = /boundary=([^\s;]+)/.exec(contentType);
      if (!match) return rejected();
      let raw;
      try {
        raw = await request.text();
      } catch {
        return reply({ ok: false, error: "Could not read the report." }, 400);
      }
      if (raw.length > MAX_FILE + MAX_LOG + 65536) return rejected();
      const { metadata, file } = parseMultipart(raw, match[1]);
      if (metadata === null || file === null) return rejected();
      let body;
      try {
        body = JSON.parse(metadata);
      } catch {
        return reply({ ok: false, error: "Could not read the report." }, 400);
      }
      if (!validMetadata(body)) return rejected();
      const fileBytes = new TextEncoder().encode(file);
      if (fileBytes.length === 0 || fileBytes.length > MAX_FILE) {
        return rejected();
      }

      // Stamp before forwarding so a failing store cannot be hammered
      // through retries.
      const addr = request.headers.get("CF-Connecting-IP") || "unknown";
      const now = Date.now();
      if (throttled(addr, now)) {
        return reply(
          { ok: false, error: "Please wait before sending again." },
          429,
        );
      }

      // One row per accepted report. Created_at is a server-side
      // default; any store failure takes the generic path below.
      // The file lives in a private host under an unguessable key;
      // the row keeps the key so the dev can fetch it later.
      try {
        const key = `reports/${crypto.randomUUID()}.log`;
        await env.LOGS.put(key, fileBytes);
        await storeIssue(env, body, key);
      } catch {
        return reply(
          { ok: false, error: "The report store is unreachable." },
          502,
        );
      }
      return reply({ ok: true });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return reply({ ok: false, error: "Could not read the report." }, 400);
    }
    if (!validMetadata(body)) {
      return rejected();
    }

    // Stamp before forwarding so a failing store cannot be hammered
    // through retries.
    const addr = request.headers.get("CF-Connecting-IP") || "unknown";
    const now = Date.now();
    if (throttled(addr, now)) {
      return reply(
        { ok: false, error: "Please wait before sending again." },
        429,
      );
    }

    // One row per accepted report. Created_at is a server-side
    // default; any store failure takes the generic path below.
    try {
      await storeIssue(env, body, null);
    } catch {
      return reply(
        { ok: false, error: "The report store is unreachable." },
        502,
      );
    }
    return reply({ ok: true });
  },
};
