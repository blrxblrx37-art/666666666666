import { db } from "@/db";
import { ensureBootstrap } from "@/lib/bootstrap";
import { sql } from "drizzle-orm";

export async function GET() {
  try {
    await ensureBootstrap();
    await db.execute(sql`SELECT 1`);
    return Response.json({
      ok: true,
      service: "arenahost",
      time: new Date().toISOString(),
    });
  } catch (e) {
    return Response.json(
      { ok: false, error: e instanceof Error ? e.message : String(e) },
      { status: 503 }
    );
  }
}
