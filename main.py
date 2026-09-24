import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import yfinance as yf
from FinMind.data import DataLoader  # 修正：更新為正確的 DataLoader 載入路徑

# ================= 參數設定 =================
STOCK_LIST = ["2330", "2454", "2303", "2317", "3037"]  # 監控股票清單
DAYS_WINDOW = 30        # 觀測天數 window
MIN_BUY_DAYS = 20       # 最少買超天數門檻
MAX_AMPLITUDE = 20.0    # 振幅門檻上限 (%)

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

def get_institutional_data(stock_id):
    """取得 FinMind 法人買超資料"""
    try:
        DL = DataLoader()
        if FINMIND_TOKEN:
            DL.login_by_token(api_token=FINMIND_TOKEN)
        
        # 抓取近 60 天資料以確保扣除假日後仍有 30 個交易日
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
        df = DL.taiwan_stock_institutional_investors_buy_sell(
            stock_id=stock_id,
            start_date=start_date
        )
        if df.empty:
            return 0, 0
        
        # 加總三大法人每日買賣超
        df_grouped = df.groupby(['date', 'stock_id'])['buy_sell'].sum().reset_index()
        recent_df = df_grouped.tail(DAYS_WINDOW)
        
        buy_days = (recent_df['buy_sell'] > 0).sum()
        total_days = len(recent_df)
        return buy_days, total_days
    except Exception as e:
        print(f"FinMind 讀取 {stock_id} 失敗: {e}")
        return 0, 0

def get_stock_amplitude(stock_id):
    """取得 yfinance 近 30 個交易日最高低點振幅 (%)"""
    try:
        ticker = f"{stock_id}.TW"
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2m") # 抓取 2 個月資料
        if hist.empty or len(hist) < DAYS_WINDOW:
            ticker = f"{stock_id}.TWO" # 嘗試櫃買中心
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
    except Exception as e:
        print(f"yfinance 讀取 {stock_id} 失敗: {e}")
        return 0.0

def send_email(subject, body):
    """發送 Gmail SMTP 戰報"""
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
        print("✅ 每日戰報 Email 已成功寄出！")
    except Exception as e:
        print(f"❌ Email 發送失敗，錯誤訊息: {e}")

def main():
    report_lines = []
    matched_stocks = []
    
    print("🚀 開始執行台股籌碼掃描...")
    
    for stock_id in STOCK_LIST:
        buy_days, total_days = get_institutional_data(stock_id)
        amplitude = get_stock_amplitude(stock_id)
        
        # 判斷門檻：買超天數 >= 20 且 振幅 <= 20%
        is_buy_pass = buy_days >= MIN_BUY_DAYS
        is_amp_pass = amplitude <= MAX_AMPLITUDE and amplitude > 0
        
        status_str = ""
        if is_buy_pass and is_amp_pass:
            status_str = "🔥 符合潛伏吸籌雙門檻！"
            matched_stocks.append(stock_id)
        else:
            status_str = "籌碼沉澱中（未達雙門檻）"
            
        line = f"・[{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}% | 狀態: {status_str}"
        report_lines.append(line)
        print(line)

    # 組裝 Email 內文
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
    subject = f"【台股籌碼每日戰報】{today_str} - 符合目標: {len(matched_stocks)} 檔"
    
    body = f"📊 台股靜默籌碼收集掃描報告 ({today_str})\n"
    body += f"篩選標準：近 {DAYS_WINDOW} 日法人買超 ≥ {MIN_BUY_DAYS} 天，且價格振幅 ≤ {MAX_AMPLITUDE}%\n"
    body += "--------------------------------------------------\n\n"
    body += "\n".join(report_lines)
    body += "\n\n--------------------------------------------------\n"
    body += "🤖 本郵件由 GitHub Actions 自動系統發送（每日心跳報告）。"

    send_email(subject, body)

if __name__ == "__main__":
    main()
