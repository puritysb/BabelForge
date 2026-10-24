# BabelForge 전체 파이프라인 검토 — 2026-10-22

> 개선 전 검토 기록입니다. 이후 일반 EPUB 3 중심으로 코드와 검증 체계를 개편했습니다.
> 아래 결함 설명·행 번호·테스트 수는 당시 상태이며, 현재 계약과 구현은
> [CLAUDE.md](../CLAUDE.md)와 [EPUB 포맷 계약](epub-format.md)을 참조하십시오.

현재 구조에는 원전 조사, 개념 시트, 문단 정렬, 의미 검수, 용어 감사, 체크포인트, OPDS 배포가 모두 구현되어 있다. 그러나 **입력 작품의 동일성, 원문 보존, 최종 수정본 검증, 동시 실행과 배포 복구에 결함이 있어 자동 배포 결과의 정확성을 보장하는 상태는 아니다.** 모델 호출 횟수보다 이 경계들의 검증을 먼저 보완해야 한다.

검토는 현재 작업 트리 전체를 대상으로 했다. 시작 전부터 있던 `assemble.py`와 `publish.py`의 미커밋 변경도 포함했으며 유지했다. 이번 작업은 검토 문서만 추가했고 제품 코드, 운영 카탈로그, 체크포인트, 배포본은 수정하지 않았다.

검증 근거는 다음과 같다.

- `./venv/bin/python3 -m unittest discover -s tests -v`: **40개 통과**. 대부분 모킹 기반이며 아래 실패 입력과 동시성 경로를 충분히 다루지 않는다.
- 세 영역을 독립 검토하고 임시 TXT/EPUB, 임시 체크포인트, HTTP/모델/Calibre mock, 동시 갱신 재현으로 확인했다. 실제 번역 API, 배포, 기기 전송, LINE 알림은 실행하지 않았다.
- 저장된 EPUB 5개의 XML은 모두 파싱되었으며 원문/번역 `<p>` 개수도 각각 일치했다. 이는 문법과 개수 확인이며, EPUBCheck 적합성이나 번역 정확성 검증은 아니다.
- 저장된 일본어 EPUB에서 실질적인 일본어 한 문장이 원문과 번역 칸에 동일하게 남아 있었다. 과거 산출물이므로 현 버전이 생성했다고 단정하지 않지만, 아래 무번역 검사 결함과 동일한 형태다.
- 형제 저장소의 `~/github/crosspoint-agentdeck/docs/bilingual-epub.md`를 현재 환경에서 찾지 못했다. 코드의 블록 `<p class="cp-original">` / `<p class="cp-translation">` 사용은 확인했으나 펌웨어와 직접 대조하거나 장치에서 실행하지 않았다.

P1은 잘못된 책·누락된 본문·불완전한 번역·운영 데이터 손실을 초래하여 우선 수정할 문제, P2는 특정 입력이나 복구 상황에서 발생하는 정확성·운영 문제로 분류했다. 아래 재현 결과는 별도 설명이 없으면 외부 서비스 호출 없는 fixture 결과다.

1. **[P1] Gutenberg TXT를 EPUB로 오인하여 정상 다운로드를 처리하지 못한다.**

   근거: [sources/gutenberg.py:57](/Users/puritysb/github/BabelForge/sources/gutenberg.py:57). 확장자 외에도 URL 어디에든 `epub`가 있으면 EPUB로 분류한다. `https://www.gutenberg.org/cache/epub/1342/pg1342.txt`에 TXT 바이트를 반환하자 `format="epub"`, 이어 추출에서 `EpubException: Bad Zip file`이 발생했다. 검색의 HTML fallback도 TXT로 분류되어 HTML 태그 전체가 번역 입력으로 남는다. 후보에 선택한 MIME을 보존하고 응답·파일 signature로 확인해야 한다. HTML은 별도로 추출하거나 지원 형식에서 제외해야 한다.

