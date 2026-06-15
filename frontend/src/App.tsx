import { Link, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { api, getToken, setToken } from './api';
import Login from './pages/Login';
import Products from './pages/Products';
import Cart from './pages/Cart';
import Checkout from './pages/Checkout';
import Payment from './pages/Payment';
import OrderSuccess from './pages/OrderSuccess';
import Account from './pages/Account';

function RequireAuth({ children }: { children: JSX.Element }) {
  return getToken() ? children : <Navigate to="/login" replace />;
}

export default function App() {
  const navigate = useNavigate();
  const authed = !!getToken();

  async function logout() {
    try {
      await api('/auth/logout', { method: 'POST' });
    } catch {
      // session may already be expired — clear locally regardless
    }
    setToken(null);
    navigate('/login');
  }

  return (
    <>
      <nav>
        <Link to="/">Shop</Link>
        <Link to="/cart">Cart</Link>
        {authed && <Link to="/account">Account</Link>}
        <div className="spacer" />
        {authed ? (
          <button className="secondary" onClick={() => void logout()}>
            Logout
          </button>
        ) : (
          <Link to="/login">Login</Link>
        )}
      </nav>
      <div className="container">
        <Routes>
          <Route path="/" element={<Products />} />
          <Route path="/login" element={<Login />} />
          <Route
            path="/cart"
            element={
              <RequireAuth>
                <Cart />
              </RequireAuth>
            }
          />
          <Route
            path="/checkout"
            element={
              <RequireAuth>
                <Checkout />
              </RequireAuth>
            }
          />
          <Route
            path="/payment/:orderId"
            element={
              <RequireAuth>
                <Payment />
              </RequireAuth>
            }
          />
          <Route
            path="/account"
            element={
              <RequireAuth>
                <Account />
              </RequireAuth>
            }
          />
          <Route
            path="/order/:orderId/success"
            element={
              <RequireAuth>
                <OrderSuccess />
              </RequireAuth>
            }
          />
        </Routes>
      </div>
    </>
  );
}
