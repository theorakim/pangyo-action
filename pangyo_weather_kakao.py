#!/usr/bin/env python3
"""
판교 날씨 → 카카오톡 자동 전송 스크립트 🌤️🐰

기상청 단기예보 API로 판교 날씨를 조회하여
카카오톡 '나에게 보내기'로 전송합니다.

사용법:
  # 내일 날씨 카톡 전송 (기본)
  python3 pangyo_weather_kakao.py

  # 오늘 날씨 카톡 전송
  python3 pangyo_weather_kakao.py --today

  # 카톡 없이 콘솔만 출력
  python3 pangyo_weather_kakao.py --dry-run

  # GitHub Actions 환경변수로 실행
  KMA_API_KEY=xxx KAKAO_ACCESS_TOKEN=xxx KAKAO_REFRESH_TOKEN=xxx \
  KAKAO_REST_API_KEY=xxx python3 pangyo_weather_kakao.py

설정:
  1. 공공데이터포털(data.go.kr) 가입 → '기상청_단기예보 조회서비스' 활용신청
  2. 카카오 개발자(developers.kakao.com) 앱 생성 → REST API 키 발급
  3. 환경변수 또는 CONFIG에 키 입력
"""

import json
import math
import os
import sys
import subprocess
import argparse
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests 라이브러리가 필요합니다.")
    print("  pip install requests")
    sys.exit(1)

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# ============================================================
# 설정 (CONFIG) - 환경변수 우선, 없으면 기본값 사용
# ============================================================

CONFIG = {
    # 공공데이터포털 API 키 (Decoding 버전)
    "KMA_API_KEY": os.environ.get("KMA_API_KEY", "YOUR_API_KEY_HERE"),

    # 카카오톡 인증 (GitHub Actions에서는 환경변수 사용)
    "KAKAO_ACCESS_TOKEN": os.environ.get("KAKAO_ACCESS_TOKEN", ""),
    "KAKAO_REFRESH_TOKEN": os.environ.get("KAKAO_REFRESH_TOKEN", ""),
    "KAKAO_REST_API_KEY": os.environ.get("KAKAO_REST_API_KEY", ""),
    "KAKAO_CLIENT_SECRET": os.environ.get("KAKAO_CLIENT_SECRET", ""),

    # Claude API (브리핑 생성용)
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),

    # 카카오톡 send_message.py 경로 (로컬 실행용 폴백)
    "KAKAO_SCRIPT": os.environ.get("KAKAO_SCRIPT", "/mnt/skills/user/kakaotalk/scripts/send_message.py"),

    # 메시지 프리픽스
    "PREFIX": os.environ.get("KAKAO_PREFIX", "🐰🔔"),

    # 판교 좌표 (기상청 격자: nx=62, ny=123)
    "NX": 62,
    "NY": 123,
    "LOCATION_NAME": "판교",
}


# ============================================================
# 기상청 격자 좌표 변환 (위경도 → 격자)
# ============================================================

