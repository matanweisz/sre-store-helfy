import { Router } from 'express';
import { pool } from '../db.js';
import { requireAuth } from '../auth.js';
import { asyncHandler, HttpError } from '../util.js';
import { ecomCartItemsAddedTotal, stampRouteTemplate } from '../metrics.js';

const router = Router();
router.use(stampRouteTemplate);
router.use(requireAuth);

async function getOrCreateCart(userId: number): Promise<{ id: number }> {
  const [[cart]] = await pool.execute<any[]>(
    'SELECT id FROM carts WHERE user_id = ?',
    [userId]
  );
  if (cart) return cart as { id: number };
  const [result] = await pool.execute<any>(
    'INSERT INTO carts (user_id) VALUES (?)',
    [userId]
  );
  return { id: (result as { insertId: number }).insertId };
}

router.get(
  '/',
  asyncHandler(async (req, res) => {
    const cart = await getOrCreateCart(req.user!.id);
    const [items] = await pool.execute<any[]>(
      `
      SELECT ci.product_id, ci.quantity, p.name, p.sku, p.price_cents, p.stock
      FROM cart_items ci
      JOIN products p ON p.id = ci.product_id
      WHERE ci.cart_id = ?
      `,
      [cart.id]
    );
    const total_cents = (items as Array<{ price_cents: number; quantity: number }>)
      .reduce((acc, it) => acc + it.price_cents * it.quantity, 0);
    res.json({ items, total_cents });
  })
);

router.post(
  '/items',
  asyncHandler(async (req, res) => {
    const { product_id, quantity = 1 } = (req.body ?? {}) as {
      product_id?: number;
      quantity?: number;
    };
    if (!product_id || quantity <= 0) throw new HttpError(400, 'invalid_input');

    const [[product]] = await pool.execute<any[]>(
      'SELECT id, stock, category FROM products WHERE id = ?',
      [product_id]
    );
    if (!product) throw new HttpError(404, 'product_not_found');
    if ((product as { stock: number }).stock < quantity) {
      throw new HttpError(409, 'insufficient_stock');
    }

    const cart = await getOrCreateCart(req.user!.id);

    const [[existing]] = await pool.execute<any[]>(
      'SELECT quantity FROM cart_items WHERE cart_id = ? AND product_id = ?',
      [cart.id, product_id]
    );
    if (existing) {
      await pool.execute(
        'UPDATE cart_items SET quantity = quantity + ? WHERE cart_id = ? AND product_id = ?',
        [quantity, cart.id, product_id]
      );
    } else {
      await pool.execute(
        'INSERT INTO cart_items (cart_id, product_id, quantity) VALUES (?, ?, ?)',
        [cart.id, product_id, quantity]
      );
    }
    ecomCartItemsAddedTotal.inc(
      { product_category: (product as { category: string }).category },
      quantity
    );
    res.status(201).json({ ok: true });
  })
);

router.delete(
  '/items/:productId',
  asyncHandler(async (req, res) => {
    const cart = await getOrCreateCart(req.user!.id);
    await pool.execute(
      'DELETE FROM cart_items WHERE cart_id = ? AND product_id = ?',
      [cart.id, req.params['productId']!]
    );
    res.json({ ok: true });
  })
);

export default router;
