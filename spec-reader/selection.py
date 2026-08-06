# -*- coding: utf-8 -*-
"""dash 선정 — 판독된 치수표에서 조건에 맞는 dash 를 고른다.

⚠ 파일 이름이 `select.py` 가 아니라 `selection.py` 인 이유: `select` 는 **표준
라이브러리 모듈 이름**이다. 프로젝트 폴더가 sys.path 앞에 오므로 select.py 를 두면
requests/urllib3 등이 내부에서 하는 `import select` 가 이 파일을 집어 엉뚱한 곳에서
깨진다. (HANDOFF §7.3 은 select.py 로 적고 있지만 그 이름은 쓰면 안 된다.)

**여기에는 LLM 이 없다.** 판독은 VLM 이, 선정은 이 모듈의 결정론적 함수가 한다.
항공 부품이라 "골라 주는" 자리에 환각이 끼면 안 된다(HANDOFF §2).

원칙 셋:
    1. 조건에 맞는 게 없으면 **없다고 한다.** 가까운 것을 슬쩍 올려주지 않는다.
       (가까운 후보는 따로 'near' 로 보고해 사람이 판단하게 한다.)
    2. K 는 범위값이라 **최악값을 전파**한다. 나사부 길이는 L - K_max 로 본다
       (K_max = 그립이 가장 길 때 = 나사부가 가장 짧을 때).
    3. 판정식은 **교체될 예정**이다. 지금은 직접 비교지만 체결두께 기반 2.5산 역산으로
       바뀔 수 있어(HANDOFF §7.3) `thread_length_rule` 한 곳에 격리해 뒀다.
"""

import re
from verify import to_float, range_max

# 판정식 이름. 바뀌면 여기와 thread_length_rule 만 손대면 된다.
RULE = "direct"          # 직접 비교
DEFAULT_TOL = 0.002      # 치수 비교 허용오차(인치)


# ---------------------------------------------------------------- 나사규격 정규화

# 번호 사이즈 -> 공칭 직경(인치). '10-32' 와 '.190-32' 가 같은 나사임을 알기 위해 필요.
NUMBER_SIZES = {
    "0": 0.060, "1": 0.073, "2": 0.086, "3": 0.099, "4": 0.112,
    "5": 0.125, "6": 0.138, "8": 0.164, "10": 0.190, "12": 0.216,
}


def parse_thread(s):
    """나사 표기를 (공칭직경, TPI) 로 정규화한다. 못 읽으면 (None, None).

    같은 나사가 표준마다 다르게 적힌다:
        '.1640-36'  '.164-36 UNJF-3A'  '#8-36'  '8-36'      -> (0.164, 36)
        '.1900-32'  '10-32UNJF-3B'     '1/4-28'             -> (0.190, 32) / (0.250, 28)
    이 정규화가 없으면 볼트와 너트의 나사가 같은데도 못 맞춘다(HANDOFF §7.1).
    """
    if not s:
        return None, None
    t = str(s).strip().upper().lstrip("#")
    # 분수 표기: 1/4-28
    m = re.match(r"^(\d+)\s*/\s*(\d+)\s*-\s*(\d+)", t)
    if m:
        num, den, tpi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (round(num / den, 4), tpi) if den else (None, None)
    # 소수 표기: .1640-36 / 0.190-32
    m = re.match(r"^(\d*\.\d+)\s*-\s*(\d+)", t)
    if m:
        return round(float(m.group(1)), 4), int(m.group(2))
    # 번호 표기: 10-32 / 8-36
    m = re.match(r"^(\d{1,2})\s*-\s*(\d+)", t)
    if m and m.group(1) in NUMBER_SIZES:
        return NUMBER_SIZES[m.group(1)], int(m.group(2))
    return None, None


def threads_match(a, b, tol=0.0006):
    """두 나사 표기가 같은 나사인가. 직경과 TPI 가 모두 맞아야 한다.

    한쪽이라도 못 읽으면 **False** — '아마 같겠지'로 넘어가지 않는다.
    """
    da, ta = parse_thread(a)
    db, tb = parse_thread(b)
    if None in (da, ta, db, tb):
        return False
    return abs(da - db) <= tol and ta == tb


