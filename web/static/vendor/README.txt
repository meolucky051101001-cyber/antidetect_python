Vendored dashboard assets (no runtime CDN dependency)

- tailwind.js: Play CDN build downloaded from https://cdn.tailwindcss.com on
  2026-10-23. Tailwind CSS is MIT-licensed.
- lucide.min.js: Lucide UMD build downloaded from
  https://unpkg.com/lucide@latest/dist/umd/lucide.min.js on 2026-10-23.
  Lucide is ISC-licensed.

These files are served locally at /static/vendor/ so the Content-Security-Policy
does not need to allow third-party script hosts. The template still uses inline
handlers/scripts, so script-src keeps 'unsafe-inline' as documented technical
debt.
