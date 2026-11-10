const API_PATHS = new Set([
  "/api/health", "/api/overview", "/api/positions", "/api/trades",
  "/api/decisions", "/api/consensus", "/api/markets",
  "/api/equity-curve", "/api/costs", "/api/candles", "/api/pool"
]);

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" }
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) {
      const response = await env.ASSETS.fetch(request);
      const headers = new Headers(response.headers);
      headers.set("cache-control", "no-store");
      return new Response(response.body, { status: response.status, headers });
    }
    if (request.method !== "GET") return json(405, { error: "method not allowed" });
    if (!API_PATHS.has(url.pathname)) return json(404, { error: "not found" });
    if (!env.VELTRIX_API_ORIGIN || !env.VELTRIX_DASHBOARD_TOKEN) {
      return json(503, { error: "API proxy is not configured" });
    }

    let origin;
    try {
      origin = new URL(env.VELTRIX_API_ORIGIN);
      if (origin.protocol !== "https:") throw new Error("HTTPS required");
    } catch {
      return json(503, { error: "API origin must be a valid HTTPS URL" });
    }

    const target = new URL(url.pathname + url.search, origin);
    const headers = new Headers({ authorization: "Bearer " + env.VELTRIX_DASHBOARD_TOKEN });
    const upstream = await fetch(target, {
      method: "GET",
      headers,
      redirect: "manual",
      cf: { cacheTtl: 0, cacheEverything: false }
    });
    if (upstream.status >= 300 && upstream.status < 400) {
      return json(502, { error: "API origin redirect rejected" });
    }
    const responseHeaders = new Headers(upstream.headers);
    responseHeaders.delete("set-cookie");
    responseHeaders.set("cache-control", "no-store");
    responseHeaders.set("x-content-type-options", "nosniff");
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  }
};
