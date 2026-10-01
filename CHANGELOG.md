# Changelog

## 1.4.0

- **Colorful SVGs import in color by default.** The new *Objects: Auto* setting creates one object per color when the artwork has several colors, and a single object for one-color logos.
- **Smarter white:** the new default *White: Background Only* removes white backgrounds (also where they show through letter holes) but keeps enclosed white such as eyes or a penguin's belly. *Always a Hole* and *Keep* are still available.
- Fixed missing pieces (for example a strip of the Tux belly) and rare open edges next to hair-thin triangles in *Per Color* objects.
- **Split into Parts** (sidebar, Depth per Object): splits objects into their separate parts so every part can get its own height; tiny parts stay together. *Split Parts First* does this before the AI suggestions.
- **AI suggestions** now also work with **OpenAI** (and OpenAI-compatible servers such as LM Studio or OpenRouter) and a local, free **Ollama**. The AI also gets a map of the regions, so parts of the same color can be told apart.
- The add-on preferences show an estimated cost per request for each Claude model.

## 1.3.1

- *Check for Updates* now reads the installed version correctly when the add-on is installed as an extension (Blender 4.2+); 1.3.0 always reported an update there.

## 1.3.0

- **Illustrations import much better** (for example the Linux penguin Tux):
  - Clipping paths (`clip-path`) are applied exactly.
  - Soft effects drawn with a strong blur (drop shadows, glows, highlights) are left out instead of becoming hard blobs (*Skip Effects*). Slightly soft-edged shapes stay.
  - Very transparent shading layers are left out (*Min Opacity*, default 50 %).
  - Gradients become the average of their colors instead of the first stop, and a fade to transparent counts as half transparent.
  - When an SVG with many colors is imported as a single object, a tip suggests *Objects: Per Color*.
- **AI suggestions:** a progress bar with a cancel button shows in the sidebar while Claude is answering.
- **AI suggestions:** fixed everything staying flat after scaling the imported objects. *Base Depth* now defaults to 0 = automatic (the current thickness of the selected objects); too small values give a warning.
- **Updates from GitHub:** *Check for Updates* in the sidebar and in the add-on preferences shows when a new release is available, and *Install Update* downloads and installs it (restart Blender afterwards). The download comes only from GitHub over HTTPS and is checked against GitHub's SHA-256 checksum. Nothing is checked automatically.

## 1.2.1

- Fix for Blender 5.2: with the default *Clean N-Gons* topology, holes could be filled and letters with holes (O, R, B ...) could disappear. Blender 5.2 changed how its triangulation reports holes; the add-on now decides holes itself, identically in every Blender version, and falls back to its own triangulation if the result does not match the exact area.
- *Triangles* mode no longer drops valid triangles on hair-thin shapes.
- The automatic tests now also run inside Blender 5.2 LTS, and releases require them to pass.

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
