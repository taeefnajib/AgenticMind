import colors from "tailwindcss/colors";
import plugin from "tailwindcss/plugin";

/**
 * Theming
 * -------
 * Calm, neutral UI with one soft purple accent. Every palette is defined through CSS variables (RGB
 * triplets). Light mode uses the normal scale; dark mode (`.dark` on <html>) swaps each shade for its
 * mirror (50↔950, 100↔900, …), so classes like `bg-slate-50` or `text-accent-700` adapt automatically.
 *
 * - `slate`   cool neutral greys (dark mode ≈ #191c24 canvas, #20232b panels)
 * - `accent`  soft purple used sparingly: primary actions, selection, focus, active tabs
 * - `surface` panel / card background; `primary` the solid primary-button colour per theme
 */

const NEUTRAL = {
  50: "#f7f8fa",
  100: "#f0f1f4",
  200: "#e4e6eb",
  300: "#cfd2d9",
  400: "#9ea2ad",
  500: "#6f7381",
  600: "#545866",
  700: "#3e414c",
  800: "#2c2f38",
  900: "#191c24",
  950: "#111318",
};

const ACCENT = {
  50: "#f5f3fe",
  100: "#ece8fd",
  200: "#dbd3fb",
  300: "#c0b2f6",
  400: "#a18ef0",
  500: "#8770e6",
  600: "#7158d6",
  700: "#5d47b6",
  800: "#4b3a92",
  900: "#3a2f6e",
  950: "#251e46",
};

const SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
const MIRROR = { 50: 950, 100: 900, 200: 800, 300: 700, 400: 600, 500: 500, 600: 400, 700: 300, 800: 200, 900: 100, 950: 50 };

const PALETTES = {
  slate: NEUTRAL,
  accent: ACCENT,
  red: colors.red,
  amber: colors.amber,
  emerald: colors.emerald,
  sky: colors.sky,
  violet: colors.violet,
  indigo: colors.indigo,
  teal: colors.teal,
  rose: colors.rose,
};

const rgb = (hex) => {
  const h = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(" ");
};

const themedColors = Object.fromEntries(
  Object.keys(PALETTES).map((name) => [
    name,
    Object.fromEntries(SHADES.map((s) => [s, `rgb(var(--c-${name}-${s}) / <alpha-value>)`])),
  ]),
);

const themeVariables = plugin(({ addBase }) => {
  const light = { "--surface": "255 255 255", "--primary": rgb(ACCENT[600]), "--primary-hover": rgb(ACCENT[700]) };
  const dark = { "--surface": "32 35 43", "--primary": rgb(ACCENT[500]), "--primary-hover": rgb(ACCENT[400]) };
  for (const [name, palette] of Object.entries(PALETTES)) {
    for (const s of SHADES) {
      light[`--c-${name}-${s}`] = rgb(palette[s]);
      dark[`--c-${name}-${s}`] = rgb(palette[MIRROR[s]]);
    }
  }
  addBase({ ":root": light, ".dark": dark });
});

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ...themedColors,
        surface: "rgb(var(--surface) / <alpha-value>)",
        primary: {
          DEFAULT: "rgb(var(--primary) / <alpha-value>)",
          hover: "rgb(var(--primary-hover) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "Consolas", "monospace"],
      },
    },
  },
  plugins: [themeVariables],
};
