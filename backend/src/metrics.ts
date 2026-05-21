// Prometheus metrics. See metric-catalog.md for what each one means.

import client from 'prom-client';
import type { Request, Response, NextFunction } from 'express';

export const register = new client.Registry();
client.collectDefaultMetrics({ register });

const LATENCY_BUCKETS = [0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5] as const;

// ─── HTTP transport ────────────────────────────────────────────────────────────

export const httpRequestsTotal = new client.Counter({
  name: 'http_requests_total',
  help: 'Total HTTP requests handled, labeled by method, route template, and status code.',
  labelNames: ['method', 'route', 'status_code'],
  registers: [register],
});

export const httpRequestDurationSeconds = new client.Histogram({
  name: 'http_request_duration_seconds',
  help: 'HTTP request duration in seconds, from middleware entry to response finish.',
  labelNames: ['method', 'route', 'status_code'],
  buckets: LATENCY_BUCKETS as unknown as number[],
  registers: [register],
});

export const httpRequestsInFlight = new client.Gauge({
  name: 'http_requests_in_flight',
  help: 'Concurrent in-flight HTTP requests, by method and route template.',
  labelNames: ['method', 'route'],
  registers: [register],
});

// ─── Business: user journey ────────────────────────────────────────────────────

export const ecomCartItemsAddedTotal = new client.Counter({
  name: 'ecom_cart_items_added_total',
  help: 'Items added to a cart via POST /api/cart/items, labeled by product category.',
  labelNames: ['product_category'],
  registers: [register],
});

export const ecomCheckoutsTotal = new client.Counter({
  name: 'ecom_checkouts_total',
  help: 'Checkout attempts by business outcome.',
  labelNames: ['outcome'], // success | empty_cart | insufficient_stock
  registers: [register],
});

export const ecomPaymentsTotal = new client.Counter({
  name: 'ecom_payments_total',
  help: 'Payment attempts by provider outcome (business outcome, not HTTP code).',
  labelNames: ['outcome', 'provider'], // succeeded | failed ; provider = mock-stripe
  registers: [register],
});

export const ecomPaymentAmountCentsTotal = new client.Counter({
  name: 'ecom_payment_amount_cents_total',
  help: 'Cumulative payment amount in cents, by provider and outcome.',
  labelNames: ['provider', 'outcome'],
  registers: [register],
});

export const ecomOrderValueCents = new client.Histogram({
  name: 'ecom_order_value_cents',
  help: 'Distribution of order totals (cents) at checkout time.',
  buckets: [1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000],
  registers: [register],
});

// ─── Auth ──────────────────────────────────────────────────────────────────────

export const authLoginAttemptsTotal = new client.Counter({
  name: 'auth_login_attempts_total',
  help: 'Login attempts by outcome.',
  labelNames: ['outcome'], // succeeded | invalid_credentials
  registers: [register],
});

// ─── Database ──────────────────────────────────────────────────────────────────

export const dbQueryDurationSeconds = new client.Histogram({
  name: 'db_query_duration_seconds',
  help: 'Duration of named, instrumented MySQL queries.',
  labelNames: ['query_name'], // bounded enum — see metric-catalog.md
  buckets: LATENCY_BUCKETS as unknown as number[],
  registers: [register],
});

// Wrap a promise-returning DB call to record its duration under `query_name`.
export async function time<T>(queryName: string, fn: () => Promise<T>): Promise<T> {
  const end = dbQueryDurationSeconds.startTimer({ query_name: queryName });
  try {
    return await fn();
  } finally {
    end();
  }
}

// ─── HTTP middleware ───────────────────────────────────────────────────────────
//
// We label `route` with the Express template (`req.baseUrl + req.route.path`).
// Unmatched requests are bucketed as 'unmatched' so 404s don't blow cardinality.
//
// Express clears `req.baseUrl` when control passes to the global error handler,
// so reading it at `res.on('finish')` returns only the router-local path
// ('/login' instead of '/api/auth/login'). The fix is two-fold: each router
// installs `stampRouteTemplate` at the top, which captures baseUrl while it's
// still correct; and we hook res.status / res.json as a fallback for paths
// that reject before the stamp runs.

// Install at the top of each router via `router.use(stampRouteTemplate)`.
export function stampRouteTemplate(req: Request, res: Response, next: NextFunction): void {
  if ((res.locals as { routeTemplate?: string }).routeTemplate === undefined) {
    (res.locals as { stampedBaseUrl?: string }).stampedBaseUrl = req.baseUrl ?? '';
  }
  next();
}

export function metricsMiddleware(req: Request, res: Response, next: NextFunction): void {
  const method = req.method;
  const inFlight = httpRequestsInFlight.labels(method, 'pending');
  inFlight.inc();
  const endTimer = httpRequestDurationSeconds.startTimer();

  const captureRoute = (): void => {
    if ((res.locals as { routeTemplate?: string }).routeTemplate !== undefined) return;
    (res.locals as { routeTemplate?: string }).routeTemplate = resolveRouteTemplate(req, res);
  };

  const origStatus = res.status.bind(res);
  res.status = ((code: number) => {
    captureRoute();
    return origStatus(code);
  }) as typeof res.status;

  const origJson = res.json.bind(res);
  res.json = ((body?: unknown) => {
    captureRoute();
    return origJson(body);
  }) as typeof res.json;

  let finished = false;
  const onDone = () => {
    if (finished) return;
    finished = true;
    inFlight.dec();
    captureRoute();
    const labels = {
      method,
      route: (res.locals as { routeTemplate?: string }).routeTemplate ?? 'unmatched',
      status_code: String(res.statusCode),
    };
    endTimer(labels);
    httpRequestsTotal.inc(labels);
  };

  res.on('finish', onDone);
  res.on('close', onDone);

  next();
}

function resolveRouteTemplate(req: Request, res: Response): string {
  const tpl = req.route?.path as string | undefined;
  if (!tpl) return 'unmatched';
  const stamped = (res.locals as { stampedBaseUrl?: string }).stampedBaseUrl;
  const base = stamped !== undefined ? stamped : req.baseUrl ?? '';
  return `${base}${tpl}`;
}
