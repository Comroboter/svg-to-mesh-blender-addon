# Changelog

## 1.1.1

- New logo, also shown in the sidebar panel header and the import menu.
- New README with an animated preview, badges and images made with the add-on itself.

## 1.1.0

- New sidebar section **Depth per Object** (collapsed by default):
  - **Terrace by Order** stacks the colors of a logo by paint order (no AI, works offline).
  - **Suggest with AI** (optional) lets Claude suggest a height for every color based on what the artwork shows. Needs an Anthropic API key in the add-on preferences.
  - **Re-apply** rescales stored heights to a new base depth.
- Imported objects remember their paint order and color.

## 1.0.1

- Protection against malicious SVG files (`<use>` expansion bombs, extreme nesting).
- Overflowing numbers no longer remove the other shapes of a file.
- *Curves to Clean Mesh* keeps the original object if nothing could be built.
- Image tracing is about 4x faster, files with many separate shapes about 2x faster.

## 1.0.0

- First release: SVG import as clean mesh, image tracing, curve/text conversion, four topologies, boolean helper.
