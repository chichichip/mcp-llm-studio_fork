"""test_spec_table.py — spec_table 판독 경로의 회귀 시험.

**VLM도 PyMuPDF도 없이 돈다** (가짜 문서·가짜 VLM). 개발 PC에서 그냥 실행하면 된다:

    python mcp_server\\test_spec_table.py

왜 이 파일이 있나: 여기서 잡는 실패들은 **화면에 오류로 안 나타난다.** 회전을
잘못 잡거나 재시도 조건이 틀리면 VLM은 예외가 아니라 빈 결과를 돌려주고, 그게
"0행"으로만 찍혀 표가 반쯤 읽힌 채 조용히 끝난다. 사내 폐쇄망에서는 한 번 돌리는
데 수십 분이 걸려 시행착오로 알아낼 수가 없다 — `check_repo.py`와 같은 취지로,
글로 적어 두면 지켜질 것 같지만 조용히 깨지는 것을 기계가 확인한다.

실제로 겪은 실패 셋이 그대로 들어 있다:
  · 표가 4쪽부터인데 페이지가 90도 누워 "치수표를 찾지 못했습니다"만 나왔다
  · **한 쪽만 지정하면 읽히는데 여러 쪽을 주면 못 읽었다** (표지의 가로쓰기가
    누운 치수표를 회전 표결에서 이겼고, 어쩌다 읽힌 한 쪽이 재시도를 막았다)
  · 판독 도중 창을 닫았더니 수십 분치가 통째로 날아갔다
"""
import sys, types, os, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spec_table as st
import vision_ingest as vi
import tempfile, pathlib, time

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="spectest_"))
PDF = str(_TMP / "X.pdf")
pathlib.Path(PDF).write_bytes(b"%PDF-1.4 fake")
st.CACHE_DIR = str(_TMP / "cache")


class FakePage:
    def __init__(self, lines_dir=None):
        self._dir = lines_dir
        self.rotation = 0
    def set_rotation(self, n):
        self.rotation = n % 360
    def get_text(self, kind):
        if self._dir is None:
            raise RuntimeError("텍스트 레이어 없음")
        return {"blocks": [{"lines": [{"dir": self._dir, "spans": [1, 2]}
                                      for _ in range(10)]}]}


class FakeDoc:
    def __init__(self, n, lines_dir=None):
        self.pages = [FakePage(lines_dir) for _ in range(n)]
        self.closed = False
    def __len__(self): return len(self.pages)
    def __getitem__(self, i): return self.pages[i]
    def close(self): self.closed = True


def install(doc, table_pages, good_rot, calls):
    """가짜 vision_ingest — good_rot 로 렌더된 table_pages 만 행을 돌려준다."""
    fake = types.SimpleNamespace()
    fake.FITZ_AVAILABLE = True
    fake.FITZ_IMPORT_ERROR = ""
    fake.fitz = types.SimpleNamespace(open=lambda p: doc)
    fake.vlm_check = lambda: (True, "")
    fake.detect_rotation = vi.detect_rotation
    fake.text_dir_known = vi.text_dir_known

    def render_page(d, pno, rotate=0):
        return f"{pno}:{rotate}".encode()

    def ask_image(png, prompt):
        pno, rot = png.decode().split(":")
        pno, rot = int(pno), int(rot)
        calls.append((pno, rot))
        if rot == good_rot and pno in table_pages:
            return json.dumps({"table_title": "T", "id_column": "DASH NUMBER",
                               "columns": ["I.D. INCHES"],
                               "rows": [{"dash": f"{pno}{i:02d}", "I.D. INCHES": ".100"}
                                        for i in range(3)]})
        return json.dumps({"rows": []})

    fake.render_page = render_page
    fake.ask_image = ask_image
    st.vision_ingest = fake
    return fake


def run(name, doc, table_pages, good_rot, keep_cache=False, **kw):
    calls = []
    install(doc, table_pages, good_rot, calls)
    st.load_cached = lambda p: None
    st._save_cache = lambda p, d: None
    if not keep_cache:
        f = st._page_cache_path(PDF)
        if f.exists():
            f.unlink()
    try:
        data = st.read_table(PDF, **kw)
    except st.SpecError as e:
        return None, calls, str(e)
    return data, calls, ""


