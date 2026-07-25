/**
 * Tailwind build config. Run with CWD = this directory (app/), e.g.
 *   npx tailwindcss@3.4.10 -i static/input.css -o static/dist/tailwind.css \
 *       --minify --config tailwind.config.js
 *
 * `content` globs resolve relative to the CWD, which is why both build paths
 * (app/Dockerfile and scripts/build-tailwind.sh) cd into app/ first.
 */
const theme = require("./static/tailwind-theme.js");

module.exports = {
  content: ["./templates/**/*.html"],
  ...theme,
};