2. **[P1] 원전 자동 대체가 같은 저자의 다른 작품으로 바꿀 수 있다.**

   근거: [precision.py:241](/Users/puritysb/github/BabelForge/precision.py:241). 저자 토큰이 하나만 맞아도 제목 일치가 0인 후보를 허용한다. `Human, All-Too-Human`의 독일어 원전 검색 결과로 `Also sprach Zarathustra`만 제공하면 `replaced=True`로 선택된다. 이후 번역·감사는 잘못 선택한 작품을 기준으로 정상 수행될 수 있다. 동일 작품·권·판본 확인을 필수 조건으로 만들고 확증이 없으면 선택한 원본을 유지해야 한다. `rights == "public domain"`도 현재는 필수 조건이 아닌 가산점이다.

3. **[P1] Standard Ebooks의 동명 작품이 캐시를 공유한다.**

   근거: [sources/standard_ebooks.py:60](/Users/puritysb/github/BabelForge/sources/standard_ebooks.py:60), [동일 캐시 반환:81](/Users/puritysb/github/BabelForge/sources/standard_ebooks.py:81). 식별자가 `title_slug`뿐이다. 저자가 다른 두 `collected-poems`를 순차 fetch하면 경로가 같고 두 번째 요청은 통신 없이 첫 번째 책의 바이트를 반환한다. canonical 판본 URL을 식별자로 사용하고 캐시에 URL·내용 해시·형식을 함께 기록해야 한다.

