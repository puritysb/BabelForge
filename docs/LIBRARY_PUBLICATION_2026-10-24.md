# 교정 EPUB 라이브러리·OPDS 반영 완료

**후속 변경:** 사용자의 구판 삭제 지시에 따라 이 문서에 기록된 임시 백업 폴더와 옛 EPUB 사본은 삭제했다. 현재 정책은 구판 백업 없이 검증 후 교체·정리하는 방식이다. [최신 일반화·삭제 보고서](/Users/puritysb/github/BabelForge/docs/REVISION_PUBLICATION_2026-10-24.md)를 참조한다.

완료 확인: 2026-10-24T08:24:09+09:00 (한국 시간).

교정본 6종을 로컬 라이브러리, BabelForge 카탈로그, Calibre 및 공개 OPDS에 반영했다. 공개 피드에 각 판본이 한 번씩 표시되며, 피드에서 실제 내려받은 6개 EPUB 모두 원문·한국어 20,445개 대역 구간의 내용과 순서를 승인본과 대조하고 공식 EPUBCheck를 통과했다.

[공개 OPDS](https://books.foundby.kr/opds) · [구판 삭제 기록](/Users/puritysb/github/BabelForge/data/publications/retired-artifacts-deleted-2026-10-24.json) · [검증 JSON](/Users/puritysb/github/BabelForge/data/publications/current-library-2026-10-24.json)

| 판본 | Calibre ID | 다운로드 | 처리 |
|---|---:|---|---|
| 오만과 편견 | 44 | [최신 교정본](https://books.foundby.kr/get/EPUB/44) | 기존 ID·주소 유지 |
| 인간적인, 너무나 인간적인 — Harvey 영어판 | 46 | [최신 교정본](https://books.foundby.kr/get/EPUB/46) | 누락 항목 신규 등록 |
| 인간적인, 너무나 인간적인 — Zimmern 영어판 1부 | 47 | [최신 교정본](https://books.foundby.kr/get/EPUB/47) | 누락 항목 신규 등록 |
| 인간적인, 너무나 인간적인 — 독일어판 | 41 | [최신 교정본](https://books.foundby.kr/get/EPUB/41) | 기존 ID·주소 유지 |
| 나는 고양이로소이다 | 45 | [최신 교정본](https://books.foundby.kr/get/EPUB/45) | 기존 ID·주소 유지 |
| 오디세이아 — Murray 1919 그리스어판 | 48 | [최신 교정본](https://books.foundby.kr/get/EPUB/48) | 누락 항목 신규 등록 |

## 백업과 중복 정리

- 변경 전에 로컬 구판 EPUB 7개를 `local-library/`에 보존하고 원본과 SHA-256 일치를 확인했다.
- Calibre에 실제 남아 있던 3개 항목(41·44·45)은 원본 EPUB 형식, 표지, OPF 메타데이터를 `calibre-export/`로 내보냈다. 내보낼 때 EPUB 메타데이터를 재작성하지 않았다. 기존 로컬 구판과 대역 내용이 같은지 확인한 뒤 해당 ID를 교정본에 연결했다.
- Calibre의 변경 전 전체 항목 JSON, SQLite의 일관된 `metadata.db` 스냅샷, BabelForge 카탈로그, 대기열 및 서버 LaunchAgent 설정을 보존했다. 초기 백업 파일 해시는 `backup-sha256.json`에 있다.
- 로컬 라이브러리는 7개 구판에서 6개 교정본으로 정리했다. 같은 저본의 오디세이아 구판 두 개는 최신 교정본 하나로 통합했고, 이전 두 파일 모두 백업에 남아 있다. 서로 다른 니체 판본 3종은 각각 유지했다.
- 시작 시 Calibre에는 실제 중복 항목이 없고 3개 항목만 존재했다. 따라서 기존 레코드를 삭제하지 않고 그 EPUB 형식을 교체했으며, 없어진 Harvey·Zimmern과 미발행 오디세이아를 ID 46·47·48로 등록했다.
- 요청 카탈로그의 동일 Harvey 파일을 가리키던 과거 중복 성공 기록 2개와 `/path.epub`을 가리키던 가짜 테스트 성공 기록 1개를 `archived-catalog-entries.json`으로 보존한 뒤 활성 목록에서 정리했다. 실패·요청 이력은 유지했다. 실제 발행 항목은 판본별 하나씩 총 6개다.
- 오래된 저장소 절대 경로와 만료된 도메인·누락 ID를 가리키던 주소를 현재 경로와 검증한 공개 다운로드 주소로 갱신했다. 각 판본에 안정적인 `babelforge` 식별자를 등록하여 다음 갱신도 동일 항목을 교체하도록 했다.

## 서비스와 최종 확인

서버는 `--disable-local-write`로 실행 중이어서 HTTP 쓰기를 거부했다. 공개 서버의 권한을 완화하지 않고, LaunchAgent를 잠시 중지한 동안 CLI로 라이브러리를 갱신한 뒤 `finally` 복구 경로에서 같은 설정으로 다시 시작했다. 기존 서버 설정 파일의 바이트 동일성을 확인했고, 공개 읽기 전용 정책은 유지했다.

공개 OPDS 피드와 피드가 안내하는 EPUB 링크, 기존 형식의 안정적인 다운로드 주소에서 모두 HTTP 200을 확인했다. 기본 Python 요청의 403 응답은 브라우저 User-Agent로 재확인해 정상 접속됨을 확인했다. 서버가 내려주는 EPUB은 Calibre의 표시 메타데이터가 반영되어 로컬 승인 파일과 ZIP 바이트 해시가 다를 수 있으므로, 실제 전달 파일은 승인된 원문·한국어 전체 내용 대조와 공식 EPUBCheck로 검증했다. 공개 피드에는 중복 없이 정확히 6개 판본이 있고, 로컬 최상위 EPUB과 활성 발행 카탈로그도 각각 6개다.

발행 당시에는 백업 무결성을 확인했으나, 후속 사용자 지시에 따라 해당 백업은 모두 삭제했다. 현재 교정본과 원문·검수 기록은 유지했다. 알림·기기 전송은 실행하지 않았다.
