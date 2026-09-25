import os
import smtplib
import time
import random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import yfinance as yf
from FinMind.data import DataLoader

def fetch_data_safely(tickers, period="5y", batch_size=10, sleep_sec=1.5):
    """
    分批下載歷史資料，加入 request 間隔防止被 Yahoo 丟包卡死
    """
    all_data = pd.DataFrame()
    total_batches = (len(tickers) + batch_size - 1) // batch_size
    
    print(f"總共 {len(tickers)} 檔股票，分為 {total_batches} 批次進行下載...")

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        current_batch = i // batch_size + 1
        print(f"[{current_batch}/{total_batches}] 正在下載: {', '.join(batch)}...")
        
        success = False
        for attempt in range(3):  # 最多嘗試 3 次
            try:
                # 關鍵設定：timeout=10, threads=False
                df = yf.download(
                    tickers=batch, 
                    period=period, 
                    group_by='ticker', 
                    threads=False, 
                    timeout=10,
                    progress=False
                )
                if not df.empty:
                    # 簡單合併處理
                    all_data = pd.concat([all_data, df], axis=1)
                    success = True
                    break
            except Exception as e:
                print(f"   ⚠️ 批次下載失敗 (第 {attempt+1} 次重試): {e}")
                time.sleep(2)
        
        if not success:
            print(f"   ❌ 該批次多次失敗，已自動跳過，避免阻塞流程。")
            
        # 禮貌性停頓，避免被 Yahoo 認定為 Bot 攻擊
        time.sleep(sleep_sec)

    return all_data

# ================= 策略與系統參數設定 (優化版: 20日/12勝率) =================
DAYS_WINDOW = 20        # 觀測天數 window (約 1 個日曆月)
MIN_BUY_DAYS = 12       # 最少買超天數門檻 (勝率 >= 60%)
MAX_AMPLITUDE = 20.0    # 振幅門檻上限 (%)
NEAR_BUY_DAYS = 8       # 次級觀察：近達標買超天數門檻 (8~11天)

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

def get_all_taiwan_stock_ids(dl):
    """動態取得全台股上市與上櫃「純普通個股」清單（排除 ETF、權證、特種股）"""
    print("🔍 正在過濾並取得全台股純個股清單...")
    try:
        df = dl.taiwan_stock_info()
        df_filtered = df[df['type'].isin(['twse', 'tpex'])].copy()
        
        # 排除 ETF、存託憑證 (DR)、受益證券、權證等非普通股類別
        exclude_categories = ['ETF', '存託憑證', '受益證券', '認購權證', '認售權證']
        if 'industry_category' in df_filtered.columns:
            df_filtered = df_filtered[~df_filtered['industry_category'].isin(exclude_categories)]
            
        stocks = df_filtered['stock_id'].tolist()
        # 僅保留純 4 碼數字之普通股代號
        valid_stocks = [s for s in stocks if s.isdigit() and len(s) == 4]
        print(f"✅ 成功精準鎖定 {len(valid_stocks)} 檔台灣上市櫃純個股！")
        return valid_stocks
    except Exception as e:
        print(f"⚠️ 取得個股清單失敗，錯誤: {e}，改用預設熱門股清單。")
        return ["2330", "2454", "2303", "2317", "3037", "2382", "3231", "6669"]

def get_institutional_data(dl, stock_id):
    """取得 FinMind 法人買超資料並計算近 20 交易日買超天數"""
    try:
        # 抓取近 40 日資料以確保包含 20 個完整交易日
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=40)).strftime('%Y-%m-%d')
        df = dl.taiwan_stock_institutional_investors_buy_sell(
            stock_id=stock_id,
            start_date=start_date
        )
        if df is None or df.empty:
            return 0, 0
        
        df_grouped = df.groupby(['date', 'stock_id'])['buy_sell'].sum().reset_index()
        recent_df = df_grouped.tail(DAYS_WINDOW)
        
        buy_days = (recent_df['buy_sell'] > 0).sum()
        total_days = len(recent_df)
        return buy_days, total_days
    except Exception as e:
        return 0, 0

def get_stock_amplitude(stock_id):
    """取得股票近 20 個交易日最高低點振幅 (%)"""
    try:
        ticker = f"{stock_id}.TW"
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1m")
        if hist.empty or len(hist) < DAYS_WINDOW:
            ticker = f"{stock_id}.TWO"
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1m")
            
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
        print("✅ 全市場分層每日戰報 Email 已成功寄出！")
    except Exception as e:
        print(f"❌ Email 發送失敗，錯誤訊息: {e}")

