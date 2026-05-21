"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { usePathname } from "next/navigation";

const PASSCODE = "9472";
const SESSION_KEY = "amta_auth_ok";

export default function ClientShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
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

  const tabs = [
    { href: "/", label: "Dashboard", short: "HOME" },
    { href: "/kalshi", label: "Kalshi Paper", short: "K-PAPER" },
    { href: "/bots", label: "Kalshi Live", short: "K-LIVE" },
    { href: "/polymarket", label: "Polymarket Paper", short: "P-PAPER" },
    { href: "/polymarket/live", label: "Polymarket Live", short: "P-LIVE" },
    { href: "/candle/paper", label: "Candle Paper", short: "C-PAPER" },
    { href: "/candle/live", label: "Candle Live", short: "C-LIVE" },
  ];

  if (pathname === "/") {
    return (
      <>
        <nav className="nav">
          <div className="nav-inner">
            <span className="brand">AMTA</span>
            {tabs.map((tab) => (
              <Link key={`d-${tab.href}`} href={tab.href}>
                {tab.label}
              </Link>
            ))}
          </div>
        </nav>
        {children}
      </>
    );
  }

  return (
    <>
      <header className="amta-topbar">
        <div className="amta-topbar-inner">
          <div className="amta-brand-wrap">
            <span className="amta-terminal-dot" />
            <span className="amta-brand">AMTA OPERATOR v4.2</span>
          </div>
          <div className="amta-pill">SECURE</div>
        </div>
      </header>

      <nav className="amta-tabs">
        <div className="amta-tabs-inner">
          {tabs.map((tab) => {
            const active = pathname === tab.href;
            return (
              <Link key={tab.href} href={tab.href} className={`amta-tab ${active ? "active" : ""}`}>
                <span className="tab-full">{tab.label}</span>
                <span className="tab-short">{tab.short}</span>
              </Link>
            );
          })}
        </div>
      </nav>

      <div className="amta-page-wrap">{children}</div>

      <nav className="amta-bottom-nav">
        {tabs.slice(0, 4).map((tab) => {
          const active = pathname === tab.href;
          return (
            <Link key={`m-${tab.href}`} href={tab.href} className={`amta-bottom-item ${active ? "active" : ""}`}>
              {tab.short}
            </Link>
          );
        })}
      </nav>
    </>
  );
}
