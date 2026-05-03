"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

const PASSCODE = "9472";
const SESSION_KEY = "amta_auth_ok";

export default function ClientShell({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [unlocked, setUnlocked] = useState(false);
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const ok = sessionStorage.getItem(SESSION_KEY) === "1";
    setUnlocked(ok);
    setReady(true);
  }, []);

  const masked = useMemo(() => {
    return `${"•".repeat(pin.length)}${"○".repeat(Math.max(0, 4 - pin.length))}`;
  }, [pin]);

  function pushDigit(d: string) {
    if (pin.length >= 4) return;
    const next = `${pin}${d}`;
    setPin(next);
    setError("");
    if (next.length === 4) {
      if (next === PASSCODE) {
        sessionStorage.setItem(SESSION_KEY, "1");
        setUnlocked(true);
      } else {
        setError("Wrong passcode");
        setPin("");
      }
    }
  }

  function backspace() {
    setPin((p) => p.slice(0, -1));
    setError("");
  }

  if (!ready) return null;

  if (!unlocked) {
    return (
      <div className="auth-wrap">
        <div className="auth-card">
          <h1 className="auth-title">Welcome Back</h1>
          <p className="auth-sub">Enter 4-digit passcode</p>

          <div className="auth-dots" aria-label="passcode progress">
            {masked}
          </div>
          {error && <div className="auth-error">{error}</div>}

          <div className="auth-pad">
            {[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => (
              <button key={n} className="auth-key" onClick={() => pushDigit(String(n))}>
                {n}
              </button>
            ))}
            <button className="auth-key auth-key-muted" onClick={() => setPin("")}>
              C
            </button>
            <button className="auth-key" onClick={() => pushDigit("0")}>
              0
            </button>
            <button className="auth-key auth-key-muted" onClick={backspace}>
              ⌫
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      <nav className="nav">
        <div className="nav-inner">
          <span className="brand">🤖 AMTA</span>
          <Link href="/">Dashboard</Link>
          <Link href="/daily-earnings">Daily Earnings</Link>
          <Link href="/trades">All Trades</Link>
          <Link href="/polymarket">Polymarket</Link>
          <Link href="/strategies">Strategies</Link>
          <Link href="/quick">Quick Trade</Link>
          <Link href="/knowledge">Knowledge</Link>
        </div>
      </nav>
      {children}
    </>
  );
}
