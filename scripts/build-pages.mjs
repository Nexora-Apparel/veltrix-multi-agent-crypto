import { cp, mkdir, rm } from "node:fs/promises";
import { resolve } from "node:path";

const source = resolve("veltrix/dashboard_assets");
const output = resolve("dist");
const assets = resolve(output, "assets");

await rm(output, { recursive: true, force: true });
await mkdir(assets, { recursive: true });
await cp(resolve(source, "index.html"), resolve(output, "index.html"));
await cp(resolve(source, "dashboard.css"), resolve(assets, "dashboard.css"));
await cp(resolve(source, "dashboard.js"), resolve(assets, "dashboard.js"));
await cp(resolve(source, "_headers"), resolve(output, "_headers"));
console.log("Built Cloudflare Pages bundle from the canonical dashboard assets.");
