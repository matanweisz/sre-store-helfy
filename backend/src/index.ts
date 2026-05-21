import express, { type NextFunction, type Request, type Response } from 'express';
import cors from 'cors';
import { config } from './config.js';
import { initSchema } from './db.js';
import { HttpError } from './util.js';
import authRouter from './routes/auth.js';
import productsRouter from './routes/products.js';
import cartRouter from './routes/cart.js';
import checkoutRouter from './routes/checkout.js';
import paymentRouter from './routes/payment.js';
import ordersRouter from './routes/orders.js';

const app = express();
app.use(cors());
app.use(express.json());

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

app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
  if (err instanceof HttpError) {
    res.status(err.status).json({ error: err.code, message: err.message });
    return;
  }
  console.error('unhandled_error', err);
  res.status(500).json({ error: 'internal_error' });
});

(async () => {
  await initSchema();
  app.listen(config.port, () => {
    console.log(`backend listening on :${config.port}`);
  });
})();
