// Keep the catalogues complete and clean: every string the interface passes
// to t(), tn() or msg() must have a translation in each catalogue, and no
// catalogue may keep a string the interface no longer uses.
//
//   npm run i18n:check            report and fail on missing or unused keys
//   npm run i18n:check -- --fix   add missing keys (empty, to be translated)
//                                 and drop unused ones, sorted
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";

const root = new URL("../src/", import.meta.url).pathname;
const localesDir = join(root, "i18n", "locales");
const fix = process.argv.includes("--fix");

function* sources(directory) {
  for (const name of readdirSync(directory)) {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) {
      if (name !== "i18n") yield* sources(path); // the runtime documents itself with examples
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) yield path;
  }
}

const STRING = String.raw`"((?:[^"\\]|\\.)*)"`;
const patterns = [
  new RegExp(String.raw`\b(?:t|msg)\(\s*${STRING}`, "g"),
  new RegExp(String.raw`\btn\(\s*[^,()]+,\s*${STRING}\s*,\s*${STRING}`, "g"),
];
const badCalls = new RegExp("\\b(?:t|tn|msg)\\(\\s*`", "g");

const keys = new Map(); // key -> first file
const problems = [];
for (const path of sources(root)) {
  const text = readFileSync(path, "utf8");
  const file = relative(root, path);
  for (const match of text.matchAll(badCalls)) {
    const line = text.slice(0, match.index).split("\n").length;
    problems.push(`${file}:${line}: a template literal cannot be a translation key`);
  }
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      const key = JSON.parse(`"${match[match.length - 1]}"`);
      if (!keys.has(key)) keys.set(key, file);
    }
  }
}

let failed = problems.length > 0;
for (const problem of problems) console.error(problem);

for (const name of readdirSync(localesDir).filter((item) => item.endsWith(".json")).sort()) {
  const path = join(localesDir, name);
  const catalog = JSON.parse(readFileSync(path, "utf8"));
  const missing = [...keys.keys()].filter((key) => !(key in catalog));
  const unused = Object.keys(catalog).filter((key) => !keys.has(key));
  const empty = Object.entries(catalog).filter(([, value]) => value === "").map(([key]) => key);
  if (fix) {
    const next = {};
    for (const key of [...keys.keys()].sort((a, b) => a.localeCompare(b, "en"))) {
      next[key] = key in catalog ? catalog[key] : "";
    }
    writeFileSync(path, JSON.stringify(next, null, 2) + "\n");
    console.log(`${name}: ${missing.length} added, ${unused.length} removed, ${empty.length + missing.length} left to translate`);
    if (empty.length + missing.length) failed = true;
    continue;
  }
  for (const key of missing) console.error(`${name}: missing ${JSON.stringify(key)} (${keys.get(key)})`);
  for (const key of unused) console.error(`${name}: unused ${JSON.stringify(key)}`);
  for (const key of empty) console.error(`${name}: untranslated ${JSON.stringify(key)}`);
  if (missing.length || unused.length || empty.length) failed = true;
  console.log(`${name}: ${Object.keys(catalog).length} strings, ${missing.length} missing, ${unused.length} unused, ${empty.length} untranslated`);
}
console.log(`${keys.size} strings in the interface`);
process.exit(failed ? 1 : 0);
