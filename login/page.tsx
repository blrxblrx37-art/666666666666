"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Input, Label } from "@/components/ui";
import { Providers } from "@/components/providers";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setLoading(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Login failed");
      router.push(data.role !== "USER" ? "/admin" : "/dashboard");
      router.refresh();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Providers>
      <div className="min-h-screen flex items-center justify-center p-5">
        <div className="w-full max-w-md">
          <Link href="/" className="flex items-center gap-2 justify-center mb-8 font-bold">
            <span className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-500 flex items-center justify-center text-white text-sm">A</span>
            ArenaHost
          </Link>
          <div className="card p-7">
            <h1 className="text-xl font-semibold">Welcome back</h1>
            <p className="text-sm text-[color:var(--muted)] mt-1">
              Sign in to your account
            </p>
            <form onSubmit={onSubmit} className="mt-6 space-y-4">
              <div>
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <div>
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
              {err && (
                <div className="text-sm text-red-400 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2">
                  {err}
                </div>
              )}
              <Button type="submit" className="w-full" loading={loading}>
                Sign in
              </Button>
            </form>
            <div className="text-center text-sm text-[color:var(--muted)] mt-6">
              Don&apos;t have an account?{" "}
              <Link href="/register" className="text-indigo-400 hover:underline">
                Sign up
              </Link>
            </div>
          </div>
        </div>
      </div>
    </Providers>
  );
}
