"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Moon, Sun, Languages, Menu, X } from "lucide-react";
import { toggleTheme } from "@/components/providers";
import { useI18n } from "@/lib/i18n";

export default function LandingHeader({ brand }: { brand: string }) {
  const { locale, setLocale } = useI18n();
  const [isDark, setDark] = useState(true);
  const [me, setMe] = useState<{ name: string; role: string } | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setDark(!document.documentElement.classList.contains("light"));
    fetch("/api/auth/me")
      .then((r) => r.json())
      .then((d) => setMe(d.user ?? null))
      .catch(() => {});
  }, []);

  return (
    <header className="sticky top-0 z-40 border-b border-[color:var(--border)] backdrop-blur-md bg-[color:var(--bg)]/70">
      <div className="max-w-6xl mx-auto px-5 h-16 flex items-center justify-between gap-4">
        <Link href="/" className="flex items-center gap-2 font-bold text-lg">
          <span className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-500 flex items-center justify-center text-white text-sm">
            A
          </span>
          <span>{brand}</span>
        </Link>
        <nav className="hidden md:flex items-center gap-6 text-sm text-[color:var(--muted)]">
          <a href="#features" className="hover:text-[color:var(--fg)]">Features</a>
          <a href="#runtimes" className="hover:text-[color:var(--fg)]">Runtimes</a>
          <a href="#plans" className="hover:text-[color:var(--fg)]">Plans</a>
          <a href="#faq" className="hover:text-[color:var(--fg)]">FAQ</a>
        </nav>
        <div className="flex items-center gap-2">
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => {
              toggleTheme();
              setDark(!document.documentElement.classList.contains("light"));
            }}
            aria-label="Toggle theme"
          >
            {isDark ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => setLocale(locale === "en" ? "ar" : "en")}
            aria-label="Toggle language"
          >
            <Languages size={16} />
            <span className="hidden sm:inline">{locale === "en" ? "AR" : "EN"}</span>
          </button>
          {me ? (
            <Link href={me.role !== "USER" ? "/admin" : "/dashboard"} className="btn btn-primary btn-sm">
              Dashboard
            </Link>
          ) : (
            <>
              <Link href="/login" className="btn btn-ghost btn-sm hidden sm:inline-flex">
                Log in
              </Link>
              <Link href="/register" className="btn btn-primary btn-sm">
                Get started
              </Link>
            </>
          )}
          <button
            className="btn btn-ghost btn-sm md:hidden"
            onClick={() => setOpen((o) => !o)}
            aria-label="Menu"
          >
            {open ? <X size={16} /> : <Menu size={16} />}
          </button>
        </div>
      </div>
      {open && (
        <div className="md:hidden border-t border-[color:var(--border)] px-5 py-3 flex flex-col gap-3 text-sm">
          <a href="#features" onClick={() => setOpen(false)}>Features</a>
          <a href="#runtimes" onClick={() => setOpen(false)}>Runtimes</a>
          <a href="#plans" onClick={() => setOpen(false)}>Plans</a>
          <a href="#faq" onClick={() => setOpen(false)}>FAQ</a>
          {!me && <Link href="/login" onClick={() => setOpen(false)}>Log in</Link>}
        </div>
      )}
    </header>
  );
}
