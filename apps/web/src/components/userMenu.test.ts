import { describe, expect, it } from "vitest";

import { initials } from "./UserMenu";

describe("initials", () => {
  it("uses first and last name, or the first two letters", () => {
    expect(initials("Ada Lovelace")).toBe("AL");
    expect(initials("  grace  brewster murray hopper ")).toBe("GH");
    expect(initials("ada")).toBe("AD");
    expect(initials("   ")).toBe("?");
  });
});