# ---------------------------------------------------------------- 나사부 길이


def thread_length_rule(row):
    """이 행의 '나사부 길이'. 판정식이 바뀌면 **이 함수만** 갈아끼운다.

    현재(RULE='direct'): L - K_max.
      L      = 전체 길이
      K_max  = 그립 범위의 최대값 → 나사부가 가장 짧아지는 최악값
    K 컬럼이 없는 표(너트 등)에서는 None 을 돌려준다.
    """
    L = to_float(row.get("L"))
    if L is None:
        return None
    if row.get("K") in (None, ""):
        return None
    Kmax = range_max(row.get("K"))
    return round(L - Kmax, 4) if Kmax is not None else None


def no_grip_dashes(rows, tol=DEFAULT_TOL):
    """그립이 없는(짧은) dash 목록.

    짧은 dash 는 K 가 고정이라 L-K_max 가 계열 상수에서 벗어난다(HANDOFF §4.3).
    **전단 하중부에 쓸 수 없으므로** 선정에서 따로 다뤄야 한다.
    최빈값에서 벗어나는 행을 무그립으로 본다 — verify.py 와 같은 판정 방식이다.
    """
    from collections import Counter
    vals = [(r.get("dash"), thread_length_rule(r)) for r in rows]
    got = [v for _, v in vals if v is not None]
    if not got:
        return []
    mode, _ = Counter(got).most_common(1)[0]
    return [d for d, v in vals if v is not None and abs(v - mode) > tol]


# ---------------------------------------------------------------- 선정


def select_bolt_dash(rows, target_thread_len, tol=DEFAULT_TOL,
                     exclude_no_grip=True, near=3):
    """스레드 길이로 볼트 dash 를 고른다.

    반환: (hits, info)
        hits  : 조건을 만족한 [(dash, 나사부길이), ...]  — 없으면 빈 리스트
        info  : {'near': [...], 'no_grip': [...], 'notes': [...]}

    조건에 맞는 게 없으면 **빈 리스트**를 준다. near 에 가까운 후보를 담아 주지만
    그건 참고용이고, 호출부가 그걸 '선정'으로 바꿔 보고하면 안 된다.
    """
    notes = []
    skip = set(no_grip_dashes(rows)) if exclude_no_grip else set()
    if skip and exclude_no_grip:
        notes.append(f"무그립 dash {len(skip)}개 제외(전단 하중부 사용 불가): "
                     + ", ".join(map(str, sorted(skip)))[:80])

    scored = []
    for r in rows:
        tl = thread_length_rule(r)
        if tl is None:
            continue
        d = r.get("dash")
        if d in skip:
            continue
        scored.append((d, tl))

    if not scored:
        notes.append("나사부 길이를 계산할 수 있는 행이 없습니다(L/K 컬럼 확인).")
        return [], {"near": [], "no_grip": sorted(skip), "notes": notes}

    hits = [(d, tl) for d, tl in scored if abs(tl - target_thread_len) <= tol]
    rest = sorted(scored, key=lambda x: abs(x[1] - target_thread_len))
    nearest = [(d, tl) for d, tl in rest if (d, tl) not in hits][:near]

    if not hits:
        notes.append(f"스레드 길이 {target_thread_len} 에 맞는 dash 가 없습니다"
                     f"(허용오차 ±{tol}).")
    return hits, {"near": nearest, "no_grip": sorted(skip), "notes": notes}


def range_min(v):
    """'.062-.082' -> 0.062,  '.250' -> 0.250. (verify.range_max 의 짝)"""
    if v is None:
        return None
    n = re.findall(r"\d*\.\d+|\d+", str(v))
    return float(n[0]) if n else None


