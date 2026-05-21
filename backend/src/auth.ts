import { randomBytes } from 'node:crypto';
import type { NextFunction, Request, Response } from 'express';
import { pool } from './db.js';

export interface AuthUser {
  id: number;
  email: string;
}

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      user?: AuthUser;
    }
  }
}

const SESSION_TTL_DAYS = 7;

export async function createSession(user: AuthUser): Promise<string> {
  const token = randomBytes(32).toString('hex');
  const expiresAt = new Date();
  expiresAt.setDate(expiresAt.getDate() + SESSION_TTL_DAYS);

  await pool.execute(
    'INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)',
    [token, user.id, expiresAt.toISOString().slice(0, 19).replace('T', ' ')]
  );
  return token;
}

export async function deleteSession(token: string): Promise<void> {
  await pool.execute('DELETE FROM sessions WHERE token = ?', [token]);
}

export async function requireAuth(req: Request, res: Response, next: NextFunction): Promise<void> {
  const header = req.headers.authorization ?? '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (!token) {
    res.status(401).json({ error: 'missing_token' });
    return;
  }

  const [[row]] = await pool.execute<any[]>(
    `SELECT s.user_id AS id, u.email
     FROM sessions s
     JOIN users u ON u.id = s.user_id
     WHERE s.token = ? AND s.expires_at > NOW()`,
    [token]
  );

  if (!row) {
    res.status(401).json({ error: 'invalid_token' });
    return;
  }

  req.user = { id: (row as AuthUser).id, email: (row as AuthUser).email };
  next();
}
