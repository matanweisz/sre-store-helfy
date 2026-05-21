import { Router } from 'express';
import bcrypt from 'bcryptjs';
import { pool } from '../db.js';
import { createSession, deleteSession, requireAuth } from '../auth.js';
import { asyncHandler, HttpError } from '../util.js';
import { authLoginAttemptsTotal, stampRouteTemplate } from '../metrics.js';

const router = Router();
router.use(stampRouteTemplate);

router.post(
  '/signup',
  asyncHandler(async (req, res) => {
    const { email, password } = (req.body ?? {}) as { email?: string; password?: string };
    if (!email || !password || password.length < 6) {
      throw new HttpError(400, 'invalid_input', 'email and password (>=6 chars) required');
    }
    const [[existing]] = await pool.execute<any[]>(
      'SELECT id FROM users WHERE email = ?',
      [email]
    );
    if (existing) throw new HttpError(409, 'email_taken');

    const hash = await bcrypt.hash(password, 10);
    const [result] = await pool.execute<any>(
      'INSERT INTO users (email, password_hash) VALUES (?, ?)',
      [email, hash]
    );
    const userId: number = (result as { insertId: number }).insertId;
    await pool.execute('INSERT INTO carts (user_id) VALUES (?)', [userId]);

    const user = { id: userId, email };
    const token = await createSession(user);
    res.status(201).json({ token, user });
  })
);

router.post(
  '/login',
  asyncHandler(async (req, res) => {
    const { email, password } = (req.body ?? {}) as { email?: string; password?: string };
    if (!email || !password) throw new HttpError(400, 'invalid_input');

    const [[row]] = await pool.execute<any[]>(
      'SELECT id, email, password_hash FROM users WHERE email = ?',
      [email]
    );
    if (!row) {
      authLoginAttemptsTotal.inc({ outcome: 'invalid_credentials' });
      throw new HttpError(401, 'invalid_credentials');
    }

    const ok = await bcrypt.compare(password, (row as { password_hash: string }).password_hash);
    if (!ok) {
      authLoginAttemptsTotal.inc({ outcome: 'invalid_credentials' });
      throw new HttpError(401, 'invalid_credentials');
    }

    const [[cart]] = await pool.execute<any[]>(
      'SELECT id FROM carts WHERE user_id = ?',
      [(row as { id: number }).id]
    );
    if (!cart) {
      await pool.execute('INSERT INTO carts (user_id) VALUES (?)', [(row as { id: number }).id]);
    }

    const user = { id: (row as { id: number }).id, email: (row as { email: string }).email };
    const token = await createSession(user);
    authLoginAttemptsTotal.inc({ outcome: 'succeeded' });
    req.log.info({ user: { id: String(user.id) } }, 'login succeeded');
    res.json({ token, user });
  })
);

router.post(
  '/logout',
  requireAuth,
  asyncHandler(async (req, res) => {
    const header = req.headers.authorization ?? '';
    const token = header.startsWith('Bearer ') ? header.slice(7) : '';
    await deleteSession(token);
    res.json({ ok: true });
  })
);

export default router;
