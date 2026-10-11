/** The app's colour themes, in Claude's palette: warm ivory, slate and charcoal with terracotta
 *  (book cloth), kraft and manilla, colour-blind-friendly variants (including the research
 *  app's dark and light pair) and high contrast. The colours
 *  themselves live in `index.css` (`[data-theme]` blocks); this list names them for the picker.
 *  The choice is kept in this browser until the user changes it or resets to the default. */

export type Scheme = "light" | "dark";

export interface Theme {
  id: string;
  label: string;
  scheme: Scheme;
  /** Background, panel and accent, for the picker's swatch. */
  swatch: [string, string, string];
}

export const THEMES: Theme[] = [
  { id: "claude-light", label: "Claude Light", scheme: "light", swatch: ["#F0EFEA", "#FAF9F5", "#C6613F"] },
  { id: "claude-dark", label: "Claude Dark", scheme: "dark", swatch: ["#1F1E1D", "#262624", "#D97757"] },
  { id: "manilla", label: "Manilla", scheme: "light", swatch: ["#EFE6D2", "#FAF5EA", "#B35C3D"] },
  { id: "kraft", label: "Kraft", scheme: "dark", swatch: ["#221C17", "#2B241E", "#D4A27F"] },
  { id: "cloud", label: "Cloud", scheme: "light", swatch: ["#EEEEEC", "#FBFBFA", "#2F7FD3"] },
  { id: "slate", label: "Slate", scheme: "dark", swatch: ["#161719", "#1E1F22", "#61AAF2"] },
  { id: "sage", label: "Sage", scheme: "light", swatch: ["#ECEFE8", "#F9FAF6", "#5E7D45"] },
  { id: "colorblind-light", label: "Light, colour-blind friendly", scheme: "light", swatch: ["#F0EFEA", "#FAF9F5", "#A6427E"] },
  { id: "colorblind-dark", label: "Dark, colour-blind friendly", scheme: "dark", swatch: ["#1F1E1D", "#262624", "#CC79A7"] },
  { id: "contrast-dark", label: "High contrast", scheme: "dark", swatch: ["#000000", "#0A0A0A", "#FFB38A"] },
  { id: "charcoal", label: "Charcoal", scheme: "dark", swatch: ["#1A1A1A", "#2D2D2D", "#D97757"] },
  { id: "charcoal-colorblind-dark", label: "Charcoal dark, colour-blind friendly", scheme: "dark", swatch: ["#1A1A1A", "#2D2D2D", "#CC79A7"] },
  { id: "charcoal-colorblind-light", label: "Charcoal light, colour-blind friendly", scheme: "light", swatch: ["#E9E9E9", "#DEDEDE", "#A6427E"] },
  { id: "research-dark-cb", label: "Research dark, colour-blind friendly", scheme: "dark", swatch: ["#17181b", "#1e2024", "#79a8f2"] },
  { id: "research-light-cb", label: "Research light, colour-blind friendly", scheme: "light", swatch: ["#fafafa", "#f0f1f3", "#2b62b8"] },
];

export const DEFAULT_THEME = "claude-light";
const KEY = "jobsearch.theme.v3";
const OLD_KEY = "jobsearch.theme.v2"; // "light" | "dark" before the themes existed

export function themeById(id: string | null): Theme {
  return THEMES.find((t) => t.id === id) ?? THEMES.find((t) => t.id === DEFAULT_THEME)!;
}

/** The saved theme (the old light/dark choice carries over), or the default. */
export function loadTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved) return themeById(saved);
    const old = localStorage.getItem(OLD_KEY);
    if (old === "dark") return themeById("claude-dark");
  } catch {
    /* storage unavailable: the default theme */
  }
  return themeById(DEFAULT_THEME);
}

/** Show `theme` (the page's palette and light/dark scheme). */
export function showTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme.id;
  document.documentElement.dataset.scheme = theme.scheme;
}

/** Show `theme` and remember it until the user changes or resets it. */
export function chooseTheme(theme: Theme): void {
  showTheme(theme);
  try {
    localStorage.setItem(KEY, theme.id);
    localStorage.removeItem(OLD_KEY);
  } catch {
    /* the choice just isn't remembered */
  }
}

/** Forget the choice and show the default theme. */
export function resetTheme(): Theme {
  try {
    localStorage.removeItem(KEY);
    localStorage.removeItem(OLD_KEY);
  } catch {
    /* nothing was stored */
  }
  const theme = themeById(DEFAULT_THEME);
  showTheme(theme);
  return theme;
}