4. **[P1] EPUB 읽기 순서가 바뀌고 목차가 본문에 섞인다.**

   근거: [extract.py:165](/Users/puritysb/github/BabelForge/extract.py:165). `book.spine` 대신 모든 document item을 순회한다. manifest가 `[chapter2, chapter1, nav]`, spine이 `[chapter1, chapter2]`인 EPUB을 추출하면 `chapter2 → chapter1 → nav`가 된다. 기본 읽기 순서는 spine이 정의한다는 [W3C EPUB 3.3 규정](https://www.w3.org/TR/epub-33/#sec-spine-elem)에 맞춰 idref를 해석해야 한다. nav와 비선형 각주는 별도 정책으로 처리해야 한다.

5. **[P1] EPUB에서 시구·중간 제목·각주·강조가 누락되거나 중복된다.**

   근거: [extract.py:180](/Users/puritysb/github/BabelForge/extract.py:180). `p`와 `li`만 찾고 `get_text()`로 평탄화한다. fixture의 `div` 시구와 중간 `h2`는 사라졌고, `li > p` 각주는 두 번 추출되었으며 `em` 및 링크/anchor 구조가 없어졌다. 번역기의 태그 보존 기능에 도달하기 전에 구조가 소실된다. 중복 없는 block 순회와 구조를 담는 중간 표현을 만들고, 시·극·각주·표·inline 강조를 명시적으로 지원해야 한다. 지원하지 않는 내용은 조용히 버리지 말고 검토 대상으로 표시해야 한다.

6. **[P1] TXT의 첫 장 제목 앞에 있는 실제 본문이 삭제된다.**

   근거: [extract.py:227](/Users/puritysb/github/BabelForge/extract.py:227). 초기 `Preface` 객체는 `chapters`에 추가되지 않고 첫 heading에서 교체된다. 실제 서론 문단 뒤에 `CHAPTER I`를 둔 fixture에서 서론이 사라졌다. 초기 bucket을 보존하고 명확하게 식별한 boilerplate만 제거해야 한다. 추출 이후의 의미 감사에는 삭제된 원문 자체가 전달되지 않으므로 후속 모델 검수로는 탐지할 수 없다.

7. **[P1] 일본어·중국어를 그대로 복사해도 무번역 검사에서 통과할 수 있다.**

   근거: [quality_audit.py:65](/Users/puritysb/github/BabelForge/quality_audit.py:65). 번역 필요 여부를 Latin/Greek/Cyrillic 문자 8개 이상으로 판단한다. 긴 일본어 원문을 번역 칸에 그대로 넣고 감사 모델이 `keep`을 반환하면 `integrity_violations_remaining=0`이 된다. 입력 언어와 Unicode 문자를 기준으로 검사하고, 짧은 문장·숫자·인명 등 예외를 분리해야 한다.

   실제 보관 파일 `data/library/吾輩は猫である.epub`의 `OEBPS/chapter16.xhtml`에서도 다음 문장이 원문/번역 쌍에 동일하게 있었다: `堀川は三味線もので賑やかなばかりで実がないからよそうと云うと、細君は不平な顔をして引き下がった。` 이 관찰은 번역 한 칸이 존재한다는 사실만으로 완성을 판정할 수 없음을 보여준다.

8. **[P1] 의미 감사 뒤의 용어 교정이 번역을 훼손해도 이전 감사 결과를 사용한다.**

   근거: [translate.py:839](/Users/puritysb/github/BabelForge/translate.py:839). 의미 감사 후 용어 감사가 다시 본문을 수정하지만 마지막 수정본의 의미·무번역 검사는 없다. 정상 한국어를 의미 감사가 유지한 뒤 용어 감사가 `In truth, …` 원문으로 되돌리면서 관용구 예외를 반환하도록 재현했다. 보고서는 `passed=True`, `integrity_violations_remaining=0`인데 최종 본문을 다시 검사하면 원문 복사와 한국어 부재가 검출된다. 모든 수정 후 결정적 무결성 검사를 수행하고 변경된 문단의 의미를 재검수해야 한다. 감사 보고서는 최종 텍스트 해시에 연결해야 한다.

9. **[P1] 동시 상태 갱신·전송 큐 갱신이 유실되고 같은 요청이 중복 실행된다.**

   근거: [catalog.py:59](/Users/puritysb/github/BabelForge/catalog.py:59), [pending_queue.py:46](/Users/puritysb/github/BabelForge/pending_queue.py:46), [babelforge_mcp.py:118](/Users/puritysb/github/BabelForge/babelforge_mcp.py:118). `load → 수정 → 저장` 전체에는 프로세스 간 잠금이 없다. 서로 다른 두 요청을 동시에 published로 바꾸면 하나가 requested로 남는 경우를 재현했다. 큐의 `enqueue(new)`와 `remove(old)` 경합에서는 결과가 `[]`여서 새 작업이 사라졌다. 공통 `.tmp` 이름도 경합을 일으킨다. 같은 failed 요청의 동시 retry는 작업자를 2개 생성했다. SQLite 트랜잭션 또는 전체 갱신 잠금, 조건부 상태 전환, 요청별 실행 소유권이 필요하다. `os.replace()`만으로는 이 문제를 해결할 수 없다.

10. **[P1] 새 배포가 실패하면 기존 정상 배포본까지 영구 삭제된다.**

    근거: [publish.py:61](/Users/puritysb/github/BabelForge/publish.py:61). 명령 순서는 `list → remove --permanent → add`이다. 새 등록 실패 fixture에서 `['list', 'permanent remove 42', 'add failed']`가 확인됐다. 제목만 비교하므로 동명 다른 저자/판본도 교체 대상이다. 기존 미커밋 수정은 검색 결과의 제목을 정확히 재검사하도록 개선했지만 이 문제는 남는다. 안정적인 작품·판본 ID로 대상을 식별하고 새 파일 등록·검증 후 교체를 확정해야 한다. 가능하면 Calibre ID를 유지하여 기존 acquisition URL도 보존해야 한다.

11. **[P2] 관용구 예외 하나가 같은 문단의 독립 개념까지 면제한다.**

    근거: [quality_audit.py:519](/Users/puritysb/github/BabelForge/quality_audit.py:519), [검사 생략:265](/Users/puritysb/github/BabelForge/quality_audit.py:265). 예외 키가 문단과 concept ID까지여서 출현 위치를 구분하지 못한다. `Die Wahrheit bleibt, aber in Wahrheit zweifle ich.`에서 `in Wahrheit`만 예외로 승인해도 첫 번째 독립 `Wahrheit`를 `진리`로 옮기지 않은 결과가 통과했다. 예외를 원문의 위치별로 적용하거나 예외 표현을 제외한 나머지 출현을 재검사해야 한다.

12. **[P2] 품질 프로필 변경이 구조 체크포인트 복구로 우회된다.**

    근거: [translate.py:671](/Users/puritysb/github/BabelForge/translate.py:671). 체크포인트가 원문 변경으로 거부됐는지 품질 프로필 변경으로 거부됐는지 구분하지 않고 구조 remap을 시도한다. 같은 원문에서 profile만 바꾸면 새 번역 함수 호출이 0회이고 기존 번역이 재사용됐다. 새 strict 용어는 후속 감사가 잡을 수 있지만 비강제 문맥 지침·문체·모델 변경은 보장하지 않는다. 구조 재배치와 품질 지침 변경을 분리해, 전자는 호환 프로필에서만 복구하고 후자는 명시적으로 재번역/재교정해야 한다. 현재 동작은 CLAUDE.md의 프로필 변경 시 재시작 설명과 다르다.

13. **[P2] CrossPoint가 없을 때 일반 HTTP 장비를 전송 대상으로 선택한다.**

    근거: [device_push.py:202](/Users/puritysb/github/BabelForge/device_push.py:202). mDNS에서 장치 확인에 실패해도 `found_ips[0]`를 반환한다. `Office Printer._http._tcp.local.`만 존재하고 fingerprint가 False인 fixture에서도 IP가 선택됐다. 일반 HTTP 생존 확인 후 `/upload`로 전송을 시도할 수 있고 실제 기기 탐색도 건너뛴다. 검증된 장비만 반환하고, 없으면 다른 탐색을 계속하거나 전송을 대기해야 한다.

14. **[P2] 이미 배포된 책을 부가 전송 실패 때문에 전체 실패로 기록한다.**

    근거: [pipeline.py:217](/Users/puritysb/github/BabelForge/pipeline.py:217), [예외 처리:244](/Users/puritysb/github/BabelForge/pipeline.py:244). Calibre 등록 완료 후 큐 저장에 실패하도록 재현하면 카탈로그가 `published → failed`로 바뀌고 `ok=False`를 반환한다. 알림 타임아웃도 같은 경로로 전파될 수 있다. 배포 결과와 기기 전송/알림 상태를 별도로 저장하고 완료된 배포를 유지해야 한다. 재시도 역시 실패한 부가 단계만 재실행하도록 분리해야 한다.

추가로 확인한 범위가 좁은 결함과 운영 개선 사항은 다음과 같다.

| 항목 | 근거와 영향 | 권장 처리 |
|---|---|---|
| 짧은 문장 삭제 | [extract.py:47](/Users/puritysb/github/BabelForge/extract.py:47): `え？ 彼は戻った。`에서 `え？`가 사라짐 | 길이가 아닌 공백 여부로 필터 |
| 짧은 실제 장 병합 | [extract.py:264](/Users/puritysb/github/BabelForge/extract.py:264): 본문 한 문단씩인 I·II·III장이 III장 하나로 합쳐짐 | 검증된 front matter만 병합 |
| Standard Ebooks 번역판 검색 누락 | [sources/standard_ebooks.py:46](/Users/puritysb/github/BabelForge/sources/standard_ebooks.py:46): 경로 3조각만 허용. 공식 [War and Peace 판본](https://standardebooks.org/ebooks/leo-tolstoy/war-and-peace/louise-maude_aylmer-maude) 링크 fixture가 `[]` 반환 | 번역자/판본 경로를 포함한 실제 book 링크 처리 |
| CLI의 `.env` 사용 실패 | [run_request.sh:20](/Users/puritysb/github/BabelForge/run_request.sh:20): Python 설정 로드 전에 export된 키가 없으면 exit 3 | 키 검사를 설정 로드 이후로 통일 |
| `notify=False` 무시 | [pipeline.py:150](/Users/puritysb/github/BabelForge/pipeline.py:150): 진행 콜백 mock에서 알림 함수 1회 호출 | 모든 알림 경로에 옵션 적용 |
| 알림 timeout 전파 | [search.py:95](/Users/puritysb/github/BabelForge/search.py:95): `TimeoutExpired`를 잡지 않음 | 알림 실패를 별도 상태로 기록 |
| 비정상 종료 복구 부족 | [babelforge_mcp.py:121](/Users/puritysb/github/BabelForge/babelforge_mcp.py:121): failed만 재시도하므로 종료된 프로세스의 translating 요청을 복구할 수 없음 | worker lease/heartbeat와 중단 요청 회수 |
| 워처 성공 상태 미반영 | [auto_push_watcher.py:71](/Users/puritysb/github/BabelForge/auto_push_watcher.py:71): 성공 후 큐만 제거 | req_id의 device_push 갱신 |
| 다국어 배포 메타데이터 오류 | [publish.py:75](/Users/puritysb/github/BabelForge/publish.py:75): `kor,eng` 고정 | 실제 원문 언어 전달 |
| PNG 표지 MIME 불일치 | [assemble.py:82](/Users/puritysb/github/BabelForge/assemble.py:82): 감지한 형식과 무관하게 JPEG로 선언 | 실제 MIME을 manifest에 전달 |
| EPUB 장 크기 제한 누락 | [extract.py:188](/Users/puritysb/github/BabelForge/extract.py:188): TXT와 달리 oversized 분할 경로를 거치지 않음 | 논리 장과 기기용 파일 분할을 분리하고 두 형식에 적용 |

개선은 다음 순서로 진행하는 것이 합리적이다.

1. **입력과 배포의 정확성을 먼저 고정한다.** 형식 판정, 동일 작품/판본 확인, 캐시 키, spine/본문 추출, CJK 무번역 검사, 최종 수정본 검사, 상태 트랜잭션, 기존 배포본 보존을 수정하고 위 재현을 회귀 테스트로 추가한다.
2. **원문부터 배포본까지 추적 가능한 검증을 만든다.** 원전 URL·저자·번역자·권·언어·형식·다운로드 해시를 기록하고 추출 단위마다 원문 위치를 유지한다. 원문→추출→번역→EPUB 단계에서 누락·중복·순서 변화를 확인한다. 완전한 판본인지 확인할 장/권 목록도 필요하다. 의미 감사는 추출 이후 누락을 잡을 수 없으므로 단계별 검증이 필수다.
3. **검수의 비용과 재개를 개선한다.** 문단의 원문·번역·품질 프로필 해시에 묶인 감사 결과를 저장한다. 현재는 책 후반 감사 실패 시 앞서 완료한 감사·교정을 다시 수행한다. 변경 문단만 재감사하고 수정 수렴 횟수에 제한을 둔다. 번역·의미 검수·용어 검수·배포 진행률도 분리한다.
4. **고전의 다양한 형식으로 품질을 평가한다.** 영어/독일어/일본어의 소설뿐 아니라 시, 희곡, 각주 많은 철학서, 다권본을 작은 고정 corpus로 구성한다. 정렬·본문 보존·목차·형식은 결정적으로 검사하고, 부정·양태·대명사·인용·고유명사·희귀 개념의 의미 정확성은 사람 검토 기준이 있는 표본으로 평가한다. 동일 모델을 여러 번 호출했다는 사실만으로 검수의 독립성을 보장하지는 않는다.
5. **출판 검증을 자동화한다.** EPUBCheck, ZIP/XML, spine/nav 참조, 쌍의 교대와 개수, 빈/복사 번역, 언어·표지 MIME을 확인한 파일만 배포한다. 펌웨어 계약을 가져와 버전을 고정한 producer/consumer 회귀 fixture를 운영한다. 새 결과의 확인 후 교체를 확정하고, 상태/큐와 감사 기록을 함께 보존한다.

유지할 기반도 분명하다. 어댑터·추출·번역·조립·배포가 분리되어 있고, 요청별 개념 시트, 구조화 JSON 검증, 빈 번역 배포 차단, 체크포인트의 안전한 파일 교체, 재개 시 번역을 공백으로 되돌리지 않는 방어가 있다. 이 기반을 유지하면서 **원문 보존과 최종 결과의 증거를 강화하는 작업이 우선**이다. 현 단계에서 모델 교체나 동시 작업자 확대부터 진행하는 것은 위 정확성 문제를 해결하지 못한다.