def main():
    print("🚀 啟動台股全市場分層籌碼收集掃描引擎 (20日/12勝率優化版)...")
    
    dl = DataLoader()
    if FINMIND_TOKEN:
        dl.login_by_token(api_token=FINMIND_TOKEN)
        
    stock_list = get_all_taiwan_stock_ids(dl)
    
    # 儲存三分層結果
    perfect_matches = []  # 核心雙門檻
    high_buy_matches = [] # 備選 A：買超達標，振幅超標 (>20%)
    low_amp_matches = []  # 備選 B：低振幅 (<=20%)，買超近達標 (8~11天)
    
    scanned_count = 0
    error_count = 0

    total_stocks = len(stock_list)
    print(f"📡 開始進行 {total_stocks} 档純個股之分層籌碼與振幅比對...")

    for idx, stock_id in enumerate(stock_list, 1):
        try:
            buy_days, total_days = get_institutional_data(dl, stock_id)
            amplitude = get_stock_amplitude(stock_id)
            scanned_count += 1

            is_buy_pass = buy_days >= MIN_BUY_DAYS
            is_amp_pass = 0 < amplitude <= MAX_AMPLITUDE

            # 1. 雙門檻精選
            if is_buy_pass and is_amp_pass:
                res = f"🔥 [{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}%"
                perfect_matches.append(res)
                print(f"[{idx}/{total_stocks}] 精選 -> {res}")
            
            # 2. 備選 A：買超達標 (>=12天)，但振幅偏高 (>20%)
            elif is_buy_pass and amplitude > MAX_AMPLITUDE:
                res = f"・[{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}% (籌碼集中，等待振幅收斂)"
                high_buy_matches.append(res)
                
            # 3. 備選 B：振幅符合 (<=20%)，但買超接近達標 (8~11天)
            elif is_amp_pass and NEAR_BUY_DAYS <= buy_days < MIN_BUY_DAYS:
                res = f"・[{stock_id}] 買超天數: {buy_days}/{total_days} 天 | 振幅: {amplitude}% (低波動壓盤，法人升溫中)"
                low_amp_matches.append(res)

            # 每 50 檔輸出 log 進度
            if idx % 50 == 0:
                print(f"⏳ 掃描進度: {idx}/{total_stocks} ({(idx/total_stocks)*100:.1f}%) | 雙門檻: {len(perfect_matches)} | 單項備選: {len(high_buy_matches)+len(low_amp_matches)}")

            time.sleep(random.uniform(0.1, 0.3))

        except Exception as e:
            error_count += 1
            continue

    # ================= 郵件內容組裝 =================
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
    subject = f"【全台股籌碼戰報】{today_str} - 雙門檻: {len(perfect_matches)} 檔 | 備選: {len(high_buy_matches)+len(low_amp_matches)} 檔"
    
    body = f"📊 全台股靜默籌碼收集掃描報告 ({today_str})\n"
    body += f"篩選標準：近 {DAYS_WINDOW} 交易日法人買超 ≥ {MIN_BUY_DAYS} 天，且價格振幅 ≤ {MAX_AMPLITUDE}%\n"
    body += f"掃描範圍：台股上市櫃純個股（完成 {scanned_count} 檔精準比對）\n"
    body += "==================================================\n\n"
    
    # 核心雙門檻區
    if perfect_matches:
        body += f"🎯 🔥 今日符合「雙門檻」之核心精選標的 ({len(perfect_matches)} 檔)：\n\n"
        body += "\n".join(perfect_matches) + "\n\n"
    else:
        body += "🎯 🔥 今日符合「雙門檻」之核心精選標的：0 檔\n"
        body += "(今日全市場無個股同時符合雙門檻條件，市場可能處於高波動或籌碼發散期)\n\n"
        
    body += "--------------------------------------------------\n"
    body += "👀 備選觀察區（符合單一條件之潛力股）：\n\n"
    
    # 備選區 A
    body += f"【類別 A：法人持續進場（買超 ≥ {MIN_BUY_DAYS}/{DAYS_WINDOW} 天），等待振幅收斂】({len(high_buy_matches)} 檔)\n"
    if high_buy_matches:
        body += "\n".join(high_buy_matches) + "\n\n"
    else:
        body += "（無符合標的）\n\n"
        
    # 備選區 B
    body += f"【類別 B：價格極度壓盤（振幅 ≤ {MAX_AMPLITUDE}%），買超蓄勢待發 ({NEAR_BUY_DAYS}~{MIN_BUY_DAYS-1}天)】({len(low_amp_matches)} 檔)\n"
    if low_amp_matches:
        body += "\n".join(low_amp_matches) + "\n\n"
    else:
        body += "（無符合標的）\n\n"

    body += "==================================================\n"
    body += f"📈 系統執行摘要：\n"
    body += f"- 總掃描個股: {scanned_count} 檔\n"
    body += f"- 雙門檻精選: {len(perfect_matches)} 檔\n"
    body += f"- 備選觀察總數: {len(high_buy_matches) + len(low_amp_matches)} 檔\n"
    body += f"- 異常跳過數: {error_count} 檔\n"
    body += "🤖 本郵件由 GitHub Actions 每日盤後自動掃描引擎發送。"

    send_email(subject, body)

if __name__ == "__main__":
    main()
