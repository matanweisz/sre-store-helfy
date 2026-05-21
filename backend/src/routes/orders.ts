import { Router } from 'express';
import { pool } from '../db.js';
import { requireAuth } from '../auth.js';
import { asyncHandler, HttpError } from '../util.js';
import { stampRouteTemplate } from '../metrics.js';

const router = Router();
router.use(stampRouteTemplate);
router.use(requireAuth);

router.get(
  '/',
  asyncHandler(async (req, res) => {
    const [orders] = await pool.execute<any[]>(
      `SELECT o.id, o.total_cents, o.status, o.created_at,
              COUNT(oi.product_id) AS item_count
       FROM orders o
       LEFT JOIN order_items oi ON oi.order_id = o.id
       WHERE o.user_id = ?
       GROUP BY o.id, o.total_cents, o.status, o.created_at
       ORDER BY o.created_at DESC`,
      [req.user!.id]
    );
    res.json({ orders });
  })
);

router.get(
  '/:orderId',
  asyncHandler(async (req, res) => {
    const [[order]] = await pool.execute<any[]>(
      'SELECT id, user_id, total_cents, status, created_at FROM orders WHERE id = ?',
      [req.params['orderId']!]
    );
    if (!order || (order as { user_id: number }).user_id !== req.user!.id) {
      throw new HttpError(404, 'not_found');
    }
    const [items] = await pool.execute<any[]>(
      `SELECT oi.product_id, oi.quantity, oi.price_cents, p.name
       FROM order_items oi
       JOIN products p ON p.id = oi.product_id
       WHERE oi.order_id = ?`,
      [(order as { id: number }).id]
    );
    res.json({ order, items });
  })
);

export default router;
