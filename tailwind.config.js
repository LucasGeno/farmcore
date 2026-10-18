const v = (name) => `rgb(var(--${name}-rgb) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/farmcore/**/templates/**/*.html',
    './src/farmcore/**/*.py',
    './src/farmcore/**/static/*.js',
  ],
  // All class choices are complete literals in templates/Python/first-party JS, including
  // decision tones and border bands. No partial utility names are assembled; no safelist needed.
  theme: {extend: {
    // Tokens read CSS variables (r g b triplets in farmcore.css) so Sunlight mode can swap them.
    colors: {
      brand: {900: v('brand-900'), 700: v('brand-700'), 50: v('brand-50')},
      ground: v('ground'), surface: v('surface'), ink: v('ink'), muted: v('muted'), line: v('line'),
      spray: {DEFAULT: v('spray'), soft: v('spray-soft')},
      protected: {DEFAULT: v('protected'), soft: v('protected-soft'), ink: v('protected-ink')},
      wait: {DEFAULT: v('wait'), soft: v('wait-soft')},
    },
    fontFamily: {sans: ['DM Sans', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif']},
  }},
};
