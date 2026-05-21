import mysql from 'mysql2/promise';
import type { PoolConnection } from 'mysql2/promise';
import { config } from './config.js';

export const pool = mysql.createPool({
  host: config.mysql.host,
  port: config.mysql.port,
  user: config.mysql.user,
  password: config.mysql.password,
  database: config.mysql.database,
  waitForConnections: true,
  connectionLimit: 10,
});

export async function withTransaction<T>(fn: (conn: PoolConnection) => Promise<T>): Promise<T> {
  const conn = await pool.getConnection();
  await conn.beginTransaction();
  try {
    const result = await fn(conn);
    await conn.commit();
    return result;
  } catch (err) {
    await conn.rollback();
    throw err;
  } finally {
    conn.release();
  }
}

async function waitForConnection(retries = 15, delayMs = 2000): Promise<void> {
  for (let i = 0; i < retries; i++) {
    try {
      const conn = await pool.getConnection();
      conn.release();
      return;
    } catch {
      if (i === retries - 1) throw new Error('MySQL not reachable after retries');
      console.log(`MySQL not ready, retrying in ${delayMs}ms... (${i + 1}/${retries})`);
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }
}

export async function initSchema(): Promise<void> {
  await waitForConnection();
  await pool.execute(`
    CREATE TABLE IF NOT EXISTS users (
      id            INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      email         VARCHAR(255) UNIQUE NOT NULL,
      password_hash VARCHAR(255) NOT NULL,
      created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
  `);

  await pool.execute(`
    CREATE TABLE IF NOT EXISTS products (
      id          INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      sku         VARCHAR(50) UNIQUE NOT NULL,
      name        VARCHAR(255) NOT NULL,
      description TEXT,
      category    VARCHAR(50) NOT NULL,
      price_cents INT NOT NULL,
      stock       INT NOT NULL DEFAULT 0
    )
  `);

  await pool.execute(`
    CREATE TABLE IF NOT EXISTS carts (
      id         INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      user_id    INT NOT NULL UNIQUE,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
  `);

  await pool.execute(`
    CREATE TABLE IF NOT EXISTS cart_items (
      cart_id    INT NOT NULL,
      product_id INT NOT NULL,
      quantity   INT NOT NULL,
      PRIMARY KEY (cart_id, product_id),
      FOREIGN KEY (cart_id)    REFERENCES carts(id) ON DELETE CASCADE,
      FOREIGN KEY (product_id) REFERENCES products(id)
    )
  `);

  await pool.execute(`
    CREATE TABLE IF NOT EXISTS orders (
      id          INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      user_id     INT NOT NULL,
      total_cents INT NOT NULL,
      status      VARCHAR(50) NOT NULL,
      created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (user_id) REFERENCES users(id)
    )
  `);

  await pool.execute(`
    CREATE TABLE IF NOT EXISTS order_items (
      order_id    INT NOT NULL,
      product_id  INT NOT NULL,
      quantity    INT NOT NULL,
      price_cents INT NOT NULL,
      PRIMARY KEY (order_id, product_id),
      FOREIGN KEY (order_id)   REFERENCES orders(id) ON DELETE CASCADE,
      FOREIGN KEY (product_id) REFERENCES products(id)
    )
  `);

  await pool.execute(`
    CREATE TABLE IF NOT EXISTS sessions (
      id         INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      token      VARCHAR(64) UNIQUE NOT NULL,
      user_id    INT NOT NULL,
      expires_at DATETIME NOT NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
  `);

  await pool.execute(`
    CREATE TABLE IF NOT EXISTS payments (
      id           INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      order_id     INT NOT NULL,
      status       VARCHAR(50) NOT NULL,
      provider     VARCHAR(100) NOT NULL,
      amount_cents INT NOT NULL,
      created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (order_id) REFERENCES orders(id)
    )
  `);
}
