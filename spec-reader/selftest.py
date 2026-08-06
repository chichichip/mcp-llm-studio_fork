# -*- coding: utf-8 -*-
"""외부망 개발용. VLM 없이 verify / catalog 로직을 검증.
   python selftest.py
"""
import json, glob, os, sys, copy, csv, tempfile
from verify import verify, report
import catalog as C

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def run():
    ok = True
    for path in sorted(glob.glob("fixtures/*.json")):
        name = os.path.basename(path)[:-5]
        d = load(path)
        rows = d["rows"]
        exp_lk = d.get("_lk_constant")

        # 1) 정답 데이터는 통과해야 한다
        txt, passed = report(rows, exp_lk, len(rows), label=f"{name} 정답")
        print(txt)
        if not passed:
            ok = False

        # 2) 오독을 주입하면 잡아야 한다
        cases = {
            "숫자 오독 (L 변조)":      lambda r: r[15].update({"L": "1.325"}),
            "행 누락":                lambda r: r.pop(10),
            "null 반환":              lambda r: r[5].update({"K": None}),
            "dash 중복":              lambda r: r.__setitem__(20, copy.deepcopy(r[19])),
        }
        for desc, mutate in cases.items():
            bad = copy.deepcopy(rows)
            mutate(bad)
            issues, _ = verify(bad, exp_lk, len(rows))
            mark = "검출" if issues else "★놓침★"
            print(f"  {desc:22s} -> {mark}")
            if not issues:
                ok = False
        print()
    return ok

SAMPLE = [
    ["대분류", "중분류", "도면번호", "부품번호", "품명", "소재", "비고"],
    ["BOLT", "BOLT, DOUBLE HEX", "AS9555", "MS9555-07", "BOLT, DOUBLE HEX, 0.164", "A286", ""],
    ["BOLT", "BOLT, DOUBLE HEX", "AS9556", "MS9556-10", "BOLT, DOUBLE HEX, 0.190", "A286", ""],
    ["NUT", "NUT, SELF-LOCKING, DOUBLE HEX", "MS21043", "MS21043-3",
     "NUT, SELF-LOCKING, DOUBLE HEX, 0.190", "A286", ""],
    ["CLAMP", "CLAMP<,SADDLE", "AS21919", "AS21919-1", "CLAMP, SADDLE", "CRES", ""],
    ["FITTING", "듣도보도못한중분류", "XX1", "XX1-1", "알 수 없음", "", ""],
]


def run_catalog():
    """catalog.py — 엑셀 없이 CSV 로 검증. VLM 도 엑셀도 필요 없다."""
    ok = True

    def chk(label, cond):
        nonlocal ok
        print(f"  {'OK ' if cond else '★NG'} {label}")
        if not cond:
            ok = False

    print("[catalog] 정규화 - 엑셀 오타/공백 흔들림 흡수")
    chk("CLAMP<,SADDLE == CLAMP, SADDLE", C.norm("CLAMP<,SADDLE") == C.norm("CLAMP, SADDLE"))
    chk("DESIGNSTANDARD... == DESIGN STANDARD...",
        C.norm("DESIGNSTANDARD FOR HOSE ASSEMBLY") == C.norm("DESIGN STANDARD FOR HOSE ASSEMBLY"))
    chk("PLUG& CAP == PLUG & CAP", C.norm("PLUG& CAP") == C.norm("PLUG & CAP"))

    print("[catalog] 볼트->너트 매핑 - 단순 치환이 아니라 매핑표")
    dh, _ = C.nut_categories_for("BOLT, DOUBLE HEX")
    chk("DOUBLE HEX -> SELF-LOCKING, DOUBLE HEX 하나", dh == ["NUT, SELF-LOCKING, DOUBLE HEX"])
    hx, hint = C.nut_categories_for("BOLT, HEX")
    chk("HEX -> 후보 2개(되물어야 함)", len(hx) == 2 and bool(hint))
    th, hint2 = C.nut_categories_for("BOLT, T-HEAD")
    chk("T-HEAD -> 짝 미확인을 그대로 보고", th == [] and "확인되지 않" in hint2)
    unk, hint3 = C.nut_categories_for("NUT, LUG")
    chk("매핑표에 없으면 추정하지 않음", unk == [] and "매핑표에 없" in hint3)

    print("[catalog] 직경 파싱 - 품명 끝의 소수만")
    chk(".164 파싱", C.parse_diameter("BOLT, DOUBLE HEX, 0.164") == 0.164)
    chk("소수점 없는 꼬리는 직경 아님", C.parse_diameter("PIN, HEADED, 3") is None)
    chk("직경 없는 품명", C.parse_diameter("CLAMP, LOOP") is None)

    d = tempfile.mkdtemp()
    path = os.path.join(d, "catalog.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(SAMPLE)
    items = C.load_catalog(path)

    print("[catalog] 조회")
    chk("행 수", len(items) == 5)
    chk("오타 중분류도 조회됨(CLAMP, SADDLE)",
        len(C.find_series(items, mid="CLAMP, SADDLE")) == 1)
    chk(".190 볼트 계열 = AS9556",
        [s[0] for s in C.find_series(items, "BOLT", "BOLT, DOUBLE HEX", 0.190)] == ["AS9556"])

    print("[catalog] 모르는 중분류를 보고하는가")
    notes = C.validate_catalog(items)
    chk("미등록 중분류 보고", any("듣도보도못한중분류" in n for n in notes))

    print("[catalog] ★ 짝 없음을 짝 없음이라고 하는가 (시스템의 핵심 가치)")
    cands, _ = C.nut_categories_for("BOLT, DOUBLE HEX")
    chk(".164 더블헥스 볼트의 짝 너트 없음",
        C.find_series(items, "NUT", cands[0], 0.164) == [])
    chk(".190 은 짝이 있음", len(C.find_series(items, "NUT", cands[0], 0.190)) == 1)
    print()
    return ok


if __name__ == "__main__":
    a = run()
    b = run_catalog()
    sys.exit(0 if (a and b) else 1)
