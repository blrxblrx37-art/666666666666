import type { NextRequest } from "next/server";
import { gatewayHandler, type GatewayCtx } from "@/lib/gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const handle = (req: NextRequest, ctx: GatewayCtx) => gatewayHandler(req, ctx);

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const HEAD = handle;
export const OPTIONS = handle;
