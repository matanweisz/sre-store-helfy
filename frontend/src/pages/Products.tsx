import { useEffect, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type ApiError, type Product } from '../api';

const CATEGORIES = ['electronics', 'home', 'books', 'apparel', 'toys'] as const;

function fmt(cents: number): string {
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

export default function Products() {
  const navigate = useNavigate();
  const [products, setProducts] = useState<Product[]>([]);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [loading, setLoading] = useState(true);

  // keyed by product_id: 'adding' | 'added' | 'out_of_stock'
  const [cardState, setCardState] = useState<Record<number, string>>({});

  async function fetchProducts(s: string, cat: string) {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (s) params.set('search', s);
      if (cat) params.set('category', cat);
      const qs = params.toString() ? `?${params.toString()}` : '';
      const data = await api<{ products: Product[] }>(`/products${qs}`);
      setProducts(data.products);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void fetchProducts('', '');
  }, []);

  function onSearch(e: FormEvent) {
    e.preventDefault();
    void fetchProducts(search, category);
  }

  function onCategoryChange(cat: string) {
    setCategory(cat);
    void fetchProducts(search, cat);
  }

  async function addToCart(productId: number) {
    setCardState((s) => ({ ...s, [productId]: 'adding' }));
    try {
      await api('/cart/items', {
        method: 'POST',
        body: JSON.stringify({ product_id: productId, quantity: 1 }),
      });
      setCardState((s) => ({ ...s, [productId]: 'added' }));
      setTimeout(() => setCardState((s) => ({ ...s, [productId]: '' })), 1500);
    } catch (err) {
      const apiErr = err as ApiError;
      if (apiErr.status === 401) {
        navigate('/login');
        return;
      }
      const next = apiErr.error === 'insufficient_stock' ? 'out_of_stock' : '';
      setCardState((s) => ({ ...s, [productId]: next }));
      if (next) setTimeout(() => setCardState((s) => ({ ...s, [productId]: '' })), 1500);
    }
  }

  return (
    <>
      <form onSubmit={onSearch} style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search products..."
          style={{ flex: 1 }}
        />
        <select
          value={category}
          onChange={(e) => onCategoryChange(e.target.value)}
          style={{ padding: '8px 10px', border: '1px solid #d0d7de', borderRadius: 6 }}
        >
          <option value="">All categories</option>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c.charAt(0).toUpperCase() + c.slice(1)}
            </option>
          ))}
        </select>
        <button type="submit">Search</button>
      </form>

      {loading ? (
        <p className="muted">Loading...</p>
      ) : products.length === 0 ? (
        <p className="muted">No products found.</p>
      ) : (
        <div className="grid">
          {products.map((p) => {
            const state = cardState[p.id] ?? '';
            return (
              <div className="card" key={p.id}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{p.name}</div>
                <div className="muted" style={{ marginBottom: 4 }}>
                  {p.category}
                </div>
                <div className="price" style={{ marginBottom: 4 }}>
                  {fmt(p.price_cents)}
                </div>
                <div className="muted" style={{ marginBottom: 10 }}>
                  {p.stock > 0 ? `${p.stock} in stock` : 'Out of stock'}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <button
                    disabled={state === 'adding' || p.stock === 0}
                    onClick={() => void addToCart(p.id)}
                  >
                    Add to cart
                  </button>
                  {state === 'added' && <span style={{ color: '#16a34a', fontSize: 13 }}>Added</span>}
                  {state === 'out_of_stock' && <span className="error" style={{ fontSize: 13 }}>Out of stock</span>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
