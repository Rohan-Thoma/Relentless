import type { Config, Context } from "@netlify/edge-functions";

/**
 * Relentless API Proxy Edge Function
 *
 * Defense-in-depth proxy between the Netlify-hosted Obsidian Cyber-HUD UI
 * and the Python backend (Railway or self-hosted tunnel).
 *
 * Required Netlify Environment Variables:
 * - RELENTLESS_BACKEND_URL: Base URL of the Python Web API (e.g. https://relentless.up.railway.app)
 * - WEB_API_KEY: Shared secret matching WEB_API_KEY / WEB_ADMIN_PASSWORD on the Python backend
 *
 * Optional:
 * - REQUIRE_NETLIFY_USER: Set to "false" only if running a public site without Netlify Private Project auth
 */
export default async (req: Request, context: Context): Promise<Response> => {
  const url = new URL(req.url);
  const isLocalDev =
    url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]";
  const requireNetlifyUser = Netlify.env.get("REQUIRE_NETLIFY_USER") !== "false";

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

  const rawBackend = (Netlify.env.get("RELENTLESS_BACKEND_URL") || "").trim().replace(/\/+$/, "");
  if (!rawBackend) {
    return new Response(
      JSON.stringify({
        ok: false,
        error: "BACKEND_URL_NOT_CONFIGURED",
        message: "RELENTLESS_BACKEND_URL is not configured in Netlify environment variables.",
      }),
      {
        status: 503,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }

  const targetUrl = `${rawBackend}${url.pathname}${url.search}`;
  const proxyHeaders = new Headers(req.headers);
  proxyHeaders.delete("host");
  proxyHeaders.delete("connection");

  const webApiKey = (Netlify.env.get("WEB_API_KEY") || "").trim();
  if (webApiKey) {
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
