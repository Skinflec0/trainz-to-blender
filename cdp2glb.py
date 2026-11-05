#!/usr/bin/env python3
"""
cdp2glb.py - convert Trainz .cdp (ACS$ / CHUMP) packages to glTF binary (.glb) for Blender.

    python cdp2glb.py "BN SW1200.cdp" out_folder
    python cdp2glb.py "BN SW1200.cdp" out_folder --list
    python cdp2glb.py "BN SW1200.cdp" out_folder --extract      (also dump every file in the package)
    python cdp2glb.py "BN SW1200.cdp" out_folder --all-lods     (export every LOD mesh, not just the best)
    python cdp2glb.py "BN SW1200.cdp" out_folder --asset sw1200 (only assets whose name/kuid contains this text)
    python cdp2glb.py "BN SW1200.cdp" out_folder --no-anim      (skip .kin animations)
    python cdp2glb.py bell.kin out_folder                       (a loose .kin -> bones + animation .glb)

Then in Blender: File > Import > glTF 2.0 (.glb/.gltf).

Tip: a big package holds hundreds of assets.  Run with --list first, then export
one or a few with --asset <text>.

Pure Python 3, no third-party packages.  The container + compression format
(LZSS / adaptive-Huffman) follows the community reverse-engineering in `cdptool`
and CDP Explorer; the .im mesh reader was worked out from your sample files.

Animations: every .kin (JIRF/ANIM) in an asset becomes a glTF animation on a small bone hierarchy
(empties named after the bones).  Meshes whose name / mesh-table entry points at a .kin are parented
to the matching bone, so they move in Blender.  Skinned meshes (vertex weights) are not supported.

Trainz models are Z-up / Y-forward (same as Blender); the glb is rotated to glTF's
Y-up convention so Blender's importer puts them back upright.
"""
import sys, os, re, json, struct, zlib, argparse, time

VERSION = "2.0"

# Windows consoles (cp1252) crash on odd characters in asset names -> never let print() kill the run.
# line_buffering: progress shows up immediately even when output is redirected to a file.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass


def _disable_quickedit():
    """Windows console 'QuickEdit': a stray click in the window freezes the program until Enter is pressed.
    Switch it off for this console (no effect elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)                       # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)   # clear QUICK_EDIT, set EXTENDED_FLAGS
    except Exception:
        pass

LEAF = 0x8000
REBUILD = 0x7FFF
UNPROC = 0xFFFFFFFE
LENGTH_TABLE = [(0,0),(1,0),(2,0),(3,0),(4,0),(5,1),(7,1),(9,2),(13,2),(17,3),
                (25,3),(33,4),(49,4),(65,5),(97,5),(129,7)]
EOF_SYM = 272


class BitReader:
    def __init__(self, data):
        self.data = data
        self.nbits = len(data) * 8
        self.pos = 0

    def read_bit(self):
        if self.pos >= self.nbits:
            return 0
        b = (self.data[self.pos >> 3] >> (self.pos & 7)) & 1
        self.pos += 1
        return b

    def read_bits(self, n):
        if n == 0:
            return 0
        avail = min(self.nbits - self.pos, n)
        if avail <= 0:
            return 0
        v = 0
        got = 0
        pos = self.pos
        data = self.data
        while got < avail:
            byte = data[pos >> 3]
            off = pos & 7
            take = min(8 - off, avail - got)
            v |= ((byte >> off) & ((1 << take) - 1)) << got
            got += take
            pos += take
        self.pos = pos
        return v


class AdaptiveHuffman:
    def __init__(self, nraw):
        n = (nraw + 1) & 0xFFE
        total = n * 2 - 1
        self.n = n
        self.total = total
        self.root = total - 1
        self.freq = [0] * total
        self.parent = [0] * total
        self.child = [0] * total
        self.s2n = [0] * n
        for i in range(n):
            self.freq[i] = 1
            self.child[i] = i | LEAF
            self.s2n[i] = i
        j = 0
        for k in range(n, total):
            self.freq[k] = self.freq[j] + self.freq[j + 1]
            self.child[k] = j
            self.parent[j] = k
            self.parent[j + 1] = k
            j += 2
        self.parent[self.root] = self.root

    def decode(self, br):
        child = self.child
        v = child[self.root]
        depth = 0
        while not (v & LEAF):
            idx = v + br.read_bit()
            if idx >= self.total:
                raise ValueError("tree index out of bounds")
            v = child[idx]
            depth += 1
            if depth > 50:
                raise ValueError("tree depth exceeded")
        sym = v & ~LEAF
        self.update(sym)
        return sym

    def update(self, symbol):
        freq, parent, child, s2n = self.freq, self.parent, self.child, self.s2n
        total = self.total
        if freq[self.root] > REBUILD:
            self.rebuild()
        i = s2n[symbol]
        while True:
            freq[i] += 1
            f = freq[i]
            if i + 1 < total and freq[i + 1] < f:
                j = i + 2
                while j < total and freq[j] < f:
                    j += 1
                j -= 1
                ci = child[i]
                cj = child[j]
                if not (cj & LEAF):
                    parent[cj] = i
                    parent[cj + 1] = i
                else:
                    s2n[cj & ~LEAF] = i
                if not (ci & LEAF):
                    parent[ci] = j
                    parent[ci + 1] = j
                else:
                    s2n[ci & ~LEAF] = j
                freq[i], freq[j] = freq[j], freq[i]
                freq[j] = f
                child[i], child[j] = child[j], child[i]
                i = j
            p = parent[i]
            if p == i or p == self.root:
                break
            i = p
        freq[self.root] += 1

    def rebuild(self):
        freq, parent, child, s2n = self.freq, self.parent, self.child, self.s2n
        root = self.root
        remaining = self.n
        for i in range(root):
            if child[i] & LEAF:
                freq[i] = (freq[i] + 1) >> 1
            parent[i] = UNPROC
        parent[root] = UNPROC
        pair_scan = 0
        node_scan = 0
        while True:
            found = False
            while pair_scan < root:
                if parent[pair_scan] == UNPROC:
                    found = True
                    break
                pair_scan += 1
            if not found or pair_scan >= root:
                parent[root] = 0xFFFFFFFF
                return
            left = pair_scan
            right = pair_scan + 1
            pair_scan += 2
            while node_scan <= root:
                if child[node_scan] & LEAF:
                    remaining -= 1
                elif parent[node_scan] == UNPROC:
                    break
                node_scan += 1
            slot = node_scan
            node_scan += 1
            next_scan = node_scan
            new_freq = freq[left] + freq[right]
            insert_at = slot
            if slot > 0 and freq[slot - 1] > new_freq:
                scan = slot - 1
                while scan >= 0 and freq[scan] > new_freq:
                    c = child[scan]
                    if not (c & LEAF):
                        parent[c] += 1
                        parent[c + 1] += 1
                    else:
                        s2n[c & ~LEAF] += 1
                    scan -= 1
                pos = scan + 1
                count = slot - pos
                if count > 0:
                    freq[pos + 1:pos + 1 + count] = freq[pos:pos + count]
                    child[pos + 1:pos + 1 + count] = child[pos:pos + count]
                insert_at = pos
            elif remaining > 0:
                scan_pos = next_scan
                dst = slot
                nxt = next_scan
                while True:
                    while scan_pos < root:
                        if child[scan_pos] & LEAF:
                            break
                        scan_pos += 1
                    if scan_pos >= root or freq[scan_pos] >= new_freq:
                        break
                    freq[dst] = freq[scan_pos]
                    child[dst] = child[scan_pos]
                    parent[dst] = UNPROC
                    s2n[child[scan_pos] & ~LEAF] = dst
                    dst = nxt
                    child[scan_pos] = 0
                    nxt += 1
                    node_scan = nxt
                    remaining -= 1
                    if remaining <= 0:
                        break
                    scan_pos += 1
                insert_at = dst
            freq[insert_at] = new_freq
            child[insert_at] = left
            parent[left] = insert_at
            parent[right] = insert_at


def copy_match(out, dist, length, limit):
    length = min(length, limit - len(out))
    if length <= 0:
        return
    if dist > len(out):                       # reference before start of data -> zeros
        pad = min(dist - len(out), length)
        out.extend(b"\0" * pad)
        length -= pad
        if length <= 0:
            return
    start = len(out) - dist
    if dist >= length:
        out.extend(out[start:start + length])
    else:                                     # overlapping run: repeat the pattern
        pat = bytes(out[start:])
        out.extend((pat * (length // dist + 1))[:length])


def mode_params(mode):
    if mode == 0:
        return 8, 16, 4
    if mode == 1:
        return 12, 64, 6
    if mode == 2:
        return 14, 64, 8
    raise ValueError("bad mode %d" % mode)


def decompress(comp, usize):
    if not comp:
        raise ValueError("empty")
    first = comp[0]
    mode = (first >> 4) & 3
    level = first & 0xF
    if level == 0:
        return bytes(comp[1:1 + usize])
    dbits, ndsym, dextra = mode_params(mode)
    if level < 7:
        return _bitstream(comp[1:], usize, dbits, dextra)
    return _huffman(comp[1:], usize, dbits, ndsym, dextra)


def _huffman(stream, usize, dbits, ndsym, dextra):
    dmask = (1 << dbits) - 1
    lit = AdaptiveHuffman(273)
    dst = AdaptiveHuffman(ndsym)
    br = BitReader(stream)
    out = bytearray()
    while len(out) < usize:
        sym = lit.decode(br)
        if sym == EOF_SYM:
            break
        if sym > EOF_SYM:
            raise ValueError("bad symbol")
        if sym < 256:
            out.append(sym)
        else:
            base, extra = LENGTH_TABLE[sym - 256]
            ev = br.read_bits(extra) if extra else 0
            mlen = base + ev + 3
            ds = dst.decode(br)
            de = br.read_bits(dextra)
            d = ((ds << dextra) | de) & dmask
            if d == 0:
                d = 1
            copy_match(out, d, mlen, usize)
    return bytes(out[:usize])


def _bitstream(stream, usize, dbits, dextra):
    dmask = (1 << dbits) - 1
    len_over = ((1 << (16 - dbits)) - 1) if dbits < 16 else 0
    shift = min(dbits, 15)
    br = BitReader(stream)
    out = bytearray()
    if dbits == 8:
        while len(out) < usize:
            flags = br.read_bits(32)
            mask = 1
            while mask and len(out) < usize:
                if not (flags & mask):
                    out.append(br.read_bits(8))
                    mask = (mask << 1) & 0xFFFFFFFF
                else:
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    lc = 1 if flags & mask else 0
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    lc |= 2 if flags & mask else 0
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    lc |= 4 if flags & mask else 0
                    if lc == 7:
                        lc = br.read_bits(8)
                    d = br.read_bits(8) or 1
                    copy_match(out, d, lc + 3, usize)
                    mask = (mask << 1) & 0xFFFFFFFF
    else:
        while len(out) < usize:
            flags = br.read_bits(32)
            mask = 1
            while mask and len(out) < usize:
                if not (flags & mask):
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    if not (flags & mask):
                        out.append(br.read_bits(8))
                    else:
                        run = br.read_bits(8) + 3
                        for _ in range(run):
                            if len(out) >= usize:
                                break
                            out.append(br.read_bits(8))
                    mask = (mask << 1) & 0xFFFFFFFF
                else:
                    word = br.read_bits(16)
                    d = (word & dmask) or 1
                    lc = (word >> shift) & len_over
                    if lc == len_over:
                        lc = br.read_bits(8)
                    copy_match(out, d, lc + 3, usize)
                    mask = (mask << 1) & 0xFFFFFFFF
    return bytes(out[:usize])


# =====================================================================
#  CDP container
# =====================================================================
class Node:
    __slots__ = ("name", "type", "off", "end", "kids")
    def __init__(self, name, type_, off, end):
        self.name, self.type, self.off, self.end, self.kids = name, type_, off, end, []


def parse_tree(data, off, end, depth=0):
    nodes = []
    if depth > 40:
        return nodes
    while off + 6 <= end:
        tag_len = struct.unpack_from("<I", data, off)[0]
        start = off
        off += 4
        nl = data[off]
        off += 1
        if nl:
            if off + nl > end:
                break
            name = bytes(data[off:off + nl - 1]).decode("utf-8", "replace")
            off += nl
        else:
            name = ""
        if off >= end:
            break
        ttype = data[off]
        off += 1
        vend = start + 4 + tag_len
        if vend > end:
            break
        n = Node(name, ttype, off, vend)
        if ttype == 0:
            n.kids = parse_tree(data, off, vend, depth + 1)
        nodes.append(n)
        off = vend
    return nodes


def _base(fname):
    """'Textures\\Main.TGA' -> 'main.tga'"""
    return fname.replace("\\", "/").split("/")[-1].strip().lower()


class Asset:
    def __init__(self, name):
        self.name = name
        self.info = {}
        self.files = {}      # filename -> Node (binary)
        self.node = None
        self.cache = {}

    def has(self, fname):
        return _base(fname) in self.files


class Package:
    def __init__(self, data):
        if bytes(data[:4]) != b"ACS$":
            raise SystemExit("Not a Trainz ACS$ package (header is %r). Is it a .cdp from Trainz? "
                             "Other containers (zip / encrypted) are not supported." % bytes(data[:4]))
        if len(data) < 32:
            raise SystemExit("File is too small to be a .cdp.")
        self.data = data
        self.assets = []
        tree = parse_tree(data, 16, len(data))
        self._walk(tree)
        self._global = {}
        for a in self.assets:
            for k in a.files:
                self._global.setdefault(k, (a, k))

    def _walk(self, nodes):
        for n in nodes:
            if n.type == 0:
                files = [k for k in n.kids if k.type == 0 and k.name == "files"]
                if files:
                    a = Asset(n.name)
                    for k in n.kids:
                        if k.type == 3:
                            a.info[k.name] = bytes(self.data[k.off:k.end]).split(b"\0")[0].decode("utf-8", "replace")
                    def collect(kids):
                        for f in kids:
                            if f.type == 0:
                                collect(f.kids)
                            elif f.type == 4:
                                a.files.setdefault(f.name.lower().replace("\\", "/").split("/")[-1], f)
                    collect(files[0].kids)
                    a.node = n
                    self.assets.append(a)
                else:
                    self._walk(n.kids)

    def read(self, asset, fname):
        key = _base(fname)
        if key in asset.cache:
            return asset.cache[key]
        if key not in asset.files:
            raise KeyError("file %r not in asset %s" % (fname, asset.name))
        n = asset.files[key]
        us = struct.unpack_from("<I", self.data, n.off)[0]
        out = decompress(bytes(self.data[n.off + 4:n.end]), us)
        if len(asset.cache) < 16:
            asset.cache[key] = out
        return out

    def find(self, asset, fname):
        """look in the asset first, then any other asset (dependencies). Returns (asset, name) or None"""
        k = _base(fname)
        if k in asset.files:
            return asset, k
        return self._global.get(k)


# =====================================================================
#  Images -> PNG   (TGA / BMP decoded here, PNG/JPG passed through)
# =====================================================================
def png_encode(w, h, rgba, has_alpha=True):
    ch = 4 if has_alpha else 3
    raw = bytearray()
    stride = w * ch
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if has_alpha else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def decode_tga(b):
    idlen, cmtype, imtype = b[0], b[1], b[2]
    cmlen = struct.unpack_from("<H", b, 5)[0]
    cmbits = b[7]
    w, h = struct.unpack_from("<HH", b, 12)
    bpp, desc = b[16], b[17]
    pos = 18 + idlen
    cmap = None
    if cmtype:
        cb = (cmbits + 7) // 8
        cmap = [bytes(b[pos + i * cb:pos + (i + 1) * cb]) for i in range(cmlen)]
        pos += cmlen * cb
    px = (bpp + 7) // 8
    need = w * h * px
    if imtype in (9, 10, 11):
        out = bytearray()
        while len(out) < need and pos < len(b):
            c = b[pos]
            pos += 1
            cnt = (c & 0x7F) + 1
            if c & 0x80:
                out += b[pos:pos + px] * cnt
                pos += px
            else:
                out += b[pos:pos + cnt * px]
                pos += cnt * px
        src = bytes(out[:need])
    else:
        src = bytes(b[pos:pos + need])
    if len(src) < need:                      # truncated data: pad so slicing never mismatches
        src = src + b"\0" * (need - len(src))
    rgba = bytearray(w * h * 4)
    if imtype in (1, 9) and cmap:
        for i in range(w * h):
            c = cmap[src[i * px]] if px == 1 else cmap[struct.unpack_from("<H", src, i * px)[0]]
            rgba[i * 4:i * 4 + 4] = bytes((c[2], c[1], c[0], c[3] if len(c) > 3 else 255))
    elif imtype in (2, 10) and bpp == 32:
        rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = src[2::4], src[1::4], src[0::4], src[3::4]
    elif imtype in (2, 10) and bpp == 24:
        rgba[0::4], rgba[1::4], rgba[2::4] = src[2::3], src[1::3], src[0::3]
        rgba[3::4] = b"\xff" * (w * h)
    elif imtype in (2, 10) and bpp == 16:
        for i in range(w * h):
            v = src[i * 2] | (src[i * 2 + 1] << 8)
            rgba[i * 4:i * 4 + 4] = bytes((((v >> 10) & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, (v & 31) * 255 // 31, 255))
    elif imtype in (3, 11):
        rgba[0::4] = rgba[1::4] = rgba[2::4] = src[0::px]
        rgba[3::4] = b"\xff" * (w * h)
    else:
        raise ValueError("unsupported TGA type %d/%d" % (imtype, bpp))
    if not (desc & 0x20):  # bottom-up -> flip
        rows = [rgba[y * w * 4:(y + 1) * w * 4] for y in range(h)]
        rgba = bytearray(b"".join(reversed(rows)))
    return w, h, rgba


def decode_bmp(b):
    off = struct.unpack_from("<I", b, 10)[0]
    hs = struct.unpack_from("<I", b, 14)[0]
    w, h = struct.unpack_from("<ii", b, 18)
    bpp = struct.unpack_from("<H", b, 28)[0]
    comp = struct.unpack_from("<I", b, 30)[0]
    ncol = struct.unpack_from("<I", b, 46)[0]
    if comp not in (0, 3):
        raise ValueError("compressed BMP unsupported")
    top_down = h < 0
    h = abs(h)
    pal = None
    if bpp <= 8:
        ncol = ncol or (1 << bpp)
        pal = [bytes(b[14 + hs + i * 4:14 + hs + i * 4 + 4]) for i in range(ncol)]
    rowb = ((w * bpp + 31) // 32) * 4
    rgba = bytearray(w * h * 4)
    for y in range(h):
        row = b[off + y * rowb:off + (y + 1) * rowb]
        dy = y if top_down else h - 1 - y
        o = dy * w * 4
        if bpp == 24:
            for x in range(w):
                rgba[o + x * 4:o + x * 4 + 4] = bytes((row[x * 3 + 2], row[x * 3 + 1], row[x * 3], 255))
        elif bpp == 32:
            for x in range(w):
                rgba[o + x * 4:o + x * 4 + 4] = bytes((row[x * 4 + 2], row[x * 4 + 1], row[x * 4], 255))
        elif bpp == 8:
            for x in range(w):
                p = pal[row[x]]
                rgba[o + x * 4:o + x * 4 + 4] = bytes((p[2], p[1], p[0], 255))
        elif bpp == 4:
            for x in range(w):
                v = (row[x >> 1] >> (4 if x % 2 == 0 else 0)) & 15
                p = pal[v]
                rgba[o + x * 4:o + x * 4 + 4] = bytes((p[2], p[1], p[0], 255))
        elif bpp == 1:
            for x in range(w):
                p = pal[(row[x >> 3] >> (7 - (x & 7))) & 1]
                rgba[o + x * 4:o + x * 4 + 4] = bytes((p[2], p[1], p[0], 255))
        else:
            raise ValueError("unsupported BMP depth %d" % bpp)
    return w, h, rgba


def _rgb565(v):
    return (((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31)


def decode_dds(b):
    if b[:4] != b"DDS ":
        raise ValueError("not a DDS")
    h, w = struct.unpack_from("<II", b, 12)
    flags = struct.unpack_from("<I", b, 80)[0]
    cc = bytes(b[84:88])
    bpp = struct.unpack_from("<I", b, 88)[0]
    masks = struct.unpack_from("<4I", b, 92)
    pos = 128
    if cc == b"DX10":
        raise ValueError("DX10 DDS unsupported")
    rgba = bytearray(w * h * 4)
    if cc in (b"DXT1", b"DXT3", b"DXT5"):
        bw, bh = (w + 3) // 4, (h + 3) // 4
        bs = 8 if cc == b"DXT1" else 16
        for by in range(bh):
            for bx in range(bw):
                blk = b[pos:pos + bs]
                pos += bs
                if len(blk) < bs:
                    break
                cb = blk[bs - 8:]
                c0, c1, bits = struct.unpack_from("<HHI", cb, 0)
                p0, p1 = _rgb565(c0), _rgb565(c1)
                if c0 > c1 or cc != b"DXT1":
                    pal = [p0 + (255,), p1 + (255,),
                           tuple((2 * p0[i] + p1[i]) // 3 for i in range(3)) + (255,),
                           tuple((p0[i] + 2 * p1[i]) // 3 for i in range(3)) + (255,)]
                else:
                    pal = [p0 + (255,), p1 + (255,), tuple((p0[i] + p1[i]) // 2 for i in range(3)) + (255,), (0, 0, 0, 0)]
                al = None
                if cc == b"DXT3":
                    al = [((struct.unpack_from("<Q", blk, 0)[0] >> (4 * i)) & 15) * 17 for i in range(16)]
                elif cc == b"DXT5":
                    a0, a1 = blk[0], blk[1]
                    t = [a0, a1] + ([(( 6 - i) * a0 + (1 + i) * a1) // 7 for i in range(6)] if a0 > a1
                                    else [((4 - i) * a0 + (1 + i) * a1) // 5 for i in range(4)] + [0, 255])
                    ab = int.from_bytes(blk[2:8], "little")
                    al = [t[(ab >> (3 * i)) & 7] for i in range(16)]
                for i in range(16):
                    x, y = bx * 4 + (i & 3), by * 4 + (i >> 2)
                    if x < w and y < h:
                        c = pal[(bits >> (2 * i)) & 3]
                        o = (y * w + x) * 4
                        rgba[o:o + 4] = bytes((c[0], c[1], c[2], al[i] if al else c[3]))
        return w, h, rgba
    if bpp in (24, 32) and masks[0]:
        px = bpp // 8
        sh = [(m & -m).bit_length() - 1 if m else None for m in masks]
        for i in range(w * h):
            v = int.from_bytes(b[pos + i * px:pos + (i + 1) * px], "little")
            rgba[i * 4:i * 4 + 4] = bytes(((v >> sh[0]) & 255, (v >> sh[1]) & 255, (v >> sh[2]) & 255,
                                            ((v >> sh[3]) & 255) if sh[3] is not None else 255))
        return w, h, rgba
    raise ValueError("unsupported DDS format")


def decode_image(name, data):
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "tga":
        return decode_tga(data)
    if ext == "bmp":
        return decode_bmp(data)
    if ext == "dds":
        return decode_dds(data)
    return None


# =====================================================================
#  Trainz .im mesh
# =====================================================================
class Primitive:
    pass


def read_im(b):
    if b[:4] != b"JIRF":
        raise ValueError("not a Trainz .im (no JIRF header)")
    total = struct.unpack_from("<I", b, 4)[0] + 8
    prims, attach = [], []
    p = 12
    while p + 8 <= min(total, len(b)):
        tag = bytes(b[p:p + 4])
        sz = struct.unpack_from("<I", b, p + 4)[0]
        body = b[p + 8:p + 8 + sz]
        if tag == b"CHNK":
            try:
                pr = read_chnk(body)
            except Exception:
                pr = None
            if pr:
                prims.append(pr)
        elif tag == b"ATCH":
            try:
                q = 8
                cnt = struct.unpack_from("<I", body, 4)[0]
                for _ in range(cnt):
                    n = struct.unpack_from("<I", body, q)[0]
                    name = bytes(body[q + 4:q + 4 + n]).split(b"\0")[0].decode("latin1")
                    m = struct.unpack_from("<12f", body, q + 4 + n)
                    attach.append((name, m))
                    q += 4 + n + 48
            except Exception:
                pass          # damaged attach table: keep the mesh anyway
        p += 8 + sz
    return prims, attach


def _geom_standard(sub, h):
    nv, ni, nf = h[4], h[6], h[7]
    o = 40
    V = struct.unpack_from("<%df" % (nv * 5), sub, o)
    o += nv * 20
    idx = struct.unpack_from("<%dH" % ni, sub, o)
    o += ni * 2
    o += nf * 12
    N = None
    if o + nv * 12 <= len(sub):
        N = struct.unpack_from("<%df" % (nv * 3), sub, o)
    return nv, V, idx, N


def _geom_ok(g):
    nv, V, idx, N = g
    return nv > 0 and len(idx) >= 3 and max(idx) < nv


def _geom_search(sub, h):
    """Layout not the usual one (animated / special meshes): work it out from the chunk size.
    Tries vertex stride = (size - indices - face data) / nv, 16- and 32-bit indices,
    indices before or after the vertices.  Returns (nv, V, idx, None) or None."""
    nv = h[4]
    if not 0 < nv <= 65536:
        return None
    sz = len(sub)
    nis = sorted({x for i, x in enumerate(h) if i != 4 and 3 <= x <= 400000 and x % 3 == 0})
    nfs = sorted({0} | {x for x in h if x <= 100000})
    for isize in (2, 4):
        for ni in nis:
            for nf in nfs:
                rem = sz - 40 - ni * isize - nf * 12
                if rem < nv * 20 or rem % nv:
                    continue
                stride = rem // nv
                if stride % 4:
                    continue
                for idx_first in (False, True):
                    try:
                        vo = 40 + (ni * isize if idx_first else 0)
                        io = 40 if idx_first else 40 + nv * stride
                        V = []
                        for i in range(nv):
                            V.extend(struct.unpack_from("<5f", sub, vo + i * stride))
                        if not all(c == c and abs(c) < 1e6 for c in V):
                            continue
                        idx = struct.unpack_from("<%d%s" % (ni, "H" if isize == 2 else "I"), sub, io)
                    except struct.error:
                        continue
                    if idx and max(idx) < nv:
                        return nv, tuple(V), idx, None
    return None


def _unit_normals(sub, o, nv):
    """True if the nv*12 bytes at o look like unit-length normals"""
    if o < 0 or o + nv * 12 > len(sub):
        return False
    for i in range(0, nv, max(1, nv // 8)):
        x, y, z = struct.unpack_from("<3f", sub, o + i * 12)
        l2 = x * x + y * y + z * z
        if not (0.8 < l2 < 1.25):
            return False
    return True


def _vert_score(V, nv):
    """how plausible is this vertex block: UVs small, and not 'pad bytes read as x,y'"""
    good = 0
    zero_xy = 0
    for i in range(nv):
        x, y, z, u, v = V[i * 5:i * 5 + 5]
        if abs(u) <= 16 and abs(v) <= 16:
            good += 1
        if x == 0.0 and y == 0.0:
            zero_xy += 1
    sc = good / nv
    if nv > 3 and zero_xy > nv * 0.5:
        sc -= 1.0
    return sc


def _geom_gap(sub, h):
    """Layout seen in animated / extra-data meshes: the usual blocks, but h[9] extra bytes
    sit before the vertices or between vertices and indices (the header's last number).
    Both placements are tried and the one whose vertices look sane wins."""
    nv, ni, nf, extra = h[4], h[6], h[7], h[9]
    if not 0 < nv <= 65536 or ni < 3:
        return None
    gaps = sorted({0, 4, 8, 12, 16, 24})
    first = [(extra, 0), (0, extra)]
    rest = [(a, b) for a in sorted({0, extra, 4, 8, 12, 16}) for b in gaps if (a, b) not in first]
    best = None
    for k, (pre, gap) in enumerate(first + rest):
        vo = 40 + pre
        io = vo + nv * 20 + gap
        if io + ni * 2 > len(sub):
            continue
        try:
            V = struct.unpack_from("<%df" % (nv * 5), sub, vo)
            idx = struct.unpack_from("<%dH" % ni, sub, io)
        except struct.error:
            continue
        if not all(c == c and abs(c) < 1e5 for c in V) or max(idx) >= nv:
            continue
        sc = _vert_score(V, nv)
        if best is None or sc > best[0] + 1e-9:
            best = (sc, V, idx, io)
        if k >= 1 and best is not None:      # compared the two main candidates; accept
            break
    if best is None:
        return None
    _, V, idx, io = best
    N = None
    base = io + ni * 2 + nf * 12
    for g2 in (0, extra, 4, 8, 12, 16):
        for blk in (0, nv * 12):             # normals first, or a block before them
            o = base + g2 + blk
            if _unit_normals(sub, o, nv):
                N = struct.unpack_from("<%df" % (nv * 3), sub, o)
                break
        if N:
            break
    return nv, V, idx, N


def _score_normals(V, idx, N, nv):
    """compare vertex normals with the real face normals of the triangles.
    returns (mean |cos|, mean signed cos); ~1.0 / +-1.0 means the block really holds normals"""
    n = len(idx) // 3
    step = max(1, n // 300)
    tot = sgn = 0.0
    cnt = 0
    for t in range(0, n, step):
        a, b, c = idx[t * 3:t * 3 + 3]
        ax, ay, az = V[a * 5:a * 5 + 3]
        ux, uy, uz = V[b * 5] - ax, V[b * 5 + 1] - ay, V[b * 5 + 2] - az
        wx, wy, wz = V[c * 5] - ax, V[c * 5 + 1] - ay, V[c * 5 + 2] - az
        fx, fy, fz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
        fl = (fx * fx + fy * fy + fz * fz) ** 0.5
        nx = N[a * 3] + N[b * 3] + N[c * 3]
        ny = N[a * 3 + 1] + N[b * 3 + 1] + N[c * 3 + 1]
        nz = N[a * 3 + 2] + N[b * 3 + 2] + N[c * 3 + 2]
        nl = (nx * nx + ny * ny + nz * nz) ** 0.5
        if fl < 1e-12 or nl < 1e-9:
            continue
        d = (fx * nx + fy * ny + fz * nz) / (fl * nl)
        tot += abs(d)
        sgn += d
        cnt += 1
    return (tot / cnt, sgn / cnt) if cnt else (0.0, 0.0)


def _choose_normals(sub, h, g):
    """find the real vertex-normal block (not tangents / face data) and fix winding to match it"""
    nv, V, idx, N0 = g
    ni, nf, extra = len(idx), h[7], h[9]
    base0 = 40 + nv * 20 + ni * 2
    offs = set()
    for e in (0, extra, extra * 2, 4, 8, 12, 16):
        for blk in (0, 1, 2):
            for f in (nf * 12, 0):
                offs.add(base0 + f + e + blk * nv * 12)
    best = None
    for o in sorted(offs):
        if not _unit_normals(sub, o, nv):
            continue
        N = struct.unpack_from("<%df" % (nv * 3), sub, o)
        sc = _score_normals(V, idx, N, nv)
        if best is None or sc[0] > best[0][0] + 1e-9:
            best = (sc, N, o)
    if best is None or best[0][0] < 0.5:
        return nv, V, idx, None, "no usable normals (flat shading)"
    (ab, sg), N, o = best
    info = "normals@%d match=%.2f" % (o, ab)
    if sg < -0.3:                                   # winding opposite to normals -> flip triangles
        idx = list(idx)
        for t in range(0, len(idx) - 2, 3):
            idx[t + 1], idx[t + 2] = idx[t + 2], idx[t + 1]
        idx = tuple(idx)
        info += ", winding flipped"
    return nv, V, idx, N, info


def parse_geom(sub):
    h = struct.unpack_from("<10I", sub, 0)
    g = None
    try:
        g = _geom_standard(sub, h)
    except (struct.error, ValueError):
        pass
    if g and _geom_ok(g):
        return _finish_geom(sub, h, g)
    alt = None
    for fn in (_geom_gap, _geom_search):
        try:
            alt = fn(sub, h)
        except Exception:
            alt = None
        if alt:
            break
    g = alt or g
    if g and _geom_ok(g):
        return _finish_geom(sub, h, g)
    return g, (h, len(sub), "")


def _finish_geom(sub, h, g):
    try:
        nv, V, idx, N, info = _choose_normals(sub, h, g)
        return (nv, V, idx, N), (h, len(sub), info)
    except Exception as e:
        return (g[0], g[1], g[2], None), (h, len(sub), "normals failed: %s" % e)


def read_chnk(body):
    pr = Primitive()
    pr.mat_name, pr.textures, pr.verts = "", {}, None
    q = 8
    while q + 8 <= len(body):
        tag = bytes(body[q:q + 4])
        sz = struct.unpack_from("<I", body, q + 4)[0]
        sub = body[q + 8:q + 8 + sz]
        if tag == b"MATL":
            try:
                nl = struct.unpack_from("<I", sub, 4)[0]
                pr.mat_name = bytes(sub[8:8 + nl]).split(b"\0")[0].decode("latin1")
                pos = 8 + nl + 64
                cnt = struct.unpack_from("<I", sub, pos)[0]
                pos += 4
                for _ in range(cnt):
                    ttype, ln = struct.unpack_from("<II", sub, pos)
                    path = bytes(sub[pos + 8:pos + 8 + ln]).split(b"\0")[0].decode("latin1")
                    pos += 8 + ln + 4
                    pr.textures[ttype] = path
            except Exception:
                pass
        elif tag == b"GEOM":
            pr.verts, pr.dbg = parse_geom(sub)
        q += 8 + sz
    return pr if pr.verts else None


# =====================================================================
#  glTF writer
# =====================================================================
class GLB:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accs, self.meshes, self.nodes = [], [], [], []
        self.mats, self.images, self.textures, self.samplers = [], [], [], [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        self.matkey, self.imgkey, self.cfgkey = {}, {}, {}
        self.attach, self.meshnode, self.bbox = {}, {}, {}
        self.anims = []
        self.kinmap = {}

    def _view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin += data
        self.views.append(v)
        return len(self.views) - 1

    def acc(self, data, ctype, count, atype, target=None, minmax=None):
        a = {"bufferView": self._view(data, target), "componentType": ctype, "count": count, "type": atype}
        if minmax:
            a["min"], a["max"] = minmax
        self.accs.append(a)
        return len(self.accs) - 1

    def image(self, key, mime, data):
        if key in self.imgkey:
            return self.imgkey[key]
        nm = key if isinstance(key, str) else "_".join(str(k) for k in key)
        self.images.append({"bufferView": self._view(data), "mimeType": mime, "name": nm})
        self.textures.append({"source": len(self.images) - 1, "sampler": 0})
        self.imgkey[key] = len(self.textures) - 1
        return self.imgkey[key]

    def write(self, path):
        doc = {"asset": {"version": "2.0", "generator": "cdp2glb"},
               "scene": 0, "scenes": [{"nodes": [i for i, n in enumerate(self.nodes) if not n.pop("_child", False)]}],
               "nodes": self.nodes, "buffers": [{"byteLength": len(self.bin)}]}
        for k, v in (("bufferViews", self.views), ("accessors", self.accs), ("meshes", self.meshes),
                     ("materials", self.mats), ("images", self.images), ("textures", self.textures),
                     ("animations", self.anims)):
            if v:
                doc[k] = v
        doc["samplers"] = self.samplers
        def clean(o):
            if isinstance(o, float):
                return o if o == o and abs(o) != float("inf") else 0.0
            if isinstance(o, dict):
                return {k: clean(v) for k, v in o.items()}
            if isinstance(o, list):
                return [clean(v) for v in o]
            return o
        js = json.dumps(clean(doc), separators=(",", ":"), allow_nan=False).encode("ascii")
        js += b" " * (-len(js) % 4)
        binb = bytes(self.bin) + b"\0" * (-len(self.bin) % 4)
        with open(path, "wb") as f:
            f.write(struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + 8 + len(binb)))
            f.write(struct.pack("<I4s", len(js), b"JSON") + js)
            f.write(struct.pack("<I4s", len(binb), b"BIN\0") + binb)


def parse_texture_txt(txt):
    d = {}
    for line in txt.decode("latin1").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip().lower()] = v.strip()
    return d


def build_texture(pkg, asset, glb, texref, want_alpha, log):
    """texref like 'main1.texture' -> (glTF texture index, cfg) or None.  Never raises."""
    try:
        return _build_texture(pkg, asset, glb, texref, want_alpha, log)
    except Exception as e:
        log("   texture failed (%s): %s" % (texref, e))
        return None


def _build_texture(pkg, asset, glb, texref, want_alpha, log):
    base = texref.replace("\\", "/").split("/")[-1]
    for cand in (base + ".txt", base + ".texture.txt" if not base.endswith(".texture") else base + ".txt"):
        hit = pkg.find(asset, cand)
        if hit:
            break
    else:
        log("   texture config not found: %s" % base)
        return None
    ta, tn = hit
    cfg = parse_texture_txt(pkg.read(ta, tn))
    prim = cfg.get("primary")
    if not prim:
        return None
    key = (base, want_alpha)
    if key in glb.imgkey:
        return glb.imgkey[key], glb.cfgkey.get(key, cfg)
    h = pkg.find(ta, prim)
    if not h:
        log("   image missing: %s" % prim)
        return None
    pa, pn = h
    pdata = pkg.read(pa, pn)
    dec = decode_image(prim, pdata)
    if dec is None:  # png / jpg passthrough
        mime = "image/png" if prim.lower().endswith(".png") else "image/jpeg"
        ti = glb.image(key, mime, pdata)
        glb.cfgkey[key] = cfg
        return ti, cfg
    w, hh, rgba = dec
    alpha = cfg.get("alpha")
    if want_alpha and alpha and alpha.lower() != prim.lower():
        ah = pkg.find(ta, alpha)
        if ah:
            ad = decode_image(alpha, pkg.read(*ah))
            if ad and ad[0] == w and ad[1] == hh:
                rgba[3::4] = ad[2][0::4]
    if not want_alpha:
        rgba[3::4] = b"\xff" * (w * hh)
    if want_alpha and max(rgba[3::4]) == 0:   # 32-bit TGA whose alpha channel was never filled in
        rgba[3::4] = b"\xff" * (w * hh)
    has_a = any(c != 255 for c in rgba[3::4][:: max(1, (w * hh) // 4096)]) if want_alpha else False
    png = png_encode(w, hh, bytes(rgba), True)
    idx = glb.image(key, "image/png", png)
    cfg["_has_alpha"] = has_a
    glb.cfgkey[key] = cfg
    return idx, cfg


def rot(x, y, z):
    """Trainz/Blender (Z-up, Y-forward) -> glTF (Y-up, -Z forward)"""
    return x, z, -y


def _matmul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_to_quat(R):
    t = R[0][0] + R[1][1] + R[2][2]
    if t > 0:
        s = (t + 1) ** 0.5 * 2
        return ((R[2][1] - R[1][2]) / s, (R[0][2] - R[2][0]) / s, (R[1][0] - R[0][1]) / s, s / 4)
    if R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s = (1 + R[0][0] - R[1][1] - R[2][2]) ** 0.5 * 2
        return (s / 4, (R[0][1] + R[1][0]) / s, (R[0][2] + R[2][0]) / s, (R[2][1] - R[1][2]) / s)
    if R[1][1] > R[2][2]:
        s = (1 + R[1][1] - R[0][0] - R[2][2]) ** 0.5 * 2
        return ((R[0][1] + R[1][0]) / s, s / 4, (R[1][2] + R[2][1]) / s, (R[0][2] - R[2][0]) / s)
    s = (1 + R[2][2] - R[0][0] - R[1][1]) ** 0.5 * 2
    return ((R[0][2] + R[2][0]) / s, (R[1][2] + R[2][1]) / s, s / 4, (R[1][0] - R[0][1]) / s)


def attach_quat(m, transpose=False):
    """attach matrix (3x3 rows + translation) -> glTF quaternion, or None if identity / not a clean rotation.
    Default: rows are the local axes (DirectX style).  --attach-transpose flips that."""
    try:
        R = [[m[j * 3 + i] if not transpose else m[i * 3 + j] for j in range(3)] for i in range(3)]
        det = (R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1]) - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
               + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0]))
        cols = [sum(R[i][j] ** 2 for i in range(3)) for j in range(3)]
        if abs(det - 1) > 0.05 or any(abs(c - 1) > 0.05 for c in cols):
            return None
        C = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
        Ct = [[C[j][i] for j in range(3)] for i in range(3)]
        q = _qnorm(mat_to_quat(_matmul(_matmul(C, R), Ct)))
        if abs(q[3]) > 0.99999:
            return None
        return q if q[3] >= 0 else tuple(-c for c in q)
    except Exception:
        return None


def add_mesh(pkg, asset, glb, im_name, label, log):
    hit = pkg.find(asset, im_name)
    if not hit:
        raise KeyError("mesh %s not found in package" % im_name)
    asset, im_name = hit                      # may live in a dependency asset; its textures are looked up there first
    prims, attach = read_im(pkg.read(asset, im_name))
    gprims = []
    tri_counts = []
    omn, omx = [1e30] * 3, [-1e30] * 3
    for pr in prims:
        if not getattr(glb, "keep_all", False) and re.search(r"shadow|collision", pr.mat_name or "", re.I):
            continue                       # game helper geometry (flat shadow blob etc.)
        nv, V, idx, N = pr.verts
        idx = [i for i in idx]
        idx = idx[:len(idx) // 3 * 3]
        if nv == 0 or not idx or max(idx) >= nv:
            dh, dsz = getattr(pr, "dbg", ((), 0))[:2]
            log("   skipped a broken material group (nv=%d)  GEOM header=%s size=%d" % (nv, list(dh), dsz))
            continue
        pos = bytearray()
        uv = bytearray()
        nor = bytearray()
        mn = [1e30] * 3
        mx = [-1e30] * 3
        for i in range(nv):
            x, y, z, u, v = V[i * 5:i * 5 + 5]
            if not all(c == c and abs(c) < 1e8 for c in (x, y, z, u, v)):
                x = y = z = u = v = 0.0
            gx, gy, gz = rot(x, y, z)
            pos += struct.pack("<3f", gx, gy, gz)
            uv += struct.pack("<2f", u, v)
            if gx < mn[0]: mn[0] = gx
            if gy < mn[1]: mn[1] = gy
            if gz < mn[2]: mn[2] = gz
            if gx > mx[0]: mx[0] = gx
            if gy > mx[1]: mx[1] = gy
            if gz > mx[2]: mx[2] = gz
            if N:
                nn = N[i * 3:i * 3 + 3]
                if not all(c == c and abs(c) < 1e8 for c in nn):
                    nn = (0.0, 0.0, 1.0)
                ln = (nn[0] * nn[0] + nn[1] * nn[1] + nn[2] * nn[2]) ** 0.5
                nn = (nn[0] / ln, nn[1] / ln, nn[2] / ln) if ln > 1e-8 else (0.0, 0.0, 1.0)
                nor += struct.pack("<3f", *rot(*nn))
        attrs = {"POSITION": glb.acc(bytes(pos), 5126, nv, "VEC3", 34962, ([*mn], [*mx])),
                 "TEXCOORD_0": glb.acc(bytes(uv), 5126, nv, "VEC2", 34962)}
        if N:
            attrs["NORMAL"] = glb.acc(bytes(nor), 5126, nv, "VEC3", 34962)
        ind = glb.acc(struct.pack("<%dH" % len(idx), *idx), 5123, len(idx), "SCALAR", 34963)
        for k_ in range(3):
            omn[k_] = min(omn[k_], mn[k_])
            omx[k_] = max(omx[k_], mx[k_])
        # material
        mk = (pr.mat_name, pr.textures.get(1), pr.textures.get(8))
        if mk not in glb.matkey:
            m = {"name": pr.mat_name or "mat", "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.7}}
            if 1 in pr.textures:
                r = build_texture(pkg, asset, glb, pr.textures[1], True, log)
                if r:
                    ti, cfg = r
                    m["pbrMetallicRoughness"]["baseColorTexture"] = {"index": ti}
                    hint = cfg.get("alphahint", "").lower()
                    if hint == "masked":
                        m["alphaMode"] = "MASK"
                    elif hint or cfg.get("_has_alpha"):
                        m["alphaMode"] = "BLEND" if hint != "opaque" else "OPAQUE"
            if 8 in pr.textures:
                r = build_texture(pkg, asset, glb, pr.textures[8], False, log)
                if r:
                    m["normalTexture"] = {"index": r[0]}
            if m.get("alphaMode") in ("MASK", "BLEND"):
                m["doubleSided"] = True
            glb.mats.append(m)
            glb.matkey[mk] = len(glb.mats) - 1
        tri_counts.append(len(idx) // 3)
        if os.environ.get("CDP_DEBUG"):
            log("      group %-24s %s" % (pr.mat_name, (getattr(pr, "dbg", (0, 0, ""))[2:] or [""])[0]))
        gprims.append({"attributes": attrs, "indices": ind, "material": glb.matkey[mk], "mode": 4})
    if not gprims:
        return 0, 0, 0
    ntris = sum(a_ for a_ in tri_counts)
    glb.meshes.append({"name": label, "primitives": gprims})
    glb.nodes.append({"name": label, "mesh": len(glb.meshes) - 1})
    ni = len(glb.nodes) - 1
    glb.meshnode[label] = ni
    glb.bbox[label] = (omn, omx)
    for name, m in attach:
        t = m[9:12]
        if all(c == c and abs(c) < 1e8 for c in t):
            if not getattr(glb, "no_attach", False):
                en = {"name": "%s.%s" % (label, name), "translation": list(rot(*t))}
                q = attach_quat(m, getattr(glb, "attach_transpose", False))
                if q:
                    en["rotation"] = list(q)
                glb.nodes.append(en)
            glb.attach.setdefault(name.lower(), (tuple(t), label, tuple(m)))
    return ntris, len(gprims), len(attach)


# =====================================================================
#  Trainz .kin animation  (JIRF/ANIM: INFO, EVNT, SKEL(BONE tree), FRAM per frame)
#  per bone per frame: position (3 floats) + quaternion x,y,z,w (4 floats), local to the parent bone
# =====================================================================
class Kin:
    pass


def read_kin(b):
    b = bytes(b)
    if b[:4] != b"JIRF" or b[8:12] != b"ANIM":
        raise ValueError("not a Trainz .kin (no JIRF/ANIM header)")
    total = min(struct.unpack_from("<I", b, 4)[0] + 8, len(b))
    k = Kin()
    k.src_name, k.nframes, k.fps = "", 0, 30
    k.bones, k.frames = [], []              # bones: (name, parent index);  frames: [(pos, quat) per bone]

    def bone(body, o, parent, depth=0):
        if depth > 64:
            raise ValueError("bone tree too deep")
        nl = struct.unpack_from("<I", body, o + 4)[0]
        o += 8
        name = bytes(body[o:o + nl]).split(b"\0")[0].decode("latin1")
        o += nl
        nch = struct.unpack_from("<I", body, o)[0]
        o += 4
        k.bones.append((name, parent))
        me = len(k.bones) - 1
        for _ in range(nch):
            if bytes(body[o:o + 4]) != b"BONE":
                raise ValueError("expected BONE")
            sz = struct.unpack_from("<I", body, o + 4)[0]
            bone(body, o + 8, me, depth + 1)
            o += 8 + sz

    p = 12
    while p + 8 <= total:
        tag = bytes(b[p:p + 4])
        sz = struct.unpack_from("<I", b, p + 4)[0]
        body = b[p + 8:p + 8 + sz]
        if tag == b"INFO":
            nl = struct.unpack_from("<I", body, 4)[0]
            k.src_name = bytes(body[8:8 + nl]).split(b"\0")[0].decode("latin1")
            k.nframes, fps = struct.unpack_from("<II", body, 8 + nl)
            k.fps = fps if 0 < fps <= 1000 else 30
        elif tag == b"SKEL":
            o = 4
            while o + 8 <= len(body) and bytes(body[o:o + 4]) == b"BONE":
                bone(body, o + 8, -1)
                o += 8 + struct.unpack_from("<I", body, o + 4)[0]
        elif tag == b"FRAM":
            nb = len(k.bones)
            if nb and len(body) >= 8 + nb * 28:
                idx = struct.unpack_from("<I", body, 4)[0]
                v = struct.unpack_from("<%df" % (nb * 7), body, 8)
                k.frames.append((idx, [(v[i * 7:i * 7 + 3], v[i * 7 + 3:i * 7 + 7]) for i in range(nb)]))
        p += 8 + sz
    k.frames.sort(key=lambda f: f[0])
    if not k.bones:
        raise ValueError("kin has no skeleton")
    return k


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)


def _qrot(q, v):
    qx, qy, qz, qw = q
    t = (2 * (qy * v[2] - qz * v[1]), 2 * (qz * v[0] - qx * v[2]), 2 * (qx * v[1] - qy * v[0]))
    return (v[0] + qw * t[0] + qy * t[2] - qz * t[1], v[1] + qw * t[1] + qz * t[0] - qx * t[2],
            v[2] + qw * t[2] + qx * t[1] - qy * t[0])


def _qnorm(q):
    l = sum(c * c for c in q) ** 0.5
    if not l or l != l:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(c / l for c in q)


def rotq(q):
    """quaternion Z-up -> glTF Y-up (same axis change as rot(); it is a proper rotation so xyz just maps)"""
    x, y, z = rot(q[0], q[1], q[2])
    return (x, y, z, q[3])


def add_rig(glb, kin, label, log):
    """Bone hierarchy as empties + one glTF animation.  Returns (bone node indices, world rest positions in glTF space)."""
    used = {n.get("name") for n in glb.nodes}
    ids, rest_w = [], []
    f0 = kin.frames[0][1] if kin.frames else [((0, 0, 0), (0, 0, 0, 1))] * len(kin.bones)
    wp, wq = [], []
    for i, (name, par) in enumerate(kin.bones):
        p, q = f0[i]
        q = _qnorm(q)
        nm = name if name not in used else "%s.%s" % (label, name)
        used.add(nm)
        node = {"name": nm, "translation": list(rot(*p)), "rotation": list(rotq(q))}
        if par >= 0:
            node["_child"] = True
            glb.nodes[ids[par]].setdefault("children", []).append(len(glb.nodes))
            wq.append(_qnorm(_qmul(wq[par], q)))
            r = _qrot(wq[par], p)
            wp.append((wp[par][0] + r[0], wp[par][1] + r[1], wp[par][2] + r[2]))
        else:
            wq.append(q)
            wp.append(tuple(p))
        glb.nodes.append(node)
        ids.append(len(glb.nodes) - 1)
        rest_w.append(rot(*wp[-1]))
    nf = len(kin.frames)
    if nf < 2:
        return ids, rest_w
    times = [f[0] / float(kin.fps) for f in kin.frames]
    t0 = times[0]
    times = [t - t0 for t in times]
    t_acc = glb.acc(struct.pack("<%df" % nf, *times), 5126, nf, "SCALAR", None, ([min(times)], [max(times)]))
    samplers, channels = [], []
    moving = 0
    for i in range(len(kin.bones)):
        P = [kin.frames[f][1][i][0] for f in range(nf)]
        Q = []
        for f in range(nf):
            q = _qnorm(kin.frames[f][1][i][1])
            if Q and sum(a * b for a, b in zip(q, Q[-1])) < 0:      # shortest path for LINEAR interpolation
                q = tuple(-c for c in q)
            Q.append(q)
        if any(max(abs(P[f][c] - P[0][c]) for c in range(3)) > 1e-6 for f in range(nf)):
            data = b"".join(struct.pack("<3f", *rot(*p)) for p in P)
            samplers.append({"input": t_acc, "output": glb.acc(data, 5126, nf, "VEC3"), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": ids[i], "path": "translation"}})
        if any(max(abs(Q[f][c] - Q[0][c]) for c in range(4)) > 1e-6 for f in range(nf)):
            data = b"".join(struct.pack("<4f", *rotq(q)) for q in Q)
            samplers.append({"input": t_acc, "output": glb.acc(data, 5126, nf, "VEC4"), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": ids[i], "path": "rotation"}})
            moving += 1
    if channels:
        glb.anims.append({"name": label, "samplers": samplers, "channels": channels})
    return ids, rest_w


def _pick_bone(kin, names):
    """bone that belongs to a mesh: name tail matches one of names (throttle <-> b.r.throttle)"""
    want = [_norm(n) for n in names if n]
    best = None
    for i, (bn, par) in enumerate(kin.bones):
        if par < 0:
            continue
        t = _norm(bn[4:] if bn.lower().startswith("b.r.") else bn)
        if not t:
            continue
        for w in want:
            if t == w:
                return i
            if len(t) >= 3 and (t in w or w in t) and best is None:
                best = i
    if best is not None:
        return best
    kids = [i for i, (_, p) in enumerate(kin.bones) if p >= 0]
    return kids[0] if len(kids) == 1 else None


def add_animations(pkg, asset, glb, meshes, log):
    kins = sorted(k for k in asset.files if k.endswith(".kin"))
    rigs = {}
    for kf in kins:
        base = kf[:-4]
        try:
            kin = read_kin(pkg.read(asset, kf))
            ids, rest_w = add_rig(glb, kin, base, log)
        except Exception as e:
            log("   animation %s skipped: %s" % (kf, e))
            continue
        rigs[base] = (kin, ids, rest_w)
        n_anim = len(kin.frames)
        log("   animation %-20s %d bones, %d frames @ %d fps (%.2fs)%s" % (
            base, len(kin.bones), n_anim, kin.fps, max(n_anim - 1, 0) / float(kin.fps),
            "" if n_anim > 1 else "  (single pose, no motion)"))
    for lab, im in meshes:
        node = glb.meshnode.get(lab)
        base = im[:-3] if im.endswith(".im") else im
        kb = glb.kinmap.get(base)
        if kb not in rigs:
            kb = next((b for b in (base, lab) if b in rigs), None)
        if node is None or kb is None:
            continue
        kin, ids, rest_w = rigs[kb]
        bi = _pick_bone(kin, [base, lab, kb])
        why = ""
        if bi is None:
            kids = [i for i, (_, p) in enumerate(kin.bones) if p >= 0]
            bi = next((i for i, (_, p) in enumerate(kin.bones) if p < 0), 0)
            mesh = glb.meshes[glb.nodes[node]["mesh"]]
            mn_, mx_ = glb.bbox.get(lab, ([0] * 3, [0] * 3))
            sz_ = max(mx_[k] - mn_[k] for k in range(3))
            ctr_ = [(mx_[k] + mn_[k]) / 2 for k in range(3)]
            at_origin = sum(c * c for c in ctr_) ** 0.5 <= sz_ * 1.5 + 0.5
            prims = mesh["primitives"]
            if at_origin and len(prims) == len(kids) and len(prims) > 1:
                # one material group per bone, each modelled around its own pivot: group k belongs to bone k (by order)
                n0 = glb.nodes[node]
                for k_, (prim, b_) in enumerate(zip(prims, kids)):
                    if k_ == 0:
                        mesh["primitives"] = [prim]
                        tgt, ti_ = n0, node
                    else:
                        glb.meshes.append({"name": "%s.%d" % (lab, k_), "primitives": [prim]})
                        tgt = {"name": "%s.%s" % (lab, kin.bones[b_][0]), "mesh": len(glb.meshes) - 1}
                        glb.nodes.append(tgt)
                        ti_ = len(glb.nodes) - 1
                    tgt["_child"] = True
                    tgt.pop("translation", None)
                    glb.nodes[ids[b_]].setdefault("children", []).append(ti_)
                log("   bound %-22s group-per-bone: %d groups -> %d bones, by order (check the result)" % (lab, len(prims), len(kids)))
                continue
            why = "  (several bones, none matches the mesh name: bound to the root; skinned meshes are not supported)"
        n = glb.nodes[node]
        w = rest_w[bi]
        mn, mx = glb.bbox.get(lab, ([0] * 3, [0] * 3))
        size = max(mx[k] - mn[k] for k in range(3))
        centre = [(mx[k] + mn[k]) / 2 for k in range(3)]
        if sum(c * c for c in centre) ** 0.5 > size * 1.5 + 0.5:
            loc = [-w[0], -w[1], -w[2]]          # modelled in place: undo the bone offset so it stays put at rest
        else:                                    # pivot at its own origin: the bone supplies the position
            cur = n.get("translation", w)        # (place_parts may already have used an attach point)
            loc = [cur[0] - w[0], cur[1] - w[1], cur[2] - w[2]]
        if any(abs(c) > 1e-5 for c in loc):
            n["translation"] = loc
        else:
            n.pop("translation", None)
        n["_child"] = True
        glb.nodes[ids[bi]].setdefault("children", []).append(node)
        log("   bound %-22s to bone %s%s" % (lab, kin.bones[bi][0], why))
    return len(rigs)


def parse_config(txt):
    """tiny parser for Trainz config.txt  ->  list of (key, value-or-list)"""
    txt = re.sub(r'(?m)("[^"\n]*")|#.*$|//.*$', lambda m: m.group(1) or "", txt)
    toks = re.findall(r'"[^"]*"|[{}=]|[^\s{}="]+', txt)
    pos = 0

    def block():
        nonlocal pos
        out = []
        while pos < len(toks):
            t = toks[pos]
            if t == "}":
                pos += 1
                return out
            if t in "{=":
                pos += 1
                continue
            key = t.strip('"')
            pos += 1
            if pos < len(toks) and toks[pos] == "=":
                pos += 1
                val = toks[pos].strip('"') if pos < len(toks) else ""
                pos += 1
                out.append((key, val))
            elif pos < len(toks) and toks[pos] == "{":
                pos += 1
                out.append((key, block()))
            else:
                out.append((key, ""))
        return out
    return block()


def _find_key(tree, name):
    for k, v in tree:
        if k.lower() == name and isinstance(v, list):
            return v
        if isinstance(v, list):
            r = _find_key(v, name)
            if r is not None:
                return r
    return None


def _strings(v):
    if isinstance(v, list):
        for _, x in v:
            yield from _strings(x)
    elif isinstance(v, str):
        yield v


def node_config(pkg, node):
    """convert the asset's own tag tree (where compiled configs live) into (key, value|list) pairs"""
    out = []
    for k in node.kids:
        if k.name == "files":
            continue
        if k.type == 0:
            out.append((k.name, node_config(pkg, k)))
        elif k.type == 3:
            out.append((k.name, bytes(pkg.data[k.off:k.end]).split(b"\0")[0].decode("latin1")))
    return out


def _norm(n):
    n = n.lower()
    n = n[2:] if n.startswith("a.") else n
    return re.sub(r"[^a-z0-9]", "", n)


def fuzzy_attach(label, attach):
    """best attach point for a mesh whose names differ slightly (throttle_lever <-> a.throttle)"""
    import difflib
    ln = _norm(label)
    scored = []
    for name in attach:
        an = _norm(name)
        if not an:
            continue
        sc = difflib.SequenceMatcher(None, ln, an).ratio()
        if len(an) >= 3 and an in ln or len(ln) >= 3 and ln in an:
            sc = max(sc, 0.8)
        scored.append((sc, name))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= 0.6 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
        return scored[0][1]
    return None


def place_parts(pkg, asset, glb, meshes, log):
    """Animated / accessory meshes are stored around their own pivot (the origin).  Move each one to the
    attach point that belongs to it: via the mesh-table in the asset's config, by name, or (flagged) by
    similar name.  Meshes that own attach points themselves are the 'main' meshes and stay put."""
    table = {}                                # mesh basename -> [candidate attach names]
    trees = []
    try:
        trees.append(node_config(pkg, asset.node))
    except Exception:
        pass
    if asset.has("config.txt"):
        try:
            trees.append(parse_config(pkg.read(asset, "config.txt").decode("latin1")))
        except Exception as e:
            log("   (config.txt not read: %s)" % e)
    for tree in trees:
        try:
            mt = _find_key(tree, "mesh-table") or []
            for ename, entry in mt:
                if not isinstance(entry, list):
                    continue
                mesh = next((v for k, v in entry if k.lower() == "mesh" and isinstance(v, str)), "")
                base = mesh.replace("\\", "/").split("/")[-1].lower()
                if base.endswith(".im"):
                    base = base[:-3]
                if not base:
                    continue
                cands = [ename, "a." + ename] + [x for x in _strings(entry) if x.lower() != mesh.lower()]
                table.setdefault(base, []).extend(cands)
                for x in _strings(entry):
                    if x.lower().endswith(".kin"):
                        glb.kinmap.setdefault(base, _base(x)[:-4])
        except Exception as e:
            log("   (mesh-table not read: %s)" % e)
    glb.trees = trees
    owners = {src for (_, src, _) in glb.attach.values()}      # meshes that define attach points
    loose = []
    for lab, im in meshes:
        node = glb.meshnode.get(lab)
        if node is None or lab in owners and lab not in table:
            continue
        base = im[:-3] if im.endswith(".im") else im
        mn, mx = glb.bbox.get(lab, ([0] * 3, [0] * 3))
        size = max(mx[k] - mn[k] for k in range(3))
        centre = [(mx[k] + mn[k]) / 2 for k in range(3)]
        if sum(c * c for c in centre) ** 0.5 > size * 1.5 + 0.5:
            continue                          # already sits at its real position in the model
        names = table.get(base.lower(), []) + [base, "a." + base, lab, "a." + lab]
        key = next((n.lower() for n in names if n.lower() in glb.attach), None)
        how = ""
        if key is None:
            free = [n for n, (_, src, _) in glb.attach.items() if src != lab]
            key = fuzzy_attach(lab, free)
            how = "  (matched by similar name - check it)"
        if key is None:
            if not (asset.has(base + ".kin") or base in glb.kinmap):   # animated parts get their position from the .kin bones
                loose.append((lab, base))
            continue
        t, src, m = glb.attach[key]
        glb.nodes[node]["translation"] = list(rot(*t))
        q = attach_quat(m, getattr(glb, "attach_transpose", False))
        if q:
            glb.nodes[node]["rotation"] = list(q)
        log("   placed %-22s at %s%s%s" % (lab, key, how, "  (+rotation)" if q else ""))
    if loose:
        log("   NOT placed (no attach point found, left at origin): %s" % ", ".join(l for l, _ in loose))
        log("   attach points available: %s" % ", ".join(sorted(glb.attach)[:60]))
        log("   asset config keys: %s" % ", ".join(sorted({k for t_ in trees for k, _ in t_})[:40]))
        mt_names = sorted(table)
        if mt_names:
            log("   config mesh-table meshes: %s" % ", ".join(mt_names[:40]))


_KUID = re.compile(r"<?kuid2?:(-?\d+):(-?\d+)(?::\d+)?>?", re.I)


def _kuid_core(x):
    m = _KUID.search(x or "")
    return "kuid:%s:%s" % (m.group(1), m.group(2)) if m else None


def _tree_text(v, depth=0):
    if isinstance(v, list):
        return "{" + "; ".join("%s=%s" % (k, _tree_text(x, depth + 1)) for k, x in v) + "}"
    return repr(v)


def add_bogeys(pkg, asset, glb, log):
    """Trucks are separate assets referenced by the car's `bogeys` config table: place the main truck mesh at each attach point."""
    trees = getattr(glb, "trees", [])
    tab = None
    for t in trees:
        tab = _find_key(t, "bogeys")
        if tab:
            break
    if not tab:
        return 0
    kt = {}
    for t in trees:
        for k, v in (_find_key(t, "kuid-table") or []):
            if isinstance(v, str):
                kt[k.lower()] = v
    made = 0
    cache = {}
    bad = []
    for ename, entry in tab:
        if not isinstance(entry, list):
            continue
        strs = [x for x in _strings(entry)]
        core = None
        for x in strs:
            core = _kuid_core(x) or _kuid_core(kt.get(x.lower()))
            if core:
                break
        att = next((x.lower() for x in strs if x.lower() in glb.attach), None)
        if att is None:
            att = next(("a." + x.lower() for x in strs if "a." + x.lower() in glb.attach), None)
        if att is None and ename.isdigit() and ("a.bog%s" % ename) in glb.attach:
            att = "a.bog%s" % ename
        if not core or att is None:
            bad.append(ename)
            if os.environ.get("CDP_DEBUG"):
                log("   bogey entry %s not understood (kuid=%s attach=%s): %s" % (ename, core, att, _tree_text(entry)[:300]))
            continue
        if core not in cache:
            ba = next((a for a in pkg.assets if core in a.name.lower().replace("kuid2", "kuid")), None)
            hit = None
            if ba is not None:
                ms = pick_meshes(pkg, ba, False, False)
                if ms:
                    lab, im = ms[0]
                    lab = "%s_%s" % ("bogey", core.split(":")[-1])
                    add_mesh(pkg, ba, glb, im, lab, log)
                    mi = glb.nodes[glb.meshnode[lab]]["mesh"]
                    glb.nodes[glb.meshnode[lab]]["_child"] = True      # template node, hidden under no scene root
                    hit = (mi, lab)
            if hit is None:
                log("   bogey %s is not in this package: place it by hand" % core)
            cache[core] = hit
        hit = cache[core]
        if hit is None:
            continue
        t, _, m = glb.attach[att]
        node = {"name": "bogey%s.%s" % (ename, att), "mesh": hit[0], "translation": list(rot(*t))}
        q = attach_quat(m, getattr(glb, "attach_transpose", False))
        if q:
            node["rotation"] = list(q)
        glb.nodes.append(node)
        made += 1
        log("   bogey %-3s %s placed at %s%s" % (ename, core, att, "  (+rotation)" if q else ""))
    if bad:
        log("   bogeys not placed automatically (config entries %s unreadable): the trucks are separate .glb files - "
            "place them at the a.bog* empties by hand" % ", ".join(bad))
    # template nodes (first instance) would duplicate a truck at the origin: drop their mesh
    for core, hit in cache.items():
        if hit:
            glb.nodes[glb.meshnode[hit[1]]].pop("mesh", None)
    return made


def pick_meshes(pkg, asset, all_lods, keep_all=False):
    ims = [k for k in asset.files if k.endswith(".im")]
    out = []        # (label, imfile)
    covered = set()
    for k in sorted(asset.files):
        if not k.endswith(".lm.txt"):
            continue
        txt = pkg.read(asset, k).decode("latin1")
        lods = []
        for a_, b_ in re.findall(r'mesh\s*\(\s*"([\d.]+)"\s*\)\s*\{\s*name\s*=\s*"([^"]+)"', txt):
            try:
                lods.append((float(a_), b_.replace("\\", "/").split("/")[-1].lower()))
            except ValueError:
                pass
        for _, b in lods:
            covered.add(b)
        # best LOD = the biggest mesh (ties: highest LOD number)
        def _size(l):
            hit = pkg.find(asset, l[1])
            return (struct.unpack_from("<I", pkg.data, hit[0].files[hit[1]].off)[0], l[0]) if hit else None
        have = sorted([l for l in lods if _size(l) is not None], key=_size, reverse=True)
        label = k[:-len(".lm.txt")]
        if all_lods:
            for lod, b in have:
                out.append(("%s_lod%g" % (label, lod), b))
        elif have:
            out.append((label, have[0][1]))
    for im in ims:
        if im not in covered:
            out.append((im[:-3], im))
    if not keep_all:   # drop helper meshes that only clutter Blender
        out = [o for o in out if not re.search(r"shadow|collision", o[0], re.I)]
    return out


def validate_glb(path):
    """strict re-read of a written .glb; returns list of problems"""
    bad = []
    with open(path, "rb") as fh:
        b = fh.read()
    if b[:4] != b"glTF" or struct.unpack_from("<I", b, 8)[0] != len(b):
        bad.append("bad header/length")
        return bad
    jl = struct.unpack_from("<I", b, 12)[0]
    def nope(c):
        raise ValueError("non-standard JSON constant " + c)
    doc = json.loads(b[20:20 + jl].decode("ascii"), parse_constant=nope)
    bl = struct.unpack_from("<I", b, 20 + jl)[0]
    for i, a in enumerate(doc.get("accessors", [])):
        v = doc["bufferViews"][a["bufferView"]]
        size = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]] * {5126: 4, 5123: 2}[a["componentType"]] * a["count"]
        if size > v["byteLength"] or v["byteOffset"] + v["byteLength"] > bl:
            bad.append("accessor %d out of range" % i)
        if v["byteLength"] == 0 or a["count"] == 0:
            bad.append("accessor %d empty" % i)
    for key in ("images", "materials", "meshes", "nodes", "textures"):
        for i, o in enumerate(doc.get(key, [])):
            if "name" in o and not isinstance(o["name"], str):
                bad.append("%s %d: name is not a string" % (key, i))
    for i, o in enumerate(doc.get("images", [])):
        if o.get("mimeType") not in ("image/png", "image/jpeg"):
            bad.append("image %d: bad mimeType" % i)
    nacc, nnode = len(doc.get("accessors", [])), len(doc.get("nodes", []))
    for ai, an in enumerate(doc.get("animations", [])):
        for sm in an["samplers"]:
            if sm["input"] >= nacc or sm["output"] >= nacc:
                bad.append("animation %d: sampler accessor out of range" % ai)
        for ch in an["channels"]:
            if ch["target"]["node"] >= nnode or ch["sampler"] >= len(an["samplers"]):
                bad.append("animation %d: bad channel" % ai)
    for m in doc.get("meshes", []):
        if not m["primitives"]:
            bad.append("mesh %s has no primitives" % m.get("name"))
    return bad


