module.exports = {
  content: [
    "./backend/app/templates/admin/**/*.html",
    "./backend/app/static/js/admin_tables.js",
    "./backend/app/static/js/mobile_shell.js",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef2ff",
          100: "#e0e7ff",
          500: "#6b74f0",
          600: "#5d67df",
          700: "#4b56c9",
        },
      },
      fontFamily: {
        sans: ["Inter", "sans-serif"],
        display: ["Be Vietnam Pro", "sans-serif"],
        mono: ["IBM Plex Mono", "monospace"],
      },
    },
  },
  safelist: [
    "hidden",
    "pointer-events-none",
    "opacity-60",
    "bg-slate-50",
    "text-slate-500",
    "border",
    "border-emerald-200",
    "border-rose-200",
    "border-sky-200",
    "border-amber-200",
    "bg-emerald-50",
    "bg-rose-50",
    "bg-sky-50",
    "bg-amber-50",
    "text-emerald-800",
    "text-rose-800",
    "text-sky-800",
    "text-amber-800",
  ],
};
