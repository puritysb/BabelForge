# BabelForge

고전 서적의 원문과 한국어 번역을 함께 읽을 수 있는 **일반 EPUB 3**를 만듭니다.
목차, 장 순서, 강조, 시·표, 이미지와 연결된 각주를 보존하고, 원문·번역을 각 본문
블록 안에서 차례로 보여 줍니다. 특정 펌웨어, 전용 폰트나 기기 버튼은 필요하지 않습니다.

Gutenberg, Standard Ebooks, 로컬 파일을 입력으로 사용합니다. 원전·판본 조사와 개념
교차검증, 문단 정렬 번역, 최종 의미·용어 검수를 거쳐 파일을 생성하며 Calibre/OPDS
배포도 선택할 수 있습니다. 상세 운영 절차의 정본은
[book-translator 스킬](.agents/skills/book-translator/SKILL.md)입니다.

## 설치

```bash
python3 -m venv venv
./venv/bin/python3 -m pip install -r requirements.txt
cp .env.example .env
# .env에 GLM_API_KEY 또는 ZAI_API_KEY 설정
```

전체 EPUB 규격 검사에는 Java와 EPUBCheck가 필요합니다.

```bash
./venv/bin/python3 scripts/setup_epubcheck.py
```

이 명령은 공식 배포본의 체크섬을 확인하고 `data/tools/`에 설치합니다. 일반 구조 검사는
항상 실행하며, 설치된 EPUBCheck도 자동으로 실행합니다. 정식 배포 환경에서는
`BABELFORGE_REQUIRE_EPUBCHECK=1`로 전체 검사기 없이 배포되지 않게 설정할 수 있습니다.

## 사용

```bash
# 후보 검색 후 선택한 후보 JSON을 candidate.json에 저장
./venv/bin/python3 search.py "pride and prejudice" --no-line

# EPUB 파일만 생성: Calibre나 기기 없이 동작
./run_request.sh candidate.json --no-publish --no-notify

# Calibre 라이브러리와 OPDS에도 배포
./start.sh candidate.json
```

파일은 `data/library/`에 저장됩니다. 제목 외에 판본 식별 해시를 포함하여 동명 작품의
파일 충돌을 피합니다. 로컬 생성 완료 상태는 `completed`, 공개 OPDS 다운로드까지 검증한 발행 완료는 `published`입니다.

교정본 발행은 기존 항목의 ID와 주소를 유지해 교체하고, 공개 다운로드의 내용 대조와
EPUBCheck가 끝난 뒤 같은 판본의 구판·중복을 삭제합니다. 구판 EPUB 백업은 만들지 않습니다.
완료된 교정 manifest는 `publish_reviewed.py`로 발행하며, 절차와 설정은
[book-translator 스킬](.agents/skills/book-translator/SKILL.md)에 있습니다.
생성은 실제 번역 API 키가 필요하며 오류가 남은 번역이나 손상된 EPUB는 완료되지 않습니다.

MCP에서는 `search_books`, `translate_book`, `retry_request`, `get_status`, `list_recent`를
사용합니다. `translate_book(candidate, publish_output=False, notify=False)`는 로컬 EPUB만
생성합니다. 재시도는 같은 요청 ID, 출력 모드, 알림 설정을 유지하며 종료된 작업자도 복구합니다.

운영 MCP 코드 배포는 `deploy_mcp.py`로 수행합니다. 현재 승인, 로컬·공개 EPUBCheck,
OPDS 최신본과 구판·중복 여부를 자동 검사하고, 실패 시 단계별 보고서를 남깁니다.
재시작 없는 점검도 지원하며, 명령과 매니페스트 요건은 위 스킬을 참조하십시오.

OpenClaw 정기 점검은 저장소의 `openclaw_monitor.py`를 실행합니다.
`install_openclaw_monitor.py`가 검증된 Java 환경과 로컬 승인 기준을 설정하고 기존 예약을
갱신합니다. 정상·복구 시에는 알리지 않고 이상만 승인된 LINE 대상으로 통지합니다.
재설치와 기준 갱신 절차도 위 스킬에서 관리합니다.
`openclaw_line_drill.py`는 운영 상태와 분리된 LINE 알림 시험을 제공합니다. 기본은 미리보기이며,
명시적 시험 실행에서 최초 1회 전송·중복 억제·무알림 복구와 기존 일정 유지를 검증합니다.
`install_openclaw_followup.py`는 특정 날짜의 실제 예약 실행을 확인하는 일회성 후속 점검을
설정합니다. `openclaw_followup.py`가 실행 기록과 검증 보고서를 연결하고, 정상은 무알림으로
처리하며 이미 통지한 동일 이상은 다시 보내지 않습니다. 설정·사용 절차는 위 스킬을 따릅니다.

공개 OPDS 주소: **https://books.foundby.kr/opds**. 기기로 직접 보내는 기능은 기본적으로
꺼져 있으며, 기존 장치 연동이 필요한 경우에만 `BABELFORGE_DEVICE_PUSH=1`로 켭니다.
기기 전송이나 알림 실패는 EPUB 생성·배포 완료를 취소하지 않습니다.

## 개발과 검증

```bash
./venv/bin/python3 -m unittest discover -s tests -v
./venv/bin/python3 epub_validate.py /path/to/book.epub
```

테스트는 임시 상태와 가짜 API 응답을 사용합니다. 다운로드 포맷, 판본 캐시, 본문 보존,
각주 링크, 다국어 무번역, 검수 후 변경, 체크포인트, 동시 갱신 및 배포 실패를 검사합니다.
외부 API 번역의 문학적 품질은 별도 표본 평가가 필요합니다.

번역 API나 배포 서비스 없이 직접 작성한 짧은 예제로 EPUB 생성·검증을 확인할 수 있습니다.

```bash
./venv/bin/python3 scripts/make_demo.py
# data/examples/portable-bilingual-demo.epub 생성
```

[아키텍처·개발 지침](CLAUDE.md) · [EPUB 포맷 계약](docs/epub-format.md) ·
[개선 전 검토 보고서](docs/PIPELINE_REVIEW_2026-10-22.md)
