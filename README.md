# SVG to Clean Mesh – Blender Add-on

Importiert **SVG-Dateien direkt als saubere Meshes** und verwandelt **Bilder (z. B. Logos) automatisch in Vektoren und Meshes** – mit Geometrie, die sich gut weiterverarbeiten lässt, vor allem für **Booleans** (Gravieren, Prägen, Ausschneiden).

![Vergleich: Blender-Standardweg vs. Add-on](docs/compare_svg.png)

*Links: Blenders Standardweg (SVG als Kurven importieren und dann „Convert to Mesh"): gestapelte, überlappende Ebenen, Splitter-Dreiecke, doppelte Vertices. Mitte/rechts: dieses Add-on, entweder als minimale N-Gons oder als gleichmäßiges Quad-Netz.*

## Das Problem

Blender importiert SVGs nur als Kurven. Wandelt man sie in Meshes um, entstehen:

- lange, dünne Splitter-Dreiecke und Dreiecksfächer,
- jede Form als eigene, übereinanderliegende Fläche (weiße Flächen werden als Geometrie mitimportiert statt als Loch),
- doppelte Vertices und offene, nicht-manifold Kanten,
- keine Dicke. Extrudiert man über die Kurve, gibt es oft kaputte Normalen.

Für Booleans ist das ungünstig: Der Exact-Solver wird langsam oder liefert Artefakte.

## Was das Add-on macht

| Funktion | Beschreibung |
|---|---|
| **Import SVG as Mesh** | Liest das SVG selbst (Pfade inkl. Bögen, Rechtecke, Kreise, Polygone, Gruppen, Transforms, CSS-Klassen, `<use>`, Strokes) und erzeugt direkt ein Mesh. |
| **Trace Image to Mesh** | Vektorisiert PNG/JPG/… automatisch, entweder einfarbig (Helligkeit/Transparenz) oder mehrfarbig (Farb-Clustering). Optional wird ein SVG gespeichert. |
| **Curves to Clean Mesh** | Wandelt vorhandene Kurven- und **Text-Objekte** in saubere Meshes um, z. B. bereits importierte SVGs. |
| **Boolean-Helfer** | Setzt die ausgewählten Objekte mit einem Klick als Boolean-Cutter (Gravieren/Prägen) auf das aktive Objekt. |

### Saubere Geometrie

- **Geschlossen und manifold.** Extrudierte Objekte sind wasserdichte Körper mit korrekt nach außen zeigenden Normalen.
- **Keine Überlappungen, keine doppelten Vertices.** Alle Formen werden gemeinsam trianguliert, Schnittpunkte also exakt aufgelöst.
- **Füllregeln wie im SVG** (`nonzero`/`evenodd`). Löcher in Buchstaben (O, A, B …) werden korrekt erkannt.
- **„Was man sieht"**: Formen, die weiter oben liegen, schneiden darunterliegende aus. **Weiß wird als Loch behandelt**, z. B. weiße Schrift auf rotem Kreis. Das ist optional.
- **Adaptive Kurvenauflösung.** Starke Krümmungen bekommen mehr Punkte, gerade Strecken keine überflüssigen.
- **Vier Topologien zur Wahl:**
  - **Clean N-Gons**: minimale Geometrie, eine Deckfläche pro Region und Quads an den Seiten. **Ideal für Booleans.**
  - **Triangles**: Constrained-Delaunay-Triangulierung ohne Splitter-Fächer.
  - **Uniform Triangles**: gleich große Dreiecke für Displacement, Cloth und Deformation.
  - **Quads**: quad-dominantes Gitter für Subdivision und Sculpting.
- Optional **ein Objekt pro Farbe**. Die Grenzen benachbarter Farben passen lückenlos aufeinander. Dazu Materialien aus den SVG-Farben, Ebenen-Versatz in Z und planare UVs.

### Bild → Vektor → Mesh

![Bild-Tracing](docs/trace_demo.png)

Die Vektorisierung läuft komplett im Add-on (nur numpy, das bei Blender dabei ist). Externe Programme wie Inkscape oder potrace werden nicht benötigt:

1. Vordergrund bestimmen: Transparenz, Helligkeit (Otsu-Schwelle, Hintergrund wird automatisch erkannt) oder k-Means-Farbcluster.
2. Leichter Weichzeichner gegen Treppenstufen und Rauschen.
3. Marching Squares mit Subpixel-Genauigkeit. Die Kantenglättung des Bildes wird dabei mitgenutzt.
4. Kleine Flecken entfernen, Ecken erkennen, Konturen glätten.
5. Gerade Kanten werden als exakte Linien erkannt, Ecken dazwischen per Geradenschnitt wieder spitz gemacht.
6. Kurven werden mit Bézier-Fitting (Schneider-Algorithmus) angepasst. Ein Quadrat ergibt also 4 Segmente, ein Kreis wenige glatte Kurven.
7. Optional als **SVG speichern**, danach geht es in dieselbe Mesh-Pipeline wie beim SVG-Import.

## Installation

1. ZIP bauen (oder aus den Releases laden):
   ```bash
   python build.py        # erzeugt dist/svg_to_mesh-1.0.0.zip
   ```
2. In Blender:
   - **Blender 4.2 und neuer:** *Edit → Preferences → Get Extensions → ⌄ (oben rechts) → Install from Disk…* → ZIP wählen
   - **Blender 3.6 – 4.1:** *Edit → Preferences → Add-ons → Install…* → ZIP wählen → Häkchen setzen

Getestet mit Blender 5.0. Mindestversion ist 3.6.

## Benutzung

Das Panel findest du in der **3D-Ansicht → Seitenleiste (N) → Tab „SVG Mesh"**. Alternativ:

- *File → Import → SVG as Clean Mesh (.svg)* bzw. *Image Trace to Mesh*
- **Drag & Drop** einer `.svg` in die 3D-Ansicht (ab Blender 4.1; Blender fragt dann, welcher Importer verwendet werden soll)
- *Object → Convert → Clean Mesh (from Curve/Text)* für vorhandene Kurven und Texte

> **Tipp:** Nach dem Import kannst du mit **F9** (bzw. dem Panel „Adjust Last Operation" unten links) alle Einstellungen live ändern, etwa Schwelle, Farben, Topologie oder Tiefe. Das Ergebnis wird sofort neu berechnet.

### Logo in ein Objekt gravieren (Beispiel-Workflow)

1. *Import SVG as Mesh* (oder *Trace Image to Mesh*), Topologie **Clean N-Gons**, **Extrude** an, **Center Depth** an
2. Logo auf der Oberfläche positionieren, sodass es durch die Oberfläche ragt
3. Logo auswählen, dann mit Shift das Zielobjekt anklicken (es ist jetzt aktiv)
4. Seitenleiste → **Cut** (Gravieren) oder **Add** (Prägen)
5. Der Cutter wird als Drahtgitter versteckt und bleibt editierbar. Mit „Apply Immediately" wird der Modifier direkt angewendet.

### Wichtige Optionen

| Option | Bedeutung |
|---|---|
| Topology | N-Gons / Triangles / Uniform Triangles / Quads (siehe oben) |
| Grid Size | Zellgröße für Uniform/Quads, relativ zur Objektgröße |
| Curve Precision | Max. Abweichung von der echten Kurve (in % der Größe). Kleiner = runder |
| Extrude / Depth / Center Depth | Dicke des Körpers, optional symmetrisch um Z=0 |
| Objects | Ein Objekt / pro Farbe / pro Form |
| Overlaps | *Visible Only* (wie das SVG aussieht) oder *Union* (jede Form vollständig) |
| White = Hole | Weiße Flächen werden zu Löchern bzw. ignoriert |
| Size | Auf Größe skalieren (*Fit*) oder echte Dokumentgröße (*Real*, z. B. mm aus dem SVG) |
| **Tracing:** Mode | Auto / Brightness / Transparency / Colors |
| Edge Smoothing / Curve Smoothing | Glättung gegen Pixeltreppen und Rauschen |
| Fit Tolerance | Wie genau die Bézierkurven den Pixeln folgen (px) |
| Corner Angle | Ab welchem Knickwinkel eine Ecke entsteht |
| Despeckle | Kleinere Flecken und Löcher (px²) werden entfernt |
| Save SVG | Vektorisiertes Ergebnis zusätzlich als SVG speichern |

## Grenzen

- SVG-`<text>` wird nicht unterstützt. Text vorher in Pfade umwandeln (Inkscape: *Pfad → Objekt in Pfad*) oder Blender-Text mit *Curves to Clean Mesh* verwenden.
- Verläufe werden zur Farbe ihres ersten Stopps. `clipPath`, `mask`, Filter und Strichelungen (`stroke-dasharray`) werden ignoriert.
- Das Tracing ist für Logos, Icons und Grafiken mit klaren Farbflächen gedacht, nicht für Fotos.

## Technik (für Interessierte)

```
SVG ──parser──┐
Bild ─tracer──┼─► Bézier-Formen ─► adaptive Unterteilung ─► Polygone
Kurve/Text ───┘                                                │
            Constrained Delaunay (alle Formen gemeinsam) ◄─────┘
            Windungszahl je Dreieck per Flood-Fill (Füllregel, Sichtbarkeit, Gruppen)
            Randkonturen je Objekt extrahieren und bereinigen
            ├─ N-Gons (CDT mit Lochauflösung)
            ├─ Dreiecke / gleichmäßig (CDT + Steiner-Gitter)
            └─ Quads (+ Dreiecke zu Quads verbinden, glätten)
            Extrusion zu geschlossenem Körper, UVs, Materialien
```

- `svg_to_mesh/core/` ist reines Python ohne `bpy` und damit außerhalb von Blender testbar: SVG-Parser, Geometrie und Strokes, Tracer, Bézier-Fit, SVG-Writer.
- `svg_to_mesh/mesh_builder.py` enthält Triangulierung und Mesh-Aufbau (`mathutils`, `bmesh`).
- `svg_to_mesh/pipeline.py`, `operators.py` und `ui.py` binden alles an Blender an.

## Entwicklung & Tests

```bash
pip install pytest numpy bpy   # bpy = Blender als Python-Modul (für die Integrationstests)
python -m pytest
```

Die Tests in `tests/test_blender.py` laufen mit dem `bpy`-Modul gegen echtes Blender und prüfen unter anderem, ob die Körper manifold sind, ob das Volumen stimmt und ob Booleans funktionieren. Ohne `bpy` werden sie übersprungen.
