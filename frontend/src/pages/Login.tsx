import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, setToken, type ApiError, type AuthResponse } from '../api';

export default function Login() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<'login' | 'signup'>('login');
  const [email, setEmail] = useState('demo@shop.local');
  const [password, setPassword] = useState('demopass');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const path = mode === 'login' ? '/auth/login' : '/auth/signup';
      const data = await api<AuthResponse>(path, {
        method: 'POST',
        body: JSON.stringify({ email, password }),
      });
      setToken(data.token, data.user.email);
      navigate('/');
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.message ?? apiErr.error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ maxWidth: 360 }}>
      <h2>{mode === 'login' ? 'Login' : 'Sign up'}</h2>
      <form onSubmit={onSubmit}>
        <label>
          <span>Email</span>
          <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" required />
        </label>
        <label>
          <span>Password</span>
          <input
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            type="password"
            required
            minLength={6}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button disabled={busy} type="submit">
          {busy ? '...' : mode === 'login' ? 'Login' : 'Sign up'}
        </button>
        <p className="muted" style={{ marginTop: 12 }}>
          {mode === 'login' ? 'No account?' : 'Have an account?'}{' '}
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              setMode(mode === 'login' ? 'signup' : 'login');
            }}
          >
            {mode === 'login' ? 'Sign up' : 'Login'}
          </a>
        </p>
      </form>
    </div>
  );
}
