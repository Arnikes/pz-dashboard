# Bundled fonts

All WOFF2 files are stored in the repository and copied into the Docker image.
The browser loads them from the panel's own server, without Google Fonts or a CDN.
Shared `@font-face` rules live in `fonts.css`, loaded before `style.css`.

| Family | Weights | Character sets | License |
| --- | --- | --- | --- |
| Golos Text | Variable 400–900 | Latin, Cyrillic | [SIL OFL 1.1](golostext-OFL.txt) |
| JetBrains Mono | Variable 400–800 | Latin, Cyrillic | [SIL OFL 1.1](jetbrainsmono-OFL.txt) |

Each family uses one variable font per character set. The `400` filenames supply
every declared weight; separate files for 500, 600 and 700 are unnecessary.
Preserve the license copies alongside the fonts.

Font sources and license provenance:
[Golos Text](https://github.com/google/fonts/tree/main/ofl/golostext),
[JetBrains Mono](https://github.com/google/fonts/tree/main/ofl/jetbrainsmono).
These are reference links; the running application does not request them.

`tests/browser/test_bundled_assets.py` blocks external requests, opens all application
surfaces, and verifies actual local font loading with Latin and Cyrillic samples.
Do not add external imports, font URLs, CDN scripts, or preconnects for UI assets.
