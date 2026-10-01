# Changelog

## 1.2.0

- Much better tracing of small images such as logos from websites: anti-aliased images under about 1000 pixels are enlarged internally first, so thin lines, small text and letter holes survive instead of turning into zigzags or disappearing.
- Anti-aliased images are no longer blurred (that broke hair-thin lines); hard-edged images are still smoothed.
- **Auto** mode now detects images with several colors and traces each color separately, and *Trace Image to Mesh* creates one object per color by default.
- Safer corner sharpening on short edges (no more spikes).

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
