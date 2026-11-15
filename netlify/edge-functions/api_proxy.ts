import type { Config, Context } from "@netlify/edge-functions";

/**
 * Relentless API Proxy Edge Function
 *
 * Defense-in-depth proxy between the Netlify-hosted Obsidian Command Center UI
 * and the Python backend (Railway or self-hosted tunnel).
 *
 * Environment Variables (set in Netlify with Functions/Runtime scope):
 * - RELENTLESS_BACKEND_URL: Base URL of the Python Web API (defaults to live Railway backend)
 * - WEB_API_KEY: Shared secret matching WEB_API_KEY / WEB_ADMIN_PASSWORD on the Python backend
 *
 * Optional:
 * - REQUIRE_NETLIFY_USER: Set to "true" to enforce strict context.user check inside the edge proxy
 */
export default async (req: Request, context: Context): Promise<Response> => {
  const url = new URL(req.url);
  const isLocalDev =
    url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]";
  const requireNetlifyUser = Netlify.env.get("REQUIRE_NETLIFY_USER") === "true";

  if (requireNetlifyUser && !isLocalDev && !context.user) {
    return new Response(
      JSON.stringify({
        ok: false,
        error: "NETLIFY_AUTH_REQUIRED",
        message: "Netlify Private Project authentication required.",
      }),
      {
        status: 401,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }

  const envBackend = (Netlify.env.get("RELENTLESS_BACKEND_URL") || "")
    .trim()
    .replace(/^['"]+|['"]+$/g, "")
    .trim();
  if (!envBackend) {
    return new Response(
      JSON.stringify({
        ok: false,
        error: "RELENTLESS_BACKEND_URL_MISSING",
        message:
          "RELENTLESS_BACKEND_URL is not configured in Netlify Environment Variables. Set it to your backend URL (e.g. https://your-app.up.railway.app).",
      }),
      {
        status: 500,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }
  let rawBackend = envBackend.replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(rawBackend)) {
    const scheme = /^(localhost|127\.0\.0\.1)(:|\/|$)/i.test(rawBackend) ? "http://" : "https://";
    rawBackend = `${scheme}${rawBackend}`;
  }

  let backendOrigin: string;
  try {
    backendOrigin = new URL(rawBackend).origin;
  } catch {
    return new Response(
      JSON.stringify({
        ok: false,
        error: "INVALID_BACKEND_URL",
        message: `RELENTLESS_BACKEND_URL is not a valid URL: '${envBackend || rawBackend}'`,
      }),
      {
        status: 500,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }

  const targetUrl = `${backendOrigin}${url.pathname}${url.search}`;
  const proxyHeaders = new Headers(req.headers);
  proxyHeaders.delete("host");
  proxyHeaders.delete("connection");

  const webApiKey = (Netlify.env.get("WEB_API_KEY") || "").trim();
  if (webApiKey && !proxyHeaders.has("x-relentless-api-key")) {
    proxyHeaders.set("X-Relentless-Api-Key", webApiKey);
  }

  if (context.user) {
    proxyHeaders.set("X-Netlify-User-Id", context.user.id);
    if (context.user.email) {
      proxyHeaders.set("X-Netlify-User-Email", context.user.email);
    }
  }

  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  try {
    const upstream = await fetch(targetUrl, {
      method: req.method,
      headers: proxyHeaders,
      body: hasBody ? req.body : undefined,
      redirect: "manual",
    });

    const respHeaders = new Headers(upstream.headers);
    respHeaders.set("cache-control", "no-store");

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: respHeaders,
    });
  } catch (err) {
    return new Response(
      JSON.stringify({
        ok: false,
        error: "UPSTREAM_UNREACHABLE",
        message: err instanceof Error ? err.message : "Failed to reach Relentless backend",
      }),
      {
        status: 502,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }
};

export const config: Config = {
  path: "/api/*",
};
