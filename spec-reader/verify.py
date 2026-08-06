# -*- coding: utf-8 -*-
"""판독 결과 검증. VLM 없이 동작하므로 외부망에서 개발/테스트 가능."""

import re
from collections import Counter

GRID = 0.0625          # 1/16 inch
TOL = 0.002            # 치수 비교 허용오차


def to_float(v):
    """'.250' -> 0.250,  '1.062' -> 1.062,  None -> None"""
    if v is None:
        return None
    m = re.search(r"\d*\.\d+|\d+", str(v))
    return float(m.group()) if m else None


def range_max(v):
    """'.062-.082' -> 0.082,  '.250' -> 0.250"""
    if v is None:
        return None
    n = re.findall(r"\d*\.\d+|\d+", str(v))
    return float(n[-1]) if n else None


def compute_lk(rows):
    """[(dash, L-Kmax or None), ...]"""
    out = []
    for r in rows:
        L, Km = to_float(r.get("L")), range_max(r.get("K"))
        out.append((r.get("dash"), round(L - Km, 4) if (L is not None and Km is not None) else None))
    return out


def verify(rows, expect_lk=None, expect_rows=None):
    """반환: (issues, info)
    issues: 사람이 확인해야 할 항목 리스트
    info:   요약 dict
    """
    issues, info = [], {}
    info["n_rows"] = len(rows)

    if expect_rows is not None and len(rows) != expect_rows:
        issues.append(f"행 수 {len(rows)} (기대 {expect_rows})")

    # --- dash 중복 / 누락 ---
    dashes = [r.get("dash") for r in rows]
    dup = [d for d, c in Counter(dashes).items() if c > 1]
    if dup:
        issues.append(f"dash 중복: {', '.join(map(str, dup))}")

    nums = sorted(int(d) for d in dashes if str(d).isdigit())
    if nums:
        info["dash_range"] = f"{nums[0]:02d}~{nums[-1]:02d}"
        missing = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
        if missing:
            issues.append(f"dash 누락: {', '.join(f'{m:02d}' for m in missing)}")

    # --- null 판독 ---
    nulls = [f"{r.get('dash')}.{k}" for r in rows for k, v in r.items() if v is None]
    if nulls:
        issues.append(f"판독 실패 {len(nulls)}건: {', '.join(nulls[:8])}"
                      + (" ..." if len(nulls) > 8 else ""))

    # --- L - K_max 상수성 ---
    has_lk = bool(rows) and "L" in rows[0] and "K" in rows[0]
    if has_lk:
        lk = compute_lk(rows)
        vals = [v for _, v in lk if v is not None]
        if vals:
            mode, _ = Counter(vals).most_common(1)[0]
            info["lk_mode"] = mode
            if expect_lk is not None and abs(mode - expect_lk) > TOL:
                issues.append(f"L-Kmax 최빈값 {mode} != 기대 {expect_lk}")
            off = [d for d, v in lk if v is not None and abs(v - mode) > TOL]
            if off:
                info["lk_outliers"] = off
                # 앞쪽 연속 dash 는 무그립이라 정상. 중간에 튀면 오독.
                nums_off = [int(d) for d in off if str(d).isdigit()]
                head = sorted(int(d) for d in dashes if str(d).isdigit())[:len(nums_off)]
                if sorted(nums_off) != head:
                    issues.append(f"L-Kmax 이탈(무그립 구간 아님): {', '.join(off)}")

    # --- L 이 1/16 격자 위인가 ---
    off_grid = []
    for r in rows:
        L = to_float(r.get("L"))
        if L is not None:
            g = L / GRID
            if abs(g - round(g)) > 0.02:
                off_grid.append(f"{r.get('dash')}={L}")
    if off_grid:
        issues.append(f"L 격자 이탈: {', '.join(off_grid)}")

    return issues, info


def report(rows, expect_lk=None, expect_rows=None, label=""):
    """사내망에서 눈으로 보고 구두 전달할 수 있는 짧은 요약."""
    issues, info = verify(rows, expect_lk, expect_rows)
    head = f"[{label}] " if label else ""
    line = (f"{head}행 {info.get('n_rows')} "
            f"dash {info.get('dash_range', '?')} "
            f"L-Kmax {info.get('lk_mode', '-')}")
    if issues:
        return f"{line}\nNG {len(issues)}건:\n" + "\n".join(f"  - {i}" for i in issues), False
    return f"{line}\nOK - 자동검증 통과", True
