# trainz-to-blender

Convert **Trainz `.cdp` packages** (the `ACS$` / CHUMP container) into **glTF binary (`.glb`)** files that open directly in Blender, with meshes, textures, materials, attach points and (experimental) animations.

Pure Python 3. No third-party packages, no Blender required to run it.

```
python cdp2glb.py "CHR RS1s Ph1.cdp" out --asset 700
```

![status](https://img.shields.io/badge/python-3.8%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-brightgreen)

## What it does

- Reads the `.cdp` container and decompresses its files (LZSS / adaptive-Huffman variants).
- Finds every mesh (`.im`) in each asset, using the `.lm.txt` LOD tables, and exports the highest-detail LOD by default. Meshes that live in a dependency asset are found too.
- Decodes textures (TGA, BMP, DDS DXT1/3/5; PNG/JPG pass through) and applies alpha / alpha-test hints from the texture config. Cut-out and blended materials are exported double-sided.
- Writes one `.glb` per asset: separate objects per mesh, one primitive per material group, UVs, normals, base-colour and normal-map textures.
- Exports attach points (couplers, lights, bogey positions...) as named empty objects, with their rotation.
- Reads `.kin` animation files and exports them as glTF animations on a small bone hierarchy (see below).
- Rotates Trainz's Z-up / Y-forward space into glTF's Y-up so Blender's importer puts models upright.
- Re-reads every file it writes and checks it. One bad mesh, texture or asset is reported and skipped; it never aborts the whole run.

## Requirements

- Python 3.8 or newer
- A `.cdp` file (not included; use packages you have the right to convert)

## Usage

```
python cdp2glb.py <package.cdp> [out_folder] [options]
python cdp2glb.py <animation.kin> [out_folder]        # loose .kin -> bones + animation .glb
```

On Windows, open a Command Prompt in the folder with the script (type `cmd` in File Explorer's address bar). If `py` is not recognised, use `python`. Put paths with spaces in quotes.

| Option | What it does |
|---|---|
| `--list` | Print the assets in the package (with mesh and animation counts) and exit. Do this first. |
| `--asset TEXT` | Only export assets whose name or kuid contains `TEXT`. |
| `--all-lods` | Export every LOD level instead of the best one. |
| `--keep-all` | Also export `shadow` / `collision` helper meshes and material groups (skipped by default). |
| `--no-anim` | Do not export `.kin` animations. |
| `--no-attach` | Do not export attach points as empty objects. |
| `--no-bogeys` | Do not try to place the trucks referenced by the car's config. |
| `--attach-transpose` | Read attach-point rotation matrices transposed (try it if parts face the wrong way). |
| `--extract` | Also dump every file in the package to `out/files/...`. |
| `--extract-only EXT` | Like `--extract` but only files ending with `EXT` (e.g. `.kin`). |
| `--tree` | Print the raw tag tree (debugging the container). |
| `--debug` | Print which normals / winding were chosen for every material group, and unreadable config entries. |

Typical session:

```
python cdp2glb.py "My Loco.cdp" --list
python cdp2glb.py "My Loco.cdp" out --asset 700
```

The decompressor is pure Python, so big packages take a while. A package with a handful of assets (a locomotive, its cab, a truck) is fine to export in one go; for packages with hundreds of assets, export one or a few with `--asset`.

Then in Blender: **File > Import > glTF 2.0 (.glb/.gltf)**.

## Notes on the output

- **Each mesh is its own object** (`body`, `bell`, `window1l`, ...). Join them with `Ctrl+J` if you want one mesh.
- **Attach points and bones appear as empties** (`<mesh>.<attach name>`, `b.r.<bone>`). They show as thin axis lines in Blender; hide them with Overlays > Object Types > Empties, or export with `--no-attach`.
- **Cabs, interiors and trucks (bogeys) are separate assets**, so they come out as separate `.glb` files.
- **Part placement.** Animated / accessory meshes are stored around their own pivot. They are moved to their attach point (found through the config's mesh table or by name) or parented to the matching `.kin` bone. The log says what was placed and what was not.
- Materials use the base colour and normal map only. Trainz-specific shader features (reflections, bump settings, etc.) are not translated.

## Animations (experimental)

A `.kin` file (`JIRF` / `ANIM`) holds a bone tree and, per frame, a position and a quaternion for every bone. Each one becomes a glTF animation (30 fps) on empties named after the bones; a mesh whose name or mesh-table entry points at a `.kin` is parented to the matching bone, so it moves in Blender.

Known limits:

- **No vertex skinning.** Meshes that deform with several bones are not weighted. If a mesh has one material group per bone, the groups are bound to the bones **by order** (the log says "check the result"); otherwise it is bound to the root and does not animate.
- **Rotation direction is unverified.** If a part swings the wrong way or an attach-rotated part faces backwards, try `--attach-transpose`, or flip the quaternion in `rotq()`.
- **Trucks are not placed automatically for every package.** The car's `bogeys` config entries are read when possible; otherwise the truck `.glb` has to be placed by hand at the `a.bog*` empties.

## The `.im` mesh format (what was worked out)

Documented here in case it helps others. All values little-endian.

- File: `JIRF` header, then chunks. `CHNK` holds one material group (`MATL` + `GEOM`); `ATCH` holds attach points (name + 3x4 matrix).
- `GEOM` header is 10 `uint32`: `[?, ?, flag, ?, vertex_count, ?, index_count, face_count, 0, extra]`.
- Data follows in order: vertices (`x y z u v`, 5 floats each), 16-bit triangle indices, per-face data (12 bytes each), then **vertex normals** (3 floats each).
- When `flag == 1` there is a **second** block of unit vectors (likely tangents) after the normals.
- Some meshes (animated parts) carry `extra` additional bytes before the vertex data.
- Because the layout varies, the script does not trust fixed offsets for normals. It tests each candidate block against the real face normals of the triangles, keeps the best match, and flips triangle winding if it is consistently opposite to the normals.

## The `.kin` animation format

- `JIRF` header, form type `ANIM`, then chunks: `INFO` (version 100, name, frame count, fps, scale), `EVNT` (events, empty in all samples), `SKEL`, and one `FRAM` per frame.
- `SKEL` holds a nested tree of `BONE` chunks: version, name length, name, child count, then the child `BONE` chunks.
- Each `FRAM` holds a version, the frame index, then for every bone in tree order **28 bytes: position (3 floats) + quaternion x, y, z, w (4 floats)**, local to the parent bone.

## Troubleshooting

| Problem | Try this |
|---|---|
| The console stops until you press Enter | That is Windows QuickEdit (a click in the window pauses the program). Version 2.0 switches it off automatically; on older versions avoid clicking in the console while it runs. |
| `Couldn't parse glTF` in Blender | Make sure you are using the latest script. |
| `skipped a broken material group` | Open an issue with the printed `GEOM header=[...] size=...` line. |
| Faces look inside-out in Blender | Turn on **Overlay > Face Orientation**. The script auto-flips winding when normals disagree; run with `--debug` and look for `winding flipped`. Some Trainz materials (glass, grates) are modelled to be seen from both sides. |
| Model is invisible or transparent | Some TGAs have an unused alpha channel. The script treats an all-zero alpha as opaque; report any others. |
| Parts sit under or off the model | Check the log for `NOT placed` and the list of attach points; place them by hand, or try `--attach-transpose`. |
| Thin lines everywhere | Attach points / bones are drawn as empties. Hide them in Overlays, or use `--no-attach`. |
| Output is slow | Use `--asset` to export only what you need. |

## Tested with

- `CHR RS1s Ph1.cdp`: C&HR RS1 locomotive and cab assets, imported into Blender and a web glTF viewer. Meshes, textures and simple animations (bell, door, hood) work; multi-bone parts and truck placement need manual fixing.
- Synthetic packages and the three sample `.kin` files for the animation code.

More samples welcome. Please open an issue with the console output and, if you are allowed to share it, the package.

## Credits

The container and compression format follow the community reverse-engineering in **cdptool** and **CDP Explorer**. The `.im` mesh and `.kin` animation readers were worked out from sample files. Trainz is a trademark of its owners; this project is not affiliated with them.

## Legal

This tool is for converting content you own or have permission to use. Respect the licence of any asset you convert. Models made by Trainz content creators remain their work.

## License

MIT
