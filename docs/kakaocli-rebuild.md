# kakaocli 재빌드 가이드 (user_id 브루트포스 확장)

## 왜 필요한가
[kakaocli](https://github.com/silver-flight-group/kakaocli)는 카톡 로그인 계정 번호(user_id)를 SHA-512 브루트포스로 찾아 복호화 키를 만든다. 원본(0.4.1 기준)은 **10억 상한 + 10초 타임아웃 + 단일 스레드**라, 최신 App Store 카톡처럼 user_id가 크면 못 찾고 `No candidate user id produced a valid key`로 실패한다.

## 수정 내용 (patch)
`Sources/KakaoCore/Database/DeviceInfo.swift`의 `recoverUserIdFromSHA512`를 다음으로 바꾼다:
- **전 CPU 코어 병렬**(`DispatchQueue.concurrentPerform`)
- **범위·타임아웃을 환경변수로**: `KAKAOCLI_BRUTE_MAX`(기본 200억), `KAKAOCLI_BRUTE_TIMEOUT`(초, 0=무제한)

patch 파일: `patches/kakaocli-userid-bruteforce.patch`.

## 빌드 절차
```
brew install sqlcipher
git clone https://github.com/silver-flight-group/kakaocli.git kakaocli-rebuild/kakaocli
cd kakaocli-rebuild/kakaocli
git apply ../../patches/kakaocli-userid-bruteforce.patch
swift build -c release
# 산출물: .build/<arch>/release/kakaocli
```

## 복호화 실행
대상 방을 카톡에서 한 번 연 뒤(서버 동기화):
```
KAKAOCLI_BRUTE_TIMEOUT=1800 .build/release/kakaocli auth --verbose
```
성공하면 `User ID: <숫자>` 와 `Database opened successfully!`가 나온다.

주의: `--verbose`는 복호화 키(`Secure key: ...`)를 함께 출력한다. 이 출력을 로그·화면공유·스크린샷으로 남기지 말 것.

## 안 될 때
- **user_id를 못 찾음**: `KAKAOCLI_BRUTE_MAX`를 더 키워 재시도(느려짐). 그래도 안 되면 user_id가 정수 브루트포스 범위 밖이거나 UUID형일 수 있다([kakaocli 이슈 #16](https://github.com/silver-flight-group/kakaocli/issues/16)). 그 경우 user_id를 직접 확보해야 하며(모바일 앱 등), 현재로선 실패 가능성이 높다.
- **patch 적용 실패 / 빌드 오류**: 카톡·kakaocli 최신 변경으로 patch가 안 맞을 수 있다. `recoverUserIdFromSHA512` 함수를 최신 소스에 맞춰 같은 취지(병렬·범위확대)로 직접 수정한다.
- `ld: warning ... libsqlcipher.dylib ... newer version` 같은 링크 경고는 무해하다(빌드는 완료됨).

## 출처
- kakaocli: https://github.com/silver-flight-group/kakaocli (MIT)
- 카톡 Mac DB 복호화 연구: https://gist.github.com/blluv/8418e3ef4f4aa86004657ea524f2de14