def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도 좌표를 기상청 격자 좌표로 변환합니다.

    판교 기본값: lat=37.3947, lon=127.1112 → nx=62, ny=123
    """
    RE = 6371.00877    # 지구 반경(km)
    GRID = 5.0         # 격자 간격(km)
    SLAT1 = 30.0       # 투영 위도1(degree)
    SLAT2 = 60.0       # 투영 위도2(degree)
    OLON = 126.0       # 기준점 경도(degree)
    OLAT = 38.0        # 기준점 위도(degree)
    XO = 43            # 기준점 X좌표(GRID)
    YO = 136           # 기준점 Y좌표(GRID)

    DEGRAD = math.pi / 180.0
    re = RE / GRID
    slat1 = SLAT1 * DEGRAD
    slat2 = SLAT2 * DEGRAD
    olon = OLON * DEGRAD
    olat = OLAT * DEGRAD

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    x = int(ra * math.sin(theta) + XO + 0.5)
    y = int(ro - ra * math.cos(theta) + YO + 0.5)
    return x, y


# ============================================================
# 기상청 단기예보 API 호출
# ============================================================

def get_base_datetime(target_date: str) -> tuple[str, str]:
    """단기예보 API의 base_date, base_time을 결정합니다.

    단기예보 발표시각: 0200, 0500, 0800, 1100, 1400, 1700, 2000, 2300
    내일 날씨를 조회하려면 오늘 23시 또는 내일 02시 발표 데이터를 사용합니다.
    """
    now = datetime.now()
    today_str = now.strftime("%Y%m%d")

    # 발표 시각 목록 (단기예보)
    base_times = ["2300", "2000", "1700", "1400", "1100", "0800", "0500", "0200"]

    if target_date == today_str:
        # 오늘 날씨: 현재 시각 기준 가장 최근 발표 시각
        current_time = now.strftime("%H%M")
        for bt in base_times:
            if current_time >= bt:
                return today_str, bt
        # 자정~02시 사이: 전날 23시 발표 사용
        yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        return yesterday, "2300"
    else:
        # 내일 이후: 가장 최근 발표 데이터 사용
        current_time = now.strftime("%H%M")
        for bt in base_times:
            if current_time >= bt:
                return today_str, bt
        yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        return yesterday, "2300"


def fetch_kma_forecast(target_date: str, nx: int, ny: int, api_key: str) -> list[dict]:
    """기상청 단기예보 API를 호출합니다."""

    base_date, base_time = get_base_datetime(target_date)

    url = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
    params = {
        "serviceKey": api_key,
        "pageNo": 1,
        "numOfRows": 1000,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny,
    }

    print(f"📡 기상청 API 호출 중... (base: {base_date} {base_time}, 격자: {nx},{ny})")

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()

    data = resp.json()
    header = data.get("response", {}).get("header", {})

    if header.get("resultCode") != "00":
        raise Exception(f"기상청 API 오류: {header.get('resultMsg', 'UNKNOWN')}")

    items = data["response"]["body"]["items"]["item"]

    # target_date에 해당하는 데이터만 필터
    return [item for item in items if item["fcstDate"] == target_date]


# ============================================================
# 날씨 데이터 파싱 및 메시지 생성
# ============================================================

# 하늘 상태 코드
SKY_MAP = {
    "1": "맑음 ☀️",
    "3": "구름많음 ⛅",
    "4": "흐림 ☁️",
}

# 강수 형태 코드
PTY_MAP = {
    "0": "없음",
    "1": "비 🌧️",
    "2": "비/눈 🌨️",
    "3": "눈 ❄️",
    "4": "소나기 🌦️",
    "5": "빗방울 💧",
    "6": "빗방울/눈날림 🌨️",
    "7": "눈날림 🌬️❄️",
}

# 요일 이름
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def parse_forecast(items: list[dict], target_date: str) -> dict:
    """API 응답 아이템들을 시간대별로 정리합니다."""

    hourly = {}  # {시간: {카테고리: 값}}

    for item in items:
        time = item["fcstTime"]
        cat = item["category"]
        val = item["fcstValue"]

        if time not in hourly:
            hourly[time] = {}
        hourly[time][cat] = val

    # 일 요약 추출
    temps = []
    rain_hours = []
    snow_hours = []
    sky_values = []

    for time, data in sorted(hourly.items()):
        # 기온
        if "TMP" in data:
            try:
                temps.append(float(data["TMP"]))
            except ValueError:
                pass

        # 강수 형태
        pty = data.get("PTY", "0")
        if pty != "0":
            hour_str = f"{int(time[:2])}시"
            rain_hours.append((hour_str, PTY_MAP.get(pty, pty)))

        # 하늘 상태
        if "SKY" in data:
            sky_values.append(data["SKY"])

        # 적설
        sno = data.get("SNO", "적설없음")
        if sno not in ("적설없음", "0"):
            snow_hours.append((f"{int(time[:2])}시", sno))

    # 최저/최고 기온 (TMN, TMX)
    tmn = None
    tmx = None
    for time, data in hourly.items():
        if "TMN" in data:
            try:
                tmn = float(data["TMN"])
            except ValueError:
                pass
        if "TMX" in data:
            try:
                tmx = float(data["TMX"])
            except ValueError:
                pass

    # TMN/TMX가 없으면 시간별 기온에서 추출
    if tmn is None and temps:
        tmn = min(temps)
    if tmx is None and temps:
        tmx = max(temps)

    # 대표 하늘 상태 (가장 많은 값)
    if sky_values:
        from collections import Counter
        most_common_sky = Counter(sky_values).most_common(1)[0][0]
    else:
        most_common_sky = "1"

    return {
        "date": target_date,
        "tmn": tmn,
        "tmx": tmx,
        "temps": temps,
        "sky": most_common_sky,
        "sky_text": SKY_MAP.get(most_common_sky, "알 수 없음"),
        "rain_hours": rain_hours,
        "snow_hours": snow_hours,
        "hourly": hourly,
    }


def build_message_simple(forecast: dict, location: str, label: str) -> str:
    """폴백용 간단한 메시지 생성 (Claude API 없을 때)."""

    d = forecast["date"]
    month = int(d[4:6])
    day = int(d[6:8])
    dt = datetime(int(d[:4]), month, day)
    weekday = WEEKDAYS[dt.weekday()]

    lines = []
    lines.append(f"{label}({month}/{day} {weekday}) {location} 날씨 {forecast['sky_text']}")
    lines.append("")

    if forecast["tmn"] is not None and forecast["tmx"] is not None:
        lines.append(f"▸ 아침 {forecast['tmn']:.0f}°C, 낮 최고 {forecast['tmx']:.0f}°C")

    lines.append(f"▸ {forecast['sky_text']}")

    if forecast["rain_hours"]:
        hours = [h for h, _ in forecast["rain_hours"]]
        lines.append(f"▸ 강수 예상: {', '.join(hours)}")
    else:
        lines.append("▸ 강수 예상 없음")

    if forecast["snow_hours"]:
        for hour, sno in forecast["snow_hours"]:
            lines.append(f"▸ 적설 예상: {hour} {sno}")

    key_hours = ["0600", "0900", "1200", "1500", "1800", "2100"]
    temp_summary = []
    for h in key_hours:
        if h in forecast["hourly"] and "TMP" in forecast["hourly"][h]:
            temp_summary.append(f"{int(h[:2])}시 {forecast['hourly'][h]['TMP']}°")
    if temp_summary:
        lines.append("")
        lines.append("🕐 " + " → ".join(temp_summary))

    lines.append("")
    if forecast["rain_hours"]:
        lines.append("👉 우산 챙기세요!")
    elif forecast["tmn"] is not None and forecast["tmn"] <= 0:
        lines.append("👉 빙판길 조심! 따뜻하게 입으세요 🧥")
    elif forecast["tmx"] is not None and forecast["tmx"] >= 30:
        lines.append("👉 더위 조심! 수분 보충 잊지 마세요 💧")
    else:
        lines.append("👉 좋은 하루 보내세요!")

    return "\n".join(lines)


def build_message_claude(forecast: dict, location: str, label: str) -> str | None:
    """Claude API로 자연스러운 날씨 브리핑을 생성합니다."""

    api_key = CONFIG["ANTHROPIC_API_KEY"]
    if not api_key or not HAS_ANTHROPIC:
        return None

    d = forecast["date"]
    month = int(d[4:6])
    day = int(d[6:8])
    dt = datetime(int(d[:4]), month, day)
    weekday = WEEKDAYS[dt.weekday()]

    # 시간대별 상세 데이터 구성
    hourly_summary = []
    for time, data in sorted(forecast["hourly"].items()):
        hour = int(time[:2])
        tmp = data.get("TMP", "?")
        sky = SKY_MAP.get(data.get("SKY", "1"), "알 수 없음")
        pty = PTY_MAP.get(data.get("PTY", "0"), "없음")
        sno = data.get("SNO", "적설없음")
        pop = data.get("POP", "0")  # 강수확률
        reh = data.get("REH", "?")  # 습도
        wsd = data.get("WSD", "?")  # 풍속
        entry = f"{hour}시: {tmp}°C, {sky}, 강수형태={pty}, 강수확률={pop}%, 습도={reh}%, 풍속={wsd}m/s"
        if sno not in ("적설없음", "0"):
            entry += f", 적설={sno}"
        hourly_summary.append(entry)

    weather_data = f"""날짜: {month}/{day} ({weekday})
