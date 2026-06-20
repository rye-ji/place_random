import json
import os
import random
import re
from dataclasses import dataclass, asdict
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup


APP_TITLE = "장소 추천기"
DATA_FILE = Path("places_data.json")
KST = ZoneInfo("Asia/Seoul")
# '기타' 카테고리 추가
CATEGORY_OPTIONS = ["전체", "식당", "카페", "놀거리", "기타"]
WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


@dataclass
class PlaceRecord:
    place_id: str
    source_url: str
    name: str = ""
    category: str = "식당"
    original_category: str = "" 
    closed_days: List[str] = None
    weekly_hours: Dict[str, Dict[str, Any]] = None
    last_updated: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["closed_days"] = self.closed_days or []
        d["weekly_hours"] = self.weekly_hours or default_weekly_hours()
        return d


def default_weekly_hours() -> Dict[str, Dict[str, Any]]:
    return {
        day: {"open": "09:00", "close": "18:00", "closed": False, "breaks": []}
        for day in WEEKDAYS_KO
    }


def map_naver_category_to_app(category_text: str) -> str:
    text = (category_text or "").strip()

    if any(x in text for x in ["카페", "디저트", "베이커리", "브런치", "커피", "제과", "제빵"]):
        return "카페"

    if any(x in text for x in ["체험", "전시", "공원", "관광", "테마", "오락", "방탈출", "노래방", "게임", "PC"]):
        return "놀거리"
        
    if any(x in text for x in ["식당", "음식점", "한식", "중식", "일식", "양식", "고기", "찌개", "술집", "포차", "바", "치킨", "꼬치", "국수", "김밥", "만두", "족발"]):
        return "식당"

    # 위 세 개에 걸리지 않으면 '기타'로 분류
    return "기타"


def ensure_state() -> None:
    if "places" not in st.session_state:
        st.session_state.places = load_places()
    if "pending_place" not in st.session_state:
        st.session_state.pending_place = None
    if "crawl_message" not in st.session_state:
        st.session_state.crawl_message = ""
    if "render_key" not in st.session_state:
        st.session_state.render_key = str(random.randint(1000, 9999))
    if "editing_place_id" not in st.session_state:
        st.session_state.editing_place_id = None


