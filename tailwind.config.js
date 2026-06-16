/** Tailwind config — compiles web/static/tailwind.input.css → web/static/app.css.
 *  Replaces the dev-only cdn.tailwindcss.com runtime build that previously lived
 *  in base.html AND in every standalone template (landing, auth/*, errors/*).
 *
 *  theme.extend mirrors the inline tailwind.config that landing.html shipped to
 *  the CDN (Inter font + the full animation/keyframe set), so landing keeps its
 *  motion. Base/auth/error pages don't load Inter, so their `font-sans` falls
 *  back to system-ui exactly as before — no visual change.
 *
 *  Content globs scan every template (including inline JS class literals) so the
 *  JIT purge keeps all classes actually used. No dynamically concatenated class
 *  names exist in the codebase, so purge is safe.
 */
module.exports = {
  darkMode: 'class',
  content: [
    './web/templates/**/*.html',
    './web/static/**/*.js',
  ],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
      animation: {
        'float': 'float 7s ease-in-out infinite',
        'float-delayed': 'float 7s ease-in-out 3.5s infinite',
        'pulse-slow': 'pulse-slow 2.5s ease-in-out infinite',
        'gradient-shift': 'gradient-shift 8s ease infinite',
        'shimmer': 'shimmer 2.5s linear infinite',
        'spin-slow': 'spin 12s linear infinite',
        'bounce-soft': 'bounce-soft 2s ease-in-out infinite',
        'slide-up': 'slide-up 0.6s ease forwards',
        'fade-in': 'fade-in 0.8s ease forwards',
        'counter-up': 'counter-up 0.4s ease forwards',
        'orb1': 'orb1 14s ease-in-out infinite',
        'orb2': 'orb2 18s ease-in-out infinite',
        'orb3': 'orb3 11s ease-in-out infinite',
      },
      keyframes: {
        'float': {
          '0%,100%': { transform: 'translateY(0px) rotate(0deg)' },
          '50%': { transform: 'translateY(-10px) rotate(0.5deg)' },
        },
        'pulse-slow': {
          '0%,100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.5', transform: 'scale(0.95)' },
        },
        'shimmer': {
          '0%': { backgroundPosition: '-200% center' },
          '100%': { backgroundPosition: '200% center' },
        },
        'bounce-soft': {
          '0%,100%': { transform: 'translateY(0)' },
          '50%': { transform: 'translateY(-6px)' },
        },
        'slide-up': {
          '0%': { opacity: '0', transform: 'translateY(24px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'orb1': {
          '0%,100%': { transform: 'translate(0,0) scale(1)' },
          '33%': { transform: 'translate(80px,-60px) scale(1.15)' },
          '66%': { transform: 'translate(-50px,40px) scale(0.9)' },
        },
        'orb2': {
          '0%,100%': { transform: 'translate(0,0) scale(1)' },
          '33%': { transform: 'translate(-70px,50px) scale(1.1)' },
          '66%': { transform: 'translate(60px,-70px) scale(0.85)' },
        },
        'orb3': {
          '0%,100%': { transform: 'translate(0,0) scale(1)' },
          '50%': { transform: 'translate(40px,60px) scale(1.2)' },
        },
      },
    },
  },
  plugins: [],
};
