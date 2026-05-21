import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, getToken, type ApiError } from '../api';

interface Order {
  id: number;
  total_cents: number;
  status: string;
  created_at: string;
  item_count: number;
}

interface OrderItem {
  product_id: number;
  name: string;
  quantity: number;
  price_cents: number;
}

function fmt(cents: number): string {
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

const STATUS_COLOR: Record<string, string> = {
  paid: '#16a34a',
  pending_payment: '#d97706',
  payment_failed: '#b91c1c',
};

export default function Account() {
  const navigate = useNavigate();
  const email = (() => {
    const t = getToken();
    if (!t) return '';
    try {
      // token is opaque — get email from localStorage if stored, else show nothing
      return localStorage.getItem('sre_shop_email') ?? '';
    } catch {
      return '';
    }
  })();

  const [orders, setOrders] = useState<Order[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [items, setItems] = useState<Record<number, OrderItem[]>>({});
  const [loadingItems, setLoadingItems] = useState<number | null>(null);

  useEffect(() => {
    api<{ orders: Order[] }>('/orders')
      .then((d) => setOrders(d.orders))
      .catch((err: ApiError) => {
        if (err.status === 401) navigate('/login');
      });
  }, [navigate]);

  async function toggleOrder(orderId: number) {
    if (expanded === orderId) {
      setExpanded(null);
      return;
    }
    setExpanded(orderId);
    if (items[orderId]) return;
    setLoadingItems(orderId);
    try {
      const d = await api<{ order: Order; items: OrderItem[] }>(`/orders/${orderId}`);
      setItems((prev) => ({ ...prev, [orderId]: d.items }));
    } finally {
      setLoadingItems(null);
    }
  }

  return (
    <>
      <h2>Account</h2>
      {email && <p className="muted" style={{ marginBottom: 16 }}>{email}</p>}

      <h3 style={{ marginBottom: 12 }}>Order history</h3>

      {orders.length === 0 ? (
        <div className="card">
          <p>No orders yet. <Link to="/">Start shopping</Link></p>
        </div>
      ) : (
        orders.map((order) => (
          <div className="card" key={order.id} style={{ marginBottom: 8 }}>
            <div
              style={{ display: 'flex', alignItems: 'center', gap: 12, cursor: 'pointer' }}
              onClick={() => void toggleOrder(order.id)}
            >
              <div style={{ flex: 1 }}>
                <span style={{ fontWeight: 600 }}>Order #{order.id}</span>
                <span className="muted" style={{ marginLeft: 12, fontSize: 13 }}>
                  {new Date(order.created_at).toLocaleDateString()}
                </span>
              </div>
              <span style={{ fontSize: 13, color: STATUS_COLOR[order.status] ?? '#6b7280', fontWeight: 600 }}>
                {order.status.replace(/_/g, ' ')}
              </span>
              <span className="price">{fmt(order.total_cents)}</span>
              <span className="muted" style={{ fontSize: 13 }}>{order.item_count} item{order.item_count !== 1 ? 's' : ''}</span>
              <span className="muted" style={{ fontSize: 12 }}>{expanded === order.id ? '▲' : '▼'}</span>
            </div>

            {expanded === order.id && (
              <div style={{ marginTop: 12, borderTop: '1px solid #e5e7eb', paddingTop: 12 }}>
                {loadingItems === order.id ? (
                  <p className="muted">Loading...</p>
                ) : (
                  (items[order.id] ?? []).map((item) => (
                    <div
                      key={item.product_id}
                      style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6, fontSize: 14 }}
                    >
                      <span>{item.name} × {item.quantity}</span>
                      <span className="price">{fmt(item.price_cents * item.quantity)}</span>
                    </div>
                  ))
                )}
                {order.status === 'pending_payment' && (
                  <div style={{ marginTop: 8 }}>
                    <Link to={`/payment/${order.id}`}>Complete payment →</Link>
                  </div>
                )}
              </div>
            )}
          </div>
        ))
      )}
    </>
  );
}
