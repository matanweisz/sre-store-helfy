import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, type Cart as CartType, type ApiError } from '../api';

function fmt(cents: number): string {
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

export default function Cart() {
  const navigate = useNavigate();
  const [cart, setCart] = useState<CartType | null>(null);
  const [removing, setRemoving] = useState<number | null>(null);

  async function fetchCart() {
    try {
      const data = await api<CartType>('/cart');
      setCart(data);
    } catch (err) {
      const apiErr = err as ApiError;
      if (apiErr.status === 401) navigate('/login');
    }
  }

  useEffect(() => {
    void fetchCart();
  }, []);

  async function removeItem(productId: number) {
    setRemoving(productId);
    try {
      await api(`/cart/items/${productId}`, { method: 'DELETE' });
      await fetchCart();
    } finally {
      setRemoving(null);
    }
  }

  if (!cart) return <p className="muted">Loading...</p>;

  if (cart.items.length === 0) {
    return (
      <div className="card">
        <p>Your cart is empty.</p>
        <Link to="/">Browse products</Link>
      </div>
    );
  }

  return (
    <>
      <h2>Cart</h2>
      {cart.items.map((item) => (
        <div
          className="card"
          key={item.product_id}
          style={{ display: 'flex', alignItems: 'center', gap: 12 }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600 }}>{item.name}</div>
            <div className="muted">
              {fmt(item.price_cents)} × {item.quantity} ={' '}
              <strong>{fmt(item.price_cents * item.quantity)}</strong>
            </div>
          </div>
          <button
            className="secondary"
            disabled={removing === item.product_id}
            onClick={() => void removeItem(item.product_id)}
          >
            {removing === item.product_id ? '...' : 'Remove'}
          </button>
        </div>
      ))}
      <div className="card" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span className="price">Total: {fmt(cart.total_cents)}</span>
        <button onClick={() => navigate('/checkout')} disabled={cart.items.length === 0}>
          Checkout
        </button>
      </div>
    </>
  );
}
