import { Router } from 'express';
import { pool, withTransaction } from '../db.js';
import { requireAuth } from '../auth.js';
import { asyncHandler, HttpError, sleep } from '../util.js';
import { config } from '../config.js';
import {
  ecomPaymentAmountCentsTotal,
  ecomPaymentsTotal,
  stampRouteTemplate,
  time,
} from '../metrics.js';

const router = Router();
router.use(stampRouteTemplate);
router.use(requireAuth);

router.post(
  '/',
  asyncHandler(async (req, res) => {
    const { order_id, card_number } = (req.body ?? {}) as {
      order_id?: number;
      card_number?: string;
    };
    if (!order_id || !card_number) throw new HttpError(400, 'invalid_input');

    const [[order]] = await pool.execute<any[]>(
      'SELECT id, user_id, total_cents, status FROM orders WHERE id = ?',
      [order_id]
    );
    if (!order || (order as { user_id: number }).user_id !== req.user!.id) {
      throw new HttpError(404, 'order_not_found');
    }
    if ((order as { status: string }).status !== 'pending_payment') {
      throw new HttpError(
        409,
        'order_not_payable',
        `order is in status ${(order as { status: string }).status}`
      );
    }

    // Simulate calling out to a mock payment provider.
    const latency =
      config.paymentLatencyMsMin +
      Math.floor(Math.random() * (config.paymentLatencyMsMax - config.paymentLatencyMsMin));
    await sleep(latency);

    const failed = Math.random() < config.paymentFailureRate;
    const status = failed ? 'failed' : 'succeeded';
    const provider = 'mock-stripe';
    const orderId = (order as { id: number }).id;
    const totalCents = (order as { total_cents: number }).total_cents;

    await time('payment_record', () =>
      withTransaction(async (conn) => {
        await conn.execute(
          'INSERT INTO payments (order_id, status, provider, amount_cents) VALUES (?, ?, ?, ?)',
          [orderId, status, provider, totalCents]
        );
        await conn.execute('UPDATE orders SET status = ? WHERE id = ?', [
          failed ? 'payment_failed' : 'paid',
          orderId,
        ]);
      })
    );

    // Business metrics: count by outcome AND sum amount by outcome.
    // The `failed` outcome is a *business error* (provider declined) even though
    // the DB transaction succeeded — that's why this counter has its own label.
    ecomPaymentsTotal.inc({ outcome: status, provider });
    ecomPaymentAmountCentsTotal.inc({ provider, outcome: status }, totalCents);

    // Structured business event — searchable in ES by ecom.order_id /
    // ecom.payment_status. Level promoted by pino-http's customLogLevel when
    // the HttpError below sets a 4xx status; success path stays info.
    req.log.info(
      {
        ecom: {
          order_id: String(orderId),
          payment_status: status,
          payment_amount_cents: totalCents,
        },
      },
      'payment recorded'
    );

    if (failed) {
      throw new HttpError(402, 'payment_declined', 'mock provider declined the charge');
    }

    res.json({ order_id: orderId, status: 'paid', amount_cents: totalCents });
  })
);

export default router;
