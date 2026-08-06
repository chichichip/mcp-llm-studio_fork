# spec-reader

항공 표준품 스펙 PDF 치수표를 Gemma VLM 으로 판독하고 자동 검증하는 모듈.

## 구조

```
read_spec.py     메인. PDF 렌더링 -> VLM 호출 -> 검증 -> 출력
verify.py        검증 로직 (VLM 불필요, 외부망에서 개발 가능)
prompts.py       프롬프트. 판독 안 맞으면 여기만 수정
selftest.py      fixture 로 verify 로직 자체 테스트
fixtures/        공개 표준 정답 데이터
config.py        사내 URL/모델명 (gitignore, 직접 생성)
```

## 외부망 (개발)

```bash
pip install -r requirements.txt
python selftest.py          # VLM 없이 검증 로직 테스트
```

`selftest.py` 는 fixture 정답이 통과하는지, 오독을 주입했을 때 잡아내는지 확인한다.
verify.py 를 수정하면 반드시 이걸 돌릴 것.

## 사내망 (실행)

```bash
cp config.example.py config.py    # URL, MODEL 채우기
python read_spec.py preview --pdf MS9555.pdf
python read_spec.py read --pdf MS9555.pdf --page 1 --expect-lk 0.578 --expect-rows 29
```

첫 실행은 `--raw --save-image out.png` 를 붙일 것.
판독이 틀렸을 때 모델이 무엇을 보고 무엇이라 답했는지 확인해야 고칠 수 있다.

## 판독이 안 맞을 때

| 증상 | 조치 |
|---|---|
| 행이 절반만 나옴 | 표가 좌우 그룹으로 쪼개짐. `--crop` 으로 그룹별 분할 판독 |
| 숫자가 틀림 | `--dpi 400` 으로 상향 |
| JSON 파싱 실패 | `--raw` 로 원본 확인 후 prompts.py 수정 |
| 행 0개 | `--crop 0,0.45,1,0.85` 처럼 표 영역만 지정 |

## 검증 규칙

- `L - K_max` 가 계열 상수 (MS9555=0.578, MS9556=0.630)
- `L` 이 1/16" 격자 위
- dash 중복/누락 없음
- null 값 없음

무그립 구간(짧은 dash)은 상수에서 벗어나므로 최빈값 기준으로 자동 분리한다.

자세한 도메인 배경은 `CLAUDE.md` 참조.