def select_bolt_dash_by_grip(rows, thickness, margin=0.0):
    """★ 제안 — 체결두께로 볼트 dash 를 고른다. **도메인 확인 전까지 기본 경로가 아니다.**

    왜 이게 필요한가: MS9555 fixture 로 확인해 보면 `L - K_max` 가 그립 있는 dash
    전 구간에서 0.578 로 **똑같다**. 그게 바로 계열 상수라 검증 규칙으로 쓰는 값이다
    (HANDOFF §4.3). 그래서 **나사부 길이로는 dash 를 가릴 수 없다** — 조건을 주면
    22개가 전부 걸린다. dash 마다 실제로 달라지는 건 L(전체 길이)과 K(그립)다.

    설계자가 아는 값은 보통 '겹쳐 조이는 판 두께(체결두께)'이고, 그 두께를 K 범위가
    받아 주는 dash 를 고르는 것이 자연스럽다. HANDOFF §7.3 의 '체결두께 기반 2.5산
    역산' 이라는 메모도 이쪽을 가리킨다.

    판정: K_min <= (체결두께 + margin) <= K_max 인 dash.
    ⚠ 이 규칙은 **아직 확인되지 않았다.** 상사/설계 기준 확인 후 확정할 것.
    """
    hits, notes = [], []
    t = thickness + margin
    for r in rows:
        kmin, kmax = range_min(r.get("K")), range_max(r.get("K"))
        if kmin is None or kmax is None:
            continue
        if kmin <= t <= kmax:
            hits.append((r.get("dash"), r.get("K")))
    if not hits:
        notes.append(f"체결두께 {thickness} 를 받는 그립(K) 구간이 없습니다.")
    notes.append("⚠ 그립 기준 선정은 미확인 규칙입니다(도메인 확인 필요).")
    return hits, {"notes": notes, "near": []}


def select_nut_dash(rows, thread, thread_column=None):
    """나사규격으로 너트 dash 를 고른다. 너트는 길이 개념이 없다(HANDOFF §7.3).

    thread_column: 표에서 나사규격이 들어 있는 컬럼 이름. 생략하면 자동 추정한다
    (THREAD / THD 같은 이름, 없으면 값이 나사 표기로 파싱되는 컬럼).

    반환: (hits, info). 맞는 게 없으면 빈 리스트 — 이 경우가 바로
    "MS9555 에 맞는 너트가 없다"를 잡아내는 자리다.
    """
    notes = []
    if not rows:
        return [], {"notes": ["표가 비어 있습니다."]}

    col = thread_column
    if col is None:
        cands = [k for k in rows[0] if k != "dash"]
        named = [k for k in cands if "THREAD" in str(k).upper() or str(k).upper() == "THD"]
        if named:
            col = named[0]
        else:
            for k in cands:
                if any(parse_thread(r.get(k))[0] is not None for r in rows):
                    col = k
                    break
    if col is None:
        notes.append("나사규격 컬럼을 찾지 못했습니다. thread_column 으로 지정하세요.")
        return [], {"notes": notes}

    notes.append(f"나사규격 컬럼: {col}")
    hits = [(r.get("dash"), r.get(col)) for r in rows if threads_match(r.get(col), thread)]
    if not hits:
        d, t = parse_thread(thread)
        seen = sorted({str(r.get(col)) for r in rows if r.get(col)})[:6]
        notes.append(f"'{thread}'"
                     + (f" (={d}, {t}TPI)" if d else " (해석 실패)")
                     + f" 에 맞는 dash 가 없습니다. 표에 있는 값: {', '.join(seen)}")
    return hits, {"notes": notes}


# ---------------------------------------------------------------- 출력


def report(hits, info, label="", unit=""):
    """사내망에서 눈으로 보고 구두로 옮길 수 있는 짧은 요약 (verify.report 와 같은 방침)."""
    head = f"[{label}] " if label else ""
    lines = []
    if hits:
        body = ", ".join(f"-{d}({v}{unit})" for d, v in hits)
        lines.append(f"{head}선정 {len(hits)}건: {body}")
    else:
        lines.append(f"{head}선정 없음")
        if info.get("near"):
            near = ", ".join(f"-{d}({v}{unit})" for d, v in info["near"])
            lines.append(f"  가까운 후보(선정 아님): {near}")
    for n in info.get("notes", []):
        lines.append(f"  - {n}")
    return "\n".join(lines)
