// Structured JSON logger — see guidelines.md §1 and metric-catalog.md "Log fields".
//
// Conventions:
//   - ECS 9.x field paths: @timestamp, log.level, message, service.{name,version,environment},
//     http.request.method, url.path, http.response.status_code, event.{duration,outcome},
//     trace.id, user.id, and our custom ecom.* namespace.
//   - One JSON record per line, written to stdout. Filebeat tails the container.
//   - pino's defaults (`level` as integer, `time` as ms epoch) don't fit ECS; we override.

import pino from 'pino';
import pinoHttp from 'pino-http';
import { randomUUID } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';
import pkg from '../package.json' with { type: 'json' };

const SERVICE_NAME = 'shop-backend';
const SERVICE_VERSION = (pkg as { version?: string }).version ?? '0.0.0';
const SERVICE_ENV = process.env['NODE_ENV'] ?? 'development';

export const logger = pino({
  level: process.env['LOG_LEVEL'] ?? 'info',
  // ECS-aligned message key
  messageKey: 'message',
  // ECS uses `@timestamp` (date type). pino's default key is `time` — override
  // so Elasticsearch + Kibana auto-detect the time field with no field-mapping
  // gymnastics in Filebeat.
  timestamp: () => `,"@timestamp":"${new Date().toISOString()}"`,
  // Re-key pino's `level` integer into ECS `log.level` string, and rename
  // pino's `time` to `@timestamp` so Filebeat doesn't need a remap.
  formatters: {
    level(label) {
      return { 'log.level': label };
    },
    // Re-shape default bindings into ECS service.* — these are merged into
    // every log record. We don't include pid/hostname (suppressed via
    // `base: undefined` below) to keep records tight; container.id from
    // Filebeat already identifies the producer.
    bindings() {
      return {
        service: {
          name: SERVICE_NAME,
          version: SERVICE_VERSION,
          environment: SERVICE_ENV,
        },
      };
    },
    // We don't want pino-http's nested {req,res,...} dump in production logs;
    // we'll shape it explicitly via the serializers below.
  },
  base: undefined, // suppress default {pid, hostname} — bindings() handles it
});

// ─── pino-http middleware ───────────────────────────────────────────────────────

// Custom serializers shape pino-http's defaults into ECS-flat fields.
// pino-http's stock serializers nest under `req`/`res`/`responseTime` which is
// neither searchable nor consistent with ECS. We replace them.

export const httpLogger = pinoHttp({
  logger,
  // Random per-request id — propagated through req.log.* calls so every event
  // emitted inside a request handler carries the same trace.id.
  genReqId: (req: IncomingMessage) => {
    const headerId = req.headers['x-request-id'];
    return typeof headerId === 'string' ? headerId : randomUUID();
  },
  // Promote 4xx -> warn, 5xx -> error. Auto-emits the right log.level for
  // alerting and Kibana's level filter.
  customLogLevel: (_req: IncomingMessage, res: ServerResponse, err?: Error) => {
    if (err || res.statusCode >= 500) return 'error';
    if (res.statusCode >= 400) return 'warn';
    return 'info';
  },
  // The per-request response message. Short, present-tense, no values inline.
  customSuccessMessage: () => 'request completed',
  customErrorMessage: () => 'request failed',
  // Drop pino-http's nested {req, res, responseTime} serializers entirely —
  // we promote everything to ECS root via customProps below so Kibana queries
  // are `url.path:"/api/payment"` not `req.url.path:"..."`.
  serializers: {
    req: () => undefined,
    res: () => undefined,
    err: pino.stdSerializers.err,
  },
  // customProps runs per request and merges into the root of the log record.
  // This is where we shape ECS top-level fields: http.*, url.*, trace.id,
  // user.id, event.duration, event.outcome.
  customProps: (req, res) => {
    // pino-http's types differ from Express's; we read the few fields we need
    // off `any` because the structural shape is stable.
    const r = req as unknown as {
      id?: string | number;
      method?: string;
      url?: string;
      originalUrl?: string; // Express's full request path (includes router prefix)
      user?: { id?: number };
    };
    const s = res as unknown as { statusCode: number; responseTime?: number };
    const ms = s.responseTime ?? 0;
    return {
      http: {
        request: { method: r.method },
        response: { status_code: s.statusCode },
      },
      // Prefer originalUrl (full path) over url (router-local). The metrics
      // middleware uses Express templates; logs use resolved paths so an LLM
      // can see exactly which resource was touched.
      url: { path: stripQuery(r.originalUrl ?? r.url) },
      trace: { id: r.id !== undefined ? String(r.id) : undefined },
      ...(r.user?.id !== undefined ? { user: { id: String(r.user.id) } } : {}),
      event: {
        duration: Math.round(ms * 1_000_000), // ms -> ns per ECS
        outcome: s.statusCode >= 400 ? 'failure' : 'success',
      },
    };
  },
});

function stripQuery(url?: string): string | undefined {
  if (!url) return undefined;
  const qi = url.indexOf('?');
  return qi >= 0 ? url.slice(0, qi) : url;
}
