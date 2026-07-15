import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

function extractBlock(source, marker) {
  const markerIndex = source.indexOf(marker);
  if (markerIndex < 0) return "";
  const openBrace = source.indexOf("{", markerIndex);
  let depth = 0;

  for (let index = openBrace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(openBrace + 1, index);
  }

  throw new Error(`Unclosed CSS block: ${marker}`);
}

const calmRefresh = styles.slice(styles.indexOf("/* Workbench Calm refresh */"));
const mobileStyles = extractBlock(calmRefresh, "@media (max-width: 760px)");

describe("mobile wiki layout styles", () => {
  it("uses one topbar track and lets the actions occupy a wrapping row", () => {
    expect(extractBlock(mobileStyles, ".wiki-topbar")).toMatch(
      /grid-template-columns:\s*minmax\(0,\s*1fr\);/,
    );
    expect(extractBlock(mobileStyles, ".topbar-actions")).toMatch(/width:\s*100%;/);
    expect(extractBlock(mobileStyles, ".topbar-actions")).toMatch(/flex-wrap:\s*wrap;/);
    expect(extractBlock(mobileStyles, ".sync-state")).toMatch(/white-space:\s*normal;/);
  });

  it("uses a compact horizontally scrollable primary navigation", () => {
    const navList = extractBlock(mobileStyles, ".nav-list");
    expect(navList).toMatch(/display:\s*flex;/);
    expect(navList).toMatch(/overflow-x:\s*auto;/);
    expect(extractBlock(mobileStyles, ".nav-item")).toMatch(/flex:\s*0\s+0\s+auto;/);
    expect(extractBlock(mobileStyles, ".nav-item")).toMatch(/width:\s*auto;/);
  });

  it("removes the space tree from the mobile flow", () => {
    expect(extractBlock(mobileStyles, ".space-tree")).toMatch(/display:\s*none;/);
  });
});
