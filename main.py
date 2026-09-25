from datetime import datetime, timezone, timedelta
import json
import os
import re
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET_URL = "https://ticketdive.com/artist/LNsAgWW9j47MTvVOZbgz"
DATA_FILE = "seen_events.json"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_GROUP_ID = os.getenv("LINE_GROUP_ID")

JST = timezone(timedelta(hours=9))


def clean_title(raw_title):
    """タイトルから不要な表記や接尾辞を除去"""
    if not raw_title:
        return ""
    title = re.sub(r"[\r\n]+", " ", str(raw_title)).strip()
    ng_words = [
        "お気に入り", "シェア", "チケットの分配", "チケット分配",
        "販売情報", "チケット情報", "TicketDive", "ログイン",
        "マイページ", "新規会員登録", "チケット購入", "名前", "氏名"
    ]
    for ng in ng_words:
        if title == ng:
            return ""
        title = re.sub(re.escape(ng), "", title, flags=re.IGNORECASE).strip()

    title = title.split("｜")[0].split(" - ")[0].strip()
    title = re.split(r"(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])", title)[0].strip()
    return title.strip(" :：-–|/／")


def parse_datetime_str(raw_str):
    """ISO文字列等を読みやすい形式（YYYY/MM/DD HH:MM）に変換"""
    if not raw_str:
        return ""
    try:
        dt = datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
        dt_jst = dt.astimezone(JST)
        return dt_jst.strftime("%Y/%m/%d %H:%M")
    except Exception:
        # パースできない場合は文字列のまま整形
        return str(raw_str).replace("T", " ")[:16]


def extract_sales_periods_from_json(html_content):
    """Next.jsの内部JSON(__NEXT_DATA__)から販売期間情報をダイレクトパース"""
    periods = []
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if script_tag and script_tag.string:
            data = json.loads(script_tag.string)

            def search_tickets(obj):
                if isinstance(obj, dict):
                    # TicketDiveのチケットオブジェクト構造を直接参照
                    if "salesStartAt" in obj or "salesEndAt" in obj or "sales_start_at" in obj:
                        name = obj.get("name") or obj.get("title") or "チケット"
                        start = obj.get("salesStartAt") or obj.get("sales_start_at") or ""
                        end = obj.get("salesEndAt") or obj.get("sales_end_at") or ""

                        start_fmt = parse_datetime_str(start)
                        end_fmt = parse_datetime_str(end)

                        if start_fmt or end_fmt:
                            period_str = f"{name}: {start_fmt} ～ {end_fmt}".strip(" ～")
                            if period_str not in periods:
                                periods.append(period_str)

                    for v in obj.values():
                        search_tickets(v)
                elif isinstance(obj, list):
                    for item in obj:
                        search_tickets(item)

            search_tickets(data)
    except Exception as e:
        print(f"JSONパース例外: {e}")
    return periods


