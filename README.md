# Kakao-Digest

지정한 카카오톡 오픈채팅방을 매일 읽어 **노션에 일자별로 아카이브**하고, 방에서 공유된 **링크를 따로 모아 정리**하는 개인용 파이프라인입니다. macOS에서 동작합니다.

## 무엇을 하나요
1. 카톡방 대화를 수집해 로컬(`daily/`)에 저장
2. 노션 "톡방 대화" DB에 일자별 페이지로 기록
3. 공유된 링크를 추출·분류해 노션에 정리

## 어떻게 읽나요 (핵심)
카카오톡 Mac 앱은 대화를 **암호화된 로컬 DB(SQLCipher)**에 저장합니다. 이 도구는 그 DB를 **본인 기기에서 복호화해** 읽습니다. 카카오 서버에 접속하거나 남의 데이터를 보는 게 아니라, **내 맥에 이미 있는 내 대화**를 읽는 것입니다.

- 창을 띄워둘 필요가 없고, 스크롤 한계 없이 방의 전체 대화를 정확하게(읽을 때마다 동일하게) 읽습니다.
- 읽기 도구로 [kakaocli](https://github.com/silver-flight-group/kakaocli)를 씁니다. 다만 최신 카톡에서 로그인 계정 번호(user_id)를 못 찾는 문제가 있어, **탐색 범위를 넓힌 patch**를 적용해 다시 빌드합니다(아래).

## AI 에이전트로 셋업하기 (권장)
셋업이 여러 단계라, **Claude Code 같은 AI 코딩 에이전트에게 이 저장소를 통째로 주고 맡기는 것**을 권합니다. 에이전트에게 이렇게 말하면 됩니다:

> "이 저장소의 AGENTS.md를 읽고, 카카오톡 아카이빙 파이프라인을 내 맥에 셋업해줘."

에이전트가 kakaocli 빌드 → 복호화 확인 → 설정 → 첫 수집까지 단계별로 진행하고, 사람이 직접 해야 하는 부분(권한 부여, 노션 토큰 발급)은 그때그때 알려줍니다. 에이전트용 상세 지침은 [AGENTS.md](AGENTS.md)에 있습니다.

## 직접 셋업하기 (수동)
1. **필요한 것**: macOS(Apple Silicon 권장), 카카오톡 Mac 앱(로그인), Swift(Xcode Command Line Tools), Homebrew, Python 3.10+, 노션 계정.
2. **전체 디스크 접근(FDA)**: 시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근에 터미널을 추가(암호화 DB를 읽으려면 필요).
3. **kakaocli 빌드** — 상세는 [docs/kakaocli-rebuild.md](docs/kakaocli-rebuild.md):
   ```
   brew install sqlcipher
   git clone https://github.com/silver-flight-group/kakaocli.git kakaocli-rebuild/kakaocli
   cd kakaocli-rebuild/kakaocli
   git apply ../../patches/kakaocli-userid-bruteforce.patch
   swift build -c release
   ```
4. **복호화 확인** — 대상 방을 카톡에서 한 번 연 뒤:
   ```
   KAKAOCLI_BRUTE_TIMEOUT=1800 .build/release/kakaocli auth --verbose
   ```
   "User ID: ..." 와 "Database opened successfully"가 나오면 성공입니다.
5. **설정**:
   ```
   cp config/targets.txt.example config/targets.txt   # 방 이름(순수 한글 조각) 채우기
   cp config/rooms.json.example config/rooms.json      # 방별 마스킹 정책
   ```
   노션 토큰·DB ID는 `.env`에 넣습니다(`.env`는 커밋되지 않습니다).
6. **실행**:
   ```
   python scripts/collect.py --limit 500                    # 오늘 수집
   python scripts/collect.py --backfill --date 2026-01-15   # 놓친 날 채우기
   python scripts/notion_push.py                            # 노션에 기록
   ```

## 한계와 주의 (꼭 읽으세요)
- **계정에 따라 user_id를 못 찾을 수 있습니다.** kakaocli는 로그인 계정 번호를 숫자 대입(브루트포스)으로 찾는데, 그 번호가 아주 크거나 UUID형이면 patch로 범위를 넓혀도 못 찾을 수 있습니다([kakaocli 이슈 #16](https://github.com/silver-flight-group/kakaocli/issues/16)). 그러면 현재로선 이 방식이 불가합니다.
- **카톡 업데이트로 깨질 수 있습니다.** 복호화는 카톡 내부 구조에 의존해서, 카톡이 크게 바뀌면 kakaocli와 이 patch가 다시 맞춰질 때까지 동작하지 않을 수 있습니다. 업스트림(kakaocli) 대응에 의존합니다.
- **본인 기기·본인 대화·본인 책임.** 이 도구는 내 맥의 내 대화만 읽습니다. 남의 기기나 데이터에 쓰지 마세요. 카카오톡 이용약관과 관련 법을 확인하고 본인 책임하에 사용하세요.
- **강한 권한·비밀값 주의.** 전체 디스크 접근(FDA)을 준 터미널은 맥 전체 파일에 접근하니, 그 터미널에서 신뢰할 수 없는 스크립트를 돌리지 마세요. `kakaocli auth --verbose`는 복호화 키를 화면에 출력하니 로그·스크린샷으로 남기지 마세요. 노션 토큰이 유출되면 올린 대화가 노출되니 토큰 관리에 유의하세요.
- **개인정보**: 수집한 대화 원본(`daily/`)과 노션 기록에는 실제 대화가 담깁니다. 노션 push 전 **전화번호만** 정책(rooms.json)에 따라 자동 마스킹됩니다 — **실명·주소 등 다른 개인정보 마스킹은 아직 구현돼 있지 않습니다**(`notion_push.py`의 `mask_names`는 TODO). `daily/`·`.env`·`state/`는 절대 공개 저장소에 올리지 마세요(`.gitignore`에 이미 제외돼 있습니다).
- 사진·이모티콘 등 비텍스트는 수집하지 않습니다(텍스트만).

## 크레딧
- 읽기 도구: [silver-flight-group/kakaocli](https://github.com/silver-flight-group/kakaocli) (MIT)
- 카톡 Mac DB 복호화 연구: [blluv](https://gist.github.com/blluv/8418e3ef4f4aa86004657ea524f2de14)

## 라이선스
MIT — [LICENSE](LICENSE) 참조. (공개 전 LICENSE의 저작자 이름을 채우세요.)
