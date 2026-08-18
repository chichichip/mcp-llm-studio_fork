# samples

판독을 시험해 볼 스펙 PDF를 여기에 둔다. **비어 있는 것이 정상이다** —
실제 스펙은 사내 문서라 저장소에 올리지 않는다(`spec-reader/.gitignore`가 막는다).

```cmd
python read_spec.py preview --pdf samples\MS9555.pdf
python read_spec.py read --pdf samples\MS9555.pdf --expect-lk 0.578
```

이 파일은 폴더를 유지하려고 둔다. 예전에는 `.gitkeep`을 썼지만 사내 보안검사가
점(.)으로 시작하는 파일을 막아서 확장자 있는 파일로 바꿨다. **새 폴더를 추가할 때도
`.gitkeep` 대신 README.md를 둘 것.**