def fetch_event_details_with_browser(event_url):
    """PlaywrightでTicketDiveの分割要素(DOM)から販売期間を統合・抽出"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # ページ読み込み
            page.goto(event_url, wait_until="domcontentloaded", timeout=30000)

            # 動的描画（React/Next.js）の待機
            try:
                page.wait_for_selector("h1", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

            # 1. タイトルの取得
            title = "イベント名称未設定"
            try:
                h1_elem = page.query_selector("h1")
                if h1_elem:
                    title = clean_title(h1_elem.inner_text())
            except Exception:
                pass

            if not title or title == "イベント名称未設定":
                content = page.content()
                soup = BeautifulSoup(content, "html.parser")
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    title = clean_title(og_title["content"])

            sales_periods = []

            # 2. 画面上の要素からチケット枠ごとに販売期間を取得
            # 日時表記と思われるテキストが含まれる要素を探索
            body_text = page.inner_text("body")
            lines = [l.strip() for l in body_text.splitlines() if l.strip()]

            # 日時を表す行のインデックスを探す
            date_line_indices = []
            for idx, line in enumerate(lines):
                if re.search(r"\d{2,4}[/\.-]\d{1,2}[/\.-]\d{1,2}|\d{1,2}:\d{2}", line):
                    date_line_indices.append(idx)

            # 近接する日時行・チケット情報を統合して期間文字列を作成
            i = 0
            while i < len(lines):
                line = lines[i]
                # 「販売」「受付」「先着」「抽選」「～」「~」などが含まれるか検証
                if any(k in line for k in ["販売", "受付", "先着", "抽選", "チケット"]):
                    # 周辺5行のテキストを取得
                    chunk = lines[max(0, i-1):min(len(lines), i+6)]
                    chunk_text = " ".join(chunk)

                    # chunk内に日時が2つ以上（開始と終了）含まれていれば抽出
                    dates_found = re.findall(r"(\d{2,4}[/\.-]\d{1,2}[/\.-]\d{1,2}(?:\(.*?\))?\s*\d{1,2}:\d{2}|\d{1,2}月\d{1,2}日(?:\(.*?\))?\s*\d{1,2}:\d{2})", chunk_text)
                    if len(dates_found) >= 2:
                        period_candidate = f"{dates_found[0]} ～ {dates_found[1]}"
                        if period_candidate not in sales_periods:
                            sales_periods.append(period_candidate)
                    elif len(dates_found) == 1 and any(s in chunk_text for s in ["～", "~", "-"]):
                        # 1つの文脈の中に範囲指定がある場合
                        if dates_found[0] not in sales_periods:
                            sales_periods.append(chunk_text)
                i += 1

            # 3. DOMから取得できなかった場合、内部JSON(__NEXT_DATA__)を探索
            if not sales_periods:
                html_content = page.content()
                sales_periods = extract_sales_periods_from_json(html_content)

            # 4. 公演日時の抽出
            event_date = "情報なし"
            m_date = re.search(r"(\d{4}[/\.-]\d{1,2}[/\.-]\d{1,2}|\d{1,2}月\d{1,2}日)", body_text)
            if m_date:
                event_date = m_date.group(1)

            browser.close()

            # 重複の削除と整理
            clean_periods = []
            for sp in sales_periods:
                sp_clean = re.sub(r"\s+", " ", sp).strip()
                if sp_clean and sp_clean not in clean_periods and len(sp_clean) < 100:
                    clean_periods.append(sp_clean)

            if not clean_periods:
                clean_periods = ["公式ページをご確認ください"]

            return {
                "title": title if title else "イベント名称未設定",
                "event_date": event_date,
                "sales_periods": clean_periods[:3],  # 最大3枠まで通知
                "url": event_url
            }

    except Exception as e:
        print(f"ブラウザ実行エラー ({event_url}): {e}")
        return {
            "title": "イベント名称未設定",
            "event_date": "情報なし",
            "sales_periods": ["公式ページをご確認ください"],
            "url": event_url
        }


def fetch_events():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    response = requests.get(TARGET_URL, headers=headers)
    if response.status_code != 200:
        print(f"一覧取得エラー: {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    events = []

    links = soup.find_all("a", href=True)
    for link in links:
        href = link["href"]
        if "/events/" in href or "/event/" in href:
            full_url = href if href.startswith("http") else f"https://ticketdive.com{href}"
            if full_url not in [e["url"] for e in events]:
                print(f"解析中: {full_url}")
                details = fetch_event_details_with_browser(full_url)
                if details:
                    events.append(details)

    return events


def load_seen_events():
    """既読リストの安全な読み込み"""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return set()
                return set(json.loads(content))
        except (json.JSONDecodeError, Exception) as e:
            print(f"seen_events.json 読み込みスキップ: {e}")
            return set()
    return set()


def save_seen_events(seen_set):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_set), f, ensure_ascii=False, indent=2)


def send_line_message(message):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_GROUP_ID,
        "messages": [{"type": "text", "text": message}],
    }
    res = requests.post(url, headers=headers, json=payload)
    if res.status_code != 200:
        print(f"LINE送信エラー: {res.status_code}, {res.text}")
        raise Exception("LINE送信失敗")


def main():
    events = fetch_events()
    seen = load_seen_events()

    new_events = []
    for ev in events:
        if ev["url"] not in seen:
            new_events.append(ev)
            seen.add(ev["url"])

    if new_events:
        print(f"{len(new_events)} 件の新着イベントを検知しました。LINEに通知します。")

        msg_blocks = []
        for ev in new_events:
            sales_text = "\n".join(ev["sales_periods"])
            block_text = (
                f"イベント名：{ev['title']}\n"
                f"販売期間：{sales_text}\n"
                f"公演日：{ev['event_date']}\n"
                f"URL：{ev['url']}"
            )
            msg_blocks.append(block_text)

        chunk_size = 2
        for i in range(0, len(msg_blocks), chunk_size):
            chunk = msg_blocks[i : i + chunk_size]
            msg = "【Falench.ライブ情報（ダイブ）】\n\n"
            msg += "\n\n──────────────────\n\n".join(chunk)
            send_line_message(msg)

        save_seen_events(seen)
    else:
        print("新着イベントはありませんでした。")


if __name__ == "__main__":
    main()
