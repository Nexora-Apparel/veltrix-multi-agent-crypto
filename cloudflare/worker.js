const API_PATHS = new Set([
  "/api/health", "/api/overview", "/api/positions", "/api/trades", "/api/sol-paper-wallet",
  "/api/decisions", "/api/consensus", "/api/markets",
  "/api/equity-curve", "/api/costs", "/api/candles", "/api/pool"
]);

function secureHeaders(source) {
  const headers = new Headers(source || {});
  headers.set("cache-control", "no-store");
  headers.set("x-content-type-options", "nosniff");
  headers.set("x-frame-options", "DENY");
  headers.set("referrer-policy", "no-referrer");
  headers.set("permissions-policy", "camera=(), microphone=(), geolocation=()");
  headers.set("content-security-policy",
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
    "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; " +
    "base-uri 'self'; frame-ancestors 'none'");
  headers.set("strict-transport-security", "max-age=31536000; includeSubDomains");
  return headers;
}

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: secureHeaders({ "content-type": "application/json; charset=utf-8" })
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) {
      const response = await env.ASSETS.fetch(request);
      return new Response(response.body, {
        status: response.status,
        headers: secureHeaders(response.headers)
      });
    }
    if (request.method !== "GET") return json(405, { error: "method not allowed" });
    if (!API_PATHS.has(url.pathname)) return json(404, { error: "not found" });
    if (!env.VELTRIX_API_ORIGIN || !env.VELTRIX_DASHBOARD_TOKEN) {
      return json(503, { error: "API proxy is not configured" });
    }
    if (
      typeof env.VELTRIX_DASHBOARD_TOKEN !== "string" ||
      env.VELTRIX_DASHBOARD_TOKEN.length < 32 ||
      env.VELTRIX_DASHBOARD_TOKEN.trim() !== env.VELTRIX_DASHBOARD_TOKEN
    ) {
      return json(503, { error: "API proxy authentication is not configured securely" });
    }

    let origin;
    try {
      origin = new URL(env.VELTRIX_API_ORIGIN);
      if (
        origin.protocol !== "https:" || origin.username || origin.password ||
        origin.pathname !== "/" || origin.search || origin.hash
      ) throw new Error("A bare HTTPS origin is required");
    } catch {
      return json(503, { error: "API origin must be a valid HTTPS URL" });
    }

    const target = new URL(url.pathname + url.search, origin);
    const headers = new Headers({ authorization: "Bearer " + env.VELTRIX_DASHBOARD_TOKEN });
    let upstream;
    try {
      upstream = await fetch(target, {
        method: "GET",
        headers,
        redirect: "manual",
        cf: { cacheTtl: 0, cacheEverything: false }
      });
    } catch {
      return json(502, { error: "API origin request failed" });
    }
    if (upstream.status >= 300 && upstream.status < 400) {
      return json(502, { error: "API origin redirect rejected" });
    }
    const responseHeaders = secureHeaders(upstream.headers);
    responseHeaders.delete("set-cookie");
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  }
};
