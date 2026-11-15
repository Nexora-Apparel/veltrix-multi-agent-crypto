import { createClient } from "@libsql/client/web";

const url = String(process.env.TURSO_DATABASE_URL || "").trim();
const authToken = String(process.env.TURSO_READONLY_AUTH_TOKEN || "").trim();

if (!url || !authToken) {
  throw new Error("TURSO_DATABASE_URL and TURSO_READONLY_AUTH_TOKEN are required");
}
const parsed = new URL(url);
if (parsed.protocol !== "libsql:" || !parsed.hostname.endsWith(".turso.io") ||
    parsed.hostname === "veltrix-h0-conformance-test-nexora-apparel.aws-eu-central-1.turso.io" ||
    parsed.username || parsed.password || parsed.search || parsed.hash ||
    !["", "/"].includes(parsed.pathname)) {
  throw new Error("A separate production libsql://*.turso.io endpoint is required");
}

const client = createClient({ url, authToken });
let transaction;
try {
  await client.execute("SELECT 1 FROM account WHERE id = 1");
  transaction = await client.transaction("write");
  await transaction.execute({
    sql: "UPDATE account SET cash = cash WHERE id = -1",
  });
  await transaction.rollback();
  transaction = null;
  throw new Error("Dashboard database token is write-capable; a read-only token is required");
} catch (error) {
  if (transaction) {
    try { await transaction.rollback(); } catch { /* transaction may already be aborted */ }
  }
  const message = String(error?.message || error);
  if (/read.?only|permission|forbidden|not authorized|unauthorized/i.test(message)) {
    console.log("Verified: production dashboard token cannot perform a write statement.");
  } else {
    throw new Error("Could not verify the production dashboard token as read-only");
  }
} finally {
  await client.close();
}
