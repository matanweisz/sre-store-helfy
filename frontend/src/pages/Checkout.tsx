import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, type Cart, type CheckoutResponse, type ApiError } from '../api';

function fmt(cents: number): string {
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

export default function Checkout() {
  const navigate = useNavigate();
  const [cart, setCart] = useState<Cart | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Cart>('/cart')
      .then(setCart)
      .catch((err: ApiError) => {
        if (err.status === 401) navigate('/login');
      });
  }, [navigate]);

  async function placeOrder() {
    setError(null);
    setBusy(true);
    try {
      const data = await api<CheckoutResponse>('/checkout', { method: 'POST' });
      navigate(`/payment/${data.order_id}`);
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.message ?? apiErr.error);
    } finally {
      setBusy(false);
    }
  }

  if (!cart) return <p className="muted">Loading...</p>;

  return (
    <div className="card" style={{ maxWidth: 480 }}>
      <h2>Order summary</h2>
      {cart.items.map((item) => (
        <div key={item.product_id} style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <span>{item.name} × {item.quantity}</span>
          <span className="price">{fmt(item.price_cents * item.quantity)}</span>
        </div>
      ))}
      <hr style={{ margin: '12px 0' }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <strong>Total</strong>
        <strong>{fmt(cart.total_cents)}</strong>
      </div>
      {error && (
        <p className="error" style={{ marginBottom: 12 }}>
          {error} — <Link to="/cart">Back to cart</Link>
        </p>
      )}
      <button disabled={busy || cart.items.length === 0} onClick={() => void placeOrder()}>
        {busy ? '...' : 'Place order'}
      </button>
    </div>
  );
}
