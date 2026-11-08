/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    // FIX: Tells Tailwind to look everywhere inside your src root folder recursively
    "./src/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
