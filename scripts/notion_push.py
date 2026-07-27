#!/usr/bin/env python3
"""
Kakao-Digest 노션 push (2단계)

daily/YYYY-MM-DD/ 의 방별 원문 JSON을 노션 데이터베이스에 일자별 페이지로 올린다.
실행: .venv/bin/python scripts/notion_push.py [--date YYYY-MM-DD] [--force]
자체 검증(네트워크 없이): .venv/bin/python scripts/notion_push.py --self-test

주의: 이 DB에 두 번째 데이터소스가 추가되면 이 스크립트의 API 호출이 깨진다
(databases 조회 결과 data_sources[0]만 사용하는 전제라서).
"""
import argparse
import datetime
import json
import pathlib
import re
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
CONFIG_DIR = ROOT / "config"
ROOMS_FILE = CONFIG_DIR / "rooms.json"
ENV_FILE = ROOT / ".env"

BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

# 앞뒤 숫자 경계 필수 — 없으면 URL 안 긴 숫자열(X 게시물 ID 등)을 전화번호로 오인해 링크를 훼손함
PHONE_RE = re.compile(r"(?<!\d)01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")


# ---------- .env 로더 ----------

def load_env(path: pathlib.Path) -> dict:
    """ROOT/.env 를 KEY=VALUE 로 파싱한다. 주석·빈 줄 무시, 따옴표 벗김."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        env[key] = val
    return env


# ---------- 마스킹 ----------

def mask_phone(text: str) -> str:
    return PHONE_RE.sub("01*-****-****", text)


def mask_names(text: str) -> str:
    # TODO: 실명 마스킹 로직 미구현 (인명 사전 대조 등) — 원문 그대로 반환
    return text


def apply_masking(text: str, mask: bool) -> str:
    if not mask:
        return text
    text = mask_phone(text)
    text = mask_names(text)
    return text


def load_rooms_config(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"Warning: {path} 파싱 실패 — 기본값(mask=true)으로 처리")
        return {}


def get_mask_setting(target: str, rooms_cfg: dict) -> bool:
    if target in rooms_cfg:
        return bool(rooms_cfg[target].get("mask", True))
    return bool(rooms_cfg.get("_default", {}).get("mask", True))


# ---------- 본문 구성 ----------

def build_message_lines(messages: list) -> list:
    lines = []
    for m in messages:
        clock = m.get("clock") or m.get("time") or "?"
        sender = m.get("sender", "")
        msg = m.get("message", "") or ""
        lines.append(f"[{clock}] {sender}: {msg}")
    return lines


def chunk_lines(lines: list, max_len: int = 1900) -> list:
    """줄(메시지 단위) 경계를 지키며 max_len 이하 덩어리로 합친다.
    단, 한 줄 자체가 max_len을 넘으면 그 줄만 강제로 잘라 별도 조각으로 낸다."""
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        if len(line) > max_len:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i + max_len])
            continue
        added_len = len(line) + (1 if current else 0)  # 줄바꿈 1자 포함
        if current_len + added_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_callout_text(chat: str, collected_at: str, new_count, first_run: bool) -> str:
    text = f"{chat} | 수집 {collected_at} | 신규 {new_count}건"
    if first_run:
        text += " | 초회 수집: 이전 날짜 대화 포함 가능"
    return text


def paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def callout_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


# ---------- 속성/상태 ----------

def compute_status(error, message_count: int) -> str:
    if not error:
        return "성공"
    if message_count > 0:
        return "부분수집"
    return "실패"


def build_properties(date_str: str, target: str, message_count: int,
                      link_count: int, mask: bool, status: str) -> dict:
    return {
        "이름": {"title": [{"text": {"content": f"{date_str} {target}"}}]},
        "날짜": {"date": {"start": date_str}},
        "방": {"select": {"name": target}},
        "메시지 수": {"number": message_count},
        "링크 수": {"number": link_count},
        "마스킹": {"checkbox": mask},
        "상태": {"select": {"name": status}},
    }


def count_links_by_room(day_dir: pathlib.Path) -> dict:
    """links.jsonl 을 room(=chat 제목) 기준으로 세어 dict로 반환."""
    counts = {}
    links_file = day_dir / "links.jsonl"
    if not links_file.exists():
        return counts
    for line in links_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        room = obj.get("room")
        counts[room] = counts.get(room, 0) + 1
    return counts


# ---------- Notion API ----------

def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def api_call(method: str, url: str, token: str, body: dict = None):
    """429/5xx면 Retry-After(없으면 2초) 대기 후 1회 재시도."""
    headers = notion_headers(token)
    resp = requests.request(method, url, headers=headers, json=body, timeout=30)
    if resp.status_code == 429 or resp.status_code >= 500:
        wait = float(resp.headers.get("Retry-After", 2))
        log(f"{resp.status_code} 응답 — {wait}초 대기 후 재시도")
        time.sleep(wait)
        resp = requests.request(method, url, headers=headers, json=body, timeout=30)
    return resp


def get_data_source_id(token: str, db_id: str) -> str:
    resp = api_call("GET", f"{BASE_URL}/databases/{db_id}", token)
    if not resp.ok:
        log(f"Error: 데이터베이스 조회 실패 {resp.status_code} {resp.text[:300]}")
        sys.exit(2)
    data = resp.json()
    sources = data.get("data_sources", [])
    if not sources:
        log("Error: 데이터베이스에 data_source가 없습니다.")
        sys.exit(2)
    ds_id = sources[0]["id"]
    log(f".env에 NOTION_DATA_SOURCE_ID={ds_id} 추가하면 이 조회를 생략합니다")
    return ds_id


def find_existing_pages(token: str, data_source_id: str, date_str: str, target: str):
    """중복 페이지 id 목록 반환. 조회 자체가 실패하면 None."""
    body = {
        "filter": {
            "and": [
                {"property": "날짜", "date": {"equals": date_str}},
                {"property": "방", "select": {"equals": target}},
            ]
        }
    }
    resp = api_call("POST", f"{BASE_URL}/data_sources/{data_source_id}/query", token, body)
    if not resp.ok:
        # 새 방 첫 push: '방' select에 옵션이 아직 없으면 중복도 없는 것
        # (옵션은 페이지 생성 시 자동 추가되므로 빈 목록으로 통과시킨다)
        if resp.status_code == 400 and "not found for property" in resp.text:
            return []
        log(f"중복 조회 실패: {resp.status_code} {resp.text[:300]}")
        return None
    return [r["id"] for r in resp.json().get("results", [])]


def trash_pages(token: str, page_ids: list) -> None:
    for pid in page_ids:
        resp = api_call("PATCH", f"{BASE_URL}/pages/{pid}", token, {"in_trash": True})
        if not resp.ok:
            log(f"기존 페이지 삭제 실패 ({pid}): {resp.status_code}")


def create_page(token: str, data_source_id: str, properties: dict, children: list):
    body = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": properties,
        "children": children,
    }
    return api_call("POST", f"{BASE_URL}/pages", token, body)


def append_blocks(token: str, page_id: str, blocks: list) -> bool:
    for i in range(0, len(blocks), 100):
        batch = blocks[i:i + 100]
        resp = api_call("PATCH", f"{BASE_URL}/blocks/{page_id}/children", token, {"children": batch})
        if not resp.ok:
            log(f"블록 추가 실패: {resp.status_code} {resp.text[:300]}")
            return False
        time.sleep(0.4)
    return True


# ---------- 자체 검증 ----------

def run_self_test() -> None:
    log("자체 검증 시작")
    ok = True

    # (1) 1,900자 분할 — 줄 경계 검증
    lines = ["A" * 900, "B" * 900, "C" * 900, "D" * 2500, "E" * 10]
    chunks = chunk_lines(lines, max_len=1900)
    if max(len(c) for c in chunks) > 1900:
        log("FAIL: 청크 길이가 1900자를 초과함")
        ok = False
    else:
        log(f"PASS: 청크 길이 검증 (청크 {len(chunks)}개, 최대 {max(len(c) for c in chunks)}자)")

    if chunks[-1] != "E" * 10:
        log(f"FAIL: 짧은 마지막 줄이 온전히 보존되지 않음 (got={chunks[-1][:30]!r})")
        ok = False
    else:
        log("PASS: 짧은 줄 경계 보존 확인")

    rebuilt = []
    for c in chunks:
        rebuilt.extend(c.split("\n"))
    if "A" * 900 not in rebuilt or "B" * 900 not in rebuilt or "C" * 900 not in rebuilt:
        log("FAIL: 줄이 청크 경계에서 잘림")
        ok = False
    else:
        log("PASS: 일반 줄들이 청크 내에서 유실·분할 없이 존재")

    # (2) 전화번호 마스킹 3케이스 (하이픈 / 공백 / 붙임)
    cases = [
        ("연락처 010-1234-5678 입니다", "연락처 01*-****-**** 입니다"),
        ("연락처 010 1234 5678 입니다", "연락처 01*-****-**** 입니다"),
        ("연락처 01012345678 입니다", "연락처 01*-****-**** 입니다"),
    ]
    mask_ok = True
    for src, expected in cases:
        got = mask_phone(src)
        if got != expected:
            log(f"FAIL: 마스킹 불일치 src={src!r} got={got!r} expected={expected!r}")
            mask_ok = False
    if mask_ok:
        log("PASS: 전화번호 마스킹 3케이스(하이픈/공백/붙임) 통과")
    else:
        ok = False

    # (3) 속성 페이로드 스키마 검증 (모의 방 JSON)
    mock_room = {
        "target": "테스트방", "chat": "테스트방", "collected_at": "2026-01-01",
        "message_count": 12, "error": None, "first_run": True, "new_count": 5,
        "messages": [{"sender": "익명", "time": "10:00",
                      "message": "안녕하세요 010-1234-5678", "is_me": False}],
    }
    status = compute_status(mock_room["error"], mock_room["message_count"])
    props = build_properties("2026-01-01", mock_room["target"],
                              mock_room["message_count"], 3, True, status)
    checks = [
        props["이름"]["title"][0]["text"]["content"] == "2026-01-01 테스트방",
        props["날짜"]["date"]["start"] == "2026-01-01",
        props["방"]["select"]["name"] == "테스트방",
        props["메시지 수"]["number"] == 12,
        props["링크 수"]["number"] == 3,
        props["마스킹"]["checkbox"] is True,
        props["상태"]["select"]["name"] == "성공",
    ]
    if all(checks):
        log("PASS: 속성 페이로드가 스키마와 일치")
    else:
        log(f"FAIL: 속성 페이로드 불일치 {props}")
        ok = False

    status_cases = [
        (None, 10, "성공"),
        ("일부 오류", 5, "부분수집"),
        ("완전 실패", 0, "실패"),
    ]
    status_ok = all(compute_status(e, c) == s for e, c, s in status_cases)
    if status_ok:
        log("PASS: 상태(성공/부분수집/실패) 판정 로직 3케이스 통과")
    else:
        log("FAIL: 상태 판정 로직 실패")
        ok = False

    if ok:
        log("자체 검증 전체 통과")
    else:
        log("자체 검증 일부 실패")
        sys.exit(1)


# ---------- 메인 ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="daily/ 방별 JSON을 노션 DB로 push")
    ap.add_argument("--date", type=str, default=None, help="대상 날짜 (YYYY-MM-DD, 기본 오늘)")
    ap.add_argument("--force", action="store_true", help="기존 페이지를 휴지통으로 보내고 재생성")
    ap.add_argument("--self-test", action="store_true", help="네트워크 없이 내부 로직만 검증")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return

    env = load_env(ENV_FILE)
    token = env.get("NOTION_TOKEN")
    db_id = env.get("NOTION_DB_ID")
    if not token or not db_id:
        log("Error: ROOT/.env 에 NOTION_TOKEN, NOTION_DB_ID 를 설정하세요.")
        sys.exit(2)

    data_source_id = env.get("NOTION_DATA_SOURCE_ID")
    if not data_source_id:
        data_source_id = get_data_source_id(token, db_id)

    date_str = args.date or datetime.date.today().isoformat()
    day_dir = DAILY_DIR / date_str
    if not day_dir.exists():
        log(f"Error: {day_dir} 없음")
        sys.exit(2)

    rooms_cfg = load_rooms_config(ROOMS_FILE)
    link_counts = count_links_by_room(day_dir)

    json_files = sorted(p for p in day_dir.glob("*.json") if p.name != "_summary.json")
    if not json_files:
        log("push할 방 JSON이 없습니다.")
        return

    exit_code = 0
    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log(f"{jf.name}: JSON 파싱 실패 ({e}) — skip")
            exit_code = 1
            continue

        target = data.get("target", jf.stem)
        chat = data.get("chat") or target
        message_count = data.get("message_count", len(data.get("messages", [])))
        error = data.get("error")
        first_run = data.get("first_run", False)
        new_count = data.get("new_count", message_count)
        collected_at = data.get("collected_at", date_str)
        messages = data.get("messages", []) or []
        link_count = link_counts.get(chat, 0)
        mask = get_mask_setting(target, rooms_cfg)
        status = compute_status(error, message_count)

        log(f"[{target}] 처리 중 (상태: {status})")

        existing = find_existing_pages(token, data_source_id, date_str, target)
        if existing is None:
            log(f"[{target}] 중복 조회 실패 — skip")
            exit_code = 1
            continue
        if existing:
            if not args.force:
                log(f"[{target}] 이미 존재 — skip")
                continue
            log(f"[{target}] --force: 기존 페이지 {len(existing)}개 휴지통으로 보내고 재생성")
            trash_pages(token, existing)

        properties = build_properties(date_str, target, message_count, link_count, mask, status)
        callout_text = build_callout_text(chat, collected_at, new_count, first_run)
        children = [callout_block(callout_text)]
        if error and not messages:
            children.append(paragraph_block(f"수집 실패: {error}"))
        else:
            lines = build_message_lines(messages)
            masked_lines = [apply_masking(l, mask) for l in lines]
            chunks = chunk_lines(masked_lines, 1900)
            children.extend(paragraph_block(c) for c in chunks)

        first_batch = children[:100]
        rest = children[100:]

        resp = create_page(token, data_source_id, properties, first_batch)
        if not resp.ok:
            log(f"[{target}] 페이지 생성 실패: {resp.status_code} {resp.text[:300]}")
            exit_code = 1
            continue
        page_id = resp.json().get("id")
        log(f"[{target}] 페이지 생성 완료 ({page_id})")

        if rest:
            if not append_blocks(token, page_id, rest):
                exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
