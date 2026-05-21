import bcrypt from 'bcryptjs';
import type { PoolConnection } from 'mysql2/promise';
import { pool, initSchema, withTransaction } from './db.js';

const categories = ['electronics', 'home', 'books', 'apparel', 'toys'] as const;
const adjectives = ['Compact', 'Premium', 'Eco', 'Rustic', 'Smart', 'Vintage', 'Modern', 'Handmade'];
const nouns = ['Lamp', 'Mug', 'Notebook', 'Backpack', 'Headphones', 'Bottle', 'Chair', 'Speaker', 'Keyboard', 'Mouse'];

function rand<T>(arr: readonly T[]): T {
  return arr[Math.floor(Math.random() * arr.length)] as T;
}

(async () => {
  await initSchema();

  const [[countRow]] = await pool.execute<any[]>('SELECT COUNT(*) AS n FROM products');
  const productCount = (countRow as { n: number }).n;

  if (productCount === 0) {
    await withTransaction(async (conn: PoolConnection) => {
      for (let i = 0; i < 120; i++) {
        const name = `${rand(adjectives)} ${rand(nouns)} ${i + 1}`;
        const sku = `SKU-${String(i + 1).padStart(5, '0')}`;
        const category = rand(categories);
        const price = 500 + Math.floor(Math.random() * 9500);
        const stock = Math.floor(Math.random() * 200);
        await conn.execute(
          'INSERT INTO products (sku, name, description, category, price_cents, stock) VALUES (?, ?, ?, ?, ?, ?)',
          [sku, name, `${name} - a fine ${category} item.`, category, price, stock]
        );
      }
    });
    console.log('Seeded 120 products.');
  } else {
    console.log(`Products table already has ${productCount} rows, skipping product seed.`);
  }

  const demoEmail = 'demo@shop.local';
  const [[existingUser]] = await pool.execute<any[]>(
    'SELECT id FROM users WHERE email = ?',
    [demoEmail]
  );

  if (!existingUser) {
    const hash = await bcrypt.hash('demopass', 10);
    const [result] = await pool.execute<any>(
      'INSERT INTO users (email, password_hash) VALUES (?, ?)',
      [demoEmail, hash]
    );
    const userId = (result as { insertId: number }).insertId;
    await pool.execute('INSERT INTO carts (user_id) VALUES (?)', [userId]);
    console.log(`Seeded demo user: ${demoEmail} / demopass`);
  } else {
    console.log('Demo user already exists.');
  }

  await pool.end();
})();