def check(ok, msg):
    print(("  ✓ " if ok else "  ✗ ") + msg)
    return 0 if ok else 1


bad = 0
print("_parse_pages")
bad += check(st._parse_pages("4-12", 20) == list(range(4, 13)), "구간 4-12")
bad += check(st._parse_pages("2,3", 20) == [2, 3], "쉼표 2,3")
bad += check(st._parse_pages("2 4-6 4", 20) == [2, 4, 5, 6], "혼합 + 중복 제거")
bad += check(st._parse_pages("4-99", 6) == [4, 5, 6], "문서 끝에서 잘린다")
bad += check(st._parse_pages("", 9) == [], "빈 지정")

print("\nvision_ingest.detect_rotation (글자 방향으로 공짜 판정)")
bad += check(vi.detect_rotation(FakeDoc(5, (0.0, -1.0)), [1, 2]) == 90, "아래→위 = 90도")
bad += check(vi.detect_rotation(FakeDoc(5, (0.0, 1.0)), [1, 2]) == 270, "위→아래 = 270도")
bad += check(vi.detect_rotation(FakeDoc(5, (1.0, 0.0)), [1, 2]) == 0, "가로쓰기 = 0도")
bad += check(vi.detect_rotation(FakeDoc(5, None), [1, 2]) == 0, "텍스트 레이어 없음 = 0도")

print("\n실제 상황: 표가 4쪽부터 12쪽, 페이지는 90도 누움")
tp = set(range(4, 13))

# ① 텍스트 레이어가 있는 경우 — VLM 재시도 없이 바로 맞는 각도로 읽어야 한다
data, calls, err = run("a", FakeDoc(20, (0.0, -1.0)), tp, 90)
bad += check(not err, f"판독 성공 ({err})")
bad += check(data and data.get("rotate") == 90, "회전 90도로 기록")
bad += check(data and data["pages_used"] == list(range(4, 13)),
             f"4~12쪽 전부 읽음 → {data and data['pages_used']}")
bad += check(all(r == 90 for _p, r in calls), "0도로는 한 번도 안 불렀다(공짜 판정)")
bad += check(13 in [p for p, _ in calls] and 14 not in [p for p, _ in calls],
             "13쪽에서 끊긴 걸 확인하고 멈춘다")

# ② 스캔본(텍스트 레이어 없음) — 0도 전멸 후 90도 재시도로 살아나야 한다
data, calls, err = run("b", FakeDoc(20, None), tp, 90)
bad += check(not err, f"재시도로 판독 성공 ({err})")
bad += check(data and data.get("rotate") == 90, "회전 90도로 기록")
bad += check(data and data["pages_used"] == list(range(4, 13)), "4~12쪽 전부 읽음")
bad += check(any("누워 있어" in n for n in (data or {}).get("notes", [])),
             "누웠다는 사실을 알림에 남긴다")

# ③ 바로 선 문서(텍스트 레이어 있음)는 각도를 더듬지 않는다 — 쪽당 딱 한 번.
#    표지(p1~p3)가 0행이어도 글자 방향을 아는 쪽이라 회전 문제가 아님이 분명하다.
data, calls, err = run("c", FakeDoc(20, (1.0, 0.0)), tp, 0)
bad += check(not err and data["pages_used"] == list(range(4, 13)), "0도로 읽힌다")
bad += check(all(r == 0 for _p, r in calls), "다른 각도를 시도하지 않는다")
bad += check(len(calls) == len({p for p, _r in calls}),
             f"쪽마다 VLM을 한 번씩만 부른다 ({len(calls)}회)")

# ③-b 스캔본(텍스트 레이어 없음)은 **각도를 더듬는 게 맞다** — 이게 이번 버그의
#     핵심이다. 표지 쪽에서 헛도는 비용은 문서당 한 번, 세 번까지로 묶여 있다.
data, calls, err = run("c2", FakeDoc(20, None), tp, 0)
probes = [(p, r) for p, r in calls if r != 0]
bad += check(not err and data["pages_used"] == list(range(4, 13)), "스캔본도 다 읽는다")
bad += check(len(probes) <= 3, f"각도 더듬기는 세 번 이하 ({len(probes)}회: {probes})")

