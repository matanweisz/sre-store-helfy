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
// isn't labeled into its own histograms.
app.get('/metrics', async (_req, res) => {
  res.set('Content-Type', register.contentType);
  res.end(await register.metrics());
});

// Skip /healthz and /metrics so they don't pollute the log stream or histograms.
app.use((req, res, next) => {
  if (req.path === '/healthz' || req.path === '/metrics') return next();
  return httpLogger(req, res, next);
});

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
    // pino-http already auto-logged the request at warn/error level; this adds
    // the structured error_code alongside.
    req.log?.warn?.({ ecom: { error_code: err.code } }, `handled error: ${err.code}`);
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

