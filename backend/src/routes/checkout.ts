import { Router } from 'express';
import { pool, withTransaction } from '../db.js';
import { requireAuth } from '../auth.js';
import { asyncHandler, HttpError } from '../util.js';
import { ecomCheckoutsTotal, ecomOrderValueCents, stampRouteTemplate, time } from '../metrics.js';

const router = Router();
router.use(stampRouteTemplate);
router.use(requireAuth);

interface CheckoutItem {
  product_id: number;
  quantity: number;
  price_cents: number;
  stock: number;
  name: string;
}

router.post(
  '/',
  asyncHandler(async (req, res) => {
    const [[cart]] = await pool.execute<any[]>(
      'SELECT id FROM carts WHERE user_id = ?',
      [req.user!.id]
    );
    if (!cart) {
      ecomCheckoutsTotal.inc({ outcome: 'empty_cart' });
      throw new HttpError(400, 'empty_cart');
    }

    const [items] = await pool.execute<any[]>(
      `
      SELECT ci.product_id, ci.quantity, p.price_cents, p.stock, p.name
      FROM cart_items ci
      JOIN products p ON p.id = ci.product_id
      WHERE ci.cart_id = ?
      `,
      [(cart as { id: number }).id]
    );

    if ((items as CheckoutItem[]).length === 0) {
      ecomCheckoutsTotal.inc({ outcome: 'empty_cart' });
      throw new HttpError(400, 'empty_cart');
    }

    for (const it of items as CheckoutItem[]) {
      if (it.stock < it.quantity) {
        ecomCheckoutsTotal.inc({ outcome: 'insufficient_stock' });
        throw new HttpError(409, 'insufficient_stock', `not enough stock for ${it.name}`);
      }
    }

    const total_cents = (items as CheckoutItem[]).reduce(
      (acc, it) => acc + it.price_cents * it.quantity,
      0
    );
    const cartId = (cart as { id: number }).id;

    const orderId = await time('checkout_create_order', () =>
      withTransaction(async (conn) => {
        const [result] = await conn.execute<any>(
          'INSERT INTO orders (user_id, total_cents, status) VALUES (?, ?, ?)',
          [req.user!.id, total_cents, 'pending_payment']
        );
        const id: number = (result as { insertId: number }).insertId;

        for (const it of items as CheckoutItem[]) {
          await conn.execute(
            'INSERT INTO order_items (order_id, product_id, quantity, price_cents) VALUES (?, ?, ?, ?)',
            [id, it.product_id, it.quantity, it.price_cents]
          );
          await conn.execute(
            'UPDATE products SET stock = stock - ? WHERE id = ?',
            [it.quantity, it.product_id]
          );
        }
        await conn.execute('DELETE FROM cart_items WHERE cart_id = ?', [cartId]);
        return id;
      })
    );

    ecomCheckoutsTotal.inc({ outcome: 'success' });
    ecomOrderValueCents.observe(total_cents);

    res.status(201).json({ order_id: orderId, total_cents, status: 'pending_payment' });
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
    const [orderItems] = await pool.execute<any[]>(
      `
      SELECT oi.product_id, oi.quantity, oi.price_cents, p.name
      FROM order_items oi
      JOIN products p ON p.id = oi.product_id
      WHERE oi.order_id = ?
      `,
      [(order as { id: number }).id]
    );
    res.json({ order, items: orderItems });
  })
);

export default router;
