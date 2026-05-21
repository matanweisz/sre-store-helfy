const TOKEN_KEY = 'sre_shop_token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null, email?: string): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
    if (email) localStorage.setItem('sre_shop_email', email);
  } else {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem('sre_shop_email');
  }
}

export interface ApiError {
  status: number;
  error: string;
  message?: string;
}

export async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(opts.headers as Record<string, string> | undefined),
  };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`/api${path}`, { ...opts, headers });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;

  if (!res.ok) {
    const err: ApiError = {
      status: res.status,
      error: body?.error ?? 'unknown_error',
      message: body?.message,
    };
    throw err;
  }
  return body as T;
}

export interface Product {
  id: number;
  sku: string;
  name: string;
  description?: string;
  category: string;
  price_cents: number;
  stock: number;
}

export interface CartItem {
  product_id: number;
  quantity: number;
  name: string;
  sku: string;
  price_cents: number;
  stock: number;
}

export interface Cart {
  items: CartItem[];
  total_cents: number;
}

export interface CheckoutResponse {
  order_id: number;
  total_cents: number;
  status: string;
}

export interface PaymentResponse {
  order_id: number;
  status: string;
  amount_cents: number;
}

export interface AuthResponse {
  token: string;
  user: { id: number; email: string };
}
