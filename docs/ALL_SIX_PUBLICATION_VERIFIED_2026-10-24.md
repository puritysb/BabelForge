# 교정본 6종 공통 발행 및 최종 검증 완료

확인 시각: **2026-10-24 09:09 KST**.

`publish_reviewed.py data/reviews/library-2026-10-23/manifest.json`으로 교정본 6종 전체에 새 공통 발행 파이프라인을 실제 적용했다. 전체 판본의 사전 검수 승인·파일 해시 검사, 기존 Calibre 항목 교체, 공개 OPDS 다운로드 검증, 로컬 라이브러리·카탈로그 갱신을 모두 완료했다.

[공개 OPDS](https://books.foundby.kr/opds) · [최종 검증 JSON](/Users/puritysb/github/BabelForge/data/publications/all-six-2026-10-24/final-verification.json) · [실제 발행 결과](/Users/puritysb/github/BabelForge/data/reviews/library-2026-10-23/publication-results.json)

| 판본 | 유지한 ID | 검증한 대역 구간 | 최신 교정본 |
|---|---:|---:|---|
| 오만과 편견 — 영어·한국어 대역 (교정본) | 44 | 2,528 | [다운로드](https://books.foundby.kr/get/EPUB/44) |
| 인간적인, 너무나 인간적인 — Harvey 영어판·한국어 대역 (교정본) | 46 | 1,300 | [다운로드](https://books.foundby.kr/get/EPUB/46) |
| 인간적인, 너무나 인간적인 — Zimmern 영어판 1부·한국어 대역 (교정본) | 47 | 3,174 | [다운로드](https://books.foundby.kr/get/EPUB/47) |
| 인간적인, 너무나 인간적인 — 독일어·한국어 대역 (교정본) | 41 | 2,932 | [다운로드](https://books.foundby.kr/get/EPUB/41) |
| 나는 고양이로소이다 — 일본어·한국어 대역 (교정본) | 45 | 9,217 | [다운로드](https://books.foundby.kr/get/EPUB/45) |
| 오디세이아 — Murray 1919 그리스어·한국어 대역 (교정본) | 48 | 1,294 | [다운로드](https://books.foundby.kr/get/EPUB/48) |

## 최종 상태

| 확인 대상 | 결과 |
|---|---|
| 활성 로컬 라이브러리 EPUB | 6개, 모두 승인된 최종 산출물과 SHA-256 일치 |
| Calibre 항목 | 6개, 판본 식별자 중복 없음 |
| 발행 카탈로그 항목 | 6개, 실제 파일·ID·주소 일치 |
| 공개 OPDS 항목 | 전체 탐색·페이지 조회 결과 6개, 판본별 하나 |
| 구판·중복 항목 | 0개 |
| 관련 구판 백업·옛 EPUB 사본 | 0개, 새 백업 생성 없음 |
| 기존 ID·제목 | 41·44·45·46·47·48과 기존 제목 모두 유지 |
| 서버 설정 | 읽기 전용 설정 파일 해시 유지, 정상 재시작 |

공통 발행이 모두 끝난 후 전체 목록을 다시 조회하고, 공개 다운로드 6종을 독립적으로 재검사했다. 각 EPUB의 원문·한국어 **20,445개 대역 구간 전체**를 승인 체크포인트와 순서대로 비교했고, **6종 모두 공식 EPUBCheck를 통과**했다. 안정적인 기존 다운로드 주소와 OPDS가 안내하는 실제 링크 양쪽을 확인했다.

로컬 라이브러리 하위의 배포용 EPUB 사본도 모두 같은 최신 산출물 해시인지 검사했으며, 배포 ZIP 안의 6개 파일과 CRC도 확인했다. 원문 저본·검수 기록은 유지했다. 알림이나 기기 전송은 실행하지 않았다.

발행 전후 목록은 `data/publications/all-six-2026-10-24/before.json`, `calibre-final.json`, `opds-final-*.xml`에 기록했다. 각 판본의 공통 발행 저널도 이번 실행 이후 갱신됐고 전부 `stage=completed`임을 확인했다.