# ④ 사람이 --rotate 를 주면 그것만 쓴다(틀려도 자동 재시도로 덮지 않는다)
data, calls, err = run("d", FakeDoc(20, None), tp, 90, rotate=0)
bad += check(bool(err) and all(r == 0 for _p, r in calls),
             "--rotate 0 을 주면 0도만 시도하고 실패를 알린다")
bad += check("pages" in err and "rotate" in err, "실패 안내가 쪽·각도를 가리킨다")

# ⑤ pages 를 직접 주면 그 쪽만 — 이어읽기로 범위를 넘지 않는다
data, calls, err = run("e", FakeDoc(20, (0.0, -1.0)), tp, 90, pages="4-6")
bad += check(not err and data["pages_used"] == [4, 5, 6], "지정한 4~6쪽만 읽는다")

# ⑥ 이어읽기 상한
st.MAX_TABLE_PAGES = 3
data, calls, err = run("f", FakeDoc(40, (0.0, -1.0)), set(range(4, 40)), 90)
# 찾기 창(1~6쪽)에서 4,5,6을 찾고, 거기서 **3쪽 더** 이어 읽고 멈춘다
bad += check(not err and data["pages_used"] == [4, 5, 6, 7, 8, 9],
             f"상한에서 멈춘다 → {data and data['pages_used']}")
bad += check(any("STD_SPEC_MAX_TABLE_PAGES" in n for n in data["notes"]),
             "상한에 걸렸다는 사실을 알린다")
st.MAX_TABLE_PAGES = 40

print("\n끊겨도 잃지 않는가 (쪽마다 캐시)")
st.MAX_TABLE_PAGES = 40
CACHE = st._page_cache_path(PDF)


def fresh(doc, table_pages, good_rot):
    """캐시를 비우고 가짜 vision_ingest를 새로 건다. 반환: (calls, 느리게 만들 훅)"""
    calls = []
    install(doc, table_pages, good_rot, calls)
    st.load_cached = lambda p: None
    if CACHE.exists():
        CACHE.unlink()
    return calls


# ⑦ 시간 예산으로 중간에 멈춘다 → 읽은 쪽은 저장, complete=False, 최종 캐시엔 안 들어감
saved = {}
st._save_cache = lambda p, d: saved.update(d)
calls = fresh(FakeDoc(30, (0.0, -1.0)), set(range(3, 30)), 90)
plain = st.vision_ingest.ask_image


def slow(png, prompt):
    time.sleep(0.03)                      # 사내 게이트웨이의 쪽당 수십 초를 본뜬다
    return plain(png, prompt)


st.vision_ingest.ask_image = slow
st.TIME_BUDGET = 0.2
data = st.read_table(PDF)
bad += check(data.get("complete") is False, "예산 초과 → complete=False")
bad += check(not saved, "모자란 결과를 최종 캐시에 넣지 않는다")
bad += check(CACHE.exists(), "쪽 캐시 파일은 남는다")
stopped_at = len(data["pages_used"])
bad += check(0 < stopped_at < 27, f"중간에 멈췄다(읽은 표 쪽 {stopped_at}개)")
bad += check(any("이어갑니다" in n for n in data["notes"]), "이어 돌리라고 안내한다")

# ⑧ 다시 돌리면 캐시에서 이어간다 — 이미 읽은 쪽은 VLM을 다시 안 부른다
st.vision_ingest.ask_image = plain
st.TIME_BUDGET = 60
before = set(data["pages_used"])
calls.clear()
data2 = st.read_table(PDF)
again = {p for p, _r in calls} & before
bad += check(not again, f"이미 읽은 쪽을 다시 안 부른다 (다시 부른 쪽: {sorted(again)})")
bad += check(data2.get("complete") is True, "두 번째 실행에서 끝까지 읽는다")
bad += check(set(data2["pages_used"]) >= before, "첫 실행에서 읽은 쪽이 결과에 남아 있다")
bad += check(data2["pages_used"] == list(range(3, 30)),
             f"3~29쪽 전부 → {data2['pages_used'][:3]}…{data2['pages_used'][-1:]}")