def load_places() -> List[Dict[str, Any]]:
    if not DATA_FILE.exists():
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_places(places: List[Dict[str, Any]]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(places, f, ensure_ascii=False, indent=2)


def delete_place(place_id: str) -> None:
    places = st.session_state.places
    places = [p for p in places if p.get("place_id") != place_id]
    st.session_state.places = places
    save_places(places)


def resolve_short_url(url: str) -> str:
    """naver.me 단축 URL의 실제 목적지 주소를 추적합니다."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        # allow_redirects=True로 설정하여 최종 목적지까지 리다이렉트 유도
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=5)
        return response.url
    except Exception:
        return url


def normalize_input(raw: str):
    raw = raw.strip()
    
    # naver.me 단축 URL인 경우 먼저 풀어서 진짜 URL 확보
    if "naver.me" in raw:
        with st.spinner("단축 URL 주소 확인 중..."):
            raw = resolve_short_url(raw)
            
    m = re.search(r"place/(\d+)", raw)
    if m:
        pid = m.group(1)
        return pid, f"https://m.place.naver.com/place/{pid}"
    if raw.isdigit():
        return raw, f"https://m.place.naver.com/place/{raw}"
    if raw.startswith("http"):
        return "", raw.rstrip("/")
    return raw, f"https://m.place.naver.com/place/{raw}"


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/605.1.15"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8" 
    return resp.text


def parse_apollo_state(html: str) -> Optional[Tuple[str, Dict[str, Dict[str, Any]], List[str], str, str, str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    apollo_data = None

    for tag in soup.find_all("script"):
        text = tag.get_text()
        if "__APOLLO_STATE__" in text:
            m = re.search(r"__APOLLO_STATE__\s*=\s*(\{.+?\});?\s*$", text, re.MULTILINE | re.DOTALL)
            if not m:
                m = re.search(r"__APOLLO_STATE__\s*=\s*(\{.+?\});", text, re.DOTALL)

            if m:
                try:
                    apollo_data = json.loads(m.group(1))
                    break
                except Exception:
                    continue

    if not apollo_data:
        return None

    place_name = ""
    category_hint = "기타"
    original_category = ""
    weekly_hours = default_weekly_hours()
    closed_days: List[str] = []
    hours_summary_list: List[str] = []

    def add_hours_from_container(container: Any) -> None:
        nonlocal weekly_hours, closed_days, hours_summary_list

        if not isinstance(container, list):
            return

        for root_item in container:
            if not isinstance(root_item, dict):
                continue

            daily_items = root_item.get("businessHours")
            if isinstance(daily_items, list):
                source_items = daily_items
            elif "day" in root_item:
                source_items = [root_item]
            else:
                continue

            for item in source_items:
                if not isinstance(item, dict):
                    continue

                day_str = item.get("day")
                target_days = WEEKDAYS_KO if day_str == "매일" else [day_str]

                for day in target_days:
                    if day not in WEEKDAYS_KO:
                        continue

                    bh = item.get("businessHours")
                    break_hours = item.get("breakHours") or []
                    last_order_times = item.get("lastOrderTimes") or []
                    description = item.get("description") or ""
                    show_ends_next_day = bool(item.get("showEndsNextDay"))

                    if bh is None or "휴무" in description:
                        weekly_hours[day] = {
                            "open": "",
                            "close": "",
                            "closed": True,
                            "breaks": [],
                            "last_order": "",
                            "showEndsNextDay": False,
                        }
                        if day not in closed_days:
                            closed_days.append(day)
                        hours_summary_list.append(f"{day}(휴무)")
                        continue

                    start_time = bh.get("start", "")
                    end_time = bh.get("end", "")

                    breaks = []
                    if isinstance(break_hours, list):
                        for br in break_hours:
                            if isinstance(br, dict) and br.get("start") and br.get("end"):
                                breaks.append({"start": br["start"], "end": br["end"]})

                    last_order = ""
                    if isinstance(last_order_times, list) and last_order_times and isinstance(last_order_times[0], dict):
                        last_order = last_order_times[0].get("time", "") or ""

                    weekly_hours[day] = {
                        "open": start_time,
                        "close": end_time,
                        "closed": False,
                        "breaks": breaks,
                        "last_order": last_order,
                        "showEndsNextDay": show_ends_next_day,
                    }

                    parts = [f"{start_time}~{end_time}"]
                    if breaks:
                        parts.append(
                            "브레이크 "
                            + ", ".join(f"{b['start']}~{b['end']}" for b in breaks)
                        )
                    if last_order:
                        parts.append(f"LO {last_order}")
                    if show_ends_next_day:
                        parts.append("다음날 마감")

                    hours_summary_list.append(f"{day}(" + " / ".join(parts) + ")")

    def walk(node: Any) -> None:
        nonlocal place_name, category_hint, original_category

        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith("PlaceDetailBase:") and isinstance(value, dict):
                    if value.get("name"):
                        place_name = value["name"]
                    
                    orig_cat = value.get("category", "")
                    if orig_cat:
                        original_category = orig_cat
                    category_hint = map_naver_category_to_app(orig_cat)

                if isinstance(key, str) and "newBusinessHours" in key:
                    add_hours_from_container(value)

                walk(value)

        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(apollo_data)

    hours_text = " / ".join(dict.fromkeys(hours_summary_list)) if hours_summary_list else "정보 없음"
    closed_days = sorted(set(closed_days), key=lambda x: WEEKDAYS_KO.index(x))

    if place_name:
        return place_name, weekly_hours, closed_days, hours_text, category_hint, original_category, apollo_data

    return None


def crawl_naver_place(raw_input: str) -> Dict[str, Any]:
    place_id, url = normalize_input(raw_input)
    html = fetch_html(url)

    result: Dict[str, Any] = {
        "place_id": place_id,
        "source_url": url,
        "name": "",
        "category": "기타",
        "original_category": "",
        "closed_days": [],
        "weekly_hours": default_weekly_hours(),
        "success": False,
        "raw_json": None,
        "raw": html,
    }

    parsed = parse_apollo_state(html)
    
    if parsed:
        name, weekly_hours, closed_days, hours_text, category_hint, original_category, apollo_data = parsed
        result["raw_json"] = apollo_data 
        
        if name:
            result["name"] = name
            result["weekly_hours"] = weekly_hours
            result["closed_days"] = closed_days
            result["category"] = category_hint
            result["original_category"] = original_category
            result["success"] = True
    else:
        soup = BeautifulSoup(html, "html.parser")
        for selector in ["meta[property='og:title']", "meta[name='title']", "title"]:
            node = soup.select_one(selector)
            if node:
                val = node.get("content") or node.get_text(strip=True)
                if val:
                    result["name"] = val.replace("네이버 플레이스", "").strip().rstrip("-").strip()
                    break

    return result


def parse_time_str(s: str) -> Optional[time]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
        
        # [수정] 24:00 예외 안전하게 예외처리 (23:59:59로 매핑)
        if h == 24 and m == 0:
            return time(23, 59, 59)
            
        # 25:00, 26:00 같은 익일 새벽 마감시간 처리
        return time(h % 24, m)
    except Exception:
        return None


def is_open_now(record: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(KST)
    cur = now.time()
    
    today_idx = now.weekday()
    yesterday_idx = (today_idx - 1) % 7
    
    weekly_hours = record.get("weekly_hours") or {}
    
    def is_open_for_schedule(info: Dict, is_yesterday_schedule: bool) -> bool:
        if info.get("closed"):
            return False
            
        op = parse_time_str(info.get("open", ""))
        cl = parse_time_str(info.get("close", ""))
        if not op or not cl:
            return False
            
        is_operating = False
        if op <= cl: 
            if not is_yesterday_schedule:
                is_operating = op <= cur <= cl
        else: 
            if is_yesterday_schedule:
                is_operating = cur <= cl
            else:
                is_operating = cur >= op
                
        if not is_operating:
            return False
            
        for b in info.get("breaks", []):
            bs = parse_time_str(b.get("start", ""))
            be = parse_time_str(b.get("end", ""))
            if bs and be and bs <= cur <= be:
                return False 
                
        return True

    today_info = weekly_hours.get(WEEKDAYS_KO[today_idx], {})
    yesterday_info = weekly_hours.get(WEEKDAYS_KO[yesterday_idx], {})
    
    return is_open_for_schedule(today_info, False) or is_open_for_schedule(yesterday_info, True)


# [수정] 랜덤 추천 시 선택한 카테고리만 필터링되도록 보완
def random_recommendation(records: List[Dict[str, Any]], category_filter: str) -> Optional[Dict[str, Any]]:
    candidates = [r for r in records if is_open_now(r)]
    
    if category_filter != "전체":
        candidates = [c for c in candidates if c.get("category", "기타") == category_filter]
        
    if not candidates:
        return None
    return random.choice(candidates)


def render_manual_form(initial: Dict[str, Any]) -> Dict[str, Any]:
    st.subheader("수동 입력")
    st.caption("크롤링이 실패했거나 정보가 부족한 경우 직접 입력하세요.")
    rk = st.session_state.render_key
    app_categories = ["식당", "카페", "놀거리", "기타"]

    edited = {
        "name": st.text_input("장소명", value=initial.get("name", ""), key=f"manual_name_{rk}"),
        "category": st.selectbox(
            "앱 카테고리", app_categories, index=app_categories.index(initial.get("category", "기타")) if initial.get("category", "기타") in app_categories else 3, key=f"manual_category_{rk}"
        ),
    }

    st.write("요일별 운영 여부와 시간을 입력하세요.")
    weekly_hours = {}
    cols = st.columns(2)
    for i, day in enumerate(WEEKDAYS_KO):
        with cols[i % 2]:
            st.markdown(f"**{day}요일**")
            closed = st.checkbox(f"{day} 휴무", value=initial.get("weekly_hours", default_weekly_hours()).get(day, {}).get("closed", False), key=f"manual_closed_{day}_{rk}")
            open_val = st.text_input(
                f"{day} 오픈",
                value=initial.get("weekly_hours", default_weekly_hours()).get(day, {}).get("open", "09:00"),
                key=f"manual_open_{day}_{rk}",
                disabled=closed,
            )
            close_val = st.text_input(
                f"{day} 마감",
                value=initial.get("weekly_hours", default_weekly_hours()).get(day, {}).get("close", "18:00"),
                key=f"manual_close_{day}_{rk}",
                disabled=closed,
            )
            weekly_hours[day] = {"closed": closed, "open": open_val, "close": close_val, "breaks": []}

    edited["weekly_hours"] = weekly_hours
    edited["closed_days"] = [day for day, info in weekly_hours.items() if info.get("closed")]
    return edited


def render_auto_form(crawled: Dict[str, Any]) -> Dict[str, Any]:
    st.subheader("자동 입력 확인")
    st.caption("크롤링 결과를 확인하고 필요하면 수정하세요.")
    rk = st.session_state.render_key

    app_categories = ["식당", "카페", "놀거리", "기타"]

    initial_category = crawled.get("category", "기타")
    if initial_category not in app_categories:
        initial_category = map_naver_category_to_app(initial_category)

    category_index = (
        app_categories.index(initial_category)
        if initial_category in app_categories
        else 3
    )

    edited = {
        "name": st.text_input("장소명", value=crawled.get("name", ""), key=f"auto_name_{rk}"),
        "category": st.selectbox("앱 카테고리", app_categories, index=category_index, key=f"auto_category_{rk}"),
    }

    weekly_hours = {}
    cols = st.columns(2)
    base = crawled.get("weekly_hours") or default_weekly_hours()
    closed_days_set = set(crawled.get("closed_days") or [])
    for i, day in enumerate(WEEKDAYS_KO):
        with cols[i % 2]:
            st.markdown(f"**{day}요일**")
            closed_default = bool(base.get(day, {}).get("closed", False) or day in closed_days_set)
            closed = st.checkbox(f"{day} 휴무", value=closed_default, key=f"auto_closed_{day}_{rk}")
            open_val = st.text_input(
                f"{day} 오픈",
                value=base.get(day, {}).get("open", "09:00"),
                key=f"auto_open_{day}_{rk}",
                disabled=closed,
            )
            close_val = st.text_input(
                f"{day} 마감",
                value=base.get(day, {}).get("close", "18:00"),
                key=f"auto_close_{day}_{rk}",
                disabled=closed,
            )
            breaks = base.get(day, {}).get("breaks", [])
            weekly_hours[day] = {"closed": closed, "open": open_val, "close": close_val, "breaks": breaks}

    edited["weekly_hours"] = weekly_hours
    edited["closed_days"] = [day for day, info in weekly_hours.items() if info.get("closed")]
    return edited


def render_edit_form(place: Dict[str, Any]) -> Dict[str, Any]:
    st.write("---")
    st.subheader(f"✏️ '{place.get('name', '이름 없음')}' 수정 중")
    
    pid = place.get("place_id", "temp")
    app_categories = ["식당", "카페", "놀거리", "기타"]
    
    edited = place.copy()
    edited["name"] = st.text_input("장소명", value=place.get("name", ""), key=f"edit_name_{pid}")
    
    cat_index = 3
    if place.get("category", "기타") in app_categories:
        cat_index = app_categories.index(place.get("category", "기타"))
        
    edited["category"] = st.selectbox("앱 카테고리", app_categories, index=cat_index, key=f"edit_category_{pid}")
    
    st.write("요일별 운영 여부와 시간을 수정하세요.")
    weekly_hours = {}
    cols = st.columns(2)
    base = place.get("weekly_hours") or default_weekly_hours()
    for i, day in enumerate(WEEKDAYS_KO):
        with cols[i % 2]:
            st.markdown(f"**{day}요일**")
            closed = st.checkbox(f"{day} 휴무", value=base.get(day, {}).get("closed", False), key=f"edit_closed_{day}_{pid}")
            open_val = st.text_input(f"{day} 오픈", value=base.get(day, {}).get("open", "09:00"), key=f"edit_open_{day}_{pid}", disabled=closed)
            close_val = st.text_input(f"{day} 마감", value=base.get(day, {}).get("close", "18:00"), key=f"edit_close_{day}_{pid}", disabled=closed)
            breaks = base.get(day, {}).get("breaks", [])
            weekly_hours[day] = {"closed": closed, "open": open_val, "close": close_val, "breaks": breaks}
            
    edited["weekly_hours"] = weekly_hours
    edited["closed_days"] = [day for day, info in weekly_hours.items() if info.get("closed")]
    return edited


def add_place_to_store(place: Dict[str, Any]) -> None:
    places = st.session_state.places
    existing_ids = {p.get("place_id") for p in places}
    place["last_updated"] = datetime.now(KST).isoformat(timespec="seconds")
    if place.get("place_id") in existing_ids:
        places = [p for p in places if p.get("place_id") != place.get("place_id")]
    places.append(place)
    st.session_state.places = places
    save_places(places)


def filtered_places(category: str, open_only: bool) -> List[Dict[str, Any]]:
    places = st.session_state.places
    if category != "전체":
        places = [p for p in places if p.get("category", "기타") == category]
    if open_only:
        places = [p for p in places if is_open_now(p)]
    return places


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="💘", layout="wide")
    ensure_state()

    st.title("💘 데이트용 장소 추천기")
    st.caption("네이버 플레이스 ID, 단축 URL(naver.me)을 지원하며, 카테고리별 조건부 랜덤 추천이 가능합니다.")

    with st.form("add_place_form", clear_on_submit=False):
        raw_input = st.text_input("네이버 플레이스 주소 또는 단축 링크", placeholder="예: 15333275 또는 https://naver.me/xktVvqyF")
        submitted = st.form_submit_button("장소 추가")

    if submitted and raw_input.strip():
        st.session_state.render_key = str(random.randint(1000, 9999))
        
        with st.spinner("네이버 플레이스 연동 중..."):
            try:
                crawled = crawl_naver_place(raw_input)
                st.session_state.pending_place = crawled
                if crawled.get("success"):
                    st.session_state.crawl_message = f"🎉 성공적으로 분석했습니다! '{crawled.get('name')}'의 데이터를 확인해 주세요."
                else:
                    st.session_state.crawl_message = "⚠️ 영업시간 구조를 자동으로 읽지 못했습니다. 아래에서 정보를 채워주세요."
            except Exception as e:
                st.session_state.pending_place = {
                    "place_id": normalize_input(raw_input)[0],
                    "source_url": normalize_input(raw_input)[1],
                    "name": "",
                    "category": "기타",
                    "original_category": "",
                    "closed_days": [],
                    "weekly_hours": default_weekly_hours(),
                    "success": False,
                    "error": str(e),
                }
                st.session_state.crawl_message = f"❌ 오류 발생: {e}"

    if st.session_state.crawl_message:
        if "성공" in st.session_state.crawl_message or "분석" in st.session_state.crawl_message:
            st.success(st.session_state.crawl_message)
        else:
            st.warning(st.session_state.crawl_message)

    pending = st.session_state.pending_place
    if pending:
        if pending.get("success"):
            edited = render_auto_form(pending)
        else:
            edited = render_manual_form(pending)

        final = {
            "place_id": pending.get("place_id", ""),
            "source_url": pending.get("source_url", ""),
            "name": edited.get("name", pending.get("name", "")),
            "category": edited.get("category", "기타"),
            "original_category": pending.get("original_category", ""),
            "closed_days": edited.get("closed_days", []),
            "weekly_hours": edited.get("weekly_hours", default_weekly_hours()),
        }

        if st.button("최종 확인 및 저장", type="primary"):
            if not final["name"]:
                st.error("장소명을 입력하세요.")
            else:
                add_place_to_store(final)
                st.success(f"'{final['name']}' 장소가 저장되었습니다!")
                st.session_state.pending_place = None
                st.session_state.crawl_message = ""
                st.rerun()

    st.divider()
    st.subheader("저장된 장소")

    left, right = st.columns([1, 1])
    with left:
        category_filter = st.selectbox("카테고리 필터", CATEGORY_OPTIONS, index=0)
    with right:
        open_only = st.checkbox("현재 영업 중인 곳만 보기", value=False)

    places = filtered_places(category_filter, open_only)
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    st.caption(f"기준 시각: {now_str} (Asia/Seoul)")

    if places:
        for idx, place in enumerate(places, start=1):
            status = "🟢 영업 중" if is_open_now(place) else "🔴 영업 종료 / 휴무"
            orig_cat = place.get('original_category', '')
            cat_display = f" [{orig_cat}]" if orig_cat else ""
            
            with st.expander(f"{idx}. {place.get('name', '(이름 없음)')}{cat_display} — {status}"):
                
                if st.session_state.editing_place_id == place.get("place_id"):
                    edited_place = render_edit_form(place)
                    
                    ecol1, ecol2 = st.columns(2)
                    if ecol1.button("💾 변경 내용 저장", key=f"save_{place.get('place_id')}", type="primary"):
                        add_place_to_store(edited_place)
                        st.session_state.editing_place_id = None
                        st.rerun()
                    if ecol2.button("❌ 취소", key=f"cancel_{place.get('place_id')}"):
                        st.session_state.editing_place_id = None
                        st.rerun()
                else:
                    st.write(f"**앱 분류:** {place.get('category', '')}")
                    st.write(f"**URL:** {place.get('source_url', '')}")
                    
                    weekly = place.get("weekly_hours") or {}
                    for day in WEEKDAYS_KO:
                        info = weekly.get(day, {})
                        if info.get("closed"):
                            st.write(f"- {day}: 휴무")
                        else:
                            open_t = info.get('open', '')
                            close_t = info.get('close', '')
                            breaks = info.get('breaks', [])
                            
                            break_strs = [f"{b.get('start', '')}-{b.get('end', '')}" for b in breaks if b.get('start') and b.get('end')]
                            break_text = f" (브레이크 타임 {', '.join(break_strs)})" if break_strs else ""
                            
                            st.write(f"- {day}: {open_t} ~ {close_t}{break_text}")
                    
                    st.write("")
                    col1, col2 = st.columns([1, 1])
                    if col1.button("✏️ 이 장소 수정", key=f"edit_btn_{place.get('place_id', idx)}_{idx}"):
                        st.session_state.editing_place_id = place.get("place_id")
                        st.rerun()
                    if col2.button("🗑️ 이 장소 삭제", key=f"del_btn_{place.get('place_id', idx)}_{idx}"):
                        delete_place(place.get("place_id"))
                        st.rerun()
    else:
        st.info("조건에 맞는 장소가 없습니다.")

    st.divider()
    st.subheader("🎲 랜덤 추천")
    
    # [수정] 랜덤 추천받을 카테고리를 고를 수 있도록 필터 셀렉트박스 추가
    recommend_cat = st.selectbox("추천받을 카테고리 선택", CATEGORY_OPTIONS, index=0, key="recommend_cat_select")
    
    if st.button("선택한 카테고리에서 랜덤 1곳 추천", type="primary"):
        rec = random_recommendation(st.session_state.places, recommend_cat)
        if rec:
            orig_cat = rec.get('original_category', '')
            cat_display = f" [{orig_cat}]" if orig_cat else ""
            st.success(f"🎯 오늘 추천 장소: **{rec.get('name', '')}** ({rec.get('category', '기타')}){cat_display}")
            st.write(f"🔗 링크 바로가기: {rec.get('source_url', '')}")
        else:
            st.warning(f"현재 영업 중인 '{recommend_cat}' 카테고리의 저장 장소가 없습니다.")

    st.divider()
    st.subheader("데이터 백업 및 초기화")
    export_json = json.dumps(st.session_state.places, ensure_ascii=False, indent=2)
    st.download_button("JSON 다운로드", data=export_json, file_name="places_data.json", mime="application/json")

    with st.expander("저장 파일을 직접 초기화하고 싶다면"):
        st.write(f"저장 위치: `{DATA_FILE.resolve()}`")
        if st.button("저장 데이터 비우기", type="secondary"):
            st.session_state.places = []
            save_places([])
            st.success("초기화되었습니다.")
            st.rerun()


if __name__ == "__main__":
    main()