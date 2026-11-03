/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Core surfaces
        base: "#0b0d11",
        surface: "#13171f",
        elevated: "#1a202c",
        terminal: "#07080a",

        // Explicit prefixed aliases
        "bg-base": "#0b0d11",
        "bg-surface": "#13171f",
        "bg-elevated": "#1a202c",
        "bg-terminal": "#07080a",

        // Borders
        subtle: "#202736",
        "border-subtle": "#202736",
        "border-focus": "#3b82f6",

        // Gate & alerts
        gate: "#f59e0b",
        "accent-gate": "#f59e0b",

        // Engine phase accents (nested & flat)
        phase: {
          plan: "#818cf8",
          execute: "#38bdf8",
          self_verify: "#c084fc",
          review: "#a78bfa",
          repair: "#f43f5e",
          final_review: "#3b82f6",
          roadmap_check: "#34d399",
        },
        "phase-plan": "#818cf8",
        "phase-execute": "#38bdf8",
        "phase-self_verify": "#c084fc",
        "phase-review": "#a78bfa",
        "phase-repair": "#f43f5e",
        "phase-final_review": "#3b82f6",
        "phase-roadmap_check": "#34d399",
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "Fira Code",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};
