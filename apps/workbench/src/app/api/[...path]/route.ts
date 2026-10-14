// Runtime proxy: /api/<path> -> the API gateway (ADCE_API_INTERNAL, read at request time, not at build time).
// Keeps the browser on one origin and lets the same image run locally, in staging and in client installs.

const TARGET = () => process.env.ADCE_API_INTERNAL ?? "http://localhost:8700";
const PASS = ["authorization", "content-type", "accept"];

async function forward(req: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  const url = new URL(req.url);
  const target = `${TARGET()}/${path.map(encodeURIComponent).join("/")}${url.search}`;
  const headers = new Headers();
  for (const h of PASS) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const init: RequestInit & { duplex?: string } = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    init.duplex = "half"; // stream uploads through without buffering
  }
  const res = await fetch(target, init);
  const out = new Headers();
  for (const h of ["content-type", "content-disposition"]) {
    const v = res.headers.get(h);
    if (v) out.set(h, v);
  }
  return new Response(res.body, { status: res.status, headers: out });
}

export const GET = forward;
export const POST = forward;
export const dynamic = "force-dynamic";
