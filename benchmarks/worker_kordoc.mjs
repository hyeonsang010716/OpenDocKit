// One conversion worker for kordoc, a Node library. It speaks the lines worker.py speaks: a JSON request per line on the
// standard input ({"path", "out"}), a JSON reply per line on the standard output.
//
//     node worker_kordoc.mjs <folder holding node_modules/kordoc>
//
// kordoc is called through its documented entry point with default options: `parse(path)`, and `markdown` of the result.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const home = process.argv[2];
const send = process.stdout.write.bind(process.stdout);
// anything the library prints goes to the standard error, never into the protocol
process.stdout.write = process.stderr.write.bind(process.stderr);

const started = performance.now();
const manifest = JSON.parse(readFileSync(join(home, "node_modules", "kordoc", "package.json"), "utf8"));
const entry = manifest.exports?.["."]?.import ?? manifest.module ?? manifest.main;
const { parse } = await import(pathToFileURL(join(home, "node_modules", "kordoc", entry)).href);
send(JSON.stringify({ ready: true, version: manifest.version, import_seconds: (performance.now() - started) / 1000 }) + "\n");

for await (const line of createInterface({ input: process.stdin })) {
  if (!line.trim()) continue;
  const request = JSON.parse(line);
  const reply = { path: request.path };
  const began = performance.now();
  try {
    const result = await parse(request.path);
    if (result.success) {
      const text = result.markdown ?? "";
      reply.ok = true;
      reply.chars = text.trim().length;
      if (request.out) writeFileSync(request.out, text, "utf8");
    } else {
      reply.ok = false;
      reply.error = String(result.error ?? "failed").slice(0, 300);
    }
  } catch (error) {
    reply.ok = false;
    reply.error = `${error?.name ?? "Error"}: ${error?.message ?? error}`.slice(0, 300);
  }
  reply.seconds = (performance.now() - began) / 1000;
  reply.peak_rss_mb = process.resourceUsage().maxRSS / 1024; // Linux reports kilobytes
  send(JSON.stringify(reply) + "\n");
}
