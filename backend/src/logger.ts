// Structured JSON logger. See guidelines.md §1 for the ECS field schema.

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
  messageKey: 'message',
  // ECS expects `@timestamp`, not pino's default `time` key.
  timestamp: () => `,"@timestamp":"${new Date().toISOString()}"`,
  formatters: {
    level(label) {
      return { 'log.level': label };
    },
    bindings() {
      return {
        service: { name: SERVICE_NAME, version: SERVICE_VERSION, environment: SERVICE_ENV },
      };
    },
  },
  base: undefined, // drop default {pid, hostname}; bindings() handles service.*
});

// pino-http's stock serializers nest fields under req.*/res.* which isn't
// searchable as ECS. We suppress them and promote each field to root via
// customProps below — so Kibana queries look like `url.path:"/api/payment"`,
// not `req.url.path:"/..."`.
export const httpLogger = pinoHttp({
  logger,
  genReqId: (req: IncomingMessage) => {
    const headerId = req.headers['x-request-id'];
    return typeof headerId === 'string' ? headerId : randomUUID();
  },
  customLogLevel: (_req: IncomingMessage, res: ServerResponse, err?: Error) => {
    if (err || res.statusCode >= 500) return 'error';
    if (res.statusCode >= 400) return 'warn';
    return 'info';
  },
  customSuccessMessage: () => 'request completed',
  customErrorMessage: () => 'request failed',
  serializers: {
    req: () => undefined,
    res: () => undefined,
    err: pino.stdSerializers.err,
  },
  customProps: (req, res) => {
    const r = req as unknown as {
      id?: string | number;
      method?: string;
      url?: string;
      // originalUrl is the full Express path (includes the router prefix);
      // req.url is router-local, so /login instead of /api/auth/login.
      originalUrl?: string;
      user?: { id?: number };
    };
    const s = res as unknown as { statusCode: number; responseTime?: number };
    const ms = s.responseTime ?? 0;
    return {
      http: {
        request: { method: r.method },
        response: { status_code: s.statusCode },
      },
      url: { path: stripQuery(r.originalUrl ?? r.url) },
      trace: { id: r.id !== undefined ? String(r.id) : undefined },
      ...(r.user?.id !== undefined ? { user: { id: String(r.user.id) } } : {}),
      event: {
        duration: Math.round(ms * 1_000_000), // ECS event.duration is nanoseconds
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
