import colors from "tailwindcss/colors";
import plugin from "tailwindcss/plugin";

/**
 * Theming
 * -------
 * Every palette used by the UI is defined through CSS variables (RGB triplets). Light mode uses the
 * normal scale; dark mode (`.dark` on <html>) swaps each shade for its mirror (50↔950, 100↔900, …).
 * Existing classes such as `bg-slate-50` or `text-red-700` therefore adapt automatically, without a
 * `dark:` variant on every element. `surface` is the panel/card background.
 *
 * Neutrals use a violet-tinted "ink" scale and the brand colours come from the logo gradient.
 */

const INK = {
  50: "#f8f7fc",
  100: "#f1eff8",
  200: "#e3e0ee",
  300: "#c9c4da",
  400: "#9c95b3",
  500: "#6f6888",
  600: "#524b69",
  700: "#3c3651",
  800: "#241f35",
  900: "#110d1d",
  950: "#08060f",
};

export const BRAND = {
  violet: "#6b1bfd",
  purple: "#8a1dfc",
  magenta: "#fd22c4",
  pink: "#fd2079",
  red: "#fd3536",
  orange: "#fd4216",
};

const SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
const MIRROR = { 50: 950, 100: 900, 200: 800, 300: 700, 400: 600, 500: 500, 600: 400, 700: 300, 800: 200, 900: 100, 950: 50 };

const PALETTES = {
  slate: INK,
  red: colors.red,
  amber: colors.amber,
  emerald: colors.emerald,
  sky: colors.sky,
  violet: colors.violet,
  indigo: colors.indigo,
  teal: colors.teal,
  rose: colors.rose,
  fuchsia: colors.fuchsia,
  pink: colors.pink,
  orange: colors.orange,
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
  const light = { "--surface": "255 255 255" };
  const dark = { "--surface": "22 18 36" };
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
        brand: BRAND,
      },
      backgroundImage: {
        "brand-gradient": `linear-gradient(100deg, ${BRAND.violet} 0%, ${BRAND.purple} 22%, ${BRAND.magenta} 50%, ${BRAND.pink} 70%, ${BRAND.orange} 100%)`,
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "Consolas", "monospace"],
      },
    },
  },
  plugins: [themeVariables],
};
