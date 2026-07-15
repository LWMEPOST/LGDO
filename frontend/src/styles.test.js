import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterAll, describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");
const styleElement = document.createElement("style");
styleElement.textContent = styles;
document.head.append(styleElement);

const sheet = styleElement.sheet;
if (!sheet) throw new Error("Unable to parse styles.css");

afterAll(() => styleElement.remove());

function mediaMatchesWidth(conditionText, width) {
  const constraints = [...conditionText.matchAll(/\((min|max)-width:\s*(\d+(?:\.\d+)?)px\)/g)];
  if (!constraints.length) return false;

  return constraints.every(([, bound, rawWidth]) => {
    const breakpoint = Number(rawWidth);
    return bound === "min" ? width >= breakpoint : width <= breakpoint;
  });
}

function hasExactSelector(selectorText, selector) {
  return selectorText.split(",").some((candidate) => candidate.trim() === selector);
}

function declarationsAt(selector, width) {
  const declarations = {};

  function visit(rules) {
    for (const rule of rules) {
      if (rule instanceof CSSMediaRule) {
        if (mediaMatchesWidth(rule.conditionText, width)) visit(rule.cssRules);
        continue;
      }
      if (!(rule instanceof CSSStyleRule) || !hasExactSelector(rule.selectorText, selector)) continue;

      for (let index = 0; index < rule.style.length; index += 1) {
        const property = rule.style.item(index);
        declarations[property] = rule.style.getPropertyValue(property).trim();
      }
    }
  }

  visit(sheet.cssRules);
  return declarations;
}

const mobile = (selector) => declarationsAt(selector, 390);
const desktop = (selector) => declarationsAt(selector, 1440);

describe("responsive wiki styles", () => {
  it("uses one mobile topbar track and a wrapping action row", () => {
    expect(mobile(".wiki-topbar")["grid-template-columns"]).toBe("minmax(0, 1fr)");
    expect(mobile(".topbar-actions").width).toBe("100%");
    expect(mobile(".topbar-actions")["flex-wrap"]).toBe("wrap");
    expect(mobile(".sync-state")["white-space"]).toBe("normal");
  });

  it("uses a compact horizontally scrollable primary navigation", () => {
    expect(mobile(".nav-list").display).toBe("flex");
    expect(mobile(".nav-list")["overflow-x"]).toBe("auto");
    expect(mobile(".nav-item").flex).toBe("0 0 auto");
    expect(mobile(".nav-item").width).toBe("auto");
  });

  it("shows only the compact space disclosure on mobile", () => {
    expect(mobile(".space-tree").display).toBe("none");
    expect(mobile(".mobile-space-filter").display).toBe("grid");
    expect(desktop(".mobile-space-filter").display).toBe("none");
    expect(mobile(".mobile-space-directory")["overflow-y"]).toBe("auto");
    expect(mobile(".mobile-space-directory")["max-height"]).toBe("min(44vh, 360px)");
    expect(mobile(".mobile-space-disclosure > summary:focus-visible")["outline-offset"]).toBe("2px");
  });

  it("provides 44px mobile touch targets", () => {
    expect(mobile(".mobile-space-disclosure > summary")["min-height"]).toBe("44px");
    expect(mobile(".nav-item")["min-height"]).toBe("44px");
    expect(mobile(".mobile-space-directory .tree-item")["min-height"]).toBe("44px");
    expect(mobile(".mobile-clear-space-filter")["min-height"]).toBe("44px");
  });

  it("stacks overview panels in one mobile column", () => {
    expect(mobile(".overview-workspace")["grid-template-columns"]).toBe("minmax(0, 1fr)");
  });

  it("keeps the desktop navigation and topbar sizing", () => {
    expect(desktop(".nav-item")["min-height"]).toBe("48px");
    expect(desktop(".wiki-topbar")["grid-template-columns"])
      .toBe("292px minmax(320px, 660px) minmax(250px, 1fr)");
  });
});
