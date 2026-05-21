import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, type ApiError, type PaymentResponse } from '../api';

interface OrderDetail {
  id: number;
  total_cents: number;
  status: string;
}

function fmt(cents: number): string {
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

export default function Payment() {
  const { orderId } = useParams<{ orderId: string }>();
  const navigate = useNavigate();
  const [order, setOrder] = useState<OrderDetail | null>(null);
  const [cardNumber, setCardNumber] = useState('4242 4242 4242 4242');
  const [error, setError] = useState<string | null>(null);
  const [nonRetryable, setNonRetryable] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!orderId) return;
    api<{ order: OrderDetail }>(`/checkout/${orderId}`)
      .then(({ order }) => {
        if (order.status === 'paid') {
          navigate(`/order/${orderId}/success`, { replace: true });
        } else {
          setOrder(order);
        }
      })
      .catch((err: ApiError) => {
        if (err.status === 401) navigate('/login');
      });
  }, [orderId, navigate]);

  async function pay() {
    if (!orderId) return;
    setError(null);
    setBusy(true);
    try {
      await api<PaymentResponse>('/payment', {
        method: 'POST',
        body: JSON.stringify({ order_id: Number(orderId), card_number: cardNumber }),
      });
      navigate(`/order/${orderId}/success`);
    } catch (err) {
      const apiErr = err as ApiError;
      if (apiErr.error === 'order_not_payable') {
        setNonRetryable(true);
      } else {
        setError(apiErr.message ?? apiErr.error);
      }
    } finally {
      setBusy(false);
    }
  }

  if (!order) return <p className="muted">Loading...</p>;

  if (nonRetryable) {
    return (
      <div className="card" style={{ maxWidth: 400 }}>
        <p className="error">This order cannot be paid again. Please start a new checkout.</p>
        <Link to="/">Continue shopping</Link>
      </div>
    );
  }

  return (
    <div className="card" style={{ maxWidth: 400 }}>
      <h2>Payment</h2>
      <p className="muted" style={{ marginBottom: 16 }}>
        Order #{order.id} — <strong>{fmt(order.total_cents)}</strong>
      </p>
      <label>
        <span>Card number</span>
        <input
          value={cardNumber}
          onChange={(e) => setCardNumber(e.target.value)}
          placeholder="4242 4242 4242 4242"
        />
      </label>
      {error && <p className="error" style={{ marginBottom: 8 }}>{error}</p>}
      <button disabled={busy} onClick={() => void pay()}>
        {busy ? 'Processing...' : `Pay ${fmt(order.total_cents)}`}
      </button>
    </div>
  );
}
