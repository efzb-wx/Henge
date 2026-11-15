"""Validate relabelling with LLVM, independently of Henge's bitstream reader.

Default fixtures are generated from our own IR. --archive optionally checks a
locally supplied shader ZIP; those captured shaders are never repository fixtures.
This verifies binary integrity, not execution by an iOS GPU/compiler.
"""
import argparse
import ctypes
import hashlib
import re
import struct
import zipfile
from pathlib import Path

import llvmlite.binding as llvm


def tag(name, payload):
    return name + struct.pack("<H", len(payload)) + payload


def fixture(language=(3, 2), triple="air64-apple-macosx15.6.0", air=(2, 7)):
    ir = f'''target triple = "{triple}"
define float @sample(float %x) {{
  %result = fmul float %x, 4.0
  ret float %result
}}
!air.version = !{{!0}}
!air.language_version = !{{!1}}
!0 = !{{i32 {air[0]}, i32 {air[1]}, i32 0}}
!1 = !{{!"Metal", i32 {language[0]}, i32 {language[1]}, i32 0}}
'''
    with llvm.create_context() as ctx, llvm.parse_assembly(ir, context=ctx) as module:
        raw = module.as_bitcode()
    if raw[:4] == b"\xde\xc0\x17\x0b":
        start, length = struct.unpack_from("<2I", raw, 8)
        raw = raw[start:start + length]
    code = struct.pack("<5I", 0x0B17C0DE, 0, 20, len(raw), 0xFFFFFFFF) + raw
    tags = (tag(b"HASH", hashlib.sha256(code).digest())
            + tag(b"MDSZ", struct.pack("<Q", len(code)))
            + tag(b"OFFT", struct.pack("<3Q", 0, 0, 0))
            + tag(b"VERS", struct.pack("<4H", *air, *language)) + b"ENDT")
    entries = struct.pack("<I", len(tags) + 4) + tags
    code_offset = 88 + 4 + len(entries)
    header = bytearray(88)
    header[:4] = b"MTLB"
    struct.pack_into("<3HBBHH", header, 4, 0x8001, 2, 8, 0, 0x81, 15, 6)
    struct.pack_into("<9Q", header, 16, code_offset + len(code), 88, len(entries),
                     code_offset, 0, code_offset, 0, code_offset, len(code))
    return bytes(header) + struct.pack("<I", 1) + entries + code


def functions(data):
    fl = struct.unpack_from("<Q", data, 24)[0]
    bc = struct.unpack_from("<Q", data, 72)[0]
    count = struct.unpack_from("<I", data, fl)[0]
    entry = fl + 4
    for _ in range(count):
        size = struct.unpack_from("<I", data, entry)[0]
        pos, tags = entry + 4, {}
        while pos + 6 <= entry + size and data[pos:pos + 4] != b"ENDT":
            name, length = struct.unpack_from("<4sH", data, pos)
            tags[name] = data[pos + 6:pos + 6 + length]
            pos += 6 + length
        start = bc + struct.unpack_from("<Q", tags[b"OFFT"], 16)[0]
        length = struct.unpack("<Q", tags[b"MDSZ"])[0]
        yield tags, data[start:start + length]
        entry += size


def module(code, ctx):
    if code[:4] == b"\xde\xc0\x17\x0b":
        start, length = struct.unpack_from("<2I", code, 8)
        code = code[start:start + length]
    return llvm.parse_bitcode(code, context=ctx)


def version(ir, key):
    node = re.search(r"!" + key + r" = !\{!(\d+)\}", ir)[1]
    value = re.search(r"^!" + node + r" = (.*)$", ir, re.M)[1]
    return tuple(map(int, re.findall(r"i32 (\d+)", value)))


def check(convert, name, data, os):
    buf, why = ctypes.create_string_buffer(data), ctypes.c_char_p()
    changed = convert(buf, len(data), os, 2, ctypes.byref(why))
    assert changed > 0 and why.value is None, (name, changed, why.value)
    out = buf.raw[:len(data)]
    assert len(out) == len(data)
    assert struct.unpack_from("<H", out, 4)[0] == 1
    assert out[11] == 0x82
    assert struct.unpack_from("<H", out, 8)[0] == min(struct.unpack_from("<H", data, 8)[0], os - 10)
    assert struct.unpack_from("<2H", out, 12) == (os, 0)
    for (old_tags, old_code), (tags, code) in zip(functions(data), functions(out), strict=True):
        assert hashlib.sha256(code).digest() == tags[b"HASH"], (name, "invalid hash")
        air_major, air_minor, lang_major, lang_minor = struct.unpack("<4H", tags[b"VERS"])
        old_air_major, old_air_minor, old_lang_major, old_lang_minor = struct.unpack("<4H", old_tags[b"VERS"])
        assert (air_major, air_minor) == min((old_air_major, old_air_minor), (2, os - 11)), (name, "AIR tag")
        assert (lang_major, lang_minor) == min((old_lang_major, old_lang_minor), (3, os - 16)), (name, "Metal tag")
        # A separate LLVM context prevents named types gaining artificial .N suffixes.
        with llvm.create_context() as a, llvm.create_context() as b:
            with module(old_code, a) as before, module(code, b) as after:
                after.verify()
                assert f"-apple-ios{os}." in after.triple, (name, after.triple)
                normalize = lambda m: [re.sub(r"!\d+", "!metadata", str(f)) for f in m.functions]
                assert normalize(before) == normalize(after), (name, "instructions changed")
                assert [str(g) for g in before.global_variables] == [str(g) for g in after.global_variables]
                for key in ("air.version", "air.language_version"):
                    assert version(str(before), key) == version(str(after), key), (name, key)
    again, why = ctypes.create_string_buffer(out), ctypes.c_char_p()
    assert convert(again, len(out), os, 2, ctypes.byref(why)) == 0, (name, "not idempotent")
    assert again.raw[:len(out)] == out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    lib = ctypes.CDLL(str(args.library.resolve()))
    convert = lib.henge_test_relabel
    convert.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_char_p)]
    convert.restype = ctypes.c_int
    cases = [("modern", fixture()), ("old-language", fixture((1, 2))),
             ("versioned-triple", fixture(triple="air64_v27-apple-macosx15.6.0")),
             ("old-air", fixture((2, 2), air=(2, 4)))]
    if args.archive:
        with zipfile.ZipFile(args.archive) as z:
            cases.extend((n, z.read(n)) for n in z.namelist() if n.endswith(".metallib"))
    for os in (16, 17):
        for name, data in cases:
            check(convert, name, data, os)
        print(f"iOS {os}: {len(cases)} libraries passed LLVM, version tags, SHA-256, instruction preservation and idempotence")
    for malformed in (b"", b"MTLB", bytes(88)):
        buf, why = ctypes.create_string_buffer(malformed), ctypes.c_char_p()
        assert convert(buf, len(malformed), 16, 2, ctypes.byref(why)) < 0
    broken = fixture().replace(b"BC\xc0\xde", b"BAD!", 1)
    buf, why = ctypes.create_string_buffer(broken), ctypes.c_char_p()
    assert convert(buf, len(broken), 16, 3, ctypes.byref(why)) < 0
    assert why.value, "stage 3 must reject a partially rewritten unreadable module"
    print("Malformed-library rejection passed; GPU execution still requires device testing")


if __name__ == "__main__":
    main()
