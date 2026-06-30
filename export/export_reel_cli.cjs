#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { exportReel } = require("./media.cjs");

async function main() {
  const [sourcePath, payloadPath, outputPath] = process.argv.slice(2);
  if (!sourcePath || !payloadPath || !outputPath) {
    console.error("Usage: node export_reel_cli.cjs <source> <payload.json> <output.mp4>");
    process.exit(1);
  }
  const payload = JSON.parse(fs.readFileSync(path.resolve(payloadPath), "utf8"));
  await exportReel(path.resolve(sourcePath), path.resolve(outputPath), payload);
}

main().catch((error) => {
  console.error(error?.message || error);
  process.exit(1);
});
