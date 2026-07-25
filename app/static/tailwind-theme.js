/**
 * Single source of truth for the portal's Tailwind theme.
 *
 * Loaded two ways:
 *   - Node (build):    require()d by app/tailwind.config.js for the CLI build.
 *   - Browser (dev):   <script src> after the Tailwind Play CDN, which assigns
 *                      `tailwind.config` so the JIT compiler picks it up.
 *
 * Keeping both paths on this one file is deliberate: the theme used to live
 * only in an inline <script> in base.html that the CLI build never saw, so the
 * built stylesheet shipped with none of the brand or surface utilities while
 * the templates referenced them ~320 times.
 */
(function (root, factory) {
  var theme = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = theme;
  } else {
    // The Play CDN defines `window.tailwind` when it loads; assigning .config
    // afterwards is its documented configuration hook.
    root.tailwind = root.tailwind || {};
    root.tailwind.config = theme;
  }
})(typeof self !== "undefined" ? self : this, function () {
  return {
    darkMode: "class",
    theme: {
      extend: {
        fontFamily: {
          sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        },
        colors: {
          // 400/500/600 match Tailwind's orange ramp; 300 is orange-300 and is
          // required by the ~18 `hover:text-brand-300` usages in the templates.
          brand: {
            300: "#fdba74",
            400: "#fb923c",
            500: "#f97316",
            600: "#ea580c",
          },
          surface: {
            950: "#060a14",
            900: "#0d1117",
            800: "#161b27",
            700: "#1e2535",
            600: "#2a3347",
          },
        },
      },
    },
  };
});
