import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText
import numpy as np
import pandas as pd
from FinMind.data import DataLoader
import yfinance as yf

# ==========================================
# ⚙️ 1. 從環境變數讀取安全設定
# ==========================================
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")

# 監控股票清單 (台股代碼，不加 .TW)
STOCK_LIST = ["2330", "2454", "2303", "2317", "3037"]

# 策略參數
WINDOW_DAYS = 30  # 觀察 30 個交易日
MIN_BUY_DAYS = 20  # 30 天內要有 20 天法人/主力淨買超
MAX_AMPLITUDE_PCT = 10.0  # 30 天內最高低價總振幅上限 (壓盤/死沉條件 %)


# ==========================================
# 📊 2. 籌碼與股價核心邏輯
# ==========================================
def fetch_data_and_analyze(stock_id):
    dl = DataLoader()
    if FINMIND_TOKEN:
        dl.login_by_token(api_token=FINMIND_TOKEN)

    # 抓取近 80 天資料以確保包含 30 個交易日
    start_date = (
        pd.Timestamp.now() - pd.Timedelta(days=80)
    ).strftime("%Y-%m-%d")

    # 新版 FinMind API 抓取三大法人
    chip_df = dl.taiwan_stock_institutional_investors(
        stock_id=stock_id, start_date=start_date
    )

    if chip_df.empty:
        print(f"[{stock_id}] 查無籌碼資料，跳過。")
        return None

    # 計算每日淨買超
    chip_df["net_buy"] = chip_df["buy"] - chip_df["sell"]
    daily_chip = chip_df.groupby("date")["net_buy"].sum()
    recent_chip = daily_chip.tail(WINDOW_DAYS)

    if len(recent_chip) < WINDOW_DAYS:
        print(f"[{stock_id}] 籌碼交易日數不足 {WINDOW_DAYS} 天，跳過。")
        return None

    buy_days_count = (recent_chip > 0).sum()

    # 取得股價振幅資料
    ticker_symbol = f"{stock_id}.TW"
    price_df = yf.download(
        ticker_symbol, period="3mo", interval="1d", progress=False
    )

    if price_df.empty or len(price_df) < WINDOW_DAYS:
        print(f"[{stock_id}] 股價資料不足，跳過。")
        return None

    recent_price = price_df.tail(WINDOW_DAYS)

    period_high = float(recent_price["High"].max())
    period_low = float(recent_price["Low"].min())
    latest_close = float(recent_price["Close"].iloc[-1])

    amplitude_pct = ((period_high - period_low) / period_low) * 100

    has_quiet_accumulation = buy_days_count >= MIN_BUY_DAYS
    is_low_volatility = amplitude_pct <= MAX_AMPLITUDE_PCT
    is_breakout = latest_close >= (period_high * 0.98)

    print(
        f"[{stock_id}] 買超天數: {buy_days_count}/{WINDOW_DAYS} | 30天振幅: {amplitude_pct:.2f}%"
    )

    # 判斷狀態
    if has_quiet_accumulation and is_low_volatility:
        status = "🚀 帶量突破中" if is_breakout else "🎯 完美符合（吸籌+壓盤）"
        is_hit = True
    else:
        status = "⏳ 籌碼沉澱中（未達雙門檻）"
        is_hit = False

    return {
        "stock_id": stock_id,
        "buy_days": buy_days_count,
        "amplitude": amplitude_pct,
        "latest_close": latest_close,
        "period_high": period_high,
        "period_low": period_low,
        "status": status,
        "is_hit": is_hit,
    }


# ==========================================
# ✉️ 3. Email 自動通知功能 (改為每日固定回報)
# ==========================================
def send_email_alert(all_results):
    hit_stocks = [s for s in all_results if s["is_hit"]]

    today_str = pd.Timestamp.now().strftime("%Y-%m-%d")

    # 根據是否有抓到符合條件的標的，調整主旨
    if hit_stocks:
        subject = f"🎯 【籌碼黑馬警報】{today_str} 抓到 {len(hit_stocks)} 檔暗中吸籌+壓盤股！"
    else:
        subject = f"📊 【每日籌碼戰報】{today_str} 今日無精準符合標的（系統運作正常）"

    content = f"親愛的投資人：\n\n以下是 {today_str} 盤後『30天內20天買超 + 股價狹窄橫盤』策略的監控報告：\n\n"

    if hit_stocks:
        content += "🔥 【符合吸籌+壓盤條件之精選標的】\n"
        content += "=" * 60 + "\n"
        for stock in hit_stocks:
            content += f"📌 股票代號：{stock['stock_id']}\n"
            content += f"   - 狀態評估：{stock['status']}\n"
            content += f"   - 30天法人買超天數：{stock['buy_days']} / {WINDOW_DAYS} 天\n"
            content += f"   - 30天極限振幅：{stock['amplitude']:.2f}%\n"
            content += f"   - 最新收盤價：{stock['latest_close']:.2f} (30天區間: {stock['period_low']:.2f} ~ {stock['period_high']:.2f})\n"
            content += "-" * 60 + "\n"
        content += "\n"

    content += "📋 【全清單即時籌碼進度追蹤】\n"
    content += "=" * 60 + "\n"
    for stock in all_results:
        content += f"・[{stock['stock_id']}] 買超天數: {stock['buy_days']}/{WINDOW_DAYS} 天 | 振幅: {stock['amplitude']:.2f}% | 狀態: {stock['status']}\n"

    content += "\n" + "=" * 60 + "\n"
    content += (
        "💡 備註：若無顯示精選標的，代表今日清單內個股籌碼洗牌或波動尚未完全收斂至 10% 內。"
    )

    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECEIVER_EMAIL], msg.as_string())
        server.quit()
        print("✅ 每日戰報 Email 已成功寄出！")
    except Exception as e:
        print(f"❌ Email 發送失敗，錯誤訊息: {e}")


# ==========================================
# 🚀 4. 主程式入口
# ==========================================
if __name__ == "__main__":
    print("🔍 開始執行『籌碼暗中吸貨 + 壓盤橫盤』掃描程式...")

    all_results = []
    for stock in STOCK_LIST:
        result = fetch_data_and_analyze(stock)
        if result:
            all_results.append(result)

    if all_results:
        send_email_alert(all_results)
    else:
        print("⚠️ 查無任何股票資料，無法組成報告。")