def dump_tree(nodes, depth=0, maxdepth=6):
    for n in nodes:
        print("%s%s  [type %d, %d bytes]" % ("  " * depth, n.name or "<unnamed>", n.type, n.end - n.off))
        if n.type == 0 and depth < maxdepth:
            dump_tree(n.kids, depth + 1, maxdepth)


def main():
    ap = argparse.ArgumentParser(description="Convert Trainz .cdp packages to .glb")
    ap.add_argument("cdp", help=".cdp package (or a loose .kin file)")
    ap.add_argument("out", nargs="?", default="cdp_out")
    ap.add_argument("--list", action="store_true", help="just list assets and files")
    ap.add_argument("--tree", action="store_true", help="print the raw tag tree (for debugging)")
    ap.add_argument("--extract-only", help="like --extract but only files ending with this text, e.g. .kin")
    ap.add_argument("--extract", action="store_true", help="also write every file of the package to disk")
    ap.add_argument("--all-lods", action="store_true", help="export every LOD level instead of the best one")
    ap.add_argument("--keep-all", action="store_true", help="also export shadow / collision helper meshes")
    ap.add_argument("--no-anim", action="store_true", help="do not export .kin animations")
    ap.add_argument("--no-bogeys", action="store_true", help="do not place the trucks referenced by the car's config")
    ap.add_argument("--attach-transpose", action="store_true", help="read attach-point rotation matrices transposed (try this if levers / trucks face the wrong way)")
    ap.add_argument("--no-attach", action="store_true", help="do not export attach points as empty objects")
    ap.add_argument("--debug", action="store_true", help="print which normals / winding were chosen for every material group")
    ap.add_argument("--asset", help="only assets whose name / username contains this text")
    a = ap.parse_args()
    _disable_quickedit()
    if a.debug:
        os.environ["CDP_DEBUG"] = "1"

    if not os.path.isfile(a.cdp):
        raise SystemExit("File not found: %s\n(put the .cdp path in quotes if it contains spaces)" % a.cdp)
    with open(a.cdp, "rb") as f:
        data = f.read()
    if data[:4] == b"JIRF":                          # loose .kin
        kin = read_kin(data)
        print("cdp2glb %s" % VERSION)
        print("%s: %d bones, %d frames @ %d fps   (source: %s)" % (
            os.path.basename(a.cdp), len(kin.bones), len(kin.frames), kin.fps, kin.src_name))
        for i, (n_, p_) in enumerate(kin.bones):
            print("   bone %d %-16s parent %s" % (i, n_, kin.bones[p_][0] if p_ >= 0 else "-"))
        if a.list:
            return
        os.makedirs(a.out, exist_ok=True)
        glb = GLB()
        base = os.path.splitext(os.path.basename(a.cdp))[0]
        add_rig(glb, kin, base, print)
        path = os.path.join(a.out, base + "_anim.glb")
        glb.write(path)
        probs = validate_glb(path)
        print("   -> %s   [%s]" % (path, "valid" if not probs else "PROBLEMS: " + "; ".join(probs[:5])))
        return
    pkg = Package(data)
    print("cdp2glb %s" % VERSION)
    print("%d assets" % len(pkg.assets))
    if a.tree:
        dump_tree(parse_tree(data, 16, len(data)))
        return
    if a.list:
        for s in pkg.assets:
            print("%-28s kind=%-14s %s  (%d files, %d meshes, %d anims)" % (s.name, s.info.get("kind", "?"), s.info.get("username", ""), len(s.files), sum(k.endswith(".im") for k in s.files), sum(k.endswith(".kin") for k in s.files)))
        return
    os.makedirs(a.out, exist_ok=True)
    log = print
    t0, made, used = time.time(), [0], set()
    failed = []

    def export_asset(s):
        label = s.info.get("username", "") or s.name
        if a.asset and a.asset.lower() not in (s.name + " " + label).lower():
            return
        safe = re.sub(r"[^\w.\-]+", "_", "%s_%s" % (label, s.name))[:80] or "asset"
        while safe.lower() in used:
            safe += "_"
        used.add(safe.lower())
        if a.extract or a.extract_only:
            d = os.path.join(a.out, "files", safe)
            os.makedirs(d, exist_ok=True)
            for k in s.files:
                if a.extract_only and not k.endswith(a.extract_only.lower()):
                    return
                try:
                    with open(os.path.join(d, os.path.basename(k)), "wb") as fh:
                        fh.write(pkg.read(s, k))
                except Exception as e:
                    print("  extract failed", k, e)
        try:
            meshes = pick_meshes(pkg, s, a.all_lods, a.keep_all)
        except Exception as e:
            print("\n== %s: could not read mesh list: %s" % (label, e))
            return
        if not meshes:
            return
        print("\n== %s  [%s] kind=%s" % (label, s.name, s.info.get("kind", "?")))
        glb = GLB()
        glb.keep_all, glb.no_attach = a.keep_all, a.no_attach
        glb.attach_transpose = a.attach_transpose
        for lab, im in meshes:
            try:
                tris, nm, na = add_mesh(pkg, s, glb, im, lab, log)
                print("   %-30s %-24s %6d tris, %d material group(s), %d attach points" % (lab, im, tris, nm, na))
            except Exception as e:
                print("   FAILED %s: %s" % (im, e))
        if glb.meshes:
            try:
                place_parts(pkg, s, glb, meshes, log)
            except Exception as e:
                print("   part placement failed: %s" % e)
            if not a.no_bogeys:
                try:
                    add_bogeys(pkg, s, glb, log)
                except Exception as e:
                    print("   bogey placement failed: %s: %s" % (type(e).__name__, e))
            if not a.no_anim:
                try:
                    add_animations(pkg, s, glb, meshes, log)
                except Exception as e:
                    print("   animation export failed: %s" % e)
            path = os.path.join(a.out, safe + ".glb")
            try:
                glb.write(path)
                probs = validate_glb(path)
            except Exception as e:
                print("   FAILED writing %s: %s" % (path, e))
                return
            made[0] += 1
            print("   -> %s   [%s]" % (path, "valid" if not probs else "PROBLEMS: " + "; ".join(probs[:5])))
    for s in pkg.assets:
        try:
            export_asset(s)
        except Exception as e:
            failed.append(s.name)
            print("\n!! asset %s failed: %s: %s" % (s.name, type(e).__name__, e))
    print("\nDone: %d .glb file(s) written to %s in %.1fs" % (made[0], os.path.abspath(a.out), time.time() - t0))
    if failed:
        print("%d asset(s) failed: %s" % (len(failed), ", ".join(failed[:20])))
    if not made[0]:
        print("Nothing was exported. Try --list to see asset names, and check the --asset text.")


if __name__ == "__main__":
    main()
