#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""표준품 스펙 치수표 판독기 (Gemma VLM)

사내망 전용. 실행 전 config.example.py 를 config.py 로 복사해 채울 것.

    python read_spec.py preview --pdf MS9555.pdf
    python read_spec.py read --pdf MS9555.pdf --expect-lk 0.578   # 페이지 자동 탐색
    python read_spec.py read --pdf MS9555.pdf --page 1 --crop 0,0.45,1,0.85 --raw
    python read_spec.py read --image MS9555_table.png --expect-lk 0.578
"""

import argparse
import base64
import io
import json
import re
import sys

import requests

from verify import report
from prompts import TABLE_PROMPT, META_PROMPT
from merge import merge_pages

try:
    import config
except ImportError:
    sys.exit("config.py 가 없습니다. config.example.py 를 복사해서 채우세요.")


# ---------------------------------------------------------------- PDF -> 이미지

def render(pdf, page_no, dpi=300, crop=None):
    import fitz
    from PIL import Image

    doc = fitz.open(pdf)
    if not 1 <= page_no <= len(doc):
        sys.exit(f"페이지 범위 오류: {page_no} (1~{len(doc)})")
    pix = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    doc.close()

    if crop:
        W, H = img.size
        x0, y0, x1, y1 = crop
        img = img.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
    return img


KEYWORDS = ("DASH NUMBER", "PART NUMBER", "DIMENSION", "TABLE I", "TABLE 1",
            "SIZE CODE", "BASIC NO")


def find_table_pages(pdf):
    """텍스트에 표 관련 키워드가 있는 페이지 번호 목록. 스캔본이면 빈 리스트."""
    import fitz
    doc = fitz.open(pdf)
    hits, total = [], len(doc)
    for i, pg in enumerate(doc, 1):
        up = pg.get_text().upper()
        if any(k in up for k in KEYWORDS):
            hits.append(i)
    doc.close()
    return hits, total


def to_b64(img, max_side=2400):
    from PIL import Image
    if max(img.size) > max_side:
        s = max_side / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- VLM

def call_vlm(b64, prompt):
    data = {
        "model": config.MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0,
        "max_tokens": 4096,
    }
    r = requests.post(config.URL, headers=config.HEADERS,
                      json=data, timeout=config.TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_json(text):
    t = re.sub(r"^```(?:json)?\s*", "", text.strip())
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------- 출력

def show_table(rows, cols):
    hdr = ["dash"] + cols
    w = [max(len(h), 11) for h in hdr]
    print("\n" + " ".join(h.ljust(x) for h, x in zip(hdr, w)))
    print("-" * (sum(w) + len(w)))
    for r in rows:
        cells = [str(r.get("dash", ""))] + [str(r.get(c, "")) for c in cols]
        print(" ".join(c.ljust(x) for c, x in zip(cells, w)))


# ---------------------------------------------------------------- 명령

def cmd_preview(a):
    import fitz
    doc = fitz.open(a.pdf)
    print(f"{a.pdf}: {len(doc)} 페이지")
    for i, pg in enumerate(doc, 1):
        t = pg.get_text()[:300].replace("\n", " ")
        mark = "  <-- 표 가능성" if ("DASH" in t.upper() or "TABLE" in t.upper()) else ""
        print(f"[{i}] {t[:150]}{mark}")
    doc.close()
    print("\n※ 텍스트가 안 나오면 스캔본. 그냥 --page 로 시도할 것.")


def read_one(a, page_no, crop, prompt=None):
    """페이지 하나 판독. (parsed, raw_text) 반환. 실패 시 (None, raw)."""
    if a.image:
        from PIL import Image
        img = Image.open(a.image).convert("RGB")
        if crop:
            W, H = img.size
            x0, y0, x1, y1 = crop
            img = img.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
    else:
        img = render(a.pdf, page_no, a.dpi, crop)

    if a.save_image:
        name = a.save_image
        if not a.image and "." in name:
            base, ext = name.rsplit(".", 1)
            name = f"{base}_p{page_no}.{ext}"
        img.save(name)

    text = call_vlm(to_b64(img), prompt or TABLE_PROMPT)
    return parse_json(text), text


def cmd_read(a):
    if not a.pdf and not a.image:
        sys.exit("--pdf 또는 --image 중 하나는 필요합니다")

    crop = tuple(float(x) for x in a.crop.split(",")) if a.crop else None
    if crop and len(crop) != 4:
        sys.exit("--crop 형식: x0,y0,x1,y1 (0~1 비율)")

    # ---- 대상 페이지 결정 ----
    if a.image:
        targets, total = [1], 1
    elif a.page:
        targets, total = [a.page], None
    elif a.pages:
        targets, total = [int(x) for x in a.pages.split(",")], None
    else:
        hits, total = find_table_pages(a.pdf)
        if a.all_pages or not hits:
            targets = list(range(1, total + 1))
            why = "전체 순회" + ("" if a.all_pages else " (텍스트 없음 - 스캔본)")
        else:
            targets = hits
            why = f"텍스트 탐색 {len(hits)}/{total}p"
        print(f"대상 페이지: {targets}  [{why}]", file=sys.stderr)

    # ---- 페이지별 판독 ----
    results, failed = [], []
    for pno in targets:
        print(f"  p{pno} 판독...", end="", flush=True, file=sys.stderr)
        try:
            parsed, raw = read_one(a, pno, crop)
        except Exception as e:
            print(f" 오류 {type(e).__name__}", file=sys.stderr)
            failed.append((pno, str(e)[:60]))
            continue

        if a.raw:
            print(f"\n----- p{pno} 원본 -----\n{raw}\n" + "-" * 22)

        n = len(parsed.get("rows", [])) if parsed else 0
        print(f" {n}행", file=sys.stderr)
        if n:
            results.append((pno, parsed))

    if not results:
        print("\n표를 찾지 못했습니다.", file=sys.stderr)
        if failed:
            for pno, e in failed:
                print(f"  p{pno}: {e}", file=sys.stderr)
        print("  --raw 로 모델 응답 확인, --crop 으로 영역 지정, --dpi 조정을 시도하세요.",
              file=sys.stderr)
        sys.exit(1)

    # ---- 병합 ----
    res, notes = merge_pages(results)
    rows = res["rows"]
    cols = res["columns"]

    pages_used = ", ".join(f"p{p}({len(r.get('rows', []))})" for p, r in results)
    print(f"\n사용: {pages_used} -> 병합 {len(rows)}행", file=sys.stderr)
    if res.get("id_column"):
        print(f"식별 컬럼: {res['id_column']}", file=sys.stderr)

    show_table(rows, cols)

    print()
    txt, ok = report(rows, a.expect_lk, a.expect_rows,
                     label=(res.get("table_title") or "")[:30])
    print(txt)

    if notes:
        print(f"병합 알림 {len(notes)}건:")
        for n in notes[:10]:
            print(f"  - {n}")
        if len(notes) > 10:
            print(f"  ... 외 {len(notes)-10}건")
        ok = False

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"\n저장: {a.out}")

    sys.exit(0 if ok else 2)


def cmd_meta(a):
    """도면 주기에서 계열 속성(나사규격/재질 등) 추출. 계열당 1회면 충분."""
    if not a.pdf and not a.image:
        sys.exit("--pdf 또는 --image 중 하나는 필요합니다")

    crop = tuple(float(x) for x in a.crop.split(",")) if a.crop else None

    if a.image:
        targets = [1]
    elif a.page:
        targets = [a.page]
    else:
        targets = [1]   # 주기와 표제란은 보통 sheet 1
        print("페이지 미지정 -> p1 (주기/표제란은 통상 sheet 1)", file=sys.stderr)

    best = None
    for pno in targets:
        print(f"  p{pno} 판독...", end="", flush=True, file=sys.stderr)
        try:
            parsed, raw = read_one(a, pno, crop, META_PROMPT)
        except Exception as e:
            print(f" 오류 {type(e).__name__}", file=sys.stderr)
            continue
        if a.raw:
            print(f"\n----- p{pno} 원본 -----\n{raw}\n" + "-" * 22)
        filled = sum(1 for v in (parsed or {}).values() if v not in (None, "", False))
        print(f" 필드 {filled}개", file=sys.stderr)
        if parsed and (best is None or filled > best[1]):
            best = (parsed, filled)

    if best is None:
        sys.exit("판독 실패. --raw 로 확인하세요.")

    meta = best[0]
    print()
    for k in ("standard_no", "title", "thread", "thread_pd", "thread_spec",
              "material", "material_desc", "coating", "temperature"):
        v = meta.get(k)
        print(f"  {k:18s} {v if v is not None else '-'}")

    if meta.get("thread_varies_by_dash"):
        print("\n※ 나사규격이 dash 마다 다름 -> read 명령으로 치수표를 읽을 것")
    elif not meta.get("thread"):
        print("\n※ 나사규격 미판독. --crop 으로 주기 영역을 지정해 보세요.")

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"\n저장: {a.out}")


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)

    pv = s.add_parser("preview"); pv.set_defaults(fn=cmd_preview)
    pv.add_argument("--pdf", required=True)

    rd = s.add_parser("read"); rd.set_defaults(fn=cmd_read)
    rd.add_argument("--pdf", help="스펙 PDF 경로")
    rd.add_argument("--image", help="PNG/JPG 직접 입력 (PyMuPDF 불필요)")
    rd.add_argument("--page", type=int, help="특정 페이지만")
    rd.add_argument("--pages", help="여러 페이지 지정. 예: 2,3")
    rd.add_argument("--all-pages", action="store_true", help="전 페이지 강제 순회")
    rd.add_argument("--dpi", type=int, default=300)
    rd.add_argument("--crop", help="표 영역만. 예: 0,0.45,1,0.85")
    rd.add_argument("--expect-lk", type=float, help="L-Kmax 기대 상수")
    rd.add_argument("--expect-rows", type=int, help="기대 행 수")
    rd.add_argument("--raw", action="store_true")
    rd.add_argument("--save-image")
    rd.add_argument("--out")

    mt = s.add_parser("meta", help="도면 주기에서 나사규격/재질 추출")
    mt.set_defaults(fn=cmd_meta)
    mt.add_argument("--pdf")
    mt.add_argument("--image")
    mt.add_argument("--page", type=int)
    mt.add_argument("--dpi", type=int, default=200)
    mt.add_argument("--crop")
    mt.add_argument("--raw", action="store_true")
    mt.add_argument("--save-image")
    mt.add_argument("--out")

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
