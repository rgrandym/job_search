/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-scheme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        panel: "var(--bg-panel)",
        surface: "var(--bg-surface)",
        border: "var(--border)",
        fg: "var(--text-primary)",
        muted: "var(--text-secondary)",
        faint: "var(--text-muted)",
        accent: "var(--accent)",
        "accent-bg": "var(--accent-bg)",
        "accent-fg": "var(--accent-fg)",
        good: "var(--good)",
        warn: "var(--warn)",
        bad: "var(--bad)",
      },
      fontFamily: { sans: ["Inter", "system-ui", "sans-serif"] },
    },
  },
  plugins: [],
};
