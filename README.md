# trainz-to-blender

Convert **Trainz `.cdp` packages** (the `ACS$` / CHUMP container) into **glTF binary (`.glb`)** files that open directly in Blender, with meshes, textures, materials and attach points.

Pure Python 3. No third-party packages, no Blender required to run it.

```
python cdp2glb.py "CHR RS1s Ph1.cdp" out --asset 700
```

![status](https://img.shields.io/badge/python-3.8%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-brightgreen)

## What it does

- Reads the `.cdp` container and decompresses its files (LZSS / adaptive-Huffman variants).
- Finds every mesh (`.im`) in each asset, using the `.lm.txt` LOD tables, and exports the highest-detail LOD by default.
- Decodes textures (TGA, BMP; PNG/JPG pass through) and applies alpha / alpha-test hints from the texture config.
- Writes one `.glb` per asset: separate objects per mesh, one primitive per material group, UVs, normals, base-colour and normal-map textures.
- Exports attach points (couplers, lights, etc.) as named empty objects at the correct positions.
- Rotates Trainz's Z-up / Y-forward space into glTF's Y-up so Blender's importer puts models upright.
- Re-reads every file it writes and checks it, so a bad export is reported instead of failing silently in Blender.

## Requirements

- Python 3.8 or newer
- A `.cdp` file (not included; use packages you have the right to convert)

## Usage

```
python cdp2glb.py <package.cdp> [out_folder] [options]
```

On Windows, open a Command Prompt in the folder with the script (type `cmd` in File Explorer's address bar). If `py` is not recognised, use `python`. Put paths with spaces in quotes.

| Option | What it does |
|---|---|
| `--list` | Print the assets in the package and exit. Do this first. |
| `--asset TEXT` | Only export assets whose name or kuid contains `TEXT`. |
| `--all-lods` | Export every LOD level instead of the best one. |
| `--keep-all` | Also export `shadow` / `collision` helper meshes (skipped by default). |
| `--extract` | Also dump every file in the package to `out/files/...`. |
| `--tree` | Print the raw tag tree (debugging the container). |
| `--debug` | Print, for every material group, which normals block was chosen and whether winding was flipped. |

Typical session:

```
python cdp2glb.py "My Loco.cdp" --list
python cdp2glb.py "My Loco.cdp" out --asset 700
```

Large packages hold many assets and the decompressor is pure Python, so export one or a few assets at a time. A locomotive plus its cab took about a minute on a 150 MB package.

Then in Blender: **File > Import > glTF 2.0 (.glb/.gltf)**.

## Notes on the output

- **Each mesh is its own object** (`body`, `bell`, `window1l`, ...). Join them with `Ctrl+J` if you want one mesh.
- **Attach points appear as empties** named `<mesh>.<attach name>`. Hide them with the viewport's Object Types filter if they get in the way.
- **Cabs and interiors are separate assets**, so they come out as separate `.glb` files.
- **Animated parts** (`anim*`, doors, shutters) are exported in their rest position as ordinary meshes. Animations themselves are not exported.
- Materials use the base colour and normal map only. Trainz-specific shader features (reflections, bump settings, etc.) are not translated.

## The `.im` mesh format (what was worked out)

Documented here in case it helps others. All values little-endian.

- File: `JIRF` header, then chunks. `CHNK` holds one material group (`MATL` + `GEOM`); `ATCH` holds attach points (name + 3x4 matrix).
- `GEOM` header is 10 `uint32`: `[?, ?, flag, ?, vertex_count, ?, index_count, face_count, 0, extra]`.
- Data follows in order: vertices (`x y z u v`, 5 floats each), 16-bit triangle indices, per-face data (12 bytes each), then **vertex normals** (3 floats each).
- When `flag == 1` there is a **second** block of unit vectors (likely tangents) after the normals.
- Some meshes (animated parts) carry `extra` additional bytes (the header's last value, 8 or 12 in the samples seen) before the vertex data.
- Because the layout varies, the script does not trust fixed offsets for normals. It tests each candidate block against the real face normals of the triangles, keeps the best match, and flips triangle winding if it is consistently opposite to the normals.

This was derived from the sample packages listed below, so other packages may contain variants. If a mesh is skipped, the log prints its `GEOM` header and size; please include that in an issue.

## Troubleshooting

| Problem | Try this |
|---|---|
| `Couldn't parse glTF` in Blender | Make sure you are using the latest script. Older versions wrote invalid image names. |
| `skipped a broken material group` | Open an issue with the printed `GEOM header=[...] size=...` line. |
| Faces look inside-out in Blender | Turn on **Overlay > Face Orientation**. The script auto-flips winding when normals disagree; run with `--debug` and look for `winding flipped`. Some Trainz materials (glass, grates) are modelled to be seen from both sides, so that part may be as authored. |
| Model is invisible or transparent | Some TGAs have an unused alpha channel. The script treats an all-zero alpha as opaque; report any others. |
| Output is slow | Use `--asset` to export only what you need. |

## Tested with

- `CHR RS1s Ph1.cdp`: C&HR RS1 locomotive and cab assets, imported into Blender 5.2 and a web glTF viewer.

More samples welcome. Please open an issue with the console output and, if you are allowed to share it, the package.

## Credits

The container and compression format follow the community reverse-engineering in **cdptool** and **CDP Explorer**. The `.im` mesh reader was worked out from sample files. Trainz is a trademark of its owners; this project is not affiliated with them.

## Legal

This tool is for converting content you own or have permission to use. Respect the licence of any asset you convert. Models made by Trainz content creators remain their work.

## License

Add a license file of your choice (MIT is a common option for tools like this).
