import os
import smtplib
import time
import random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import yfinance as yf
from FinMind.data import DataLoader

# ================= 策略與系統參數設定 =================
DAYS_WINDOW = 30        # 觀測天數 window
MIN_BUY_DAYS = 20       # 最少買超天數門檻
MAX_AMPLITUDE = 20.0    # 振幅門檻上限 (%)

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

def get_all_taiwan_stock_ids(dl):
    """動態取得全台股上市與上櫃普通股清單"""
    print("🔍 正在取得全台股上市櫃股票清單...")
    try:
        df = dl.taiwan_stock_info()
        # 篩選上市 (twse) 與上櫃 (tpex) 的股票
        stocks = df[df['type'].isin(['twse', 'tpex'])]['stock_id'].tolist()
        # 僅保留純 4 碼數字之普通股代號
        valid_stocks = [s for s in stocks if s.isdigit() and len(s) == 4]
        print(f"✅ 成功獲取 {len(valid_stocks)} 檔個股代號！")
        return valid_stocks
    except Exception as e:
        print(f"⚠️ 取得全台股清單失敗，錯誤: {e}，改用預設熱門股清單。")
        return ["2330", "2454", "2303", "2317", "3037", "2382", "3231", "6669"]

def get_institutional_data(dl, stock_id):
    """取得 FinMind 法人買超資料並計算近 30 交易日買超天數"""
    try:
        # 抓取近 60 日資料以確保包含 30 個完整交易日
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
        df = dl.taiwan_stock_institutional_investors_buy_sell(
            stock_id=stock_id,
            start_date=start_date
        )
        if df is None or df.empty:
            return 0, 0
        
        # 依日期與股票代號加總三大法人買賣超
        df_grouped = df.groupby(['date', 'stock_id'])['buy_sell'].sum().reset_index()
        recent_df = df_grouped.tail(DAYS_WINDOW)
        
        buy_days = (recent_df['buy_sell'] > 0).sum()
        total_days = len(recent_df)
        return buy_days, total_days
    except Exception as e:
        return 0, 0

def get_stock_amplitude(stock_id):
    """取得股票近 30 個交易日最高低點振幅 (%)"""
    try:
        ticker = f"{stock_id}.TW"
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2m")
        if hist.empty or len(hist) < DAYS_WINDOW:
            ticker = f"{stock_id}.TWO"
            stock = yf.Ticker(ticker)
            hist = stock.history(period="2m")
            
        recent_hist = hist.tail(DAYS_WINDOW)
        if recent_hist.empty:
            return 0.0
        
        highest = recent_hist['High'].max()
        lowest = recent_hist['Low'].min()
        
        if lowest == 0 or pd.isna(lowest):
            return 0.0
            
        amplitude = ((highest - lowest) / lowest) * 100
        return round(amplitude, 2)
    except Exception:
        return 0.0

def send_email(subject, body):
    """發送 Gmail SMTP 每日戰報"""
    if not SENDER_EMAIL or not SENDER_PASSWORD or not RECEIVER_EMAIL:
        print("❌ 缺少 Email 設定變數，取消寄送。")
        return
        
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("✅ 全市場每日戰報 Email 已成功寄出！")
    except Exception as e:
        print(f"❌ Email 發送失敗，錯誤訊息: {e}")

def main():
    print("🚀 啟動台股全市場籌碼收集掃描引擎...")
    
    dl = DataLoader()
    if FINMIND_TOKEN:
        dl.login_by_token(api_token=FINMIND_TOKEN)
        
    stock_list = get_all_taiwan_stock_ids(dl)
    
    matched_results = []
    scanned_count = 0
    error_count = 0

    total_stocks = len(stock_list)
    print(f"📡 開始進行 {total_stocks} 檔個股之籌碼與振幅比對...")

    for idx, stock_id in enumerate(stock_list, 1):
        try:
            buy_days, total_days = get_institutional_data(dl, stock_id)
            amplitude = get_stock_amplitude(stock_id)
            scanned_count += 1

            # 雙門檻判斷：買超天數 >= 20 且 0 < 振幅 <= 20%
            if buy_days >= MIN_BUY_DAYS and 0 < amplitude <= MAX_AMPLITUDE:
                result_str = f"🔥 [{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}%"
                matched_results.append(result_str)
                print(f"[{idx}/{total_stocks}] {result_str}")
            
            # 每處理 10 檔印出進度
            if idx % 50 == 0:
                print(f"⏳ 掃描進度: {idx}/{total_stocks} ({(idx/total_stocks)*100:.1f}%) | 當前獲選: {len(matched_results)} 檔")

            # 加入微幅隨機延遲 (0.1 ~ 0.3 秒)，避免觸發伺服器流量限制
            time.sleep(random.uniform(0.1, 0.3))

        except Exception as e:
            error_count += 1
            continue

    # ================= 郵件內容組裝 =================
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
    subject = f"【全台股籌碼戰報】{today_str} - 精選出 {len(matched_results)} 檔強勢沉澱股"
    
    body = f"📊 全台股靜默籌碼收集掃描報告 ({today_str})\n"
    body += f"篩選標準：近 {DAYS_WINDOW} 交易日法人買超 ≥ {MIN_BUY_DAYS} 天，且價格振幅 ≤ {MAX_AMPLITUDE}%\n"
    body += f"掃描範圍：台股上市及上櫃全市場（共完成 {scanned_count} 檔對比）\n"
    body += "==================================================\n\n"
    
    if matched_results:
        body += f"🎯 🔥 今日符合「潛伏吸籌雙門檻」之精選標的 ({len(matched_results)} 檔)：\n\n"
        body += "\n".join(matched_results)
    else:
        body += "👻 今日全市場無任何個股同時符合雙門檻條件（市場可能處於高波動或籌碼發散狀態）。\n"
        
    body += "\n\n==================================================\n"
    body += f"📈 系統執行摘要：\n"
    body += f"- 總掃描檔數: {scanned_count} 檔\n"
    body += f"- 符合條件數: {len(matched_results)} 檔\n"
    body += f"- 異常跳過數: {error_count} 檔\n"
    body += "🤖 本郵件由 GitHub Actions 每日盤後自動掃描引擎發送。"

    send_email(subject, body)

if __name__ == "__main__":
    main()