지역: {location}
최저기온: {forecast['tmn']:.0f}°C
최고기온: {forecast['tmx']:.0f}°C
대표 하늘: {forecast['sky_text']}
강수 시간: {forecast['rain_hours'] if forecast['rain_hours'] else '없음'}
적설 시간: {forecast['snow_hours'] if forecast['snow_hours'] else '없음'}

시간대별:
{chr(10).join(hourly_summary)}"""

    prompt = f"""아래 기상청 데이터를 바탕으로 카카오톡 날씨 브리핑 메시지를 작성해줘.

{weather_data}

규칙:
- 첫 줄: "{label}({month}/{day} {weekday}) {location} 날씨 [대표 날씨 이모지]" (이모지 1개만)
- 빈 줄 후 ▸ 로 시작하는 3~5줄의 자연스러운 한국어 브리핑
- 하늘 변화 흐름을 자연스럽게 서술 (예: "아침엔 흐리다가 오후부터 맑아짐")
- 기온, 강수, 바람 등 핵심만 간결하게
- 마지막에 빈 줄 + 👉 로 시작하는 한줄 팁 (출근/퇴근 맥락, 친근한 톤)
- 프리픽스나 이모지 남발 금지, 깔끔하게
- 메시지 본문만 출력, 다른 설명 없이"""

    try:
        print("🤖 Claude API로 브리핑 생성 중...")
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        message = response.content[0].text.strip()
        print("✅ 브리핑 생성 완료")
        return message

    except Exception as e:
        print(f"⚠️  Claude API 실패, 기본 메시지로 대체: {e}")
        return None


def build_message(forecast: dict, location: str, label: str = "오늘") -> str:
    """메시지 생성 (Claude API 우선, 실패 시 기본 메시지)."""

    message = build_message_claude(forecast, location, label)
    if message:
        return message
    return build_message_simple(forecast, location, label)


# ============================================================
# OpenWeatherMap 폴백 (기상청 API 키가 없을 때)
# ============================================================

def fetch_openweathermap(target_date: str) -> dict | None:
    """OpenWeatherMap API를 사용한 폴백 (API 키 불필요, 제한적)
    웹 검색 결과 기반으로 메시지를 직접 구성하는 대안.
    """
    # 이 함수는 기상청 API 키가 없을 때의 안내용
    return None


# ============================================================
# 카카오톡 토큰 갱신
# ============================================================

def refresh_kakao_token(refresh_token: str, rest_api_key: str, client_secret: str = "") -> str | None:
    """카카오 refresh_token으로 access_token을 갱신합니다."""

    url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret

    try:
        resp = requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        result = resp.json()

        new_access_token = result.get("access_token")
        new_refresh_token = result.get("refresh_token")

        if new_access_token:
            print("🔄 카카오 access_token 갱신 완료")
            # refresh_token도 갱신된 경우 (만료 1개월 이내일 때)
            if new_refresh_token:
                print("🔄 카카오 refresh_token도 함께 갱신됨")
                # GitHub Actions에서는 수동으로 Secret 업데이트 필요
                print(f"⚠️  새 refresh_token을 GitHub Secrets에 업데이트하세요")
            return new_access_token

    except Exception as e:
        print(f"❌ 토큰 갱신 실패: {e}")

    return None


# ============================================================
# 카카오톡 전송
# ============================================================

def send_kakao_api(message: str, prefix: str, access_token: str) -> bool:
    """카카오톡 REST API로 '나에게 보내기' 메시지를 전송합니다."""

    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    full_message = f"{prefix} {message}" if prefix else message

    template = {
        "object_type": "text",
        "text": full_message,
        "link": {
            "web_url": "https://weather.naver.com",
            "mobile_web_url": "https://weather.naver.com",
        },
    }

    data = {"template_object": json.dumps(template)}

    print(f"\n📤 카카오톡 API로 전송 중...")
    try:
        resp = requests.post(url, headers=headers, data=data, timeout=10)

        if resp.status_code == 401:
            print("⚠️  access_token 만료 - 갱신 시도 중...")
            return False  # 호출부에서 토큰 갱신 후 재시도

        resp.raise_for_status()
        print("✅ 카카오톡 전송 성공!")
        return True

    except requests.exceptions.RequestException as e:
        print(f"❌ 카카오톡 API 전송 실패: {e}")
        return False


def send_kakao_script(message: str, prefix: str, script_path: str) -> bool:
    """카카오톡 send_message.py를 통해 메시지를 전송합니다 (로컬 폴백)."""

    cmd = [
        sys.executable, script_path,
        message,
        "--prefix", prefix,
    ]

    print(f"\n📤 카카오톡 전송 중 (스크립트)...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        print(result.stdout.strip())
        return True
    else:
        print(f"❌ 전송 실패: {result.stderr}")
        return False


def send_kakao(message: str, prefix: str, script_path: str) -> bool:
    """카카오톡 전송 (API 우선, 실패 시 스크립트 폴백)."""

    access_token = CONFIG["KAKAO_ACCESS_TOKEN"]
    refresh_token = CONFIG["KAKAO_REFRESH_TOKEN"]
    rest_api_key = CONFIG["KAKAO_REST_API_KEY"]
    client_secret = CONFIG["KAKAO_CLIENT_SECRET"]

    # 1) access_token이 있으면 API 직접 호출
    if access_token:
        success = send_kakao_api(message, prefix, access_token)

        if not success and refresh_token and rest_api_key:
            # 토큰 갱신 후 재시도
            new_token = refresh_kakao_token(refresh_token, rest_api_key, client_secret)
            if new_token:
                success = send_kakao_api(message, prefix, new_token)

        if success:
            return True

        print("⚠️  API 전송 실패, 스크립트 폴백 시도...")

    # 2) 스크립트 폴백 (로컬 환경)
    if Path(script_path).exists():
        return send_kakao_script(message, prefix, script_path)

    # 3) 둘 다 실패
    if not access_token:
        print("\n❌ 카카오톡 전송 수단이 없습니다.")
        print("   환경변수 KAKAO_ACCESS_TOKEN을 설정하거나")
        print("   --kakao-script로 스크립트 경로를 지정하세요.")
    return False


# ============================================================
# 메인
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="판교 날씨를 조회하여 카카오톡으로 전송합니다 🌤️",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python3 pangyo_weather_kakao.py              # 내일 날씨 카톡 전송
  python3 pangyo_weather_kakao.py --today      # 오늘 날씨 카톡 전송
  python3 pangyo_weather_kakao.py --dry-run    # 콘솔만 출력 (카톡 X)
  python3 pangyo_weather_kakao.py --api-key YOUR_KEY  # API 키 직접 지정

cron 등록:
  0 7 * * * /usr/bin/python3 /path/to/pangyo_weather_kakao.py
        """,
    )
    parser.add_argument("--tomorrow", action="store_true", help="내일 날씨 조회 (기본: 오늘)")
    parser.add_argument("--dry-run", action="store_true", help="카카오톡 전송 없이 콘솔 출력만")
    parser.add_argument("--api-key", help="공공데이터포털 API 키 (Decoding 버전)")
    parser.add_argument("--nx", type=int, default=CONFIG["NX"], help="기상청 격자 X좌표 (기본: 62 판교)")
    parser.add_argument("--ny", type=int, default=CONFIG["NY"], help="기상청 격자 Y좌표 (기본: 123 판교)")
    parser.add_argument("--location", default=CONFIG["LOCATION_NAME"], help="지역 이름 (기본: 판교)")
    parser.add_argument("--prefix", default=CONFIG["PREFIX"], help="카카오톡 프리픽스 (기본: 🐰🔔)")
    parser.add_argument("--kakao-script", default=CONFIG["KAKAO_SCRIPT"], help="send_message.py 경로")

    args = parser.parse_args()

    # 날짜 결정
    now = datetime.now()
    if args.tomorrow:
        target = now + timedelta(days=1)
        label = "내일"
    else:
        target = now
        label = "오늘"

    target_date = target.strftime("%Y%m%d")
    print(f"🗓️  {label} ({target.strftime('%Y-%m-%d')}) {args.location} 날씨 조회")
    print(f"   격자 좌표: nx={args.nx}, ny={args.ny}")
    print()

    # API 키 결정
    api_key = args.api_key or CONFIG["KMA_API_KEY"]

    if api_key == "YOUR_API_KEY_HERE":
        print("=" * 60)
        print("⚠️  기상청 API 키가 설정되지 않았습니다!")
        print()
        print("설정 방법:")
        print("  1. https://www.data.go.kr 가입")
        print("  2. '기상청_단기예보 조회서비스' 활용신청")
        print("  3. 마이페이지 → 일반 인증키(Decoding) 복사")
        print("  4. 아래 방법 중 하나로 입력:")
        print()
        print("  방법 A) 스크립트 CONFIG에 직접 입력")
        print('    CONFIG["KMA_API_KEY"] = "발급받은키"')
        print()
        print("  방법 B) 실행 시 인자로 전달")
        print("    python3 pangyo_weather_kakao.py --api-key 발급받은키")
        print()
        print("  방법 C) 환경변수 사용")
        print("    export KMA_API_KEY=발급받은키")
        print("=" * 60)

        # 환경변수 폴백
        import os
        api_key = os.environ.get("KMA_API_KEY")
        if not api_key:
            sys.exit(1)

    # 날씨 조회
    try:
        items = fetch_kma_forecast(target_date, args.nx, args.ny, api_key)

        if not items:
            print(f"⚠️  {target_date}에 대한 예보 데이터가 없습니다.")
            print("   발표 시각에 따라 아직 데이터가 없을 수 있습니다.")
            sys.exit(1)

        forecast = parse_forecast(items, target_date)
        message = build_message(forecast, args.location, label)

    except requests.exceptions.RequestException as e:
        print(f"❌ API 호출 실패: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        sys.exit(1)

    # 결과 출력
    print("\n" + "=" * 50)
    print(message)
    print("=" * 50)

    # 카카오톡 전송
    if args.dry_run:
        print("\n🔕 --dry-run 모드: 카카오톡 전송을 건너뜁니다.")
    else:
        success = send_kakao(message, args.prefix, args.kakao_script)
        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
