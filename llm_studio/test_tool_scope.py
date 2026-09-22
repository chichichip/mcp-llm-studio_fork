"""test_tool_scope.py — 도구 스코프(pick_tool_servers) 회귀 시험.

**앱도 MCP 서버도 없이 돈다.** 개발 PC에서 그냥:

    python llm_studio\\test_tool_scope.py

왜 이 파일이 있나: 스코프가 잘못 좁혀지면 **그 서버의 도구가 모델에게서 통째로
사라지는데**, 화면에는 모델이 "그런 도구가 없다"고 하는 것으로만 보인다. 실제로
`std`에 스코프 낱말이 없어 `select_dash`가 사라졌고, `치수`가 catia 낱말이라
오링 홈 치수 질문이 엉뚱한 서버로 걸렸다. 사람이 "그 도구를 쓰라"고 직접
명령해야 동작하는 상태였다.

낱말을 고치면 이걸 돌려 볼 것. 특히 **docs와 std는 한 쌍**이어야 한다 —
지침서·설계기준에서 식을 찾고(docs) 그 표에서 부품번호를 확정한다(std).
"""
import os
import sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server.context import pick_tool_servers

SERVERS = ["office", "outlook", "docs", "pdf", "std"]
mcp = types.SimpleNamespace(connected_servers=lambda: SERVERS)
S = {"tool_scope_enabled": True}

# 부품 선정 질문에는 docs(식·규격)와 std(표) 가 **둘 다** 있어야 한다.
PAIR = [
    "홈 치수는 내경 60.00mm 외경 69.20mm 깊이 2.80mm 입니다. AS568 부품번호를 확정해 주세요.",
    "오링 재질 선정 지침을 알려줘",
    "압축률 20~30% 맞는 오링 골라줘",
]
CASES = [
    ("홈 치수는 내경 60.00mm 외경 69.20mm 깊이 2.80mm 입니다. AS568 부품번호를 확정해 주세요.", "std"),
    ("오링 재질 선정 지침을 알려줘", "std"),
    ("이 요구사항에 맞는 표준품을 찾아줘", "std"),
    ("지침서에서 오링 규격을 찾아줘", "std"),
    ("볼트 그립 길이로 dash 를 골라줘", "std"),
    ("사내 규정 문서 검색해줘", "docs"),
    ("엑셀 파일 열어서 셀 읽어줘", "office"),
]
bad = 0
for q, want in CASES:
    got = pick_tool_servers(mcp, q, S)
    ok = got is None or want in got
    bad += 0 if ok else 1
    print(("  ✓ " if ok else "  ✗ ") + f"{want:6s} 살아남음  ← {q[:38]}…")
    if not ok:
        print(f"        선택된 서버: {got}")
    elif got is not None:
        print(f"        선택: {got}")
print()
for q in PAIR:
    got = pick_tool_servers(mcp, q, S) or SERVERS
    ok = "docs" in got and "std" in got
    bad += 0 if ok else 1
    print(("  ✓ " if ok else "  ✗ ") + f"docs+std 한 쌍  ← {q[:34]}…  {got}")

print(f"\n{'실패 ' + str(bad) if bad else '전부 통과'}")
sys.exit(1 if bad else 0)
