import express, { type NextFunction, type Request, type Response } from 'express';
import cors from 'cors';
import { config } from './config.js';
import { initSchema } from './db.js';
import { HttpError } from './util.js';
import { metricsMiddleware, register } from './metrics.js';
import { httpLogger, logger } from './logger.js';
import authRouter from './routes/auth.js';
import productsRouter from './routes/products.js';
import cartRouter from './routes/cart.js';
import checkoutRouter from './routes/checkout.js';
import paymentRouter from './routes/payment.js';
import ordersRouter from './routes/orders.js';

const app = express();
app.use(cors());
app.use(express.json());

// /metrics is registered BEFORE the metrics middleware so the scrape endpoint
// itself is not labeled into our histograms (which would pollute p95 with the
// time Prom takes to render the response). It's also skipped by the logger
// further down to keep scrape noise out of the log stream.
app.get('/metrics', async (_req, res) => {
  res.set('Content-Type', register.contentType);
  res.end(await register.metrics());
});

// HTTP request logging — installed BEFORE the metrics middleware so every
// downstream handler has req.log (with trace.id) available. We skip /healthz
// and /metrics manually here (pino-http has no built-in exclude list).
app.use((req, res, next) => {
  if (req.path === '/healthz' || req.path === '/metrics') return next();
  return httpLogger(req, res, next);
});

// All non-skipped requests pass through the metrics middleware.
app.use(metricsMiddleware);

app.get('/healthz', (_req, res) => {
  res.json({ ok: true });
});

app.use('/api/auth', authRouter);
app.use('/api/products', productsRouter);
app.use('/api/cart', cartRouter);
app.use('/api/checkout', checkoutRouter);
app.use('/api/payment', paymentRouter);
app.use('/api/orders', ordersRouter);

app.use((req: Request, res: Response) => {
  res.status(404).json({ error: 'not_found', path: req.path });
});

app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
  if (err instanceof HttpError) {
    // 4xx/5xx are already auto-logged by pino-http customLogLevel based on
    // res.statusCode. We attach the structured error_code so a single log
    // record carries both the HTTP shape and the business error code.
    req.log?.warn?.(
      { ecom: { error_code: err.code } },
      `handled error: ${err.code}`
    );
    res.status(err.status).json({ error: err.code, message: err.message });
    return;
  }
  req.log?.error?.({ err }, 'unhandled error');
  res.status(500).json({ error: 'internal_error' });
});

(async () => {
  await initSchema();
  app.listen(config.port, () => {
    logger.info({ event: { outcome: 'success' } }, `backend listening on :${config.port}`);
  });
})();

