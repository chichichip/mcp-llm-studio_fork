# -*- coding: utf-8 -*-
"""외부망 개발용. VLM 없이 verify 로직을 fixture 로 검증.
   python selftest.py
"""
import json, glob, os, sys, copy
from verify import verify, report

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

if __name__ == "__main__":
    sys.exit(0 if run() else 1)
