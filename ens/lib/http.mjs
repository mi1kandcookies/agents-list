// node:http request handler. Every route requires X-Sidecar-Token.
import { timingSafeEqual } from 'node:crypto';

import { SidecarError } from './errors.mjs';

const MAX_BODY_BYTES = 64 * 1024;

function tokenMatches(given, expected) {
  const a = Buffer.from(String(given || ''));
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

async function readJson(req) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new SidecarError(413, 'BODY_TOO_LARGE', 'request body too large');
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  try {
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (body === null || typeof body !== 'object' || Array.isArray(body)) throw new Error('not an object');
    return body;
  } catch {
    throw new SidecarError(400, 'INVALID_JSON', 'body must be a JSON object');
  }
}

function send(res, status, payload) {
  const body = JSON.stringify(payload, (_k, v) => (typeof v === 'bigint' ? v.toString() : v));
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(body);
}

export function createHandler(service, { token, log = console } = {}) {
  if (!token) throw new Error('ENS_SIDECAR_TOKEN must be set');
  const routes = {
    'GET /health': async () => service.health(),
    'GET /names/tree': async (_req, url) => service.tree({
      root: url.searchParams.get('root') || undefined,
      live: url.searchParams.get('live') === '1',
    }),
    'POST /names/root/setup': async (req) => service.setupRoot(await readJson(req)),
    'POST /names/agent': async (req) => service.createAgent(await readJson(req)),
    'POST /names/job': async (req) => service.createJob(await readJson(req)),
    'POST /names/subjob': async (req) => service.createSubjob(await readJson(req)),
    'POST /names/revoke': async (req) => service.revoke(await readJson(req)),
  };

  return async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    try {
      if (!tokenMatches(req.headers['x-sidecar-token'], token)) {
        throw new SidecarError(401, 'UNAUTHORIZED', 'missing or wrong X-Sidecar-Token');
      }
      const route = routes[`${req.method} ${url.pathname}`];
      if (!route) throw new SidecarError(404, 'NOT_FOUND', `no route for ${req.method} ${url.pathname}`);
      const result = await route(req, url);
      send(res, 200, result);
    } catch (err) {
      if (err instanceof SidecarError) {
        send(res, err.status, { error: err.message, code: err.code });
      } else {
        // Chain/RPC failures: report the short reason, never the stack.
        log.error?.(`[names] ${req.method} ${url.pathname} failed:`, err);
        send(res, 502, { error: err.shortMessage || err.message || 'chain call failed', code: 'CHAIN_ERROR' });
      }
    }
  };
}
