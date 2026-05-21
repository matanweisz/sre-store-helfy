import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, type ApiError } from '../api';

interface OrderDetail {
  id: number;
  total_cents: number;
  status: string;
  created_at: string;
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

export default function OrderSuccess() {
  const { orderId } = useParams<{ orderId: string }>();
  const navigate = useNavigate();
  const [order, setOrder] = useState<OrderDetail | null>(null);
  const [items, setItems] = useState<OrderItem[]>([]);

  useEffect(() => {
    if (!orderId) return;
    api<{ order: OrderDetail; items: OrderItem[] }>(`/checkout/${orderId}`)
      .then(({ order, items }) => {
        setOrder(order);
        setItems(items);
      })
      .catch((err: ApiError) => {
        if (err.status === 401) navigate('/login');
      });
  }, [orderId, navigate]);

  if (!order) return <p className="muted">Loading...</p>;

  return (
    <div className="card" style={{ maxWidth: 480 }}>
      <h2>Order #{order.id} — paid</h2>
      <p className="muted" style={{ marginBottom: 16 }}>
        Placed on {new Date(order.created_at).toLocaleString()}
      </p>
      {items.map((item) => (
        <div
          key={item.product_id}
          style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}
        >
          <span>
            {item.name} × {item.quantity}
          </span>
          <span className="price">{fmt(item.price_cents * item.quantity)}</span>
        </div>
      ))}
      <hr style={{ margin: '12px 0' }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <strong>Total</strong>
        <strong>{fmt(order.total_cents)}</strong>
      </div>
      <Link to="/">Continue shopping</Link>
    </div>
  );
}
