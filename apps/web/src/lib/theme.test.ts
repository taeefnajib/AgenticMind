import { describe, expect, it } from "vitest";

import { portColor } from "./theme";
import { NEXT_THEME, resolveTheme } from "./useTheme";

describe("theme", () => {
  it("resolves the system preference", () => {
    expect(resolveTheme("system", "dark")).toBe("dark");
    expect(resolveTheme("system", "light")).toBe("light");
    expect(resolveTheme("light", "dark")).toBe("light");
    expect(resolveTheme("dark", "light")).toBe("dark");
  });

  it("cycles light -> dark -> system -> light", () => {
    expect(NEXT_THEME.light).toBe("dark");
    expect(NEXT_THEME.dark).toBe("system");
    expect(NEXT_THEME.system).toBe("light");
  });

  it("uses lighter port colours in dark mode", () => {
    expect(portColor("String")).toBe("#4f7fd0");
    expect(portColor("String", true)).toBe("#8eaeea");
    expect(portColor("Unknown", true)).toBe("#a4a8b4");
  });
});
