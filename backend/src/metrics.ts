// Prometheus metrics — see metric-catalog.md for the contract.
//
// Conventions enforced by this module:
//   - Single dedicated Registry (not the global default).
//   - HTTP/DB latency in seconds with the shared LATENCY_BUCKETS array.
//   - Money in cents (matches DB column names).
//   - Labels are bounded — forbidden list is in guidelines.md §2.
//   - Express route templates only (req.route.path), never resolved paths.
//
// Middleware contract:
//   metricsMiddleware records http_requests_total + http_request_duration_seconds
//   + http_requests_in_flight on every request that goes through the router. It is
//   wired in index.ts AFTER express.json() and BEFORE the route mounts so it sees
//   the resolved Express template via req.route?.path on response finish.

import client from 'prom-client';
import type { Request, Response, NextFunction } from 'express';

export const register = new client.Registry();

// Node process / runtime defaults — exposes process_cpu_*, process_resident_memory_bytes,
// nodejs_eventloop_lag_seconds, nodejs_heap_size_* etc. See metric-catalog.md "Process / runtime".
client.collectDefaultMetrics({ register });

// Shared histogram buckets, tuned to a 25ms–5s envelope. Payment route (120–450ms)
// is well-resolved. See guidelines.md §2.
const LATENCY_BUCKETS = [0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5] as const;

// ─── HTTP transport ─────────────────────────────────────────────────────────────

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

// ─── Business: user journey ─────────────────────────────────────────────────────

export const ecomCartItemsAddedTotal = new client.Counter({
  name: 'ecom_cart_items_added_total',
  help: 'Items added to a cart via POST /api/cart/items, labeled by product category.',
  labelNames: ['product_category'],
  registers: [register],
});

export const ecomCheckoutsTotal = new client.Counter({
  name: 'ecom_checkouts_total',
  help: 'Checkout attempts by outcome (business outcome, not HTTP code).',
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
  // cents — see catalog. Median around 7000-15000 with seed data.
  buckets: [1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000],
  registers: [register],
});

// ─── Auth ───────────────────────────────────────────────────────────────────────

export const authLoginAttemptsTotal = new client.Counter({
  name: 'auth_login_attempts_total',
  help: 'Login attempts by outcome.',
  labelNames: ['outcome'], // succeeded | invalid_credentials
  registers: [register],
});

// ─── Database ───────────────────────────────────────────────────────────────────

export const dbQueryDurationSeconds = new client.Histogram({
  name: 'db_query_duration_seconds',
  help: 'Duration of named, instrumented MySQL queries.',
  labelNames: ['query_name'], // bounded enum — see metric-catalog.md
  buckets: LATENCY_BUCKETS as unknown as number[],
  registers: [register],
});

// time() — wrap a Promise-returning DB call to record its duration.
// Use only for the three named queries in the catalog.
export async function time<T>(queryName: string, fn: () => Promise<T>): Promise<T> {
  const end = dbQueryDurationSeconds.startTimer({ query_name: queryName });
  try {
    return await fn();
  } finally {
    end();
  }
}

// ─── HTTP middleware ────────────────────────────────────────────────────────────

// We label `route` with the Express template (req.baseUrl + req.route.path).
// For unmatched requests (404) the template is undefined; we use 'unmatched' so
// we don't explode cardinality with raw paths.
//
// Important detail: when a route handler calls next(err), Express propagates to
// the global error handler. At that point baseUrl resets to the parent app's
// baseUrl (empty for our root app), so reading it on res.on('finish') gives
// only the local route.path ('/login') instead of the full template
// ('/api/auth/login').
//
// Solution: capture in two places, first-write-wins:
//   1) `stampRouteTemplate` is installed at the top of each router (router.use(...))
//      — it runs after Express resolved the route but before any handler logic,
//      so baseUrl + route.path are guaranteed correct. Most requests are stamped
//      here.
//   2) For paths that bypass this (e.g. requireAuth middleware rejecting before
//      the stamp runs), we also patch res.status/res.json — these run before
//      Express clears baseUrl on the success path.
//   3) Final fallback: read baseUrl + route at res.on('finish') time. Works for
//      synchronous paths.

// Per-router stamp. Install with `router.use(stampRouteTemplate)` at the top of
// every route file. Safe to call multiple times — first wins.
export function stampRouteTemplate(req: Request, res: Response, next: NextFunction): void {
  if ((res.locals as { routeTemplate?: string }).routeTemplate === undefined) {
    // We're inside a mounted router here, so req.baseUrl is correct.
    // req.route.path may not be resolved yet at this exact moment (router.use
    // runs before the route matcher), so we defer with a finalize that resolves
    // it after the actual route handler picks the path. Combine: store the
    // baseUrl now (immutable for this request), and let the route.path be read
    // on response.
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

  // status() is called by every json()/send()/sendStatus() and by error handlers
  // before they emit. By hooking it we catch the moment baseUrl + route.path
  // are still in scope.
  const origStatus = res.status.bind(res);
  res.status = ((code: number) => {
    captureRoute();
    return origStatus(code);
  }) as typeof res.status;

  // Fallback for handlers that skip status() (e.g. res.json() with no explicit
  // status — defaults to 200).
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
  // Prefer the baseUrl that stampRouteTemplate captured at router-mount time
  // (correct on error paths too); fall back to the live baseUrl.
  const stamped = (res.locals as { stampedBaseUrl?: string }).stampedBaseUrl;
  const base = stamped !== undefined ? stamped : req.baseUrl ?? '';
  return `${base}${tpl}`;
}
