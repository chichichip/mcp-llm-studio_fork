# -*- coding: utf-8 -*-
r"""iso_extract.py — ISO 파일에서 내용물을 꺼낸다. **표준 라이브러리만** 쓴다.

왜 필요한가:
    폐쇄망 반입이 ISO로만 되는데, 회사 PC에서 ISO 마운트가 실패하는 일이 있다
    ("죄송합니다. 파일을 탑재하는 동안 문제가 발생했습니다"). 7-Zip 같은 압축 프로그램이
    없으면 안에 든 .gguf 하나를 꺼낼 방법이 없어 반입이 통째로 막힌다. ISO는 사실
    구조가 단순한 컨테이너라, 마운트 없이 파이썬으로 바로 읽어 낼 수 있다.

사용:
    python iso_extract.py 파일.iso                 # 안에 뭐가 있는지 목록만
    python iso_extract.py 파일.iso -o C:\models    # 전부 꺼내기
    python iso_extract.py 파일.iso -o . -p gguf    # 이름에 gguf가 든 것만

읽는 형식:
    ISO9660 (+ Rock Ridge 무시) 과 **Joliet**(긴 파일명). 굽는 프로그램이 ISO9660만
    쓰면 파일명이 8.3으로 잘려 `EMBEDD~1.GGU` 처럼 보이는데, Joliet 확장이 있으면
    원래 이름을 살려 준다 — 그래서 Joliet을 먼저 찾는다.

⚠ **UDF 전용 ISO는 못 읽는다.** 대부분의 굽는 프로그램은 호환성을 위해 ISO9660/Joliet을
   함께 넣지만, UDF만 쓴 이미지라면 이 도구가 '볼륨 기술자를 찾지 못했다'고 알린다.
   그때는 ISO를 UDF+ISO9660 혼합으로 다시 만들어야 한다.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

SECTOR = 2048            # ISO9660 논리 섹터 크기
VD_START = 16            # 볼륨 기술자는 16번 섹터부터
VD_TERMINATOR = 255      # 기술자 목록의 끝
FLAG_DIR = 0x02          # 디렉터리 레코드의 '디렉터리' 비트

# Joliet임을 알리는 이스케이프 시퀀스 (UCS-2 레벨 1/2/3)
JOLIET_ESCAPES = (b"%/@", b"%/C", b"%/E")


class IsoError(Exception):
    """사용자에게 그대로 보여줄 안내 메시지."""


def _both_endian32(buf: bytes, off: int) -> int:
    """ISO9660은 32비트 값을 리틀·빅 양쪽으로 적어 둔다. 앞(리틀)만 읽으면 된다."""
    return struct.unpack_from("<I", buf, off)[0]


def _find_descriptor(f) -> tuple[bytes, bool]:
    """볼륨 기술자를 찾는다. 반환: (기술자 2048바이트, joliet 여부).

    Joliet(보조 기술자)이 있으면 그쪽을 쓴다 — 파일명이 안 잘리기 때문이다.
    """
    primary = None
    for i in range(VD_START, VD_START + 32):  # 32개면 충분하다
        f.seek(i * SECTOR)
        vd = f.read(SECTOR)
        if len(vd) < SECTOR or vd[1:6] != b"CD001":
            break
        kind = vd[0]
        if kind == VD_TERMINATOR:
            break
        if kind == 1 and primary is None:      # Primary Volume Descriptor
            primary = vd
        elif kind == 2:                        # Supplementary — Joliet일 수 있다
            esc = vd[88:120]
            if any(e in esc for e in JOLIET_ESCAPES):
                return vd, True
    if primary is None:
        raise IsoError(
            "ISO9660 볼륨 기술자를 찾지 못했습니다. UDF 전용 이미지이거나 파일이 "
            "손상됐을 수 있습니다 — 크기가 원본과 같은지 확인하고, 다시 만들 때는 "
            "UDF와 ISO9660을 함께 넣으세요."
        )
    return primary, False


def _decode_name(raw: bytes, joliet: bool) -> str:
    """디렉터리 레코드의 파일 이름. Joliet은 UCS-2 빅엔디안이다."""
    name = raw.decode("utf-16-be", "replace") if joliet else raw.decode("ascii", "replace")
    # ISO9660은 이름 끝에 ';1' 같은 버전 번호를 붙인다. 실제 파일명이 아니므로 뗀다.
    return name.split(";")[0].rstrip(".")


def _read_dir(f, lba: int, size: int, joliet: bool) -> list[dict]:
    """디렉터리 하나의 레코드 목록. 반환: [{name, lba, size, is_dir}, ...]"""
    f.seek(lba * SECTOR)
    data = f.read(size)
    out: list[dict] = []
    pos = 0
    while pos < len(data):
        rec_len = data[pos]
        if rec_len == 0:
            # 레코드는 섹터 경계를 넘지 않는다 — 남은 부분은 패딩이니 다음 섹터로.
            pos = (pos // SECTOR + 1) * SECTOR
            if pos >= len(data):
                break
            continue
        rec = data[pos:pos + rec_len]
        if len(rec) < 33:
            break
        name_len = rec[32]
        raw_name = rec[33:33 + name_len]
        # 이름 길이 1의 0x00/0x01은 '자기 자신'과 '상위' 항목이다 — 건너뛴다.
        if not (name_len == 1 and raw_name in (b"\x00", b"\x01")):
            out.append({
                "name": _decode_name(raw_name, joliet),
                "lba": _both_endian32(rec, 2),
                "size": _both_endian32(rec, 10),
                "is_dir": bool(rec[25] & FLAG_DIR),
            })
        pos += rec_len
    return out


def walk(path: str) -> tuple[list[dict], bool]:
    """ISO 안의 모든 파일 목록. 반환: ([{path, lba, size}, ...], joliet 여부)."""
    files: list[dict] = []
    with open(path, "rb") as f:
        vd, joliet = _find_descriptor(f)
        root = vd[156:190]                      # 루트 디렉터리 레코드 (34바이트 고정)
        stack = [("", _both_endian32(root, 2), _both_endian32(root, 10))]
        seen: set[int] = set()
        while stack:
            prefix, lba, size = stack.pop()
            if lba in seen:                     # 손상된 이미지의 순환 방지
                continue
            seen.add(lba)
            for e in _read_dir(f, lba, size, joliet):
                full = f"{prefix}/{e['name']}" if prefix else e["name"]
                if e["is_dir"]:
                    stack.append((full, e["lba"], e["size"]))
                else:
                    files.append({"path": full, "lba": e["lba"], "size": e["size"]})
    files.sort(key=lambda d: d["path"])
    return files, joliet


def extract(iso: str, entry: dict, out_dir: str) -> str:
    """파일 하나를 꺼낸다. 반환: 저장한 경로.

    큰 파일(.gguf는 수백 MB)을 통째로 메모리에 올리지 않게 조각내어 옮긴다.
    """
    dest = os.path.join(out_dir, entry["path"].replace("/", os.sep))
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    remaining = entry["size"]
    with open(iso, "rb") as f, open(dest, "wb") as w:
        f.seek(entry["lba"] * SECTOR)
        while remaining > 0:
            block = f.read(min(1024 * 1024, remaining))
            if not block:
                raise IsoError(
                    f"'{entry['path']}'를 끝까지 읽지 못했습니다 — ISO가 잘렸을 수 "
                    "있습니다(원본과 크기 비교)."
                )
            w.write(block)
            remaining -= len(block)
    return dest


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n}B"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ISO에서 파일을 꺼낸다 (마운트·7-Zip 불필요, 표준 라이브러리만)"
    )
    ap.add_argument("iso", help="ISO 파일 경로")
    ap.add_argument("-o", "--out", help="꺼낼 폴더. 없으면 목록만 보여준다")
    ap.add_argument("-p", "--pattern", default="",
                    help="이름에 이 문자열이 든 파일만 (대소문자 무시)")
    a = ap.parse_args()

    if not os.path.isfile(a.iso):
        print(f"[오류] 파일이 없습니다: {a.iso}", file=sys.stderr)
        return 1

    try:
        files, joliet = walk(a.iso)
    except IsoError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1

    size = os.path.getsize(a.iso)
    print(f"{a.iso}  ({_human(size)}, {'Joliet — 긴 이름 보존' if joliet else 'ISO9660 — 이름이 8.3으로 잘렸을 수 있음'})")

    pat = a.pattern.lower()
    picked = [e for e in files if pat in e["path"].lower()] if pat else files
    if not picked:
        print(f"'{a.pattern}'에 해당하는 파일이 없습니다. (전체 {len(files)}개)")
        for e in files[:20]:
            print(f"  {_human(e['size']):>9}  {e['path']}")
        return 1

    print(f"파일 {len(picked)}개" + (f" (전체 {len(files)}개 중)" if pat else ""))
    for e in picked:
        print(f"  {_human(e['size']):>9}  {e['path']}")

    if not a.out:
        print("\n꺼내려면 -o 로 폴더를 지정하세요.  예: -o C:\\models")
        return 0

    print()
    for e in picked:
        dest = extract(a.iso, e, a.out)
        got = os.path.getsize(dest)
        mark = "OK" if got == e["size"] else "★크기 불일치"
        print(f"  {mark}  {dest}  ({_human(got)})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IsoError as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)