# ⑨ Ctrl+C 해도 가진 것으로 결과를 만들어 준다 (10분 기다린 사람에게 빈손은 안 된다)
calls = fresh(FakeDoc(30, (0.0, -1.0)), set(range(3, 30)), 90)
plain = st.vision_ingest.ask_image
hits = {"n": 0}


def boom(png, prompt):
    hits["n"] += 1
    if hits["n"] > 6:
        raise KeyboardInterrupt
    return plain(png, prompt)


st.vision_ingest.ask_image = boom
data3 = st.read_table(PDF)
bad += check(data3.get("complete") is False, "Ctrl+C → complete=False")
bad += check(bool(data3["rows"]), "중단해도 읽은 행은 돌려준다")
bad += check(any("Ctrl+C" in x for x in data3["notes"]), "중단했다고 알린다")
bad += check(CACHE.exists(), "중단해도 쪽 캐시는 남는다")

# ⑩ --pages 3 --refresh 는 그 쪽만 다시 읽고 나머지 캐시는 지킨다
calls = fresh(FakeDoc(30, (0.0, -1.0)), set(range(3, 30)), 90)
st.read_table(PDF)                      # 캐시를 채워 둔다
calls.clear()
st.read_table(PDF, pages="3", refresh=True)
bad += check([p for p, _r in calls] == [3], f"p3만 다시 읽는다 → {[p for p, _r in calls]}")


print("\n한 쪽은 되는데 여러 쪽은 안 되던 문제 (실제로 겪은 것)")
# 문서 모양: p1·p2는 표지·목차(가로쓰기), p3부터가 누운 치수표.
# ⚠ 회전을 찾기 창 전체로 한 번에 판정하면 표지가 치수표를 표결에서 이겨 0도가
#   나온다. 그리고 어쩌다 한 쪽이 0도로 읽히면 그 한 쪽이 재시도를 통째로 막아,
#   나머지 표 쪽이 전부 0행인 채 끝난다 — 원인이 화면에 안 보이는 실패다.


class Mixed(FakeDoc):
    def __init__(self, blind=False):
        d = None if blind else (1.0, 0.0)
        r = None if blind else (0.0, -1.0)
        self.pages = [FakePage(d), FakePage(d)] + [FakePage(r) for _ in range(10)]


MIX = set(range(3, 13))


def mixed_ask(png, prompt):
    pno, rot = (int(x) for x in png.decode().split(":"))
    mixed_calls.append((pno, rot))
    # ★ p4 한 쪽만 0도로도 읽힌다 — 이 한 쪽이 예전 코드에서 재시도를 막았다.
    if (rot == 90 and pno in MIX) or (pno == 4 and rot == 0):
        return json.dumps({"columns": ["I.D. INCHES"],
                           "rows": [{"dash": f"{pno}{i:02d}"} for i in range(30)]})
    return json.dumps({"rows": []})


for label, blind in (("텍스트 레이어 있음", False), ("스캔본(레이어 없음)", True)):
    mixed_calls = []
    install(Mixed(blind), MIX, 90, [])
    st.vision_ingest.ask_image = mixed_ask
    st.vision_ingest.fitz = types.SimpleNamespace(open=lambda p, b=blind: Mixed(b))
    f = st._page_cache_path(PDF)
    if f.exists():
        f.unlink()
    one = st.read_table(PDF, pages="3")
    f.unlink()
    many = st.read_table(PDF)
    bad += check(one["pages_used"] == [3], f"{label}: 한 쪽만 주면 읽힌다")
    bad += check(many["pages_used"] == list(range(3, 13)),
                 f"{label}: 여러 쪽도 전부 읽힌다 → {many['pages_used']}")
    bad += check(len(many["rows"]) == 300, f"{label}: 300행 (p4만 30행이 아니라)")

# 회전 판정을 여러 쪽에 한꺼번에 물으면 안 된다는 것 자체를 못 박아 둔다
bad += check(vi.detect_rotation(Mixed(), [3]) == 90, "쪽 하나로 물으면 90도")
bad += check(vi.detect_rotation(Mixed(), [1, 2, 3, 4]) == 0,
             "여러 쪽으로 물으면 표지가 이겨 0도 — 그래서 쪽마다 물어야 한다")

print(f"\n{'실패 ' + str(bad) + '건' if bad else '전부 통과'}")
sys.exit(1 if bad else 0)
